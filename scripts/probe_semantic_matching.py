"""Phase 2 M5.5: manual semantic matching probe.

Runs the semantic evidence agent against either fixture cases or a single
ad-hoc claim/source pair, then prints a concise summary (provider state,
support level, score, chunks, latency, cache hits, errors).

Default provider is ``deterministic`` so the script does the right thing
locally without env setup. The ``openai`` provider is opt-in and requires
``SEMANTIC_MATCHING_ENABLED=true``, ``EMBEDDING_PROVIDER=openai``,
``OPENAI_API_KEY``, and ``EMBEDDING_MODEL``. ``--no-network`` blocks live
calls so CI/tests can drill the OpenAI code path without spending.

This script is for manual use. Live OpenAI calls are intentionally not
part of the default CI workflow.
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

# Best-effort: make stdout tolerate Korean text on Windows cp949 consoles
# without forcing the user to set PYTHONUTF8=1.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import config  # noqa: E402
import database  # noqa: E402
import semantic_embeddings  # noqa: E402
import semantic_evidence_agent  # noqa: E402


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "semantic_activation_cases.json"
TRUNCATE_TEXT_DISPLAY = 160


class ProbeError(RuntimeError):
    """Raised when the probe cannot proceed for a reason the user should see."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run semantic matching against fixtures or a single claim. "
            "Defaults to the deterministic provider so no network is involved."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["disabled", "deterministic", "openai", "auto"],
        default="deterministic",
        help="Embedding provider to use. ``auto`` honors current env vars (default: %(default)s)",
    )
    parser.add_argument(
        "--case-file",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Path to a JSON fixture file (default: %(default)s)",
    )
    parser.add_argument(
        "--max-cases", type=int, default=3,
        help="Limit fixture cases evaluated (default: %(default)s)",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None,
        help="If set, write the full per-case summary as JSON to this path.",
    )
    parser.add_argument(
        "--show-matches", action="store_true",
        help="Print top match chunks (truncated) alongside the summary.",
    )
    parser.add_argument(
        "--fail-on-unavailable", action="store_true",
        help="Exit with code 2 if the resolved provider reports available=False.",
    )
    parser.add_argument(
        "--no-network", action="store_true",
        help=(
            "Block any live network call. With --provider openai this skips "
            "client init and reports the provider as unavailable."
        ),
    )
    parser.add_argument(
        "--query", default=None,
        help="Ad-hoc claim text. When set with --source-text, fixtures are ignored.",
    )
    parser.add_argument(
        "--source-text", default=None,
        help="Ad-hoc source body text. Pairs with --query for a single-case probe.",
    )
    parser.add_argument(
        "--source-title", default="ad-hoc source",
        help="Title used for the ad-hoc source.",
    )
    parser.add_argument(
        "--source-url", default="",
        help="URL used for the ad-hoc source.",
    )
    return parser


def _apply_provider_environment(provider: str, no_network: bool) -> None:
    """Translate the chosen provider into runtime env vars.

    ``auto`` is a no-op — the existing environment is honored as-is. For all
    other choices we set both flags so the script behaves the same regardless
    of what was already exported.
    """
    if provider == "auto":
        return
    if provider == "disabled":
        os.environ["SEMANTIC_MATCHING_ENABLED"] = "false"
        os.environ["EMBEDDING_PROVIDER"] = "disabled"
        return
    # deterministic + openai both require the master flag on.
    os.environ["SEMANTIC_MATCHING_ENABLED"] = "true"
    os.environ["EMBEDDING_PROVIDER"] = provider
    if provider == "openai" and no_network:
        # When the caller asked to block network calls but selected openai,
        # we still let the script construct the OpenAI provider so its
        # ``configured`` state can be reported; we just refuse to enable it.
        # Strip the API key so the provider reports unavailable cleanly.
        os.environ.pop("OPENAI_API_KEY", None)


def _resolve_provider(args: argparse.Namespace) -> semantic_embeddings.EmbeddingProvider:
    _apply_provider_environment(args.provider, args.no_network)
    return semantic_embeddings.get_active_provider()


def _load_fixture_cases(case_file: Path, max_cases: int) -> list[dict]:
    if not case_file.exists():
        raise ProbeError(f"fixture file not found: {case_file}")
    try:
        raw = json.loads(case_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProbeError(f"fixture file is not valid JSON: {error}")
    if not isinstance(raw, list):
        raise ProbeError("fixture file must contain a JSON array")
    return raw[: max(0, int(max_cases or 0)) or len(raw)]


def _adhoc_case(query: str, source_text: str, source_title: str, source_url: str) -> dict:
    return {
        "case_id": "adhoc",
        "claim_text": query,
        "sources": [
            {
                "source_id": "adhoc_source",
                "title": source_title,
                "url": source_url,
                "official_body_text": source_text,
            }
        ],
        "expected": {},
    }


def _run_case(case: dict, provider) -> dict:
    """Run one fixture/ad-hoc case and return a compact result dict."""
    claim_text = case.get("claim_text") or ""
    sources = case.get("sources") or []
    summary = semantic_evidence_agent.compute_semantic_evidence_summary(
        normalized_claims=[{"claim_text": claim_text}] if claim_text else None,
        claim_text=claim_text,
        source_candidates=sources,
        evidence_snippets=[],
        provider=provider,
    )
    return {
        "case_id": case.get("case_id") or "(unnamed)",
        "claim_text": claim_text,
        "expected": case.get("expected") or {},
        "summary": summary,
    }


def _truncate(text: object, limit: int = TRUNCATE_TEXT_DISPLAY) -> str:
    raw = "" if text is None else str(text)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def _print_summary(case_results: list[dict], provider, args: argparse.Namespace) -> None:
    status = provider.provider_status()
    print("[probe] provider summary")
    print(f"  provider={status['provider']}")
    print(f"  model={status['model'] or '(unset)'}")
    print(f"  available={status['available']}")
    print(f"  configured={status['configured']}")
    print(f"  external_calls_possible={status['external_calls_possible']}")
    if status.get("reason"):
        print(f"  reason={status['reason']}")
    if status.get("error"):
        print(f"  error={status['error']}")
    print()

    if not case_results:
        print("[probe] no cases evaluated")
        return

    total_runtime = sum(int(c["summary"].get("runtime_ms") or 0) for c in case_results)
    print(f"[probe] {len(case_results)} case(s) evaluated, total {total_runtime} ms")
    for case in case_results:
        summary = case["summary"]
        print(f"\n  ── case {case['case_id']!r}")
        print(f"     claim: {_truncate(case['claim_text'], 100)}")
        print(
            "     best_support_level={support} best_score_percent={pct} "
            "chunks={chunks} cache_hits={cache} embed_requests={req} "
            "runtime_ms={ms}".format(
                support=summary.get("best_support_level"),
                pct=summary.get("best_overall_score_percent"),
                chunks=summary.get("chunk_count"),
                cache=summary.get("cache_hits"),
                req=summary.get("embedding_request_count"),
                ms=summary.get("runtime_ms"),
            )
        )
        if summary.get("limitations"):
            for line in summary["limitations"]:
                print(f"     limitation: {_truncate(line, 200)}")
        if summary.get("errors"):
            for line in summary["errors"]:
                print(f"     error: {_truncate(line, 200)}")
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


def _write_json_out(case_results: list[dict], provider, path: Path) -> None:
    payload = {
        "provider_status": provider.provider_status(),
        "case_count": len(case_results),
        "cases": case_results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_probe(args: argparse.Namespace) -> int:
    started = time.perf_counter()

    # M12.0e-6b-3: SQLite init removed. The embedding cache is PG-backed;
    # postgres_storage.ensure_schema creates it lazily on first engine use.

    provider = _resolve_provider(args)

    # If --no-network is set with provider=openai, refuse to call out even if
    # the provider somehow reports itself available (e.g. cached state).
    if args.no_network and provider.name == "openai" and provider.available:
        provider.available = False
        provider.reason = "no-network mode forced provider offline"
        provider.error = provider.reason

    case_results: list[dict] = []
    if args.query and args.source_text:
        case_results.append(
            _run_case(
                _adhoc_case(args.query, args.source_text, args.source_title, args.source_url),
                provider,
            )
        )
    else:
        cases = _load_fixture_cases(args.case_file, args.max_cases)
        for case in cases:
            case_results.append(_run_case(case, provider))

    _print_summary(case_results, provider, args)

    if args.json_out:
        _write_json_out(case_results, provider, args.json_out)
        print(f"\n[probe] JSON summary written to {args.json_out}")

    elapsed = time.perf_counter() - started
    print(f"\n[probe] total elapsed {elapsed:.2f}s")

    if args.fail_on_unavailable and not provider.available:
        print(
            f"[probe] FAIL: provider {provider.name!r} reported "
            f"available=False (reason={provider.reason or provider.error})",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_probe(args)
    except ProbeError as error:
        print(f"[probe] FAILED: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[probe] aborted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
