"""Phase 2 M10.2: operator CLI for fetching a single registry source.

This CLI is the **only** authorized entry point for triggering the
``source_crawler.fetch_source_url`` helper. Dry-run is the default —
no network request happens unless the operator explicitly passes
``--save``.

Hard contract:
    * Never auto-invoked. The pipeline (``main.py`` / FastAPI) does
      not import this script.
    * Always prints the safety notes (truth_claim, candidate-is-not-
      accuracy) in every mode.
    * ``--dry-run`` (default) makes zero network calls.
    * ``--save`` performs exactly one fetch via the M10.2 crawler
      and persists the result via ``database.save_fetch_artifact``.

Usage:

    python scripts/fetch_registry_source.py --source-id <id> --url <url>
    python scripts/fetch_registry_source.py --source-id <id> --url <url> --dry-run
    python scripts/fetch_registry_source.py --source-id <id> --url <url> --save
    python scripts/fetch_registry_source.py --source-id <id> --url <url> --json
    python scripts/fetch_registry_source.py --registry-path <path> --source-id <id> --url <url>
    python scripts/fetch_registry_source.py --help

Exit codes:
    0 — fetch succeeded (with --save) OR every dry-run safety check passed
    1 — fetch failed, safety check refused, registry load failed, or
        source-id not found
    2 — CLI usage error (missing required flags, unrecognized args)
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
import source_crawler  # noqa: E402


CLI_VERSION = "1.0"

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_TRUTH = (
    "truth_claim: False — fetch results do not imply truth of any content"
)
SAFETY_NOTE_CANDIDATE = (
    "official_source_candidate does not guarantee content accuracy"
)
SAFETY_NOTE_REVIEW = (
    "Fetch results are raw artifacts and require separate human review "
    "before any use in verification."
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator CLI for triggering a single M10.2 source-registry "
            "static fetch. Dry-run is the default — no network request "
            "is made without --save. truth_claim is always False; the "
            "registry never asserts truth."
        ),
        epilog=(
            "Exit codes: 0=fetch ok or dry-run safety checks passed; "
            "1=fetch failed / refused / source not found / registry "
            "load failed; 2=CLI usage error."
        ),
    )
    parser.add_argument(
        "--source-id", required=True,
        help="Registry source_id (e.g. kr_law_open_data_candidate).",
    )
    parser.add_argument(
        "--url", required=True,
        help="URL to fetch. Must be inside the source's allowed_domains.",
    )
    parser.add_argument(
        "--registry-path", default=None,
        help=(
            "Path to the registry JSON. Defaults to "
            "data/source_registry.json under the repo root."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Run all safety checks and print what would happen, but do "
            "NOT make a network request. This is the default."
        ),
    )
    mode.add_argument(
        "--save", action="store_true",
        help=(
            "Actually perform the fetch via source_crawler.fetch_source_url "
            "and persist the result via database.save_fetch_artifact. "
            "Without this flag the script stays offline (dry-run)."
        ),
    )
    parser.add_argument(
        "--timeout-seconds", type=float,
        default=source_crawler.DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"Fetch timeout in seconds (default "
            f"{source_crawler.DEFAULT_TIMEOUT_SECONDS}). Only relevant "
            "with --save."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    return parser


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _shape_dry_run_payload(
    *, source_id: str, url: str, registry_path: str,
    source_found: bool, safety_refusal: Optional[str],
) -> Dict[str, Any]:
    """Return a stable payload describing what the dry-run would do.

    ``safety_refusal`` is set when the M10.2 safety checks would
    refuse the fetch; ``None`` means every check passes and a --save
    invocation would actually issue the HTTP request.
    """
    return {
        "cli_version": CLI_VERSION,
        "mode": "dry_run",
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_id": source_id,
        "url": url,
        "registry_path": registry_path,
        "source_found": bool(source_found),
        "safety_refusal": safety_refusal,
        "would_fetch": bool(source_found and safety_refusal is None),
        "network_fetch_performed": False,
        "truth_claim": False,
        "safety_notes": {
            "truth": SAFETY_NOTE_TRUTH,
            "candidate": SAFETY_NOTE_CANDIDATE,
            "review": SAFETY_NOTE_REVIEW,
        },
    }


def _shape_save_payload(
    *, fetch_result_dict: Dict[str, Any], registry_path: str,
    saved_row_id: Optional[int],
) -> Dict[str, Any]:
    """Stable payload for --save mode. Mirrors the FetchResult shape
    + adds the DB row id when persistence succeeded."""
    return {
        "cli_version": CLI_VERSION,
        "mode": "save",
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "registry_path": registry_path,
        "fetch_result": fetch_result_dict,
        "saved_row_id": saved_row_id,
        "safety_notes": {
            "truth": SAFETY_NOTE_TRUTH,
            "candidate": SAFETY_NOTE_CANDIDATE,
            "review": SAFETY_NOTE_REVIEW,
        },
    }


# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------


def _print_safety_footer() -> None:
    print("")
    print(f"[Safety] {SAFETY_NOTE_TRUTH}")
    print(f"[Safety] {SAFETY_NOTE_CANDIDATE}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")


def _print_dry_run_human(payload: Dict[str, Any]) -> None:
    print("=== fetch_registry_source: DRY RUN — no network request made ===")
    print(f"source_id: {payload['source_id']}")
    print(f"url: {payload['url']}")
    print(f"registry_path: {payload['registry_path']}")
    print(f"source_found: {payload['source_found']}")
    if not payload["source_found"]:
        print(f"result: source_id {payload['source_id']!r} not in registry")
    elif payload["safety_refusal"]:
        print(f"safety_refusal: {payload['safety_refusal']}")
        print("result: would refuse fetch — safety check failed")
    else:
        print("result: all M10.2 safety checks pass; --save would issue the fetch")
    print(f"network_fetch_performed: {payload['network_fetch_performed']}")
    print(f"truth_claim: {payload['truth_claim']}")
    _print_safety_footer()


def _print_save_human(payload: Dict[str, Any]) -> None:
    fr = payload.get("fetch_result") or {}
    print("=== fetch_registry_source: SAVE — single fetch attempted ===")
    print(f"source_id: {fr.get('source_id')}")
    print(f"url: {fr.get('url')}")
    print(f"network_fetch_performed: {fr.get('network_fetch_performed')}")
    print(f"success: {fr.get('success')}")
    print(f"status_code: {fr.get('status_code')}")
    print(f"content_type: {fr.get('content_type')}")
    print(f"fetch_duration_ms: {fr.get('fetch_duration_ms')}")
    print(f"truth_claim: {fr.get('truth_claim')}")
    print(
        f"official_source_candidate: {fr.get('official_source_candidate')}"
    )
    if fr.get("error"):
        print(f"error: {fr.get('error')}")
    text = fr.get("text_content")
    if isinstance(text, str) and text:
        print(f"text_content_length: {len(text)}")
    else:
        print("text_content_length: 0 (no text extracted)")
    if payload.get("saved_row_id") is not None:
        print(f"saved_row_id: {payload['saved_row_id']}")
    else:
        print("saved_row_id: (not persisted)")
    _print_safety_footer()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    source_id = (args.source_id or "").strip()
    url = (args.url or "").strip()
    if not source_id or not url:
        print(
            "[fetch-source] --source-id and --url are required.",
            file=sys.stderr,
        )
        return 2

    # Resolve registry path early so output can quote it consistently.
    try:
        resolved_path = registry_mod.normalize_registry_path(
            args.registry_path,
        )
    except Exception as error:
        print(
            f"[fetch-source] could not resolve registry path: {error}",
            file=sys.stderr,
        )
        return 1
    registry_path_str = str(resolved_path)

    try:
        registry = registry_mod.load_source_registry(resolved_path)
    except registry_mod.SourceRegistryError as error:
        print(
            f"[fetch-source] failed to load registry: {error}",
            file=sys.stderr,
        )
        return 1

    source = registry_mod.get_source_by_id(registry, source_id)
    save_mode = bool(args.save)

    # ----------------------------- DRY RUN -----------------------------
    if not save_mode:
        # Compute the same safety refusal the crawler would surface,
        # WITHOUT calling the crawler (and therefore without ever
        # touching the network).
        if source is None:
            payload = _shape_dry_run_payload(
                source_id=source_id, url=url,
                registry_path=registry_path_str,
                source_found=False, safety_refusal=None,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_dry_run_human(payload)
            return 1

        refusal = source_crawler._run_safety_checks(url, source)
        payload = _shape_dry_run_payload(
            source_id=source_id, url=url,
            registry_path=registry_path_str,
            source_found=True, safety_refusal=refusal,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_dry_run_human(payload)
        # Safety refusal still exits 1 (the operator's --dry-run
        # asked "would this work?"; the answer is "no").
        return 0 if refusal is None else 1

    # ------------------------------ SAVE -------------------------------
    if source is None:
        print(
            f"[fetch-source] source_id {source_id!r} not found in registry "
            f"({registry_path_str}).",
            file=sys.stderr,
        )
        return 1

    config: Dict[str, Any] = {"timeout": args.timeout_seconds}
    result = source_crawler.fetch_source_url(url, source, config=config)
    fetch_dict = source_crawler.fetch_result_to_dict(result)

    saved_row_id: Optional[int] = None
    try:
        import database
        database.init_source_fetch_artifacts_table()
        saved_row_id = database.save_fetch_artifact(fetch_dict)
    except Exception as save_error:
        # Persistence failure does not invalidate the fetch result,
        # but it does mean the artifact is lost — fail the run.
        payload = _shape_save_payload(
            fetch_result_dict=fetch_dict,
            registry_path=registry_path_str,
            saved_row_id=None,
        )
        payload["save_error"] = (
            f"{type(save_error).__name__}: {save_error}"
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_save_human(payload)
            print(
                f"[fetch-source] save_error: {payload['save_error']}",
                file=sys.stderr,
            )
        return 1

    payload = _shape_save_payload(
        fetch_result_dict=fetch_dict,
        registry_path=registry_path_str,
        saved_row_id=saved_row_id,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_save_human(payload)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
