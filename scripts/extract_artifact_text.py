"""Phase 2 M10.4: operator CLI for extracting structured text from
stored fetch artifacts.

Reads ``source_fetch_artifacts`` rows (created by the M10.2 crawler),
runs ``artifact_extractor.extract_text_from_artifact`` against each,
and optionally persists the results into the M10.4
``artifact_text_extractions`` table.

Hard contract:
    * Never auto-invoked. The pipeline (``main.py`` / FastAPI /
      ``scheduler.py``) does not import this script.
    * Always prints the safety notes (truth_claim, raw-artifact)
      in every mode.
    * ``--dry-run`` makes zero database writes.
    * ``--save`` writes only to ``artifact_text_extractions`` — the
      source ``source_fetch_artifacts`` table is never mutated.
    * No HTTP. No browser. No OpenAI. No Anthropic.

Usage::

    python scripts/extract_artifact_text.py --list-artifacts
    python scripts/extract_artifact_text.py --list-artifacts --source-id <id>
    python scripts/extract_artifact_text.py --source-id <id> --dry-run
    python scripts/extract_artifact_text.py --source-id <id> --save
    python scripts/extract_artifact_text.py --artifact-id <int> --dry-run --json
    python scripts/extract_artifact_text.py --help

Exit codes:
    0 — extractions attempted, no fatal errors (some may have
        success=False; that's reported, not fatal)
    1 — DB error, no artifacts found, or every extraction failed
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

import artifact_extractor  # noqa: E402
import database  # noqa: E402


CLI_VERSION = "1.0"

DEFAULT_LIMIT = 10
MAIN_TEXT_PREVIEW_CHARS = 200

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_TRUTH = (
    "truth_claim=False — extraction results do not imply truth of "
    "any content."
)
SAFETY_NOTE_REVIEW = (
    "Extraction results are raw text artifacts requiring separate "
    "human review."
)
SAFETY_NOTE_NO_PIPELINE = (
    "This extractor never feeds the analysis pipeline or verdict "
    "logic; extractions are stored as raw artifacts only."
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract structured text (title, body, sections, language "
            "hint) from rows in source_fetch_artifacts. Never fetches, "
            "scrapes, or contacts any external service. truth_claim is "
            "always False; extraction results never imply truth."
        ),
        epilog=(
            "Exit codes: 0=extractions attempted ok; 1=DB error, no "
            "artifacts found, or every extraction failed; 2=CLI usage "
            "error."
        ),
    )
    parser.add_argument(
        "--source-id", default=None,
        help="Filter artifacts by source_id. Optional when --artifact-id is given.",
    )
    parser.add_argument(
        "--artifact-id", type=int, default=None,
        help="Process a single specific artifact by id.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help=(
            "Path to the SQLite DB. Defaults to the module's DB_PATH "
            "(policy_ai.db in the repo root)."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=(
            f"Max number of artifacts to process (default: "
            f"{DEFAULT_LIMIT}). Clamped to a safe upper bound."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extract and print results but do not save to the DB.",
    )
    parser.add_argument(
        "--save", action="store_true",
        help=(
            "Persist successful ExtractionResults to "
            "artifact_text_extractions. Without this flag the script "
            "stays read-only against source_fetch_artifacts."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    parser.add_argument(
        "--list-artifacts", action="store_true", dest="list_artifacts",
        help=(
            "List rows in source_fetch_artifacts (optionally filtered "
            "by --source-id) and exit. Read-only."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


def _with_db_path(db_path: Optional[str]):
    """Context-manager-ish helper: when ``db_path`` is provided, swap
    ``database.DB_PATH`` for the duration of a block so the existing
    ``get_fetch_artifacts`` (which uses the module-level path) reads
    the right file. Returns a ``(set, restore)`` tuple — the caller
    must call ``restore`` from a ``finally``.
    """
    if db_path is None:
        return None
    original = database.DB_PATH
    database.DB_PATH = Path(db_path)
    return original


def _restore_db_path(original):
    if original is None:
        return
    database.DB_PATH = original


def _load_artifacts(
    *, source_id: Optional[str], artifact_id: Optional[int], limit: int,
) -> List[Dict[str, Any]]:
    rows = database.get_fetch_artifacts(
        source_id=source_id, limit=max(1, min(int(limit or 1), 500)),
    )
    if artifact_id is not None:
        rows = [r for r in rows if int(r.get("id") or 0) == int(artifact_id)]
    return rows


def _load_artifacts_for_list(
    *, source_id: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    return database.get_fetch_artifacts(
        source_id=source_id, limit=max(1, min(int(limit or 1), 500)),
    )


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------


def _summarize_artifact_row(row: Dict[str, Any]) -> Dict[str, Any]:
    raw_html = row.get("raw_html")
    return {
        "id": row.get("id"),
        "source_id": row.get("source_id"),
        "url": row.get("url"),
        "fetch_timestamp": row.get("fetch_timestamp"),
        "status_code": row.get("status_code"),
        "success": bool(row.get("success", False)),
        "has_raw_html": bool(raw_html),
        "raw_html_length": (len(raw_html) if isinstance(raw_html, str) else 0),
        "truth_claim": False,
        "official_source_candidate": bool(
            row.get("official_source_candidate", False)
        ),
    }


def _safety_notes_dict() -> Dict[str, str]:
    return {
        "truth": SAFETY_NOTE_TRUTH,
        "review": SAFETY_NOTE_REVIEW,
        "no_pipeline": SAFETY_NOTE_NO_PIPELINE,
    }


def _print_safety_footer() -> None:
    print("")
    print(f"[Safety] {SAFETY_NOTE_TRUTH}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")
    print(f"[Safety] {SAFETY_NOTE_NO_PIPELINE}")


# ---------------------------------------------------------------------------
# --list-artifacts mode
# ---------------------------------------------------------------------------


def _run_list_mode(
    *, source_id: Optional[str], limit: int, db_path: Optional[str],
    as_json: bool,
) -> int:
    original = _with_db_path(db_path)
    try:
        try:
            rows = _load_artifacts_for_list(
                source_id=source_id, limit=limit,
            )
        except Exception as error:
            print(
                f"[extract] failed to load source_fetch_artifacts: {error}",
                file=sys.stderr,
            )
            return 1
    finally:
        _restore_db_path(original)

    summaries = [_summarize_artifact_row(r) for r in rows]
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "list_artifacts",
        "db_path": str(db_path) if db_path is not None else str(database.DB_PATH),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "filter": {"source_id": source_id},
        "artifacts": summaries,
        "summary": {
            "total": len(summaries),
            "with_raw_html": sum(
                1 for s in summaries if s["has_raw_html"]
            ),
            "successful_fetches": sum(
                1 for s in summaries if s["success"]
            ),
        },
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== source_fetch_artifacts (read-only listing) ===")
    print("")
    if not summaries:
        print("(no artifacts found)")
    else:
        header = (
            f"{'id':<5} | {'source_id':<32} | {'success':<7} "
            f"| {'has_html':<8} | {'url'}"
        )
        print(header)
        print("-" * len(header))
        for s in summaries:
            sid = str(s.get("source_id") or "")
            url = str(s.get("url") or "")
            print(
                f"{str(s.get('id') or ''):<5} | {sid:<32} | "
                f"{str(s.get('success')):<7} | "
                f"{str(s.get('has_raw_html')):<8} | {url}"
            )
    summary = payload["summary"]
    print("")
    print(
        f"Total: {summary['total']} | "
        f"with_raw_html={summary['with_raw_html']} | "
        f"successful_fetches={summary['successful_fetches']}"
    )
    _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# Extraction mode
# ---------------------------------------------------------------------------


def _shape_extraction_payload(
    *, result_dict: Dict[str, Any], saved_row_id: Optional[int],
    dry_run: bool, save_error: Optional[str],
) -> Dict[str, Any]:
    payload = {
        "artifact_id": result_dict.get("artifact_id"),
        "source_id": result_dict.get("source_id"),
        "url": result_dict.get("url"),
        "extraction_timestamp": result_dict.get("extraction_timestamp"),
        "extraction_duration_ms": result_dict.get("extraction_duration_ms"),
        "success": bool(result_dict.get("success", False)),
        "error": result_dict.get("error"),
        "title": result_dict.get("title"),
        "main_text_length": (
            len(result_dict.get("main_text") or "")
        ),
        "main_text_preview": (
            (result_dict.get("main_text") or "")[:MAIN_TEXT_PREVIEW_CHARS]
        ),
        "word_count": int(result_dict.get("word_count") or 0),
        "language_hint": result_dict.get("language_hint") or "unknown",
        "sections": result_dict.get("sections"),
        "truth_claim": False,
        "official_source_candidate": bool(
            result_dict.get("official_source_candidate", False)
        ),
        "saved_row_id": saved_row_id,
        "mode": "dry_run" if dry_run else "save",
    }
    if save_error:
        payload["save_error"] = save_error
    return payload


def _print_extraction_human(payload: Dict[str, Any]) -> None:
    print("=== Extraction Result ===")
    print(f"artifact_id: {payload.get('artifact_id')}")
    print(f"source_id: {payload.get('source_id')}")
    print(f"url: {payload.get('url')}")
    print(f"success: {payload.get('success')}")
    if not payload.get("success"):
        print(f"error: {payload.get('error')}")
    else:
        title = payload.get("title")
        title_str = title if title is not None else "(none)"
        print(f"title: {title_str}")
        print(f"word_count: {payload.get('word_count')}")
        print(f"language_hint: {payload.get('language_hint')}")
        preview = payload.get("main_text_preview") or ""
        print(
            f"main_text (first {MAIN_TEXT_PREVIEW_CHARS} chars): {preview}"
        )
    print(f"truth_claim: False")
    print(
        f"official_source_candidate: {payload.get('official_source_candidate')}"
    )
    if payload.get("mode") == "dry_run":
        print("dry_run: True (no DB write)")
    else:
        if payload.get("saved_row_id") is not None:
            print(f"saved_row_id: {payload['saved_row_id']}")
        elif payload.get("save_error"):
            print(f"save_error: {payload['save_error']}")
        else:
            print("saved_row_id: (not persisted)")


def _run_extract_mode(
    *, source_id: Optional[str], artifact_id: Optional[int],
    db_path: Optional[str], limit: int, dry_run: bool, save: bool,
    as_json: bool,
) -> int:
    original = _with_db_path(db_path)
    try:
        try:
            rows = _load_artifacts(
                source_id=source_id, artifact_id=artifact_id, limit=limit,
            )
        except Exception as error:
            print(
                f"[extract] failed to load source_fetch_artifacts: {error}",
                file=sys.stderr,
            )
            return 1

        if not rows:
            payload = {
                "cli_version": CLI_VERSION,
                "mode": "extract",
                "db_path": (
                    str(db_path) if db_path is not None
                    else str(database.DB_PATH)
                ),
                "processed_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds",
                ),
                "filter": {
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                },
                "results": [],
                "summary": {
                    "total": 0, "succeeded": 0, "failed": 0, "saved": 0,
                },
                "safety_notes": _safety_notes_dict(),
                "warning": "no source_fetch_artifacts rows matched the filter",
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("=== Extraction Result ===")
                print("(no source_fetch_artifacts rows matched the filter)")
                _print_safety_footer()
            return 1

        per_artifact: List[Dict[str, Any]] = []
        saved_count = 0
        succeeded = 0
        failed = 0
        for row in rows:
            result = artifact_extractor.extract_text_from_artifact(row)
            result_dict = artifact_extractor.extraction_result_to_dict(result)
            saved_row_id: Optional[int] = None
            save_error: Optional[str] = None
            if save and not dry_run and result.success:
                try:
                    saved_row_id = database.save_extraction_result(
                        result_dict, db_path=db_path,
                    )
                    saved_count += 1
                except Exception as error:
                    save_error = (
                        f"{type(error).__name__}: {error}"
                    )
            payload = _shape_extraction_payload(
                result_dict=result_dict,
                saved_row_id=saved_row_id,
                dry_run=dry_run or not save,
                save_error=save_error,
            )
            per_artifact.append(payload)
            if result.success:
                succeeded += 1
            else:
                failed += 1
    finally:
        _restore_db_path(original)

    combined = {
        "cli_version": CLI_VERSION,
        "mode": "extract",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "filter": {
            "source_id": source_id,
            "artifact_id": artifact_id,
        },
        "dry_run": bool(dry_run),
        "save": bool(save),
        "results": per_artifact,
        "summary": {
            "total": len(per_artifact),
            "succeeded": succeeded,
            "failed": failed,
            "saved": saved_count,
        },
        "safety_notes": _safety_notes_dict(),
    }

    if as_json:
        print(json.dumps(combined, ensure_ascii=False, indent=2))
    else:
        for i, p in enumerate(per_artifact):
            _print_extraction_human(p)
            if i < len(per_artifact) - 1:
                print("")
                print("---")
                print("")
        print("")
        print(
            f"Summary: total={combined['summary']['total']} "
            f"succeeded={combined['summary']['succeeded']} "
            f"failed={combined['summary']['failed']} "
            f"saved={combined['summary']['saved']}"
        )
        _print_safety_footer()

    # Exit 1 when no extraction succeeded (every artifact failed).
    if succeeded == 0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_artifacts:
        return _run_list_mode(
            source_id=args.source_id,
            limit=args.limit,
            db_path=args.db_path,
            as_json=bool(args.json),
        )

    if args.source_id is None and args.artifact_id is None:
        print(
            "[extract] --source-id or --artifact-id is required (or pass "
            "--list-artifacts to inspect).",
            file=sys.stderr,
        )
        return 2

    if args.save and args.dry_run:
        print(
            "[extract] --save and --dry-run are mutually exclusive.",
            file=sys.stderr,
        )
        return 2

    return _run_extract_mode(
        source_id=args.source_id,
        artifact_id=args.artifact_id,
        db_path=args.db_path,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        save=bool(args.save),
        as_json=bool(args.json),
    )


if __name__ == "__main__":
    sys.exit(main())
