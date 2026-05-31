"""Phase 2 M11.3 — tests for ``scripts.audit_legacy_enrollment``.

No real network. No real DB outside the temp tree. Reuses the
M11.1 test fixture helpers (``_init_temp_db``, ``_seed_attribution_row``)
via import so the audit's identifier API surface stays consistent with
the upstream M11.1 contract.

Hard contract pinned here:
    * Audit is READ-ONLY — no write to ``review_tasks``,
      ``analysis_results``, or ``verdict_label_attributions``.
    * Idempotency: same DB state → same candidate set across runs.
    * Atomic write: a mid-write failure leaves no partial file at
      the final output path.
    * Korean text round-trips through the JSON file unchanged
      (UTF-8, ``ensure_ascii=False``).
    * schema_version is the M11.3 audit pin.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import database  # noqa: E402
import legacy_review_enrollment as enrollment  # noqa: E402
import scripts.audit_legacy_enrollment as audit_cli  # noqa: E402

# Reuse the M11.1 fixture helpers verbatim so the audit's identifier
# surface stays consistent with the upstream contract.
from tests.test_legacy_review_enrollment import (  # noqa: E402
    _init_temp_db,
    _seed_attribution_row,
    _seed_analysis_row,
    _count_review_tasks,
)


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _seed_n_attributions(
    db_path: str, n: int, *,
    weak: bool = True,
    strength: str = "none",
    verdict_label: str = "draft_verified",
):
    """Seed N rows that match the M11.1 weak-verified pattern."""
    for i in range(n):
        attribution_id = 1000 + i
        analysis_id = 2000 + i
        _seed_analysis_row(
            db_path,
            analysis_id=analysis_id,
            verdict_label=verdict_label,
            score=10,
            strength=strength,
            claim_text=f"테스트 케이스 {i}",
        )
        _seed_attribution_row(
            db_path,
            attribution_id=attribution_id,
            analysis_id=str(analysis_id),
            stored_verdict_label=verdict_label,
            score=10,
            strength=strength,
            weak=weak,
        )


def _run_main_inproc(argv):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = audit_cli.main(argv)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _read_latest_audit(output_dir: Path) -> Path:
    files = sorted(output_dir.glob("legacy_enrollment_audit_*.json"))
    assert files, f"no audit file produced in {output_dir}"
    return files[-1]


def _load_audit_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate_story_ids(payload: dict) -> list:
    return sorted(str(c.get("story_id") or "") for c in payload["candidates"])


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


class AuditEmptyAndNoMatchTests(unittest.TestCase):
    """Cases 1-2: empty DB and DB with no matching rows both produce
    a valid audit file with candidates_found=0."""

    def test_empty_db_produces_zero_candidates(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            rc, out, _ = _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            self.assertEqual(rc, 0)
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            self.assertEqual(payload["candidates_found"], 0)
            self.assertEqual(payload["candidates"], [])
            self.assertIn("[audit] candidates=0", out)

    def test_db_with_no_weak_verified_rows_zero_candidates(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            # Seed rows but with weak=False so they don't match.
            _seed_analysis_row(db_path, analysis_id=3001)
            _seed_attribution_row(
                db_path, attribution_id=4001,
                analysis_id="3001", weak=False,
            )
            output_dir = Path(tmp) / "audit-out"
            rc, _, _ = _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            self.assertEqual(rc, 0)
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            self.assertEqual(payload["candidates_found"], 0)


class AuditOneMatchTests(unittest.TestCase):
    """Case 3: one matching row produces one candidate with required fields."""

    def test_single_match_audit_shape(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_analysis_row(
                db_path, analysis_id=5001, verdict_label="draft_verified",
                claim_text="단일 매칭 테스트",
            )
            _seed_attribution_row(
                db_path, attribution_id=6001, analysis_id="5001",
            )
            output_dir = Path(tmp) / "audit-out"
            rc, _, _ = _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            self.assertEqual(rc, 0)
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            self.assertEqual(payload["candidates_found"], 1)
            cand = payload["candidates"][0]
            for field in (
                "report_path", "story_id", "title", "verdict_label",
                "evidence_strength_class", "has_official_candidate",
                "official_body_confirmed", "enrollment_reason",
                "would_enroll",
            ):
                self.assertIn(field, cand, f"missing field {field!r}")
            self.assertEqual(cand["story_id"], "5001")
            self.assertEqual(cand["verdict_label"], "draft_verified")
            self.assertTrue(cand["would_enroll"])
            self.assertIn("weak_evidence_verified", cand["enrollment_reason"])


class AuditMultipleMatchesTests(unittest.TestCase):
    """Case 4: multiple matches all have required fields."""

    def test_five_matches_all_have_required_fields(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_n_attributions(db_path, 5)
            output_dir = Path(tmp) / "audit-out"
            rc, _, _ = _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            self.assertEqual(rc, 0)
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            self.assertEqual(payload["candidates_found"], 5)
            required = {
                "report_path", "story_id", "title", "verdict_label",
                "evidence_strength_class", "has_official_candidate",
                "official_body_confirmed", "enrollment_reason",
                "would_enroll",
            }
            for cand in payload["candidates"]:
                self.assertTrue(
                    required.issubset(cand.keys()),
                    f"missing fields in {cand!r}",
                )


class AuditLimitTests(unittest.TestCase):
    """Case 5: --limit caps candidates but total_reports_scanned
    reflects everything the identifier returned."""

    def test_limit_2_with_5_matches(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_n_attributions(db_path, 5)
            output_dir = Path(tmp) / "audit-out"
            rc, _, _ = _run_main_inproc([
                "--output-dir", str(output_dir),
                "--limit", "2",
            ])
            self.assertEqual(rc, 0)
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            self.assertEqual(payload["candidates_found"], 2)
            self.assertEqual(payload["total_reports_scanned"], 5)


class AuditFileShapeTests(unittest.TestCase):
    """Cases 6 + 7 + 10 + 13: file is valid JSON, schema version
    matches, output path is inside output_dir, timestamp is ISO8601."""

    def test_audit_file_is_valid_json(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            path = _read_latest_audit(output_dir)
            # Parses without error.
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict)

    def test_schema_version_pin(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            self.assertEqual(payload["schema_version"], "m11.3.audit.v1")

    def test_output_path_inside_output_dir(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            path = _read_latest_audit(output_dir)
            self.assertEqual(path.parent.resolve(), output_dir.resolve())

    def test_timestamp_parses_iso8601(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            payload = _load_audit_payload(_read_latest_audit(output_dir))
            from datetime import datetime
            generated_at = payload["generated_at"]
            # The CLI uses isoformat with seconds precision; parse
            # must round-trip without exception.
            parsed = datetime.fromisoformat(generated_at)
            self.assertIsNotNone(parsed)


class AuditIdempotencyTests(unittest.TestCase):
    """Case 8: two consecutive runs on identical input produce
    identical candidate SETS (ordering allowed to differ)."""

    def test_two_runs_identical_candidate_set(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_n_attributions(db_path, 4)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            first_path = _read_latest_audit(output_dir)
            first_payload = _load_audit_payload(first_path)

            # Sleep is unnecessary — the audit filename includes the
            # ISO timestamp at second resolution; the second call will
            # either overwrite or produce a sibling. We just need both
            # files' candidate sets to match.
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            second_path = _read_latest_audit(output_dir)
            second_payload = _load_audit_payload(second_path)

            self.assertEqual(
                _candidate_story_ids(first_payload),
                _candidate_story_ids(second_payload),
                "candidate set drifted between idempotent runs",
            )


class AuditAtomicWriteTests(unittest.TestCase):
    """Case 9: simulate a write failure during os.replace and
    confirm no partial file is left behind at the final output path."""

    def test_replace_failure_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            output_dir.mkdir()

            def _boom(*_args, **_kwargs):
                raise OSError("simulated replace failure")

            with patch("scripts.audit_legacy_enrollment.os.replace", _boom):
                rc, _out, err = _run_main_inproc([
                    "--output-dir", str(output_dir),
                ])
            self.assertEqual(rc, 1)
            self.assertIn("simulated replace failure", err)
            final_files = list(output_dir.glob("legacy_enrollment_audit_*.json"))
            self.assertEqual(
                final_files, [],
                "atomic-write contract violated: partial audit file "
                "left at the final path after a replace failure",
            )
            leftover_tmp = list(output_dir.glob(".legacy_enrollment_audit_*"))
            self.assertEqual(
                leftover_tmp, [],
                "tempfile leak: tmp file was not cleaned up",
            )


class AuditExitCodeTests(unittest.TestCase):
    """Cases 11 + 12: exit 0 on success; exit 1 on broken output dir."""

    def test_exit_code_0_on_success(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            output_dir = Path(tmp) / "audit-out"
            rc, _, _ = _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            self.assertEqual(rc, 0)

    def test_exit_code_1_on_unwritable_output_dir(self):
        """Force mkdir to fail by pointing output-dir at a regular file."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            # Create a *file* at the path so mkdir(parents=True,
            # exist_ok=True) raises NotADirectoryError or FileExistsError.
            blocker = Path(tmp) / "not-a-dir.txt"
            blocker.write_text("blocking file", encoding="utf-8")
            # Use a sub-path under the blocker so mkdir must traverse it.
            target = blocker / "audit-out"
            rc, _, err = _run_main_inproc(
                ["--output-dir", str(target)]
            )
            self.assertEqual(rc, 1)
            self.assertIn("[audit]", err)


class AuditLogEventTests(unittest.TestCase):
    """Case 14: the ``legacy_enrollment_audit_event`` log line carries
    the expected extras."""

    LOGGER_NAME = "scripts.audit_legacy_enrollment"

    def setUp(self):
        self.records = []

        class _Capture(logging.Handler):
            def emit(_self, record):
                self.records.append(record)

        self.handler = _Capture()
        self.logger = logging.getLogger(self.LOGGER_NAME)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.logger.removeHandler(self.handler)

    def test_log_event_extras(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_n_attributions(db_path, 2)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )

        events = [
            r for r in self.records
            if r.getMessage() == "legacy_enrollment_audit_event"
        ]
        self.assertEqual(len(events), 1)
        event = events[0]
        for attr in (
            "audit_id", "candidates_found", "output_path",
            "reports_dir", "total_reports_scanned",
        ):
            self.assertTrue(
                hasattr(event, attr),
                f"log event missing extras field {attr!r}",
            )
        self.assertEqual(event.candidates_found, 2)
        self.assertEqual(event.total_reports_scanned, 2)


class AuditKoreanRoundTripTests(unittest.TestCase):
    """Case 15: Korean text round-trips through the JSON file
    unchanged. We pin via the claim_text field which the M11.1
    fixture seeds with Korean."""

    def test_korean_signals_preserved(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_analysis_row(
                db_path, analysis_id=7001,
                claim_text="한국어 클레임 텍스트",
            )
            # Seed a signal list that contains Korean.
            _seed_attribution_row(
                db_path, attribution_id=8001, analysis_id="7001",
                signals=["no_official_sources", "한국어_시그널"],
            )
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            path = _read_latest_audit(output_dir)
            raw = path.read_text(encoding="utf-8")
            self.assertIn("한국어_시그널", raw)
            payload = _load_audit_payload(path)
            cand = payload["candidates"][0]
            self.assertIn("한국어_시그널", cand["weak_evidence_signals"])


class AuditReadOnlyContractTests(unittest.TestCase):
    """Belt-and-suspenders: the audit must not create review_tasks
    even when called against a DB that has live attribution rows.
    This is the same invariant test_legacy_review_enrollment.py
    pins for the M11.1 module; we re-pin it here for the M11.3
    audit-script entry point so a future refactor that wired the
    audit to dry_run=False is caught immediately."""

    def test_audit_does_not_create_review_tasks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "audit.db")
            _init_temp_db(db_path)
            _seed_n_attributions(db_path, 3)
            output_dir = Path(tmp) / "audit-out"
            _run_main_inproc(
                ["--output-dir", str(output_dir)]
            )
            self.assertEqual(_count_review_tasks(db_path), 0)


if __name__ == "__main__":
    unittest.main()
