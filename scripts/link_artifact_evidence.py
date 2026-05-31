"""Phase 2 M10.5: operator CLI for producing keyword-overlap evidence
candidates from extracted artifact text against stored analysis claims.

Reads ``artifact_text_extractions`` (M10.4 output) and one row from
``analysis_results``, runs
``artifact_evidence_linker.find_evidence_candidates`` against the
pair, and optionally persists the results into the M10.5
``artifact_evidence_candidates`` table.

Hard contract:
    * Never auto-invoked. The pipeline (``main.py`` / FastAPI /
      ``scheduler.py``) does not import this script.
    * Always prints the four safety notes (unreviewed, truth_claim,
      operator_review_required, no-pipeline) in every mode.
    * ``--dry-run`` makes zero database writes.
    * ``--save`` writes only to ``artifact_evidence_candidates`` —
      ``source_fetch_artifacts``, ``artifact_text_extractions``, and
      ``analysis_results`` are all read-only here.
    * No HTTP. No browser. No OpenAI. No Anthropic. No embeddings.

Usage::

    python scripts/link_artifact_evidence.py --list-extractions
    python scripts/link_artifact_evidence.py --list-candidates
    python scripts/link_artifact_evidence.py --analysis-id <id> --dry-run
    python scripts/link_artifact_evidence.py --analysis-id <id> --save
    python scripts/link_artifact_evidence.py --analysis-id <id> --source-id <id> --json
    python scripts/link_artifact_evidence.py --help

Exit codes:
    0 — candidates were produced (or a list mode succeeded)
    1 — no extractions found, no analysis found, DB error, or every
        (extraction, claim) pair scored below ``--min-score``
    2 — CLI usage error (missing required flags, conflicting flags,
        unrecognized args)
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

import artifact_evidence_linker as linker  # noqa: E402
import database  # noqa: E402


CLI_VERSION = "1.0"

DEFAULT_LIMIT = 10
SUPPORTING_PASSAGE_PREVIEW_CHARS = 200

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_UNREVIEWED = (
    "These are unreviewed keyword-match candidates only."
)
SAFETY_NOTE_TRUTH = (
    "truth_claim=False — candidates do not imply truth of any content."
)
SAFETY_NOTE_REVIEW = (
    "operator_review_required=True — all candidates require human "
    "review before any verification use."
)
SAFETY_NOTE_NO_PIPELINE = (
    "Candidates do not feed into the live analysis pipeline or affect "
    "any verdict."
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Produce keyword-overlap evidence candidates from "
            "artifact_text_extractions rows against an analysis_results "
            "row. Never fetches, scrapes, or contacts any external "
            "service. truth_claim is always False; "
            "operator_review_required is always True; candidates never "
            "feed the verdict pipeline."
        ),
        epilog=(
            "Exit codes: 0=candidates produced (or list mode ok); "
            "1=no data, DB error, or all scores below --min-score; "
            "2=CLI usage error."
        ),
    )
    parser.add_argument(
        "--analysis-id", default=None,
        help=(
            "analysis_results.id (or any stable identifier) to match "
            "extractions against. Required for the link mode."
        ),
    )
    parser.add_argument(
        "--source-id", default=None,
        help="Filter extractions by source_id.",
    )
    parser.add_argument(
        "--min-score", type=float,
        default=linker.DEFAULT_MIN_SCORE,
        help=(
            f"Minimum token-overlap match score (default: "
            f"{linker.DEFAULT_MIN_SCORE}). Clamped to [0.0, 1.0]."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=(
            f"Max number of extractions to process (default: "
            f"{DEFAULT_LIMIT}). Clamped to a safe upper bound."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print candidates but do not save to the DB.",
    )
    parser.add_argument(
        "--save", action="store_true",
        help=(
            "Persist produced EvidenceCandidates to "
            "artifact_evidence_candidates. Without this flag the script "
            "stays read-only against the input tables."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    parser.add_argument(
        "--list-extractions", action="store_true",
        dest="list_extractions",
        help=(
            "List rows in artifact_text_extractions (optionally "
            "filtered by --source-id) and exit. Read-only."
        ),
    )
    parser.add_argument(
        "--list-candidates", action="store_true",
        dest="list_candidates",
        help=(
            "List rows in artifact_evidence_candidates (optionally "
            "filtered by --analysis-id) and exit. Read-only."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _safety_notes_dict() -> Dict[str, str]:
    return {
        "unreviewed": SAFETY_NOTE_UNREVIEWED,
        "truth": SAFETY_NOTE_TRUTH,
        "review": SAFETY_NOTE_REVIEW,
        "no_pipeline": SAFETY_NOTE_NO_PIPELINE,
    }


def _print_safety_footer() -> None:
    print("")
    print(f"[Safety] {SAFETY_NOTE_UNREVIEWED}")
    print(f"[Safety] {SAFETY_NOTE_TRUTH}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")
    print(f"[Safety] {SAFETY_NOTE_NO_PIPELINE}")


def _summarize_extraction_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "source_id": row.get("source_id"),
        "url": row.get("url"),
        "extraction_timestamp": row.get("extraction_timestamp"),
        "success": bool(row.get("success", False)),
        "word_count": row.get("word_count"),
        "language_hint": row.get("language_hint"),
        "title": row.get("title"),
        "has_main_text": bool(row.get("main_text")),
        "truth_claim": False,
        "official_source_candidate": bool(
            row.get("official_source_candidate", False)
        ),
    }


def _summarize_candidate_row(row: Dict[str, Any]) -> Dict[str, Any]:
    matched_tokens = row.get("matched_tokens")
    if isinstance(matched_tokens, str):
        try:
            matched_tokens_list = json.loads(matched_tokens)
        except (TypeError, ValueError):
            matched_tokens_list = matched_tokens
    else:
        matched_tokens_list = matched_tokens
    return {
        "id": row.get("id"),
        "extraction_id": row.get("extraction_id"),
        "source_id": row.get("source_id"),
        "url": row.get("url"),
        "analysis_id": row.get("analysis_id"),
        "claim_text": row.get("claim_text"),
        "match_score": row.get("match_score"),
        "matched_tokens": matched_tokens_list,
        "supporting_passage": row.get("supporting_passage"),
        "candidate_timestamp": row.get("candidate_timestamp"),
        "truth_claim": False,
        "operator_review_required": True,
        "official_source_candidate": bool(
            row.get("official_source_candidate", False)
        ),
        "notes": row.get("notes"),
    }


# ---------------------------------------------------------------------------
# --list-extractions mode
# ---------------------------------------------------------------------------


def _run_list_extractions(
    *, source_id: Optional[str], limit: int, as_json: bool,
) -> int:
    try:
        rows = database.get_extraction_results(
            source_id=source_id,
            limit=max(1, min(int(limit or 1), 500)),
        )
    except Exception as error:
        print(
            f"[link] failed to load artifact_text_extractions: {error}",
            file=sys.stderr,
        )
        return 1

    summaries = [_summarize_extraction_row(r) for r in rows]
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "list_extractions",
        "store": "postgres",
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filter": {"source_id": source_id},
        "extractions": summaries,
        "summary": {
            "total": len(summaries),
            "with_main_text": sum(1 for s in summaries if s["has_main_text"]),
            "successful": sum(1 for s in summaries if s["success"]),
        },
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== artifact_text_extractions (read-only listing) ===")
    print("")
    if not summaries:
        print("(no extractions found)")
    else:
        header = (
            f"{'id':<5} | {'source_id':<32} | {'lang':<7} "
            f"| {'words':<6} | {'url'}"
        )
        print(header)
        print("-" * len(header))
        for s in summaries:
            sid = str(s.get("source_id") or "")
            lang = str(s.get("language_hint") or "")
            words = str(s.get("word_count") or "")
            url = str(s.get("url") or "")
            print(
                f"{str(s.get('id') or ''):<5} | {sid:<32} | {lang:<7} "
                f"| {words:<6} | {url}"
            )
    summary = payload["summary"]
    print("")
    print(
        f"Total: {summary['total']} | "
        f"with_main_text={summary['with_main_text']} | "
        f"successful={summary['successful']}"
    )
    _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# --list-candidates mode
# ---------------------------------------------------------------------------


def _run_list_candidates(
    *, analysis_id: Optional[str], limit: int, as_json: bool,
) -> int:
    try:
        rows = database.get_evidence_candidates(
            analysis_id=analysis_id,
            limit=max(1, min(int(limit or 1), 500)),
        )
    except Exception as error:
        print(
            f"[link] failed to load artifact_evidence_candidates: {error}",
            file=sys.stderr,
        )
        return 1

    summaries = [_summarize_candidate_row(r) for r in rows]
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "list_candidates",
        "store": "postgres",
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filter": {"analysis_id": analysis_id},
        "candidates": summaries,
        "summary": {"total": len(summaries)},
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== artifact_evidence_candidates (read-only listing) ===")
    print("")
    if not summaries:
        print("(no candidates found)")
    else:
        header = (
            f"{'id':<5} | {'extraction':<10} | {'analysis':<10} "
            f"| {'score':<6} | {'claim'}"
        )
        print(header)
        print("-" * len(header))
        for s in summaries:
            score = s.get("match_score")
            score_str = (
                f"{float(score):.3f}" if isinstance(score, (int, float))
                else str(score or "")
            )
            print(
                f"{str(s.get('id') or ''):<5} | "
                f"{str(s.get('extraction_id') or ''):<10} | "
                f"{str(s.get('analysis_id') or ''):<10} | "
                f"{score_str:<6} | {str(s.get('claim_text') or '')[:80]}"
            )
    print("")
    print(f"Total: {payload['summary']['total']}")
    _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# Link mode
# ---------------------------------------------------------------------------


def _print_candidate_human(candidate: Dict[str, Any]) -> None:
    print("=== Evidence Candidate ===")
    print(f"extraction_id: {candidate.get('extraction_id')}")
    print(f"source_id: {candidate.get('source_id')}")
    print(f"url: {candidate.get('url')}")
    print(f"analysis_id: {candidate.get('analysis_id')}")
    print(f"claim_text: {candidate.get('claim_text')}")
    score = candidate.get("match_score")
    if isinstance(score, (int, float)):
        print(f"match_score: {float(score):.3f}")
    else:
        print(f"match_score: {score}")
    print(f"matched_tokens: {candidate.get('matched_tokens')}")
    passage = candidate.get("supporting_passage") or ""
    preview = passage[:SUPPORTING_PASSAGE_PREVIEW_CHARS]
    print(
        f"supporting_passage (first "
        f"{SUPPORTING_PASSAGE_PREVIEW_CHARS} chars): {preview}"
    )
    print("truth_claim: False")
    print("operator_review_required: True")
    print(f"notes: {candidate.get('notes')}")


def _candidate_payload(
    candidate, *, saved_row_id: Optional[int], save_error: Optional[str],
    dry_run: bool,
) -> Dict[str, Any]:
    payload = {
        "extraction_id": candidate.extraction_id,
        "source_id": candidate.source_id,
        "url": candidate.url,
        "analysis_id": candidate.analysis_id,
        "claim_text": candidate.claim_text,
        "match_score": candidate.match_score,
        "matched_tokens": list(candidate.matched_tokens or []),
        "supporting_passage": candidate.supporting_passage,
        "candidate_timestamp": candidate.candidate_timestamp,
        "truth_claim": False,
        "operator_review_required": True,
        "official_source_candidate": bool(candidate.official_source_candidate),
        "notes": candidate.notes,
        "saved_row_id": saved_row_id,
        "mode": "dry_run" if dry_run else "save",
    }
    if save_error:
        payload["save_error"] = save_error
    return payload


def _run_link_mode(
    *, analysis_id: str, source_id: Optional[str], min_score: float,
    limit: int, dry_run: bool, save: bool,
    as_json: bool,
) -> int:
    try:
        analysis_row = database.get_result_by_id(int(analysis_id))
    except (TypeError, ValueError):
        analysis_row = None
    except Exception as error:
        print(
            f"[link] failed to load analysis_results row: {error}",
            file=sys.stderr,
        )
        return 1

    if not analysis_row:
        payload = {
            "cli_version": CLI_VERSION,
            "mode": "link",
            "store": "postgres",
            "processed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
            "filter": {
                "analysis_id": analysis_id,
                "source_id": source_id,
                "min_score": min_score,
            },
            "candidates": [],
            "summary": {
                "total_candidates": 0, "total_extractions": 0,
                "matched_extractions": 0, "saved": 0,
            },
            "safety_notes": _safety_notes_dict(),
            "warning": (
                f"no analysis_results row with id={analysis_id!r}"
            ),
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("=== Evidence Candidate ===")
            print(f"(no analysis_results row with id={analysis_id!r})")
            _print_safety_footer()
        return 1

    try:
        extractions = database.get_extraction_results(
            source_id=source_id,
            limit=max(1, min(int(limit or 1), 500)),
        )
    except Exception as error:
        print(
            f"[link] failed to load artifact_text_extractions: {error}",
            file=sys.stderr,
        )
        return 1

    if not extractions:
        payload = {
            "cli_version": CLI_VERSION,
            "mode": "link",
            "store": "postgres",
            "processed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
            "filter": {
                "analysis_id": analysis_id,
                "source_id": source_id,
                "min_score": min_score,
            },
            "candidates": [],
            "summary": {
                "total_candidates": 0, "total_extractions": 0,
                "matched_extractions": 0, "saved": 0,
            },
            "safety_notes": _safety_notes_dict(),
            "warning": "no artifact_text_extractions rows matched the filter",
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("=== Evidence Candidate ===")
            print("(no artifact_text_extractions rows matched the filter)")
            _print_safety_footer()
        return 1

    all_candidates: List = []
    matched_extractions = 0
    for extraction in extractions:
        candidates = linker.find_evidence_candidates(
            extraction, analysis_row, min_score=min_score,
        )
        if candidates:
            matched_extractions += 1
        for c in candidates:
            all_candidates.append(c)

    saved_count = 0
    per_candidate_payloads: List[Dict[str, Any]] = []
    for candidate in all_candidates:
        saved_row_id: Optional[int] = None
        save_error: Optional[str] = None
        if save and not dry_run:
            try:
                candidate_dict = linker.candidate_to_dict(candidate)
                saved_row_id = database.save_evidence_candidate(
                    candidate_dict,
                )
                saved_count += 1
            except Exception as error:
                save_error = f"{type(error).__name__}: {error}"
        per_candidate_payloads.append(_candidate_payload(
            candidate,
            saved_row_id=saved_row_id,
            save_error=save_error,
            dry_run=dry_run or not save,
        ))

    combined = {
        "cli_version": CLI_VERSION,
        "mode": "link",
        "store": "postgres",
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "filter": {
            "analysis_id": analysis_id,
            "source_id": source_id,
            "min_score": min_score,
        },
        "dry_run": bool(dry_run),
        "save": bool(save),
        "candidates": per_candidate_payloads,
        "summary": {
            "total_candidates": len(per_candidate_payloads),
            "total_extractions": len(extractions),
            "matched_extractions": matched_extractions,
            "saved": saved_count,
        },
        "safety_notes": _safety_notes_dict(),
    }

    if as_json:
        print(json.dumps(combined, ensure_ascii=False, indent=2))
    else:
        if not per_candidate_payloads:
            print("=== Evidence Candidate ===")
            print(
                f"(no (extraction, claim) pair met min_score="
                f"{min_score})"
            )
        else:
            for i, p in enumerate(per_candidate_payloads):
                _print_candidate_human(p)
                if p.get("mode") == "dry_run":
                    print("dry_run: True (no DB write)")
                else:
                    if p.get("saved_row_id") is not None:
                        print(f"saved_row_id: {p['saved_row_id']}")
                    elif p.get("save_error"):
                        print(f"save_error: {p['save_error']}")
                    else:
                        print("saved_row_id: (not persisted)")
                if i < len(per_candidate_payloads) - 1:
                    print("")
                    print("---")
                    print("")
        print("")
        print(
            f"Summary: candidates={combined['summary']['total_candidates']} "
            f"extractions={combined['summary']['total_extractions']} "
            f"matched_extractions={combined['summary']['matched_extractions']} "
            f"saved={combined['summary']['saved']}"
        )
        _print_safety_footer()

    if not per_candidate_payloads:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_extractions and args.list_candidates:
        print(
            "[link] --list-extractions and --list-candidates are mutually "
            "exclusive.",
            file=sys.stderr,
        )
        return 2

    if args.list_extractions:
        return _run_list_extractions(
            source_id=args.source_id,
            limit=args.limit,
            as_json=bool(args.json),
        )

    if args.list_candidates:
        return _run_list_candidates(
            analysis_id=args.analysis_id,
            limit=args.limit,
            as_json=bool(args.json),
        )

    if not args.analysis_id:
        print(
            "[link] --analysis-id is required (or pass --list-extractions "
            "/ --list-candidates to inspect).",
            file=sys.stderr,
        )
        return 2

    if args.save and args.dry_run:
        print(
            "[link] --save and --dry-run are mutually exclusive.",
            file=sys.stderr,
        )
        return 2

    return _run_link_mode(
        analysis_id=str(args.analysis_id),
        source_id=args.source_id,
        min_score=float(args.min_score),
        limit=args.limit,
        dry_run=bool(args.dry_run),
        save=bool(args.save),
        as_json=bool(args.json),
    )


if __name__ == "__main__":
    sys.exit(main())
