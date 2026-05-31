"""Phase 2 M11.0a: read-only verdict-producer comparison CLI.

Re-runs the three current verdict producers (make_final_decision,
calibrate_final_decision, _verdict_label) against stored
``analysis_results`` rows or ``reports/policy_analysis_*.json`` files
and surfaces a disagreement matrix. The CLI does **not** modify any
of the three producers, the live pipeline, or any user-facing
output. It is purely measurement for the upcoming M11.0b
consolidation milestone.

Hard contract:
    * Never auto-invoked. ``main.py`` / ``api_server.py`` /
      ``scheduler.py`` do not import this script.
    * Always prints the three safety notes
      (truth_claim, operator_review_required, no-logic-changes) in
      every mode.
    * ``--dry-run`` makes zero database writes.
    * ``--save`` writes only to ``verdict_producer_comparisons`` —
      every other table is read-only here.
    * No HTTP. No browser. No OpenAI. No Anthropic. No embeddings.

Usage::

    python scripts/compare_verdict_producers.py --from-sqlite --limit 50 --save
    python scripts/compare_verdict_producers.py --from-reports --limit 100 --save
    python scripts/compare_verdict_producers.py --analysis-id <id>
    python scripts/compare_verdict_producers.py --summary
    python scripts/compare_verdict_producers.py --list-disagreements --limit 20
    python scripts/compare_verdict_producers.py --help

Exit codes:
    0 — comparisons computed and processed (or summary printed)
    1 — no data found, DB error, or every requested source failed
    2 — CLI usage error (missing required flags, conflicting flags,
        unrecognized args)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import database  # noqa: E402
import verdict_producer_comparison as comparator  # noqa: E402


CLI_VERSION = "1.0"

DEFAULT_LIMIT = 50
DEFAULT_REPORTS_DIR = ROOT / "reports"
TOP_PATTERN_LIMIT = 10

# Safety notes the spec requires in every output mode.
SAFETY_NOTE_TRUTH = (
    "truth_claim=False — comparison is analysis only, does not change "
    "verdicts."
)
SAFETY_NOTE_REVIEW = (
    "operator_review_required=True — disagreement patterns require "
    "human investigation."
)
SAFETY_NOTE_NO_LOGIC = (
    "No verdict logic was modified. All three producers ran against "
    "stored inputs as-is."
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the three current verdict producers against stored "
            "analysis inputs. Never fetches, scrapes, or contacts any "
            "external service. truth_claim is always False; "
            "operator_review_required is always True. No verdict "
            "logic is modified."
        ),
        epilog=(
            "Exit codes: 0=comparisons computed or summary printed; "
            "1=no data, DB error, or all sources failed; "
            "2=CLI usage error."
        ),
    )
    parser.add_argument(
        "--from-sqlite", action="store_true",
        help="Read analyses from the analysis_results SQLite table.",
    )
    parser.add_argument(
        "--from-reports", action="store_true",
        help=(
            "Read analyses from reports/policy_analysis_*.json files "
            "(or the directory given by --reports-dir)."
        ),
    )
    parser.add_argument(
        "--reports-dir", default=None,
        help=(
            "Override the default reports directory "
            f"(default: {DEFAULT_REPORTS_DIR})."
        ),
    )
    parser.add_argument(
        "--analysis-id", default=None,
        help=(
            "Compare a single specific analysis by id (from "
            "analysis_results.id)."
        ),
    )
    parser.add_argument(
        "--summary", action="store_true",
        help=(
            "Print an aggregated disagreement summary across stored "
            "comparison rows."
        ),
    )
    parser.add_argument(
        "--list-disagreements", action="store_true",
        dest="list_disagreements",
        help=(
            "List comparison rows where at least one pair disagrees "
            "(all_three_agree=False)."
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
        "--dry-run", action="store_true",
        help="Compute and print comparisons but do not save to the DB.",
    )
    parser.add_argument(
        "--save", action="store_true",
        help=(
            "Persist computed comparisons to "
            "verdict_producer_comparisons. Without this flag the "
            "script stays read-only."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print results as JSON only (no human header/footer).",
    )
    return parser


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
# Input loaders
# ---------------------------------------------------------------------------


def _load_from_sqlite(
    *, limit: int, analysis_id: Optional[str],
) -> List[Dict[str, Any]]:
    if analysis_id is not None:
        try:
            row = database.get_result_by_id(int(analysis_id))
        except (TypeError, ValueError):
            row = None
        return [row] if row else []
    rows = database.get_recent_results(limit=limit)
    return rows or []


def _flatten_reports_dir(reports_dir: Path) -> List[Dict[str, Any]]:
    """Walk ``reports_dir`` for ``policy_analysis_*.json`` files,
    extract each ``news_results`` entry, and flatten them into a list
    of analysis-shaped dicts. Files that don't parse are skipped (a
    warning is logged to stderr)."""
    flat: List[Dict[str, Any]] = []
    for path in sorted(reports_dir.glob("policy_analysis_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            print(
                f"[compare-verdicts] skipping {path.name}: {error}",
                file=sys.stderr,
            )
            continue
        if not isinstance(payload, dict):
            continue
        news_results = payload.get("news_results") or []
        if not isinstance(news_results, list):
            continue
        for entry in news_results:
            if not isinstance(entry, dict):
                continue
            # Attach a stable analysis_id derived from the file + url.
            stamped = dict(entry)
            stamped.setdefault(
                "analysis_id",
                f"{path.stem}::{entry.get('original_url', '')}",
            )
            flat.append(stamped)
    return flat


def _load_from_reports(
    *, reports_dir: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    target = Path(reports_dir) if reports_dir else DEFAULT_REPORTS_DIR
    if not target.exists():
        print(
            f"[compare-verdicts] reports directory not found: {target}",
            file=sys.stderr,
        )
        return []
    flat = _flatten_reports_dir(target)
    if limit and limit > 0:
        flat = flat[-limit:]
    return flat


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------


def _comparison_payload(
    comparison, *, saved_row_id: Optional[int],
    save_error: Optional[str], dry_run: bool,
) -> Dict[str, Any]:
    d = comparator.comparison_to_dict(comparison)
    payload = {
        "analysis_id": d.get("analysis_id"),
        "source": d.get("source"),
        "input_hash": d.get("input_hash"),
        "producer1_label": d.get("producer1_label"),
        "producer1_score": d.get("producer1_score"),
        "producer1_extra": d.get("producer1_extra"),
        "producer2_label": d.get("producer2_label"),
        "producer2_alert_level": d.get("producer2_alert_level"),
        "producer2_score": d.get("producer2_score"),
        "producer2_extra": d.get("producer2_extra"),
        "producer3_label": d.get("producer3_label"),
        "producer3_extra": d.get("producer3_extra"),
        "all_three_agree": d.get("all_three_agree"),
        "p1_p2_agree": d.get("p1_p2_agree"),
        "p1_p3_agree": d.get("p1_p3_agree"),
        "p2_p3_agree": d.get("p2_p3_agree"),
        "disagreement_pattern": d.get("disagreement_pattern"),
        "most_conservative_label": d.get("most_conservative_label"),
        "comparison_timestamp": d.get("comparison_timestamp"),
        "notes": d.get("notes"),
        "truth_claim": False,
        "operator_review_required": True,
        "saved_row_id": saved_row_id,
        "mode": "dry_run" if dry_run else "save",
    }
    if save_error:
        payload["save_error"] = save_error
    return payload


def _print_comparison_human(payload: Dict[str, Any]) -> None:
    print("=== Verdict Producer Comparison ===")
    print(f"analysis_id: {payload.get('analysis_id')}")
    print(f"source: {payload.get('source')}")
    print("")
    p1_label = payload.get("producer1_label")
    p1_score = payload.get("producer1_score")
    p2_label = payload.get("producer2_label")
    p2_alert = payload.get("producer2_alert_level")
    p2_score = payload.get("producer2_score")
    p3_label = payload.get("producer3_label")
    print(
        f"Producer 1 (make_final_decision):       "
        f"{p1_label}\tscore={p1_score}"
    )
    print(
        f"Producer 2 (calibrate_final_decision):  "
        f"{p2_label}\talert_level={p2_alert}\tscore={p2_score}"
    )
    print(f"Producer 3 (verification_card label):   {p3_label}")
    print("")
    print("Agreement matrix:")
    print(f"  all_three_agree:  {payload.get('all_three_agree')}")
    print(
        f"  p1_p2_agree:      {payload.get('p1_p2_agree')}  "
        f"({p1_label} vs {p2_label})"
    )
    print(
        f"  p1_p3_agree:      {payload.get('p1_p3_agree')}  "
        f"({p1_label} vs {p3_label})"
    )
    print(
        f"  p2_p3_agree:      {payload.get('p2_p3_agree')}  "
        f"({p2_label} vs {p3_label})"
    )
    print("")
    print(f"Disagreement pattern: {payload.get('disagreement_pattern')}")
    print(f"Most conservative label: {payload.get('most_conservative_label')}")
    if payload.get("mode") == "dry_run":
        print("dry_run: True (no DB write)")
    else:
        if payload.get("saved_row_id") is not None:
            print(f"saved_row_id: {payload['saved_row_id']}")
        elif payload.get("save_error"):
            print(f"save_error: {payload['save_error']}")


def _print_summary_human(summary: Dict[str, Any]) -> None:
    print("=== Verdict Producer Disagreement Summary ===")
    print("")
    print(f"Total comparisons:           {summary['total']}")
    print(
        f"All three agree:             "
        f"{summary['all_three_agree_count']}  "
        f"({summary['all_three_agree_percent']}%)"
    )
    print(
        f"At least one disagreement:   "
        f"{summary['at_least_one_disagreement_count']}  "
        f"({summary['at_least_one_disagreement_percent']}%)"
    )
    print("")
    print("Pairwise disagreements:")
    pc = summary["pairwise_disagreement_counts"]
    pp = summary["pairwise_disagreement_percent"]
    print(f"  P1 vs P2:  {pc['p1_vs_p2']}  ({pp['p1_vs_p2']}%)")
    print(f"  P1 vs P3:  {pc['p1_vs_p3']}  ({pp['p1_vs_p3']}%)")
    print(f"  P2 vs P3:  {pc['p2_vs_p3']}  ({pp['p2_vs_p3']}%)")
    print("")
    print("Top disagreement patterns:")
    patterns = summary["disagreement_pattern_histogram"]
    top = sorted(
        patterns.items(), key=lambda kv: kv[1], reverse=True,
    )[:TOP_PATTERN_LIMIT]
    if not top:
        print("  (none)")
    else:
        total = max(1, summary["total"])
        for pattern, count in top:
            pct = round((count / total) * 100.0, 2)
            print(f"  {pattern:<48} {count:>4}  ({pct}%)")
    print("")
    print("Per-producer label distribution:")
    label_dist = summary["producer_label_distribution"]
    for slot, key in (
        ("Producer 1", "producer1"),
        ("Producer 2", "producer2"),
        ("Producer 3", "producer3"),
    ):
        counts = label_dist.get(key) or {}
        joined = " ".join(
            f"{label}={count}" for label, count in sorted(counts.items())
        ) or "(none)"
        print(f"  {slot}: {joined}")
    print("")
    print(
        f"Errored producer runs: "
        f"{summary['errored_producer_runs_count']}"
    )


# ---------------------------------------------------------------------------
# Comparison runner
# ---------------------------------------------------------------------------


def _run_compare(
    *, rows: List[Dict[str, Any]], source_label: str,
    dry_run: bool, save: bool, as_json: bool,
) -> int:
    if not rows:
        payload = {
            "cli_version": CLI_VERSION,
            "mode": "compare",
            "source": source_label,
            "store": "postgres",
            "processed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
            "comparisons": [],
            "summary": {
                "total": 0,
                "all_three_agree_count": 0,
                "all_three_agree_percent": 0.0,
                "at_least_one_disagreement_count": 0,
                "at_least_one_disagreement_percent": 0.0,
                "pairwise_disagreement_counts": {
                    "p1_vs_p2": 0, "p1_vs_p3": 0, "p2_vs_p3": 0,
                },
                "pairwise_disagreement_percent": {
                    "p1_vs_p2": 0.0, "p1_vs_p3": 0.0, "p2_vs_p3": 0.0,
                },
                "disagreement_pattern_histogram": {},
                "producer_label_distribution": {
                    "producer1": {}, "producer2": {}, "producer3": {},
                },
                "errored_producer_runs_count": 0,
            },
            "safety_notes": _safety_notes_dict(),
            "warning": (
                f"no analyses found for source={source_label!r}"
            ),
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("=== Verdict Producer Comparison ===")
            print(f"(no analyses found for source={source_label!r})")
            _print_safety_footer()
        return 1

    comparisons: List = []
    payloads: List[Dict[str, Any]] = []
    saved_count = 0
    for row in rows:
        comparison = comparator.compare_producers_for_analysis(
            row, source=source_label,
        )
        comparisons.append(comparison)
        saved_row_id: Optional[int] = None
        save_error: Optional[str] = None
        if save and not dry_run:
            try:
                comparison_dict = comparator.comparison_to_dict(comparison)
                saved_row_id = database.save_producer_comparison(
                    comparison_dict,
                )
                saved_count += 1
            except Exception as error:
                save_error = f"{type(error).__name__}: {error}"
        payloads.append(_comparison_payload(
            comparison,
            saved_row_id=saved_row_id,
            save_error=save_error,
            dry_run=dry_run or not save,
        ))

    aggregated = comparator.compute_disagreement_summary(comparisons)
    combined = {
        "cli_version": CLI_VERSION,
        "mode": "compare",
        "source": source_label,
        "store": "postgres",
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "dry_run": bool(dry_run),
        "save": bool(save),
        "comparisons": payloads,
        "summary": aggregated,
        "saved_count": saved_count,
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(combined, ensure_ascii=False, indent=2))
    else:
        for i, payload in enumerate(payloads):
            _print_comparison_human(payload)
            if i < len(payloads) - 1:
                print("")
                print("---")
                print("")
        print("")
        print(
            f"Run summary: total={aggregated['total']} "
            f"all_three_agree={aggregated['all_three_agree_count']} "
            f"disagreements={aggregated['at_least_one_disagreement_count']} "
            f"saved={saved_count}"
        )
        _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# Mode: --summary
# ---------------------------------------------------------------------------


def _run_summary(
    *, limit: int, as_json: bool,
) -> int:
    try:
        rows = database.get_producer_comparisons(limit=limit)
    except Exception as error:
        print(
            f"[compare-verdicts] failed to load comparisons: {error}",
            file=sys.stderr,
        )
        return 1
    aggregated = comparator.compute_disagreement_summary(rows)
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "summary",
        "store": "postgres",
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "summary": aggregated,
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_summary_human(aggregated)
        _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# Mode: --list-disagreements
# ---------------------------------------------------------------------------


def _run_list_disagreements(
    *, limit: int, as_json: bool,
) -> int:
    try:
        rows = database.get_producer_comparisons(
            only_disagreements=True, limit=limit,
        )
    except Exception as error:
        print(
            f"[compare-verdicts] failed to load comparisons: {error}",
            file=sys.stderr,
        )
        return 1
    payload = {
        "cli_version": CLI_VERSION,
        "mode": "list_disagreements",
        "store": "postgres",
        "processed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "comparisons": rows,
        "summary": {"total": len(rows)},
        "safety_notes": _safety_notes_dict(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=== Verdict Producer Disagreements (read-only listing) ===")
        print("")
        if not rows:
            print("(no disagreement rows in verdict_producer_comparisons)")
        else:
            header = (
                f"{'id':<5} | {'analysis_id':<24} | {'pattern':<48} "
                f"| {'most_conservative'}"
            )
            print(header)
            print("-" * len(header))
            for r in rows:
                print(
                    f"{str(r.get('id') or ''):<5} | "
                    f"{str(r.get('analysis_id') or '')[:24]:<24} | "
                    f"{str(r.get('disagreement_pattern') or '')[:48]:<48} "
                    f"| {r.get('most_conservative_label') or ''}"
                )
        print("")
        print(f"Total: {payload['summary']['total']}")
        _print_safety_footer()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _validate_args(args) -> Optional[str]:
    mode_flags = [
        bool(args.from_sqlite),
        bool(args.from_reports),
        bool(args.analysis_id),
        bool(args.summary),
        bool(args.list_disagreements),
    ]
    if sum(1 for flag in mode_flags if flag) > 1:
        return (
            "only one of --from-sqlite, --from-reports, --analysis-id, "
            "--summary, --list-disagreements may be set at once."
        )
    if sum(1 for flag in mode_flags if flag) == 0:
        return (
            "one of --from-sqlite, --from-reports, --analysis-id, "
            "--summary, --list-disagreements is required."
        )
    if args.save and args.dry_run:
        return "--save and --dry-run are mutually exclusive."
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    error_msg = _validate_args(args)
    if error_msg:
        print(f"[compare-verdicts] {error_msg}", file=sys.stderr)
        return 2

    if args.summary:
        return _run_summary(
            limit=args.limit,
            as_json=bool(args.json),
        )

    if args.list_disagreements:
        return _run_list_disagreements(
            limit=args.limit,
            as_json=bool(args.json),
        )

    if args.analysis_id:
        rows = _load_from_sqlite(
            limit=args.limit,
            analysis_id=args.analysis_id,
        )
        return _run_compare(
            rows=rows, source_label="sqlite",
            dry_run=bool(args.dry_run), save=bool(args.save),
            as_json=bool(args.json),
        )

    if args.from_sqlite:
        rows = _load_from_sqlite(
            limit=args.limit, analysis_id=None,
        )
        return _run_compare(
            rows=rows, source_label="sqlite",
            dry_run=bool(args.dry_run), save=bool(args.save),
            as_json=bool(args.json),
        )

    if args.from_reports:
        rows = _load_from_reports(
            reports_dir=args.reports_dir, limit=args.limit,
        )
        return _run_compare(
            rows=rows, source_label="reports_json",
            dry_run=bool(args.dry_run), save=bool(args.save),
            as_json=bool(args.json),
        )

    # Should be unreachable thanks to _validate_args.
    return 2


if __name__ == "__main__":
    sys.exit(main())
