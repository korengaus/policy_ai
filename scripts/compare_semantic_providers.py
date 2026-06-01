"""Phase 2 M5.8: controlled comparison of semantic embedding providers.

Runs the same calibration fixture across two or more providers
(``deterministic``, ``openai``, ``disabled``) and produces a comparison
scorecard plus an activation-readiness recommendation
(``semantic_thresholds.recommend_thresholds``).

Strict safety contract:

    * No live OpenAI call without an explicit confirmation token
      (``--live-confirm-token LIVE_OPENAI_OK``) AND the required env
      variables (``SEMANTIC_MATCHING_ENABLED=true``,
      ``EMBEDDING_PROVIDER=openai``, ``EMBEDDING_MODEL``, ``OPENAI_API_KEY``).
    * ``--no-network`` blocks the OpenAI provider regardless of env.
    * API keys are never printed; raw source bodies are never printed.
    * No verdict-side state is read or modified. Only ``semantic_evidence_summary``
      and the scorecards built around it.
    * Exit codes:
        - 0 success,
        - 1 script error,
        - 2 required provider unavailable,
        - 3 live OpenAI requested without confirmation token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make Korean text printable on Windows cp949 consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import database  # noqa: E402
import semantic_calibration  # noqa: E402
import semantic_embeddings  # noqa: E402
import semantic_evidence_agent  # noqa: E402
import semantic_thresholds  # noqa: E402


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "semantic_calibration_cases.json"
LIVE_CONFIRM_TOKEN = "LIVE_OPENAI_OK"
SUPPORTED_PROVIDERS = ("deterministic", "openai", "disabled")
TRUNCATE_DISPLAY = 160


class CompareError(RuntimeError):
    """Raised when the script cannot proceed for a user-visible reason."""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare semantic-matching calibration across providers. "
            "Defaults to the deterministic provider so no network is involved."
        ),
    )
    parser.add_argument(
        "--case-file", type=Path, default=DEFAULT_FIXTURE,
        help="Path to calibration fixture (default: %(default)s)",
    )
    parser.add_argument(
        "--providers", default="deterministic",
        help=(
            "Comma-separated provider list. Supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)}. Default: %(default)s"
        ),
    )
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--show-matches", action="store_true")
    parser.add_argument(
        "--no-network", action="store_true",
        help="Block any live OpenAI call regardless of env state.",
    )
    parser.add_argument(
        "--require-live-confirmation", dest="require_live_confirmation",
        action="store_true", default=True,
        help="Require --live-confirm-token before any live OpenAI call (default).",
    )
    parser.add_argument(
        "--no-require-live-confirmation", dest="require_live_confirmation",
        action="store_false",
        help="Disable the live-confirmation gate (NOT recommended).",
    )
    parser.add_argument(
        "--live-confirm-token", default="",
        help=f"Pass {LIVE_CONFIRM_TOKEN!r} to authorize a live OpenAI call.",
    )
    return parser


# ---------------------------------------------------------------------------
# Provider isolation helpers
# ---------------------------------------------------------------------------

# Environment variables we save/restore so swapping providers in-process is
# fully reversible — no side-effects leak from one provider's run to another.
_ENV_KEYS = (
    "SEMANTIC_MATCHING_ENABLED",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "OPENAI_API_KEY",
)


def _snapshot_env() -> dict[str, Optional[str]]:
    return {key: os.environ.get(key) for key in _ENV_KEYS}


def _restore_env(snapshot: dict[str, Optional[str]]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _apply_provider_environment(provider: str, no_network: bool) -> None:
    if provider == "disabled":
        os.environ["SEMANTIC_MATCHING_ENABLED"] = "false"
        os.environ["EMBEDDING_PROVIDER"] = "disabled"
        return
    os.environ["SEMANTIC_MATCHING_ENABLED"] = "true"
    os.environ["EMBEDDING_PROVIDER"] = provider
    if provider == "openai" and no_network:
        # Strip the API key so the provider initializes with available=False.
        os.environ.pop("OPENAI_API_KEY", None)


def _parse_providers(raw: str) -> list[str]:
    tokens = [token.strip().lower() for token in (raw or "").split(",") if token.strip()]
    if not tokens:
        raise CompareError("at least one provider must be specified")
    bad = [token for token in tokens if token not in SUPPORTED_PROVIDERS]
    if bad:
        raise CompareError(
            f"unsupported provider(s): {bad}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _check_live_openai_gate(args: argparse.Namespace, providers: list[str]) -> Optional[int]:
    """Return an exit code if the live-OpenAI gate should refuse to run.

    Returns ``None`` when we can proceed. The gate only triggers when:
        * ``openai`` is in the provider list,
        * ``--no-network`` is **not** set,
        * ``--require-live-confirmation`` is enabled (the default).

    A live call requires both the confirmation token AND a fully configured
    env (key + model). Missing token → exit code 3. Token correct but env
    missing → exit code 2.
    """
    if "openai" not in providers:
        return None
    if args.no_network:
        return None
    if not args.require_live_confirmation:
        return None
    if args.live_confirm_token != LIVE_CONFIRM_TOKEN:
        print(
            "[compare] FAIL: provider 'openai' would make a live API call. "
            f"Pass --live-confirm-token {LIVE_CONFIRM_TOKEN!r} to authorize, "
            "or pass --no-network to drill the offline path.",
            file=sys.stderr,
        )
        return 3
    missing: list[str] = []
    if (os.environ.get("SEMANTIC_MATCHING_ENABLED") or "").strip().lower() != "true":
        missing.append("SEMANTIC_MATCHING_ENABLED=true")
    if (os.environ.get("EMBEDDING_PROVIDER") or "").strip().lower() != "openai":
        missing.append("EMBEDDING_PROVIDER=openai")
    if not (os.environ.get("EMBEDDING_MODEL") or "").strip():
        missing.append("EMBEDDING_MODEL")
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        missing.append("OPENAI_API_KEY")
    if missing:
        print(
            "[compare] FAIL: live OpenAI requested but required env is missing: "
            f"{', '.join(missing)}.",
            file=sys.stderr,
        )
        return 2
    return None


# ---------------------------------------------------------------------------
# Calibration execution
# ---------------------------------------------------------------------------

def _load_cases(case_file: Path, max_cases: Optional[int]) -> list[dict]:
    if not case_file.exists():
        raise CompareError(f"fixture file not found: {case_file}")
    try:
        raw = json.loads(case_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CompareError(f"fixture file is not valid JSON: {error}")
    if not isinstance(raw, list):
        raise CompareError("fixture file must contain a JSON array")
    if max_cases is not None and max_cases >= 0:
        raw = raw[:max_cases]
    return raw


def _run_case(case: dict, provider) -> dict:
    claim_text = case.get("claim_text") or ""
    sources = case.get("sources") or []
    summary = semantic_evidence_agent.compute_semantic_evidence_summary(
        normalized_claims=[{"claim_text": claim_text}] if claim_text else None,
        claim_text=claim_text,
        source_candidates=sources,
        evidence_snippets=[],
        provider=provider,
    )
    expected = case.get("expected") or {}
    if not expected.get("category") and case.get("category"):
        expected = {**expected, "category": case["category"]}
    evaluation = semantic_calibration.evaluate_case(summary, expected)
    return {
        "case_id": case.get("case_id") or "(unnamed)",
        "category": case.get("category") or "",
        "description": case.get("description") or "",
        "claim_text": claim_text,
        "expected": expected,
        "summary": summary,
        "evaluation": evaluation,
    }


def _run_provider(
    provider_name: str,
    cases: list[dict],
    args: argparse.Namespace,
) -> dict:
    """Run all calibration cases for one provider; return a structured result."""
    snapshot = _snapshot_env()
    try:
        _apply_provider_environment(provider_name, args.no_network)
        provider = semantic_embeddings.get_active_provider()

        # Defensive: even if the provider somehow reported available=True with
        # --no-network, force it offline so we never make a live call.
        if args.no_network and provider.name == "openai" and provider.available:
            provider.available = False
            provider.reason = "no-network mode forced provider offline"
            provider.error = provider.reason

        rows = [_run_case(case, provider) for case in cases]
        scorecard = semantic_calibration.summarize_calibration_results(rows)
        status = provider.provider_status()
        live_called = (
            provider_name == "openai"
            and provider.available
            and not args.no_network
            and bool(status.get("external_calls_possible"))
        )
        return {
            "provider": provider_name,
            "provider_status": status,
            "available": bool(status.get("available")),
            "external_calls_possible": bool(status.get("external_calls_possible")),
            "live_called": live_called,
            "scorecard": scorecard,
            "rows": rows,
        }
    finally:
        _restore_env(snapshot)


# ---------------------------------------------------------------------------
# Comparison synthesis
# ---------------------------------------------------------------------------

def _summarize_comparison(provider_results: list[dict]) -> dict:
    """Pick the provider that wins on each headline metric."""
    available_results = [r for r in provider_results if r["available"]]
    if not available_results:
        return {
            "best_provider_by_related_top1": None,
            "lowest_overstrong_provider": None,
            "lowest_fail_provider": None,
            "runtime_summary": {},
            "recommendations": [],
        }

    by_top1 = max(
        available_results,
        key=lambda r: (r["scorecard"].get("related_top1_rate") or 0.0),
    )
    by_overstrong = min(
        available_results, key=lambda r: r["scorecard"].get("overstrong_count", 0)
    )
    by_failures = min(
        available_results, key=lambda r: r["scorecard"].get("fail_count", 0)
    )
    runtime_summary = {
        r["provider"]: int(r["scorecard"].get("average_runtime_ms") or 0)
        for r in available_results
    }

    recommendations: list[str] = []
    # If both deterministic and openai ran, note which beat which on safety.
    available_names = {r["provider"] for r in available_results}
    if "deterministic" in available_names and "openai" in available_names:
        det = next(r for r in available_results if r["provider"] == "deterministic")
        oai = next(r for r in available_results if r["provider"] == "openai")
        if (oai["scorecard"].get("related_top1_rate") or 0.0) > (
            det["scorecard"].get("related_top1_rate") or 0.0
        ):
            recommendations.append(
                "OpenAI provider ranks the related official source first more "
                "often than the deterministic baseline."
            )
        if (oai["scorecard"].get("overstrong_count") or 0) > (
            det["scorecard"].get("overstrong_count") or 0
        ):
            recommendations.append(
                "OpenAI provider produced more 'overstrong' cases than the "
                "deterministic baseline — investigate before activation."
            )

    return {
        "best_provider_by_related_top1": by_top1["provider"],
        "lowest_overstrong_provider": by_overstrong["provider"],
        "lowest_fail_provider": by_failures["provider"],
        "runtime_summary": runtime_summary,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _truncate(text: object, limit: int = TRUNCATE_DISPLAY) -> str:
    raw = "" if text is None else str(text)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def _print_provider_block(result: dict, args: argparse.Namespace) -> None:
    name = result["provider"]
    status = result["provider_status"]
    scorecard = result["scorecard"]
    print(f"[compare] provider={name}")
    print(f"  available={status['available']}")
    print(f"  configured={status['configured']}")
    print(f"  external_calls_possible={status['external_calls_possible']}")
    if status.get("reason"):
        print(f"  reason={status['reason']}")
    if not result["available"]:
        print()
        return
    print(
        "  cases={cases} pass={p} fail={f} related_top1={rt}/{rte} "
        "overstrong={ovs} cap_applied={cap} critical_mismatches={cm} "
        "avg_runtime_ms={ar}".format(
            cases=scorecard["case_count"],
            p=scorecard["pass_count"],
            f=scorecard["fail_count"],
            rt=scorecard["related_top1_count"],
            rte=scorecard["related_top1_eligible"],
            ovs=scorecard["overstrong_count"],
            cap=scorecard.get("support_cap_applied_count", 0),
            cm=scorecard.get("total_critical_mismatches", 0),
            ar=scorecard["average_runtime_ms"],
        )
    )
    print(f"  support_level_distribution={scorecard['support_level_distribution']}")
    print(
        f"  raw_support_level_distribution="
        f"{scorecard.get('raw_support_level_distribution', {})}"
    )
    if scorecard.get("semantic_risk_flag_counts"):
        print(f"  guardrail_risk_flags={scorecard['semantic_risk_flag_counts']}")
    if args.show_failures:
        for row in result["rows"]:
            evaluation = row["evaluation"]
            if evaluation["passed"]:
                continue
            print(
                f"  [FAIL] case={row['case_id']!r} category={row['category']!r} "
                f"support={evaluation['support_level']} "
                f"raw={evaluation.get('raw_support_level')} "
                f"score%={evaluation['best_score_percent']}"
            )
            for failure in evaluation.get("failures") or []:
                print(f"     failure: {_truncate(failure, 200)}")
    if args.show_matches:
        for row in result["rows"]:
            for claim_match in row["summary"].get("claim_matches") or []:
                for top in (claim_match.get("top_matches") or [])[:2]:
                    print(
                        f"    case={row['case_id']!r} score={float(top.get('score') or 0):.3f} "
                        f"-> {_truncate(top.get('text'), 100)}"
                    )
    print()


def _print_recommendation(payload: dict) -> None:
    rec = payload["recommendation"]
    print(f"[compare] activation_readiness={rec['activation_readiness']}")
    if rec.get("reasons"):
        print("  reasons:")
        for reason in rec["reasons"]:
            print(f"    - {reason}")
    if rec.get("safety_notes"):
        print("  safety_notes:")
        for note in rec["safety_notes"]:
            print(f"    - {note}")
    comparison = payload["comparison"]
    if comparison.get("recommendations"):
        print("  cross-provider notes:")
        for rec_line in comparison["recommendations"]:
            print(f"    - {rec_line}")


def _write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = payload["recommendation"]
    comparison = payload["comparison"]
    providers = payload["providers"]

    lines: list[str] = []
    lines.append("# Semantic Provider Comparison Report")
    lines.append("")
    lines.append(
        "> Semantic matching is evidence-ranking metadata only and does not "
        "verify claims. Rule-based verification and official body matching "
        "remain authoritative."
    )
    lines.append("")
    lines.append("## Provider availability")
    lines.append("")
    lines.append("| provider | available | configured | external_calls_possible | reason |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, payload_block in providers.items():
        status = payload_block.get("provider_status") or {}
        lines.append(
            f"| `{name}` | {status.get('available')} | {status.get('configured')} | "
            f"{status.get('external_calls_possible')} | "
            f"{status.get('reason') or ''} |"
        )
    lines.append("")

    lines.append("## Scorecard")
    lines.append("")
    lines.append(
        "| provider | cases | pass | fail | related_top1 | overstrong | "
        "cap_applied | critical_mismatches | avg_runtime_ms | total_cache_hits | "
        "embed_requests |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for name, payload_block in providers.items():
        scorecard = payload_block.get("scorecard") or {}
        if not payload_block.get("available"):
            lines.append(f"| `{name}` | – | – | – | – | – | – | – | – | – | – |")
            continue
        lines.append(
            f"| `{name}` | {scorecard.get('case_count')} | "
            f"{scorecard.get('pass_count')} | "
            f"{scorecard.get('fail_count')} | "
            f"{scorecard.get('related_top1_count')}/"
            f"{scorecard.get('related_top1_eligible')} "
            f"({scorecard.get('related_top1_rate')}) | "
            f"{scorecard.get('overstrong_count')} | "
            f"{scorecard.get('support_cap_applied_count', 0)} | "
            f"{scorecard.get('total_critical_mismatches', 0)} | "
            f"{scorecard.get('average_runtime_ms')} | "
            f"{scorecard.get('total_cache_hits')} | "
            f"{scorecard.get('total_embedding_request_count')} |"
        )
    lines.append("")

    lines.append("## Mismatch / guardrail flags")
    lines.append("")
    for name, payload_block in providers.items():
        if not payload_block.get("available"):
            continue
        scorecard = payload_block.get("scorecard") or {}
        flags = scorecard.get("semantic_risk_flag_counts") or {}
        raw_dist = scorecard.get("raw_support_level_distribution") or {}
        adj_dist = scorecard.get("support_level_distribution") or {}
        lines.append(f"### `{name}`")
        lines.append("")
        lines.append(f"- raw_support_level_distribution: `{raw_dist}`")
        lines.append(f"- adjusted_support_level_distribution: `{adj_dist}`")
        lines.append(f"- guardrail_risk_flag_counts: `{flags}`")
        lines.append("")

    lines.append("## Failed cases")
    lines.append("")
    any_failures = False
    for name, payload_block in providers.items():
        if not payload_block.get("available"):
            continue
        rows = payload_block.get("rows") or []
        failing = [r for r in rows if not r["evaluation"]["passed"]]
        if not failing:
            continue
        any_failures = True
        lines.append(f"### `{name}`")
        lines.append("")
        for row in failing:
            evaluation = row["evaluation"]
            lines.append(
                f"- `{row['case_id']}` — support={evaluation['support_level']} "
                f"raw={evaluation.get('raw_support_level')} "
                f"score%={evaluation['best_score_percent']}"
            )
            for failure in evaluation.get("failures") or []:
                lines.append(f"  - failure: {_truncate(failure, 200)}")
        lines.append("")
    if not any_failures:
        lines.append("_None._")
        lines.append("")

    lines.append("## Cross-provider notes")
    lines.append("")
    if comparison.get("recommendations"):
        for note in comparison["recommendations"]:
            lines.append(f"- {note}")
    else:
        lines.append("_No cross-provider differences flagged._")
    runtime = comparison.get("runtime_summary") or {}
    if runtime:
        lines.append("")
        lines.append(f"Runtime summary (avg_runtime_ms per case): `{runtime}`")
    lines.append("")

    lines.append("## Threshold notes")
    lines.append("")
    lines.append(
        "Threshold tuning is intentionally not produced by this milestone. "
        "The `recommended_thresholds` block reports `null` for both `support` "
        "and `context` — operators must run an OpenAI comparison locally on a "
        "representative fixture set and adjust `SEMANTIC_MIN_SCORE_FOR_SUPPORT` "
        "/ `SEMANTIC_MIN_SCORE_FOR_CONTEXT` only after the calibration "
        "scorecard is clean across providers."
    )
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- activation_readiness: **`{rec['activation_readiness']}`**")
    if rec.get("reasons"):
        lines.append("- reasons:")
        for reason in rec["reasons"]:
            lines.append(f"  - {reason}")
    if rec.get("safety_notes"):
        lines.append("- safety notes:")
        for note in rec["safety_notes"]:
            lines.append(f"  - {note}")
    lines.append("")
    lines.append(
        "> Activation readiness vocabulary: `not_ready`, `local_only`, "
        "`debug_canary_candidate`. Production user-facing activation is **not** "
        "an output of this report — that decision belongs to a separate "
        "operator review."
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_comparison(args: argparse.Namespace) -> int:
    started = time.perf_counter()

    # M12.0e-6b-3: SQLite init removed. The embedding cache is PG-backed;
    # postgres_storage.ensure_schema creates it lazily on first engine use.

    providers = _parse_providers(args.providers)

    gate_code = _check_live_openai_gate(args, providers)
    if gate_code is not None:
        return gate_code

    cases = _load_cases(args.case_file, args.max_cases)

    provider_results: list[dict] = []
    for provider_name in providers:
        provider_results.append(_run_provider(provider_name, cases, args))

    comparison = _summarize_comparison(provider_results)

    # Build the recommendation payload via the threshold helper. Pass
    # available+scorecard so it can classify per-provider and aggregate.
    threshold_input = {
        result["provider"]: {
            "available": result["available"],
            "scorecard": result["scorecard"],
        }
        for result in provider_results
    }
    recommendation = semantic_thresholds.recommend_thresholds(threshold_input)

    payload = {
        "providers": {
            result["provider"]: result for result in provider_results
        },
        "comparison": comparison,
        "recommendation": recommendation,
        "live_openai_called": any(r.get("live_called") for r in provider_results),
    }

    # Print human-readable summary.
    for result in provider_results:
        _print_provider_block(result, args)
    _print_recommendation(payload)

    if args.json_out:
        _write_json(payload, args.json_out)
        print(f"[compare] JSON written to {args.json_out}")
    if args.markdown_out:
        _write_markdown(payload, args.markdown_out)
        print(f"[compare] Markdown written to {args.markdown_out}")

    elapsed = time.perf_counter() - started
    print(f"[compare] total elapsed {elapsed:.2f}s")
    print(
        "[compare] reminder: semantic matching is metadata only; rule-based "
        "verification and official body matching remain authoritative."
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_comparison(args)
    except CompareError as error:
        print(f"[compare] FAILED: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[compare] aborted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
