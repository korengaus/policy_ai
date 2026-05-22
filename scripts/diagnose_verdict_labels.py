"""Phase 2 M11.0b: read-only diagnostic CLI for
``verification_card._verdict_label``.

Reads stored ``analysis_results`` rows, attributes each stored
``verdict_label`` to the documented branch in
``verdict_label_diagnostic.VERDICT_LABEL_BRANCHES`` that most likely
produced it, and flags rows whose stored ``draft_verified`` label was
produced from weak-evidence inputs. The CLI does **not** modify
``verification_card.py``, ``_verdict_label``, or any other verdict
logic. The live pipeline is untouched.

Hard contract:
    * Never auto-invoked. ``main.py`` / ``api_server.py`` /
      ``scheduler.py`` do not import this script.
    * Always prints the three safety notes
      (truth_claim, operator_review_required, no-logic-changes) in
      every mode.
    * ``--dry-run`` makes zero database writes.
    * ``--save`` writes only to ``verdict_label_attributions`` —
      every other table is read-only here.
    * No HTTP. No browser. No OpenAI. No Anthropic. No embeddings.

Usage::

    python scripts/diagnose_verdict_labels.py --from-sqlite --limit 100 --save
    python scripts/diagnose_verdict_labels.py --analysis-id 105
    python scripts/diagnose_verdict_labels.py --summary
    python scripts/diagnose_verdict_labels.py --list-weak-verified --limit 20
    python scripts/diagnose_verdict_labels.py --branch-table
    python scripts/diagnose_verdict_labels.py --help

Exit codes:
    0 — diagnostics computed (or summary / branch-table printed)
    1 — no data found, DB error
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
import verdict_label_diagnostic as diagnostic  # noqa: E402


CLI_VERSION = "1.0"

DEFAULT_LIMIT = 100
EVIDENCE_SUMMARY_PREVIEW_CHARS = 240
CLAIM_TEXT_PREVIEW_CHARS = 120

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_TRUTH = (
    "truth_claim=False — diagnostic is read-only."
)
SAFETY_NOTE_REVIEW = (
    "operator_review_required=True — weak-evidence verified cases "
    "require human investigation."
)
SAFETY_NOTE_NO_LOGIC = (
    "No verdict logic was modified. _verdict_label is untouched."
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only diagnostic for verification_card._verdict_label. "
            "Attributes each stored analysis_results.verdict_label to "
            "the documented branch most likely to have produced it, "
            "and flags weak-evidence 'draft_verified' rows. Never "
            "modifies verdict logic; never connects to the live "
            "pipeline. truth_claim is always False; "
            "operator_review_required is always True."
        ),
        epilog=(
            "Exit codes: 0=diagnostics computed or summary printed; "
            "1=no data or DB error; 2=CLI usage error."
        ),
    )
    parser.add_argument(
        "--from-sqlite", action="store_true",
        help=(
            "Process all analysis_results rows (limited by --limit, "
            "newest first)."
        ),
    )
    parser.add_argument(
        "--analysis-id", default=None,
        help="Process a single specific analysis_results row by id.",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help=(
            "Print an aggregated branch-attribution summary across "
            "stored attribution rows."
        ),
    )
    parser.add_argument(
        "--list-weak-verified", action="store_true",
        dest="list_weak_verified",
        help=(
            "List stored attribution rows flagged "
            "is_weak_evidence_verified=True."
        ),
    )
    parser.add_argument(
        "--branch-table", action="store_true",
        dest="branch_table",
        help=(
            "Print the documented branch table "
            "(VERDICT_LABEL_BRANCHES) and exit. No DB needed."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=(
            f"Max number of records to process (default: "
            f"{DEFAULT_LIMIT}). Clamped to a safe upper bound."
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
        "--dry-run", action="store_true",
        help="Compute and print attributions but do not save to the DB.",
    )
    parser.add_argument(
        "--save", action="store_true",
        help=(
            "Persist computed attributions to "
            "verdict_label_attributions. Without this flag the script "
            "stays read-only."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    return parser


# ---------------------------------------------------------------------------
# DB context helpers
# ---------------------------------------------------------------------------


def _with_db_path(db_path: Optional[str]):
    if db_path is None:
        return None
    original = database.DB_PATH
    database.DB_PATH = Path(db_path)
    return original


def _restore_db_path(original):
    if original is None:
        return
    database.DB_PATH = original


def _safety_notes_dict() -> Dict[str, str]:
    return {
        "truth": SAFETY_NOTE_TRUTH,
        "review": SAFETY_NOTE_REVIEW,
        "no_logic": SAFETY_NOTE_NO_LOGIC,
    }


def _print_safety_footer() -> None:
    print("")
    print(f"[Safety] {SAFETY_NOTE_TRUTH}")
    print(f"[Safety] {SAFETY_NOTE_REVIEW}")
    print(f"[Safety] {SAFETY_NOTE_NO_LOGIC}")


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------


def _attribution_payload(
    attribution, *, saved_row_id: Optional[int],
    save_error: Optional[str], dry_run: bool,
) -> Dict[str, Any]:
    d = diagnostic.attribution_to_dict(attribution)
    payload = dict(d)
    payload["mode"] = "dry_run" if dry_run else "save"
    payload["saved_row_id"] = saved_row_id
    if save_error:
        payload["save_error"] = save_error
    return payload


def _truncate(value: Optional[str], n: int) -> str:
    if not value:
        return ""
    text = str(value)
    if len(text) <= n:
        return text
    return text[:n] + "…"


def _print_attribution_human(attribution) -> None:
    print("=== Verdict Label Attribution ===")
    print(f"analysis_id: {attribution.analysis_id}")
    print(f"stored_verdict_label: {attribution.stored_verdict_label}")
    print(
        f"stored_policy_alert_level: "
        f"{attribution.stored_policy_alert_level}"
    )
    print(
        f"stored_policy_confidence_score: "
        f"{attribution.stored_policy_confidence_score}"
    )
    print(
        f"stored_verification_strength: "
        f"{attribution.stored_verification_strength}"
    )
    print(
        f"stored_evidence_summary: "
        f"{_truncate(attribution.stored_evidence_summary, EVIDENCE_SUMMARY_PREVIEW_CHARS)}"
    )
    print("")
    print("Reconstructed inputs:")
    print(f"  claim_count: {attribution.reconstructed_claim_count}")
    print(
        f"  direct_support_count: "
        f"{attribution.reconstructed_direct_support_count}"
    )
    print(
        f"  official_reference_count: "
        f"{attribution.reconstructed_official_reference_count}"
    )
    print(
        f"  insufficient_count: "
        f"{attribution.reconstructed_insufficient_count}"
    )
    print(
        f"  confirmed_count: "
        f"{attribution.reconstructed_confirmed_count}"
    )
    print(
        f"  possible_count: "
        f"{attribution.reconstructed_possible_count}"
    )
    print(
        f"  high_framing_count: "
        f"{attribution.reconstructed_high_framing_count}"
    )
    print(
        f"  official_confirmation_count: "
        f"{attribution.reconstructed_official_confirmation_count}"
    )
    print(
        f"  insufficient_claim_count: "
        f"{attribution.reconstructed_insufficient_claim_count}"
    )
    print(f"  has_conflict: {attribution.reconstructed_has_conflict}")
    print(
        f"  comparison_status: "
        f"{attribution.reconstructed_comparison_status}"
    )
    print(
        f"  verification_level: "
        f"{attribution.reconstructed_verification_level}"
    )
    print(
        f"  official_sources_count: "
        f"{attribution.reconstructed_official_sources_count}"
    )
    print("")
    print("Attribution:")
    print(f"  attributed_branch_id: {attribution.attributed_branch_id}")
    print(f"  attribution_confidence: {attribution.attribution_confidence}")
    print(f"  attribution_reason: {attribution.attribution_reason}")
    risk = ""
    if attribution.attributed_branch_id:
        for b in diagnostic.VERDICT_LABEL_BRANCHES:
            if b["branch_id"] == attribution.attributed_branch_id:
                risk = b["risk_classification"]
                break
    print(f"  risk_classification: {risk}")
    print("")
    print("Weak evidence signals:")
    if not attribution.weak_evidence_signals:
        print("  (none)")
    else:
        for signal in attribution.weak_evidence_signals:
            print(f"  - {signal}")
    print("")
    print(
        f"is_weak_evidence_verified: "
        f"{attribution.is_weak_evidence_verified}"
    )


def _print_branch_table_human() -> None:
    print("=== _verdict_label branch catalogue (VERDICT_LABEL_BRANCHES) ===")
    print("")
    header = (
        f"{'branch_id':<48} | {'lines':<10} | {'label':<32} "
        f"| {'risk'}"
    )
    print(header)
    print("-" * len(header))
    for b in diagnostic.VERDICT_LABEL_BRANCHES:
        print(
            f"{b['branch_id']:<48} | {b['line_range']:<10} | "
            f"{b['output_label']:<32} | {b['risk_classification']}"
        )
    print("")
    print("Triggers (verbatim from the module docstring):")
    for b in diagnostic.VERDICT_LABEL_BRANCHES:
        print(f"  [{b['branch_id']}]")
        print(f"      {b['trigger_summary']}")


def _print_summary_human(summary: Dict[str, Any]) -> None:
    print("=== Verdict Label Diagnostic Summary ===")
    print("")
    print(f"Total rows attributed:       {summary['total']}")
    print(
        f"Unknown attribution:           "
        f"{summary['unknown_attribution_count']}"
    )
    print("")
    print("Per-branch attribution counts:")
    per_branch = summary["per_branch_counts"]
    if not per_branch:
        print("  (none)")
    else:
        lookup = {b["branch_id"]: b for b in diagnostic.VERDICT_LABEL_BRANCHES}
        for branch_id in sorted(per_branch.keys()):
            count = per_branch[branch_id]
            info = lookup.get(branch_id)
            tag = (
                f"   ← {info['risk_classification']}" if info else ""
            )
            print(f"  {branch_id:<48} {count:>4}{tag}")
    print("")
    print("Per-output-label counts:")
    for label in sorted(summary["per_output_label_counts"].keys()):
        count = summary["per_output_label_counts"][label]
        print(f"  {label:<48} {count:>4}")
    print("")
    print("Per-risk-classification counts:")
    risk_counts = summary["per_risk_classification_counts"]
    for risk in sorted(risk_counts.keys()):
        tag = (
            "   ← potential bug surface"
            if risk == "verified_without_strict_checks"
            else ""
        )
        print(f"  {risk:<48} {risk_counts[risk]:>4}{tag}")
    print("")
    print(
        f"Weak-evidence verified rows:              "
        f"{summary['weak_evidence_verified_count']}  "
        f"({summary['weak_evidence_verified_percent']}%)"
    )
    print("")
    print("Weak evidence signal histogram:")
    hist = summary["weak_evidence_signal_histogram"]
    if not hist:
        print("  (none)")
    else:
        for signal in sorted(hist.keys()):
            print(f"  {signal:<48} {hist[signal]:>4}")


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------


def _load_rows(
    *, db_path: Optional[str], limit: int,
    analysis_id: Optional[str],
) -> List[Dict[str, Any]]:
    original = _with_db_path(db_path)
    try:
        if analysis_id is not None:
            try:
                row = database.get_result_by_id(int(analysis_id))
            except (TypeError, ValueError):
                row = None
            return [row] if row else []
        rows = database.get_recent_results(limit=limit)
        return rows or []
    finally:
        _restore_db_path(original)


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------


def _run_attribute_mode(
    *, rows: List[Dict[str, Any]], dry_run: bool, save: bool,
    as_json: bool, db_path: Optional[str],
) -> int:
    if not rows:
        payload = {
            "cli_version": CLI_VERSION,
            "mode": "attribute",
            "db_path": (
                str(db_path) if db_path is not None
                else str(database.DB_PATH)
            ),
            "processed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
            "attributions": [],
            "summary": {"total": 0},
            "safety_notes": _safety_notes_dict(),
            "warning": "no analysis_results rows matched the filter",
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("=== Verdict Label Attribution ===")
            print("(no analysis_results rows matched the filter)")
            _print_safety_footer()
        return 1

    attributions = [diagnostic.attribute_branch_for_row(r) for r in rows]
    payloads: List[Dict[str, Any]] = []
    saved_count = 0
    for attribution in attributions:
        saved_row_id: Optional[int] = None
        save_error: Optional[str] = None
        if save and not dry_run:
            try:
                d = diagnostic.attribution_to_dict(attribution)
                saved_row_id = database.save_verdict_label_attribution(
                    d, db_path=db_path,
                )
                saved_count += 1
            except Exception as error:
                save_error = f"{type(error).__name__}: {error}"
        payloads.append(_attribution_payload(
            attribution,
            saved_row_id=saved_row_id,
            save_error=save_error,
            dry_run=dry_run or not save,
        ))

    summary = diagnostic.compute_branch_summary(attributions)
    combined = {
        "cli_version": CLI_VERSION,
        "mode": "attribute",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "dry_run": bool(dry_run),
        "save": bool(save),
        "attributions": payloads,
        "summary": summary,
        "saved_count": saved_count,
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(combined, ensure_ascii=False, indent=2))
    else:
        for i, attribution in enumerate(attributions):
            _print_attribution_human(attribution)
            payload = payloads[i]
            if payload.get("mode") == "dry_run":
                print("dry_run: True (no DB write)")
            else:
                if payload.get("saved_row_id") is not None:
                    print(f"saved_row_id: {payload['saved_row_id']}")
                elif payload.get("save_error"):
                    print(f"save_error: {payload['save_error']}")
            if i < len(attributions) - 1:
                print("")
                print("---")
                print("")
        print("")
        print(
            f"Run summary: total={summary['total']} "
            f"weak_verified={summary['weak_evidence_verified_count']} "
            f"unknown={summary['unknown_attribution_count']} "
            f"saved={saved_count}"
        )
        _print_safety_footer()
    return 0


def _run_summary_mode(
    *, db_path: Optional[str], limit: int, as_json: bool,
) -> int:
    try:
        rows = database.get_verdict_label_attributions(
            db_path=db_path, limit=limit,
        )
    except Exception as error:
        print(
            f"[diagnose-verdict-labels] failed to load attributions: "
            f"{error}",
            file=sys.stderr,
        )
        return 1
    summary = diagnostic.compute_branch_summary(rows)
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
    else:
        _print_summary_human(summary)
        _print_safety_footer()
    return 0


def _run_list_weak_verified_mode(
    *, db_path: Optional[str], limit: int, as_json: bool,
) -> int:
    try:
        rows = database.get_verdict_label_attributions(
            only_weak_evidence_verified=True,
            db_path=db_path, limit=limit,
        )
    except Exception as error:
        print(
            f"[diagnose-verdict-labels] failed to load attributions: "
            f"{error}",
            file=sys.stderr,
        )
        return 1
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "list_weak_verified",
        "db_path": (
            str(db_path) if db_path is not None else str(database.DB_PATH)
        ),
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "attributions": rows,
        "summary": {"total": len(rows)},
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=== Weak-evidence verified attributions ===")
        print("")
        if not rows:
            print("(no weak-evidence verified rows found)")
        else:
            header = (
                f"{'id':<5} | {'analysis_id':<16} | {'branch':<40} "
                f"| {'signals'}"
            )
            print(header)
            print("-" * len(header))
            for r in rows:
                signals = r.get("weak_evidence_signals") or "[]"
                if isinstance(signals, str):
                    try:
                        signals = json.loads(signals)
                    except (TypeError, ValueError):
                        signals = [signals]
                print(
                    f"{str(r.get('id') or ''):<5} | "
                    f"{str(r.get('analysis_id') or '')[:16]:<16} | "
                    f"{str(r.get('attributed_branch_id') or '')[:40]:<40} "
                    f"| {','.join(map(str, signals or []))}"
                )
        print("")
        print(f"Total: {payload['summary']['total']}")
        _print_safety_footer()
    return 0


def _run_branch_table_mode(*, as_json: bool) -> int:
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "branch_table",
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "branches": diagnostic.VERDICT_LABEL_BRANCHES,
        "risk_classifications": list(diagnostic.RISK_CLASSIFICATIONS),
        "weak_evidence_summary_phrases": list(
            diagnostic.WEAK_EVIDENCE_SUMMARY_PHRASES
        ),
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_branch_table_human()
        _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _validate_args(args) -> Optional[str]:
    mode_flags = [
        bool(args.from_sqlite),
        bool(args.analysis_id),
        bool(args.summary),
        bool(args.list_weak_verified),
        bool(args.branch_table),
    ]
    n_set = sum(1 for flag in mode_flags if flag)
    if n_set > 1:
        return (
            "only one of --from-sqlite, --analysis-id, --summary, "
            "--list-weak-verified, --branch-table may be set at once."
        )
    if n_set == 0:
        return (
            "one of --from-sqlite, --analysis-id, --summary, "
            "--list-weak-verified, --branch-table is required."
        )
    if args.save and args.dry_run:
        return "--save and --dry-run are mutually exclusive."
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    error_msg = _validate_args(args)
    if error_msg:
        print(f"[diagnose-verdict-labels] {error_msg}", file=sys.stderr)
        return 2

    if args.branch_table:
        return _run_branch_table_mode(as_json=bool(args.json))

    if args.summary:
        return _run_summary_mode(
            db_path=args.db_path, limit=args.limit,
            as_json=bool(args.json),
        )

    if args.list_weak_verified:
        return _run_list_weak_verified_mode(
            db_path=args.db_path, limit=args.limit,
            as_json=bool(args.json),
        )

    if args.analysis_id:
        rows = _load_rows(
            db_path=args.db_path, limit=args.limit,
            analysis_id=args.analysis_id,
        )
        return _run_attribute_mode(
            rows=rows, dry_run=bool(args.dry_run), save=bool(args.save),
            as_json=bool(args.json), db_path=args.db_path,
        )

    if args.from_sqlite:
        rows = _load_rows(
            db_path=args.db_path, limit=args.limit, analysis_id=None,
        )
        return _run_attribute_mode(
            rows=rows, dry_run=bool(args.dry_run), save=bool(args.save),
            as_json=bool(args.json), db_path=args.db_path,
        )

    return 2  # unreachable thanks to _validate_args


if __name__ == "__main__":
    sys.exit(main())
