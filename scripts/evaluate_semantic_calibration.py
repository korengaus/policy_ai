"""Phase 2 M5.6: semantic calibration evaluator.

Runs the semantic evidence agent over a calibration fixture, compares each
case to its declared expectations, and emits a scorecard (stdout, JSON,
CSV, and/or Markdown).

Default provider is ``deterministic`` so local runs and CI exercise the
full stack without network. The ``openai`` provider remains opt-in and
requires SEMANTIC_MATCHING_ENABLED=true, EMBEDDING_PROVIDER=openai,
EMBEDDING_MODEL, and OPENAI_API_KEY. ``--no-network`` blocks live calls.

Verdict isolation contract: this script only inspects
``semantic_evidence_summary`` — it never reads or modifies
``policy_confidence``, ``final_decision``, ``verification_card``, or any
verdict-side state.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make Korean text printable on Windows cp949 consoles without forcing the
# user to set PYTHONUTF8 first.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import config  # noqa: E402
import database  # noqa: E402
import semantic_calibration  # noqa: E402
import semantic_embeddings  # noqa: E402
import semantic_evidence_agent  # noqa: E402


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "semantic_calibration_cases.json"
TRUNCATE_DISPLAY = 160


class EvaluatorError(RuntimeError):
    """Raised when the script cannot proceed for a user-visible reason."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate semantic matching quality over calibration fixtures. "
            "Defaults to the deterministic provider so no network is involved."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["disabled", "deterministic", "openai", "auto"],
        default="deterministic",
        help="Embedding provider to use (default: %(default)s)",
    )
    parser.add_argument(
        "--case-file", type=Path, default=DEFAULT_FIXTURE,
        help="Path to calibration fixture (default: %(default)s)",
    )
    parser.add_argument(
        "--max-cases", type=int, default=None,
        help="Evaluate at most this many cases from the fixture.",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--csv-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument("--show-failures", action="store_true",
                        help="Print only failed cases with reasons.")
    parser.add_argument("--show-matches", action="store_true",
                        help="Print top match snippets for each case.")
    parser.add_argument("--threshold-support", type=float, default=None,
                        help="Override SEMANTIC_MIN_SCORE_FOR_SUPPORT for this run.")
    parser.add_argument("--threshold-context", type=float, default=None,
                        help="Override SEMANTIC_MIN_SCORE_FOR_CONTEXT for this run.")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="Exit code 3 if any calibration case fails.")
    parser.add_argument("--fail-on-unavailable", action="store_true",
                        help="Exit code 2 if the resolved provider reports available=False.")
    return parser


def _apply_provider_environment(provider: str, no_network: bool) -> None:
    if provider == "auto":
        return
    if provider == "disabled":
        os.environ["SEMANTIC_MATCHING_ENABLED"] = "false"
        os.environ["EMBEDDING_PROVIDER"] = "disabled"
        return
    os.environ["SEMANTIC_MATCHING_ENABLED"] = "true"
    os.environ["EMBEDDING_PROVIDER"] = provider
    if provider == "openai" and no_network:
        # Strip the key so the provider reports unavailable cleanly.
        os.environ.pop("OPENAI_API_KEY", None)


def _apply_threshold_overrides(args: argparse.Namespace) -> None:
    if args.threshold_support is not None:
        os.environ["SEMANTIC_MIN_SCORE_FOR_SUPPORT"] = str(args.threshold_support)
    if args.threshold_context is not None:
        os.environ["SEMANTIC_MIN_SCORE_FOR_CONTEXT"] = str(args.threshold_context)


def _load_cases(case_file: Path, max_cases: Optional[int]) -> list[dict]:
    if not case_file.exists():
        raise EvaluatorError(f"fixture file not found: {case_file}")
    try:
        raw = json.loads(case_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EvaluatorError(f"fixture file is not valid JSON: {error}")
    if not isinstance(raw, list):
        raise EvaluatorError("fixture file must contain a JSON array")
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


def _truncate(text: object, limit: int = TRUNCATE_DISPLAY) -> str:
    raw = "" if text is None else str(text)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def _print_scorecard(rows: list[dict], provider, args: argparse.Namespace) -> None:
    status = provider.provider_status()
    scorecard = semantic_calibration.summarize_calibration_results(rows)
    print("[evaluate] provider summary")
    print(f"  provider={status['provider']}")
    print(f"  model={status['model'] or '(unset)'}")
    print(f"  available={status['available']}")
    print(f"  configured={status['configured']}")
    print(f"  external_calls_possible={status['external_calls_possible']}")
    if status.get("reason"):
        print(f"  reason={status['reason']}")
    print()

    print(
        "[evaluate] scorecard: cases={cases} pass={p} fail={f} "
        "related_top1={rt}/{rte} overstrong={ovs} unavailable={ua} "
        "avg_runtime_ms={ar} total_cache_hits={tch} total_embed_requests={ter}".format(
            cases=scorecard["case_count"],
            p=scorecard["pass_count"],
            f=scorecard["fail_count"],
            rt=scorecard["related_top1_count"],
            rte=scorecard["related_top1_eligible"],
            ovs=scorecard["overstrong_count"],
            ua=scorecard["unavailable_count"],
            ar=scorecard["average_runtime_ms"],
            tch=scorecard["total_cache_hits"],
            ter=scorecard["total_embedding_request_count"],
        )
    )
    print(f"  support_level_distribution={scorecard['support_level_distribution']}")
    print(
        "  guardrails: cap_applied={cap}/{cases} critical_mismatches={cm} "
        "raw_distribution={raw} risk_flags={flags}".format(
            cap=scorecard.get("support_cap_applied_count", 0),
            cases=scorecard["case_count"],
            cm=scorecard.get("total_critical_mismatches", 0),
            raw=scorecard.get("raw_support_level_distribution", {}),
            flags=scorecard.get("semantic_risk_flag_counts", {}),
        )
    )
    print()

    show_only_failures = args.show_failures
    for row in rows:
        evaluation = row["evaluation"]
        if show_only_failures and evaluation["passed"]:
            continue
        summary = row["summary"]
        status_label = "PASS" if evaluation["passed"] else "FAIL"
        raw_level = evaluation.get("raw_support_level") or evaluation["support_level"]
        cap_marker = "*" if evaluation.get("support_cap_applied") else ""
        print(
            "  [{status}] case={cid!r} category={cat!r} support={lvl}{cap} "
            "raw={raw} score%={pct} runtime_ms={rt} chunks={ch} cache={cache} req={req} "
            "cm={cm}".format(
                status=status_label,
                cid=row["case_id"],
                cat=row["category"],
                lvl=evaluation["support_level"],
                cap=cap_marker,
                raw=raw_level,
                pct=evaluation["best_score_percent"],
                rt=summary.get("runtime_ms"),
                ch=summary.get("chunk_count"),
                cache=summary.get("cache_hits"),
                req=summary.get("embedding_request_count"),
                cm=evaluation.get("critical_mismatch_count", 0),
            )
        )
        print(f"     claim: {_truncate(row['claim_text'], 120)}")
        for failure in evaluation.get("failures") or []:
            print(f"     failure: {_truncate(failure, 200)}")
        if evaluation.get("risk_flags"):
            print(f"     risk_flags: {evaluation['risk_flags']}")
        if evaluation.get("semantic_risk_flags"):
            print(f"     guardrail_risk_flags: {evaluation['semantic_risk_flags']}")
        if args.show_matches:
            for claim_match in summary.get("claim_matches") or []:
                for top in (claim_match.get("top_matches") or [])[:3]:
                    print(
                        "       match score={score:.3f} ({pct}%) "
                        "title={title!r} -> {text}".format(
                            score=float(top.get("score") or 0.0),
                            pct=int(top.get("score_percent") or 0),
                            title=_truncate(top.get("source_title"), 50),
                            text=_truncate(top.get("text"), 120),
                        )
                    )


def _write_json(path: Path, rows: list[dict], provider, scorecard: dict) -> None:
    payload = {
        "provider_status": provider.provider_status(),
        "scorecard": scorecard,
        "cases": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "case_id", "category", "passed", "support_level", "raw_support_level",
        "support_cap_applied", "critical_mismatch_count",
        "best_score_percent",
        "related_top1", "overstrong", "chunk_count", "cache_hits",
        "embedding_request_count", "runtime_ms", "risk_flags",
        "semantic_risk_flags", "failure_reasons",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            evaluation = row["evaluation"]
            summary = row["summary"]
            writer.writerow({
                "case_id": row["case_id"],
                "category": row["category"],
                "passed": evaluation["passed"],
                "support_level": evaluation["support_level"],
                "raw_support_level": evaluation.get("raw_support_level"),
                "support_cap_applied": evaluation.get("support_cap_applied"),
                "critical_mismatch_count": evaluation.get("critical_mismatch_count"),
                "best_score_percent": evaluation["best_score_percent"],
                "related_top1": evaluation.get("related_top1"),
                "overstrong": evaluation.get("overstrong"),
                "chunk_count": summary.get("chunk_count"),
                "cache_hits": summary.get("cache_hits"),
                "embedding_request_count": summary.get("embedding_request_count"),
                "runtime_ms": summary.get("runtime_ms"),
                "risk_flags": "|".join(evaluation.get("risk_flags") or []),
                "semantic_risk_flags": "|".join(evaluation.get("semantic_risk_flags") or []),
                "failure_reasons": " | ".join(evaluation.get("failures") or []),
            })


def _write_markdown(path: Path, rows: list[dict], provider, scorecard: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    status = provider.provider_status()
    lines.append("# Semantic Calibration Report")
    lines.append("")
    lines.append(f"- provider: `{status['provider']}`")
    lines.append(f"- model: `{status['model'] or '(unset)'}`")
    lines.append(f"- available: `{status['available']}`")
    lines.append(f"- configured: `{status['configured']}`")
    lines.append(f"- external_calls_possible: `{status['external_calls_possible']}`")
    if status.get("reason"):
        lines.append(f"- reason: {status['reason']}")
    lines.append("")
    lines.append("## Scorecard")
    lines.append("")
    lines.append(f"- case_count: {scorecard['case_count']}")
    lines.append(f"- pass_count: {scorecard['pass_count']}")
    lines.append(f"- fail_count: {scorecard['fail_count']}")
    lines.append(
        f"- related_top1: {scorecard['related_top1_count']}/"
        f"{scorecard['related_top1_eligible']} "
        f"({scorecard['related_top1_rate']})"
    )
    lines.append(f"- overstrong_count: {scorecard['overstrong_count']}")
    lines.append(f"- unavailable_count: {scorecard['unavailable_count']}")
    lines.append(f"- average_runtime_ms: {scorecard['average_runtime_ms']}")
    lines.append(f"- total_cache_hits: {scorecard['total_cache_hits']}")
    lines.append(
        f"- total_embedding_request_count: {scorecard['total_embedding_request_count']}"
    )
    lines.append(
        f"- support_level_distribution: `{scorecard['support_level_distribution']}`"
    )
    lines.append(
        f"- raw_support_level_distribution: "
        f"`{scorecard.get('raw_support_level_distribution', {})}`"
    )
    lines.append(
        f"- support_cap_applied_count: "
        f"{scorecard.get('support_cap_applied_count', 0)}"
    )
    lines.append(
        f"- total_critical_mismatches: "
        f"{scorecard.get('total_critical_mismatches', 0)}"
    )
    lines.append(
        f"- semantic_risk_flag_counts: "
        f"`{scorecard.get('semantic_risk_flag_counts', {})}`"
    )
    lines.append("")
    lines.append("## Cases")
    lines.append("")
    lines.append(
        "| case_id | category | passed | support_level | raw_support_level | "
        "cap_applied | critical_mismatches | best_score_% | "
        "related_top1 | overstrong | runtime_ms | guardrail_risk_flags | failures |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for row in rows:
        evaluation = row["evaluation"]
        summary = row["summary"]
        failures = "; ".join(evaluation.get("failures") or []) or ""
        flags = ", ".join(evaluation.get("semantic_risk_flags") or []) or ""
        lines.append(
            f"| `{row['case_id']}` | {row['category']} | "
            f"{'PASS' if evaluation['passed'] else 'FAIL'} | "
            f"{evaluation['support_level']} | "
            f"{evaluation.get('raw_support_level', '')} | "
            f"{evaluation.get('support_cap_applied')} | "
            f"{evaluation.get('critical_mismatch_count', 0)} | "
            f"{evaluation['best_score_percent']} | "
            f"{evaluation.get('related_top1')} | "
            f"{evaluation.get('overstrong')} | "
            f"{summary.get('runtime_ms')} | "
            f"{flags} | "
            f"{failures} |"
        )
    lines.append("")
    lines.append(
        "Semantic match strength is metadata only; rule-based verification "
        "and official body matching remain authoritative. Never treat a "
        "`strong` semantic label as verification."
    )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _resolve_provider(args: argparse.Namespace):
    _apply_provider_environment(args.provider, args.no_network)
    _apply_threshold_overrides(args)
    provider = semantic_embeddings.get_active_provider()
    if args.no_network and provider.name == "openai" and provider.available:
        # Defensive belt-and-suspenders — _apply_provider_environment already
        # stripped the API key, but if the provider somehow reported itself
        # available we force it offline here.
        provider.available = False
        provider.reason = "no-network mode forced provider offline"
        provider.error = provider.reason
    return provider


def run_evaluation(args: argparse.Namespace) -> int:
    started = time.perf_counter()

    # Ensure the embedding_cache table exists so cache writes don't spam
    # "no such table" warnings on a fresh checkout.
    try:
        database.init_db()
    except Exception as init_error:
        print(f"[evaluate] warning: database.init_db() failed: {init_error}", file=sys.stderr)

    provider = _resolve_provider(args)
    cases = _load_cases(args.case_file, args.max_cases)
    rows = [_run_case(case, provider) for case in cases]
    scorecard = semantic_calibration.summarize_calibration_results(rows)

    _print_scorecard(rows, provider, args)
    if args.json_out:
        _write_json(args.json_out, rows, provider, scorecard)
        print(f"\n[evaluate] JSON written to {args.json_out}")
    if args.csv_out:
        _write_csv(args.csv_out, rows)
        print(f"[evaluate] CSV written to {args.csv_out}")
    if args.markdown_out:
        _write_markdown(args.markdown_out, rows, provider, scorecard)
        print(f"[evaluate] Markdown written to {args.markdown_out}")

    elapsed = time.perf_counter() - started
    print(f"\n[evaluate] total elapsed {elapsed:.2f}s")

    if args.fail_on_unavailable and not provider.available:
        print(
            f"[evaluate] FAIL: provider {provider.name!r} reported "
            f"available=False (reason={provider.reason or provider.error})",
            file=sys.stderr,
        )
        return 2
    if args.fail_on_regression and scorecard["fail_count"] > 0:
        print(
            f"[evaluate] FAIL: {scorecard['fail_count']} case(s) regressed",
            file=sys.stderr,
        )
        return 3
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_evaluation(args)
    except EvaluatorError as error:
        print(f"[evaluate] FAILED: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[evaluate] aborted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
