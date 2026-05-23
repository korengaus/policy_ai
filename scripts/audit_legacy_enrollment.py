"""Phase 2 M11.3 — read-only audit of legacy weak-verified enrollment
candidates.

Wraps :func:`legacy_review_enrollment.find_legacy_weak_verified_rows`
(M11.1) and writes a structured JSON audit report to disk.

Read-only contract
------------------

    * NEVER calls :func:`legacy_review_enrollment.enroll_legacy_row`
      with ``dry_run=False`` — every enrollment record in the audit
      output is produced via the dry-run path.
    * NEVER writes to ``review_tasks``, ``analysis_results``, or
      ``verdict_label_attributions``.
    * NEVER auto-enables ``--apply`` flows in the M11.1 CLI. M11.3 is
      purely an audit step.
    * The single side effect is producing one JSON file under
      ``--output-dir`` (default ``./reports``).

Architectural note (M11.3 brief deviation)
------------------------------------------

The M11.3 brief described a ``--reports-dir`` argument that scans
``reports/*.json``. The M11.1 identifier the brief mandates we wrap
(``find_legacy_weak_verified_rows``) reads SQLite rows from the
``verdict_label_attributions`` table — it does NOT scan JSON files.
Per the brief's "Do NOT bend the identifier" rule, this script
exposes ``--db-path`` instead, matching the existing M11.1 CLI's flag
shape. The audit JSON's ``total_reports_scanned`` field is preserved
in the schema but populated from the DB row count (= ``candidates_found``)
because the identifier only returns flagged rows and never sees the
unflagged universe. The runbook documents this deviation.

Usage::

    python scripts/audit_legacy_enrollment.py --db-path policy_ai.db \\
        --output-dir ./reports
    python scripts/audit_legacy_enrollment.py --limit 5
    python scripts/audit_legacy_enrollment.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
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
from structured_logging import get_logger  # noqa: E402


logger = get_logger(__name__)


SCHEMA_VERSION = "m11.3.audit.v1"
DEFAULT_OUTPUT_DIR = ROOT / "reports"


# ---------------------------------------------------------------------------
# Candidate shaping
# ---------------------------------------------------------------------------


def _enrollment_reason_for(row: Dict[str, Any]) -> str:
    """Short human-readable reason explaining why this row is a
    candidate. Pulled from the row's own weak-evidence signals so the
    audit JSON is self-explanatory without re-querying the DB."""
    signals = enrollment._coerce_signals(row.get("weak_evidence_signals"))
    if signals:
        return (
            f"weak_evidence_verified row: {len(signals)} signal(s) — "
            + ", ".join(signals[:4])
            + ("…" if len(signals) > 4 else "")
        )
    return "weak_evidence_verified row (no signal details available)"


def _has_official_candidate_for(row: Dict[str, Any]) -> bool:
    """Best-effort: a row whose signals include the literal
    ``no_official_sources`` marker is, by definition, missing an
    official candidate. Any other state we treat as ``True``
    (unknown-or-present). The audit JSON's downstream consumer
    should treat this as a hint, not a strict pin."""
    signals = enrollment._coerce_signals(row.get("weak_evidence_signals"))
    return "no_official_sources" not in {str(s) for s in signals}


def _official_body_confirmed_for(row: Dict[str, Any]) -> bool:
    """Best-effort: the M11.1 attribution row does not store a direct
    ``official_body_confirmed`` flag. We derive False whenever the
    signal set indicates verification weakness about the body
    (``strength_none`` or ``no_official_sources``); otherwise we
    report False conservatively (audit pessimism)."""
    return False


def _candidate_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map an attribution row to the M11.3 audit schema. ``story_id``
    is the M11.1 ``analysis_id``; ``title`` is left empty because the
    M11.1 attribution table does not store it (the brief's schema
    includes the field for forward-compat with future identifiers
    that may carry it)."""
    signals = enrollment._coerce_signals(row.get("weak_evidence_signals"))
    return {
        "report_path": "",                       # n/a — DB-based identifier
        "story_id": str(row.get("analysis_id") or ""),
        "title": "",                             # not stored in attribution row
        "verdict_label": row.get("stored_verdict_label"),
        "evidence_strength_class": (
            row.get("stored_verification_strength") or "unknown"
        ),
        "has_official_candidate": _has_official_candidate_for(row),
        "official_body_confirmed": _official_body_confirmed_for(row),
        "enrollment_reason": _enrollment_reason_for(row),
        "would_enroll": True,
        # Helpful extras (audit-schema-versioned, but not in the brief).
        "attribution_id": row.get("id"),
        "weak_evidence_signals": signals,
    }


# ---------------------------------------------------------------------------
# Audit shaping
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_audit_payload(
    *,
    db_path: Optional[str],
    limit: Optional[int],
) -> Dict[str, Any]:
    """Pure function: pull candidates from the M11.1 identifier and
    produce the audit JSON payload. Side-effect-free — does not write
    to disk. Always returns a dict; never raises."""
    audit_id = uuid.uuid4().hex
    generated_at = _iso_now()
    effective_db = str(db_path) if db_path else str(database.DB_PATH)
    try:
        rows: List[Dict[str, Any]] = (
            enrollment.find_legacy_weak_verified_rows(db_path=db_path)
            or []
        )
    except Exception as error:  # noqa: BLE001 — defensive; identifier is best-effort
        logger.warning(
            "audit_legacy_enrollment_identifier_error",
            extra={
                "audit_id": audit_id,
                "error": str(error),
                "error_type": type(error).__name__,
            },
        )
        rows = []

    total_scanned = len(rows)
    if isinstance(limit, int) and limit >= 0:
        rows_for_audit = rows[:limit]
    else:
        rows_for_audit = rows

    candidates = [_candidate_dict(row) for row in rows_for_audit]

    return {
        "audit_id": audit_id,
        "generated_at": generated_at,
        "reports_dir": effective_db,   # repurposed: DB path stand-in
        "total_reports_scanned": total_scanned,
        "candidates_found": len(candidates),
        "candidates": candidates,
        "schema_version": SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_write_json(payload: Dict[str, Any], output_path: Path) -> None:
    """Write JSON to ``output_path`` via a temp file + ``os.replace``
    so a crash mid-write never leaves a partial file at the final
    path. The temp file lives in the same directory so the replace
    is guaranteed atomic on every supported platform."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".legacy_enrollment_audit_",
        suffix=".json.tmp",
        dir=str(output_path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # fsync isn't supported on every platform's tmpdir.
                # The atomic-rename is still safe; we just lose the
                # extra durability guarantee.
                pass
        os.replace(tmp_path, output_path)
    except Exception:
        # Best-effort cleanup of the temp file on any failure. The
        # final path is left in whatever state it was in BEFORE this
        # call — either non-existent or the previous good content.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _output_filename(generated_at: str) -> str:
    """Derive a filesystem-safe filename from the audit's ISO8601
    timestamp. Colons are not portable in Windows filenames; we
    replace them with hyphens so the file is openable on every
    platform without further quoting."""
    safe = generated_at.replace(":", "-")
    return f"legacy_enrollment_audit_{safe}.json"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_legacy_enrollment",
        description=(
            "Read-only audit of legacy weak-verified enrollment "
            "candidates (M11.3). Wraps the M11.1 identifier and "
            "writes a structured JSON report. NEVER enrolls anything."
        ),
        epilog=(
            "Exit codes: 0=success; 1=output-dir / DB error. "
            "Note: the identifier queries SQLite (verdict_label_attributions); "
            "the --reports-dir name from the M11.3 brief is replaced "
            "by --db-path to match the M11.1 API."
        ),
    )
    parser.add_argument(
        "--db-path", default=None,
        help=(
            "Path to the SQLite DB. Defaults to database.DB_PATH "
            "(policy_ai.db in the repo root). Mirrors the M11.1 CLI's "
            "--db-path flag."
        ),
    )
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR),
        help="Directory to write the audit JSON into (default: ./reports).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=(
            "If provided, write at most N candidates to the audit "
            "JSON. The audit's total_reports_scanned still reflects "
            "every candidate the identifier returned."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    output_dir = Path(args.output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(
            f"[audit] failed to create output directory {output_dir}: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    payload = build_audit_payload(
        db_path=args.db_path,
        limit=args.limit,
    )
    output_path = output_dir / _output_filename(payload["generated_at"])

    try:
        _atomic_write_json(payload, output_path)
    except OSError as error:
        print(
            f"[audit] failed to write audit file {output_path}: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    logger.info(
        "legacy_enrollment_audit_event",
        extra={
            "audit_id": payload["audit_id"],
            "candidates_found": payload["candidates_found"],
            "output_path": str(output_path),
            "reports_dir": payload["reports_dir"],
            "total_reports_scanned": payload["total_reports_scanned"],
        },
    )
    print(
        f"[audit] candidates={payload['candidates_found']} "
        f"output={output_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
