"""Phase 2 M10.1: source registry URL classifier CLI.

Offline classifier that runs URLs against the M10.0 source registry
(``data/source_registry.json`` by default) and prints the
classification + future capture plan. **Makes no network requests of
any kind.**

Hard contract (pinned by ``tests/test_source_url_classifier.py``):
    * No HTTP. Imports only ``urllib.parse`` (for URL parsing) — no
      ``urllib.request``, no ``requests``, no ``httpx``, no socket
      module, no ``playwright``/``browser_use``/``openclaw``.
    * No OpenAI / Anthropic.
    * No subprocess / git verbs.
    * No DB / FastAPI / Render.
    * Never modifies the registry file.
    * Never asserts truth. ``MATCHED`` only means a URL pointed at a
      registry candidate; it does not imply the content at that URL
      is true.
    * Capture plan is *future plan only*; ``network_fetch_performed``
      is always ``false``.

Usage:

    python scripts/classify_source_url.py <url> [<url> ...]
    python scripts/classify_source_url.py --url <url> --url <url>
    python scripts/classify_source_url.py --registry-path <path> <url>
    python scripts/classify_source_url.py --json <url>
    python scripts/classify_source_url.py --help

Exit codes (conservative):
    0 — every URL matched a known, allowed registry entry
    1 — any URL was NO_MATCH / REJECTED / ERROR, or the registry
        file failed to load
    2 — CLI usage error (no URLs, unrecognized arguments)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import source_registry as registry_mod  # noqa: E402


CLI_VERSION = "1.0"

# Status tags surfaced in both human + JSON output. Kept as module-level
# constants so tests can pin them without depending on stringly-typed
# matches inside the runtime code.
STATUS_MATCHED = "MATCHED"
STATUS_NO_MATCH = "NO_MATCH"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"

# Reasons returned by ``classify_url_against_registry`` that we map to
# REJECTED. Everything else stays NO_MATCH (or ERROR for registry-
# corruption signals).
#
# The M10.0 helper documents these reason tags (see source_registry.py
# ``classify_url_against_registry``):
#   matched, no_match, empty_url, missing_scheme_or_host,
#   credentials_in_url, invalid_host, registry_not_object,
#   registry_sources_not_list
_REJECT_REASONS = frozenset({
    "empty_url",
    "missing_scheme_or_host",
    "credentials_in_url",
    "invalid_host",
})
_ERROR_REASONS = frozenset({
    "registry_not_object",
    "registry_sources_not_list",
})

# Two safety notes the CLI emits in every mode. They are deliberately
# verbose so a consumer reading the JSON or the human output cannot
# miss them. The phrasing is asserted by tests so any future edit that
# weakens them surfaces immediately.
SAFETY_NOTE_NOT_TRUTH = (
    "official_source_candidate does not imply truth"
)
SAFETY_NOTE_NO_NETWORK = (
    "The capture plan is a future plan only. No scraping or crawling "
    "is performed by this CLI."
)
SAFETY_NOTE_REVIEW = (
    "All registry entries remain operator_review_required=true "
    "and default_enabled=false until explicitly enabled by an operator."
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify URLs against the M10.0 source registry "
            "(data/source_registry.json by default). Never fetches, "
            "scrapes, or contacts any external service. MATCHED only "
            "means a URL pointed at a registry candidate — it does NOT "
            "guarantee the truthfulness of any content at the URL."
        ),
        epilog=(
            "Exit codes: 0=every URL matched a known allowed source; "
            "1=any URL was NO_MATCH / REJECTED / ERROR or registry "
            "load failed; 2=CLI usage error."
        ),
    )
    parser.add_argument(
        "urls", nargs="*",
        help="URLs to classify (positional). Combinable with --url.",
    )
    parser.add_argument(
        "--url", action="append", default=[],
        help=(
            "URL to classify. Repeatable. Combinable with positional URLs."
        ),
    )
    parser.add_argument(
        "--registry-path", default=None,
        help=(
            "Path to the registry JSON. Defaults to "
            "data/source_registry.json under the repo root."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the results as JSON only (no human header / footer).",
    )
    return parser


def _collect_urls(args: argparse.Namespace) -> List[str]:
    # Positional URLs come first, then any repeated --url values.
    seen = []
    for u in list(args.urls or []) + list(args.url or []):
        if u is None:
            continue
        try:
            s = str(u).strip()
        except Exception:
            continue
        if s:
            seen.append(s)
    return seen


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _status_from_classification(classification: Dict[str, Any]) -> str:
    reason = str((classification or {}).get("reason") or "").strip()
    if reason == "matched" and bool(classification.get("allowed")) \
            and classification.get("matched_source_id"):
        return STATUS_MATCHED
    if reason in _REJECT_REASONS:
        return STATUS_REJECTED
    if reason in _ERROR_REASONS:
        return STATUS_ERROR
    # Default — including the documented "no_match" reason.
    return STATUS_NO_MATCH


def _build_result_for_url(
    registry: Dict[str, Any], url: str,
) -> Dict[str, Any]:
    """Run one URL through ``classify_url_against_registry`` and
    (when matched) ``build_source_capture_plan``. Never re-raises —
    unexpected exceptions become ``STATUS_ERROR`` results so the
    batch can finish reporting on the other URLs."""
    result: Dict[str, Any] = {
        "url": url,
        "status": STATUS_NO_MATCH,
        "classification": None,
        "capture_plan": None,
        "safety_note": SAFETY_NOTE_NOT_TRUTH,
    }
    try:
        classification = registry_mod.classify_url_against_registry(
            registry, url,
        )
    except Exception as error:
        result["status"] = STATUS_ERROR
        result["error"] = f"{type(error).__name__}: {error}"
        return result

    if not isinstance(classification, dict):
        result["status"] = STATUS_ERROR
        result["error"] = "classify_url_against_registry returned non-dict"
        return result

    status = _status_from_classification(classification)
    result["status"] = status

    # Project a stable, compact ``classification`` payload. Avoid
    # leaking any internal fields the helper might add in future
    # milestones by only copying the documented keys.
    result["classification"] = {
        "matched_source_id": classification.get("matched_source_id"),
        "allowed": bool(classification.get("allowed")),
        "reason": classification.get("reason"),
        "host": classification.get("host"),
    }

    if status == STATUS_MATCHED:
        source = registry_mod.get_source_by_id(
            registry, classification.get("matched_source_id"),
        )
        if isinstance(source, dict):
            try:
                plan = registry_mod.build_source_capture_plan(source, url=url)
            except Exception as error:
                # Promote a planning failure to ERROR — we matched the
                # source but couldn't produce a plan, which is an
                # inconsistency we want surfaced.
                result["status"] = STATUS_ERROR
                result["error"] = f"capture_plan failed: {error}"
                return result
            # Enrich the classification with source-side metadata that
            # the human-readable output references.
            result["classification"]["source_type"] = source.get("source_type")
            result["classification"]["official_source_candidate"] = bool(
                source.get("official_source_candidate", False)
            )
            # Stable capture-plan projection. Always carries
            # ``network_fetch_performed: False`` and a ``plan_status``
            # alias of the helper's ``next_step`` (the spec's sample
            # output uses ``plan_status``).
            result["capture_plan"] = {
                "capture_method": plan.get("capture_method"),
                "browser_automation": plan.get("browser_automation"),
                "operator_review_required": bool(
                    plan.get("operator_review_required", True)
                ),
                "default_enabled": bool(plan.get("default_enabled", False)),
                "url_allowed": bool(plan.get("url_allowed", False)),
                "network_fetch_performed": False,
                "plan_status": plan.get("next_step"),
            }
        else:
            # Helper said matched but we cannot resolve the source —
            # treat as ERROR so the operator notices the inconsistency.
            result["status"] = STATUS_ERROR
            result["error"] = (
                "classification reported matched_source_id="
                f"{classification.get('matched_source_id')!r} but no "
                "source with that id was found in the registry"
            )
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {STATUS_MATCHED: 0, STATUS_NO_MATCH: 0,
              STATUS_REJECTED: 0, STATUS_ERROR: 0}
    for r in results:
        s = r.get("status")
        if s in counts:
            counts[s] += 1
    return {
        "total": len(results),
        "matched": counts[STATUS_MATCHED],
        "no_match": counts[STATUS_NO_MATCH],
        "rejected": counts[STATUS_REJECTED],
        "errors": counts[STATUS_ERROR],
        "all_matched_safely": (
            counts[STATUS_MATCHED] == len(results)
            and counts[STATUS_NO_MATCH] == 0
            and counts[STATUS_REJECTED] == 0
            and counts[STATUS_ERROR] == 0
            and len(results) > 0
        ),
    }


def _print_human_one(result: Dict[str, Any]) -> None:
    print(f"URL: {result.get('url')}")
    print(f"Status: {result.get('status')}")
    classification = result.get("classification") or {}
    status = result.get("status")
    if status == STATUS_MATCHED:
        print(f"source_id: {classification.get('matched_source_id')}")
        print(f"source_type: {classification.get('source_type')}")
        print(f"allowed: {classification.get('allowed')}")
        print(
            "official_source_candidate: "
            f"{classification.get('official_source_candidate')}"
        )
        print("")
        print("[Important]")
        print(
            "- MATCHED only means this URL matches a registry candidate."
        )
        print(f"- {SAFETY_NOTE_NOT_TRUTH}.")
        print(f"- {SAFETY_NOTE_NO_NETWORK}")
        plan = result.get("capture_plan") or {}
        print("")
        print("Capture Plan:")
        print(f"  capture_method: {plan.get('capture_method')}")
        print(f"  browser_automation: {plan.get('browser_automation')}")
        print(f"  plan_status: {plan.get('plan_status')}")
        print(
            f"  operator_review_required: "
            f"{plan.get('operator_review_required')}"
        )
        print(
            f"  default_enabled: {plan.get('default_enabled')}"
        )
        print(
            f"  network_fetch_performed: "
            f"{plan.get('network_fetch_performed')}"
        )
    elif status == STATUS_NO_MATCH:
        print("No matching registry entry found.")
        print("No capture plan available.")
        print("")
        print(f"[Important] {SAFETY_NOTE_NOT_TRUTH}.")
        print(f"[Important] {SAFETY_NOTE_NO_NETWORK}")
    elif status == STATUS_REJECTED:
        reason = classification.get("reason") or "rejected"
        print(f"Reason: {reason}")
        print("No capture plan available.")
        print("")
        print(f"[Important] {SAFETY_NOTE_NOT_TRUTH}.")
        print(f"[Important] {SAFETY_NOTE_NO_NETWORK}")
    else:  # ERROR
        err = result.get("error") or "unknown error"
        print(f"Error: {err}")
        print("No capture plan available.")
        print("")
        print(f"[Important] {SAFETY_NOTE_NOT_TRUTH}.")
        print(f"[Important] {SAFETY_NOTE_NO_NETWORK}")


def _print_human(results: List[Dict[str, Any]],
                 summary: Dict[str, Any]) -> None:
    print("=== URL Classification Results ===")
    print("")
    for i, r in enumerate(results):
        _print_human_one(r)
        if i < len(results) - 1:
            print("")
            print("---")
            print("")
    print("")
    print(
        f"Summary: {summary['total']} processed | "
        f"matched={summary['matched']} | "
        f"no_match={summary['no_match']} | "
        f"rejected={summary['rejected']} | "
        f"errors={summary['errors']}"
    )
    print("")
    print(f"[Safety] {SAFETY_NOTE_NOT_TRUTH}.")
    print(f"[Safety] {SAFETY_NOTE_NO_NETWORK}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")


def _print_json(results: List[Dict[str, Any]],
                summary: Dict[str, Any],
                *, registry_path: str) -> None:
    payload = {
        "cli_version": CLI_VERSION,
        "registry_path": registry_path,
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "results": results,
        "summary": summary,
        "safety_notes": {
            "not_truth": SAFETY_NOTE_NOT_TRUTH,
            "no_network": SAFETY_NOTE_NO_NETWORK,
            "review": SAFETY_NOTE_REVIEW,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    urls = _collect_urls(args)
    if not urls:
        print(
            "[classify] no URLs provided. Pass one or more URLs as "
            "positional arguments or via --url.",
            file=sys.stderr,
        )
        return 2

    # Resolve registry path early so the JSON / human output can quote
    # it consistently even when the file fails to load.
    try:
        resolved_path = registry_mod.normalize_registry_path(
            args.registry_path,
        )
    except Exception as error:
        print(
            f"[classify] could not resolve registry path: {error}",
            file=sys.stderr,
        )
        return 1
    registry_path_str = str(resolved_path)

    # Load the registry. A load failure is exit 1 (not 2 — usage error
    # is reserved for argparse-level issues).
    try:
        registry = registry_mod.load_source_registry(resolved_path)
    except registry_mod.SourceRegistryError as error:
        print(
            f"[classify] failed to load registry: {error}",
            file=sys.stderr,
        )
        if args.json:
            fallback = {
                "cli_version": CLI_VERSION,
                "registry_path": registry_path_str,
                "processed_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds",
                ),
                "results": [],
                "summary": {
                    "total": 0, "matched": 0, "no_match": 0,
                    "rejected": 0, "errors": 1,
                    "all_matched_safely": False,
                },
                "load_error": str(error),
                "safety_notes": {
                    "not_truth": SAFETY_NOTE_NOT_TRUTH,
                    "no_network": SAFETY_NOTE_NO_NETWORK,
                    "review": SAFETY_NOTE_REVIEW,
                },
            }
            print(json.dumps(fallback, ensure_ascii=False, indent=2))
        return 1

    results = [_build_result_for_url(registry, u) for u in urls]
    summary = _summarize(results)

    if args.json:
        _print_json(results, summary, registry_path=registry_path_str)
    else:
        _print_human(results, summary)

    return 0 if summary["all_matched_safely"] else 1


if __name__ == "__main__":
    sys.exit(main())
