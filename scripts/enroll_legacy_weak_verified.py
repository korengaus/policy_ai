"""Phase 2 M11.1: operator CLI for enrolling legacy weak-verified
``analysis_results`` rows into the existing ``review_tasks`` queue.

Reads ``verdict_label_attributions`` rows where
``is_weak_evidence_verified=1`` (produced by the M11.0b diagnostic) and,
when explicitly authorized via ``--enroll`` + ``--yes`` (or an
interactive ``YES`` confirmation), creates one ``review_tasks`` entry
per row with status ``pending_review``. The script is the only
authorized writer of ``legacy_weak_verified_m11_0c`` enrollments.

Hard contract:
    * Never auto-invoked. ``main.py`` / ``api_server.py`` /
      ``scheduler.py`` do not import this script.
    * NEVER modifies ``analysis_results``.
    * NEVER auto-approves, auto-publishes, or auto-finalizes any
      review_task. Every enrolled task is created with
      ``status=pending_review`` and ``human_review_required=True``.
    * ``--dry-run`` makes zero database writes.
    * ``--enroll`` writes ONLY to ``review_tasks`` (via
      ``review_workflow``-equivalent helpers); the input table
      ``verdict_label_attributions`` is never mutated.
    * Idempotent: re-running with the same data does not create
      duplicate review_tasks (UNIQUE idempotency_key).
    * No HTTP. No browser. No OpenAI. No Anthropic. No embeddings.

Usage::

    python scripts/enroll_legacy_weak_verified.py --list
    python scripts/enroll_legacy_weak_verified.py --check-status
    python scripts/enroll_legacy_weak_verified.py --dry-run
    python scripts/enroll_legacy_weak_verified.py --enroll --yes
    python scripts/enroll_legacy_weak_verified.py --summary
    python scripts/enroll_legacy_weak_verified.py --help

Exit codes:
    0 — success (list / dry-run / summary / check-status; or --enroll
        completed with at least one new or pre-existing enrollment)
    1 — confirmation refused, DB error, --enroll requested without
        TTY and no --yes, no candidates found when --enroll explicitly
        requested with --yes
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

import database  # noqa: E402
import legacy_review_enrollment as enrollment  # noqa: E402


CLI_VERSION = "1.0"

CONFIRM_TOKEN = "YES"

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_TRUTH = (
    "truth_claim=False — enrollment does not change any verdict."
)
SAFETY_NOTE_REVIEW = (
    "operator_review_required=True — every row enters the queue as "
    "pending human review."
)
SAFETY_NOTE_NO_RESULT_MUT = (
    "analysis_results.verdict_label is NOT modified."
)
SAFETY_NOTE_NO_AUTO = (
    "No auto-publication. No auto-approval. No auto-correction."
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enroll legacy weak-verified analysis_results rows "
            "(M11.0b/c diagnostic output) into the review_tasks queue "
            "for operator-driven correction. Read-mostly; --enroll is "
            "the only writing mode and requires --yes or interactive "
            "YES confirmation. Never modifies analysis_results, never "
            "auto-finalizes any review_task, never calls the live "
            "pipeline."
        ),
        epilog=(
            "Exit codes: 0=success or idempotent; 1=confirmation "
            "refused, DB error, or --enroll without TTY/--yes; "
            "2=CLI usage error."
        ),
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_mode",
        help="List candidate rows. Read-only.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Compute enrollments but do NOT write to review_tasks. "
            "Shows which rows would be newly enrolled vs already "
            "enrolled."
        ),
    )
    parser.add_argument(
        "--enroll", action="store_true",
        help=(
            "Actually write review_tasks. Required to perform any "
            "write. Without --yes, an interactive YES confirmation "
            "is requested; in non-TTY contexts the script refuses "
            "with exit 1 unless --yes is also passed."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Skip the interactive YES confirmation when --enroll is "
            "used. Intended for scripted operator use only."
        ),
    )
    parser.add_argument(
        "--summary", action="store_true",
        help=(
            "Print aggregated enrollment statistics across candidate "
            "rows. Read-only."
        ),
    )
    parser.add_argument(
        "--check-status", action="store_true", dest="check_status",
        help=(
            "For each candidate, report whether a matching review_task "
            "already exists. Read-only."
        ),
    )
    parser.add_argument(
        "--db-path", default=None,
        help=(
            "Path to the SQLite DB. Defaults to the module's DB_PATH "
            "(policy_ai.db in the repo root)."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    return parser


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _safety_notes_dict() -> Dict[str, str]:
    return {
        "truth": SAFETY_NOTE_TRUTH,
        "review": SAFETY_NOTE_REVIEW,
        "no_result_mutation": SAFETY_NOTE_NO_RESULT_MUT,
        "no_auto": SAFETY_NOTE_NO_AUTO,
    }


def _print_safety_footer(*, enrolled: bool = False) -> None:
    print("")
    print(f"[Safety] {SAFETY_NOTE_TRUTH}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")
    print(f"[Safety] {SAFETY_NOTE_NO_RESULT_MUT}")
    if enrolled:
        print(
            "[Safety] Review the enrolled tasks via the existing "
            "reviewer/admin UI."
        )
    else:
        print(f"[Safety] {SAFETY_NOTE_NO_AUTO}")


def _coerce_signals_for_display(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(s) for s in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return [value]
        if isinstance(decoded, list):
            return [str(s) for s in decoded]
        return [str(decoded)]
    return [str(value)]


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _run_list_mode(
    *, db_path: Optional[str], as_json: bool,
) -> int:
    candidates = enrollment.find_legacy_weak_verified_rows(db_path=db_path)
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "list",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "candidates": [
            {
                "id": c.get("id"),
                "analysis_id": c.get("analysis_id"),
                "stored_verdict_label": c.get("stored_verdict_label"),
                "stored_policy_confidence_score": c.get(
                    "stored_policy_confidence_score"
                ),
                "stored_verification_strength": c.get(
                    "stored_verification_strength"
                ),
                "weak_evidence_signals": _coerce_signals_for_display(
                    c.get("weak_evidence_signals")
                ),
            }
            for c in candidates
        ],
        "summary": {"total": len(candidates)},
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== Legacy Weak-Verified Candidates ===")
    print("")
    if not candidates:
        print("(no candidates: no verdict_label_attributions row has "
              "is_weak_evidence_verified=1)")
    else:
        header = (
            f"{'analysis_id':<12} | {'score':<5} | {'strength':<8} "
            f"| signals"
        )
        print(header)
        print("-" * len(header))
        for c in candidates:
            sig = ",".join(
                _coerce_signals_for_display(c.get("weak_evidence_signals"))
            )
            score = c.get("stored_policy_confidence_score")
            score_str = str(score) if score is not None else ""
            print(
                f"{str(c.get('analysis_id') or ''):<12} | "
                f"{score_str:<5} | "
                f"{str(c.get('stored_verification_strength') or ''):<8} | "
                f"{sig}"
            )
    print("")
    print(f"Total candidates: {len(candidates)}")
    _print_safety_footer(enrolled=False)
    return 0


def _run_dry_run_mode(
    *, db_path: Optional[str], as_json: bool,
) -> int:
    candidates = enrollment.find_legacy_weak_verified_rows(db_path=db_path)
    records = [
        enrollment.enroll_legacy_row(c, db_path=db_path, dry_run=True)
        for c in candidates
    ]
    summary = enrollment.compute_enrollment_summary(records)
    already = sum(1 for r in records if r.already_enrolled)
    would_enroll = sum(
        1 for r in records
        if not r.already_enrolled and not r.error
    )
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "dry_run",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "candidates": [enrollment.enrollment_to_dict(r) for r in records],
        "summary": {
            **summary,
            "would_enroll_now": would_enroll,
        },
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== Legacy Weak-Verified Candidates (dry-run) ===")
    print("")
    if not records:
        print("(no candidates)")
    else:
        header = (
            f"{'analysis_id':<12} | {'score':<5} | {'strength':<8} "
            f"| status      | signals"
        )
        print(header)
        print("-" * len(header))
        for r in records:
            if r.error:
                status = "error"
            elif r.already_enrolled:
                status = "enrolled"
            else:
                status = "would_enroll"
            sig = ",".join(r.weak_evidence_signals or [])
            score = (
                str(r.stored_policy_confidence_score)
                if r.stored_policy_confidence_score is not None else ""
            )
            print(
                f"{r.analysis_id:<12} | {score:<5} | "
                f"{str(r.stored_verification_strength or ''):<8} | "
                f"{status:<11} | {sig}"
            )
    print("")
    print(f"Total candidates: {len(records)}")
    print(f"Already enrolled: {already}")
    print(f"Would enroll now: {would_enroll}")
    _print_safety_footer(enrolled=False)
    return 0


def _run_check_status_mode(
    *, db_path: Optional[str], as_json: bool,
) -> int:
    candidates = enrollment.find_legacy_weak_verified_rows(db_path=db_path)
    rows = []
    enrolled_count = 0
    for c in candidates:
        analysis_id = str(c.get("analysis_id") or "")
        is_enrolled = enrollment.is_already_enrolled(
            analysis_id, db_path=db_path,
        )
        if is_enrolled:
            enrolled_count += 1
        rows.append({
            "analysis_id": analysis_id,
            "stored_verdict_label": c.get("stored_verdict_label"),
            "stored_policy_confidence_score": c.get(
                "stored_policy_confidence_score"
            ),
            "stored_verification_strength": c.get(
                "stored_verification_strength"
            ),
            "already_enrolled": is_enrolled,
        })
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "check_status",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "candidates": rows,
        "summary": {
            "total": len(rows),
            "already_enrolled": enrolled_count,
            "not_yet_enrolled": len(rows) - enrolled_count,
        },
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== Legacy Weak-Verified Enrollment Status ===")
    print("")
    if not rows:
        print("(no candidates)")
    else:
        header = f"{'analysis_id':<12} | already_enrolled | label"
        print(header)
        print("-" * len(header))
        for r in rows:
            print(
                f"{r['analysis_id']:<12} | "
                f"{str(r['already_enrolled']):<16} | "
                f"{r.get('stored_verdict_label') or ''}"
            )
    print("")
    print(f"Total candidates: {len(rows)}")
    print(f"Already enrolled: {enrolled_count}")
    print(f"Not yet enrolled: {len(rows) - enrolled_count}")
    _print_safety_footer(enrolled=False)
    return 0


def _run_summary_mode(
    *, db_path: Optional[str], as_json: bool,
) -> int:
    candidates = enrollment.find_legacy_weak_verified_rows(db_path=db_path)
    records = [
        enrollment.enroll_legacy_row(c, db_path=db_path, dry_run=True)
        for c in candidates
    ]
    summary = enrollment.compute_enrollment_summary(records)
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "summary",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "summary": summary,
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("=== Legacy Weak-Verified Enrollment Summary ===")
    print("")
    print(f"Total candidates:        {summary['total']}")
    print(f"Enrolled now:            {summary['enrolled_now']}")
    print(f"Already enrolled:        {summary['already_enrolled']}")
    print(f"Dry-run skipped:         {summary['dry_run_skipped']}")
    print(f"Errors:                  {summary['errors']}")
    print("")
    print("Weak-evidence signal histogram:")
    hist = summary["weak_evidence_signal_histogram"]
    if not hist:
        print("  (none)")
    else:
        for signal in sorted(hist.keys()):
            print(f"  {signal:<48} {hist[signal]:>4}")
    print("")
    print("Stored verdict_label histogram:")
    for label in sorted(summary["stored_verdict_label_histogram"].keys()):
        count = summary["stored_verdict_label_histogram"][label]
        print(f"  {label:<48} {count:>4}")
    print("")
    print("Stored verification_strength histogram:")
    for s in sorted(summary["stored_verification_strength_histogram"].keys()):
        count = summary["stored_verification_strength_histogram"][s]
        print(f"  {s:<48} {count:>4}")
    _print_safety_footer(enrolled=False)
    return 0


def _read_confirmation(prompt: str) -> str:
    """Indirection so tests can monkey-patch ``input``."""
    try:
        return (input(prompt) or "").strip()
    except EOFError:
        return ""


def _run_enroll_mode(
    *, db_path: Optional[str], as_json: bool, auto_yes: bool,
) -> int:
    candidates = enrollment.find_legacy_weak_verified_rows(db_path=db_path)
    if not candidates:
        msg = (
            "[enroll] no legacy weak-verified candidates found "
            "(verdict_label_attributions has no rows with "
            "is_weak_evidence_verified=1). Nothing to enroll."
        )
        print(msg, file=sys.stderr)
        return 1

    # Pre-flight dry-run pass so the operator (and the JSON consumer)
    # can see exactly what would change.
    preflight_records = [
        enrollment.enroll_legacy_row(c, db_path=db_path, dry_run=True)
        for c in candidates
    ]
    new_count = sum(
        1 for r in preflight_records
        if not r.already_enrolled and not r.error
    )
    already_count = sum(1 for r in preflight_records if r.already_enrolled)

    # Confirmation gating. --yes bypasses; otherwise we MUST be on a TTY
    # to prompt safely (subprocess / CI calls without --yes must refuse).
    if not auto_yes:
        if not sys.stdin.isatty():
            print(
                "[enroll] --enroll requires --yes when stdin is not a "
                "TTY (e.g., running under subprocess / CI). Refusing.",
                file=sys.stderr,
            )
            return 1
        # Operator preview.
        print(
            f"This will create {new_count} review_tasks with "
            f"reason='{enrollment.ENROLLMENT_REASON}'."
        )
        print(
            "Existing analysis_results.verdict_label values will NOT "
            "be modified."
        )
        print(
            f"({already_count} candidate(s) already have a matching "
            "review_task and will be skipped.)"
        )
        typed = _read_confirmation(f"Type {CONFIRM_TOKEN} to proceed: ")
        if typed != CONFIRM_TOKEN:
            print(
                f"[enroll] confirmation aborted "
                f"(expected exact {CONFIRM_TOKEN!r}, got {typed!r}).",
                file=sys.stderr,
            )
            return 1

    # --- Actual write pass ---
    records = [
        enrollment.enroll_legacy_row(c, db_path=db_path, dry_run=False)
        for c in candidates
    ]
    summary = enrollment.compute_enrollment_summary(records)
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "enroll",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "records": [enrollment.enrollment_to_dict(r) for r in records],
        "summary": summary,
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=== Legacy Weak-Verified Enrollment — WRITING ===")
        print("")
        print(
            f"Enrolling {len(records)} candidates into review_tasks "
            f"with reason='{enrollment.ENROLLMENT_REASON}'..."
        )
        for r in records:
            if r.error:
                tag = f"error: {r.error}"
            elif r.wrote_to_db:
                tag = f"review_task_id={r.review_task_id} (newly enrolled)"
            elif r.already_enrolled:
                tag = (
                    f"review_task_id={r.review_task_id} "
                    "(already enrolled; skipped)"
                )
            else:
                tag = "(no write, no idempotent skip — unexpected)"
            print(f"  analysis_id={r.analysis_id} → {tag}")
        print("")
        print(
            f"Result: enrolled={summary['enrolled_now']}, "
            f"already_enrolled={summary['already_enrolled']}, "
            f"errors={summary['errors']}"
        )
        _print_safety_footer(enrolled=True)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _validate_args(args) -> Optional[str]:
    mode_flags = [
        bool(args.list_mode),
        bool(args.dry_run),
        bool(args.enroll),
        bool(args.summary),
        bool(args.check_status),
    ]
    n = sum(1 for f in mode_flags if f)
    if n > 1:
        return (
            "only one of --list, --dry-run, --enroll, --summary, "
            "--check-status may be set at once."
        )
    if n == 0:
        return (
            "one of --list, --dry-run, --enroll, --summary, "
            "--check-status is required."
        )
    if args.yes and not args.enroll:
        return "--yes has no effect without --enroll."
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    error = _validate_args(args)
    if error:
        print(f"[enroll] {error}", file=sys.stderr)
        return 2

    if args.list_mode:
        return _run_list_mode(
            db_path=args.db_path, as_json=bool(args.json),
        )
    if args.dry_run:
        return _run_dry_run_mode(
            db_path=args.db_path, as_json=bool(args.json),
        )
    if args.check_status:
        return _run_check_status_mode(
            db_path=args.db_path, as_json=bool(args.json),
        )
    if args.summary:
        return _run_summary_mode(
            db_path=args.db_path, as_json=bool(args.json),
        )
    if args.enroll:
        return _run_enroll_mode(
            db_path=args.db_path, as_json=bool(args.json),
            auto_yes=bool(args.yes),
        )
    return 2  # unreachable thanks to _validate_args


if __name__ == "__main__":
    sys.exit(main())
