"""Phase 2 M11.1: tests for ``legacy_review_enrollment`` +
``enroll_legacy_weak_verified``.

Every test that writes uses a temp SQLite file initialised with
``database.init_db()`` so the real ``policy_ai.db`` is untouched. No
test path calls ``analyze_pipeline``, hits the network, imports
OpenAI, or invokes browser automation. The CLI's writing mode
(``--enroll``) is only exercised via the in-process ``main()``
function with explicit confirmation monkey-patched OR with ``--yes``;
the subprocess path is used for read-only modes and for the no-TTY
refusal check.

Covers the M11.1 spec items:
    A. ``find_legacy_weak_verified_rows`` returns only flagged rows
    B. ``find_legacy_weak_verified_rows`` returns empty when none exist
    C. ``is_already_enrolled`` False when no matching task
    D. ``is_already_enrolled`` True when same (analysis_id, reason)
    E. ``is_already_enrolled`` False when same analysis_id, different
       reason
    F. ``enroll_legacy_row`` dry_run=True does not write
    G. ``enroll_legacy_row`` dry_run=False writes one review_task
    H. Idempotency: two enrollments produce one review_task; second
       returns already_enrolled=True
    I. ``analysis_results`` row contents unchanged before/after enroll
    J. Created review_task status is the default pending status
       (NOT approved / published / corrected / rejected)
    K. truth_claim always False
    L. operator_review_required always True
    M. ``compute_enrollment_summary`` zero records
    N. ``compute_enrollment_summary`` counts enrolled vs already
    O. CLI --list exits 0, no writes
    P. CLI --dry-run exits 0, no writes
    Q. CLI --enroll without --yes and no TTY → exit 1
    R. CLI --enroll --yes writes; idempotent on second call
    S. CLI --check-status reports per-row enrolled flag
    T. CLI never produces truth_claim=True in JSON output
    U. ``legacy_review_enrollment.py`` not imported by main/api/scheduler
    V. No network/OpenAI imports in the module or the CLI
    W. Malformed weak_evidence_signals JSON → no crash, empty list
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
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
import review_workflow  # noqa: E402
import scripts.enroll_legacy_weak_verified as enroll_cli  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "enroll_legacy_weak_verified.py"
MODULE_PATH = ROOT / "legacy_review_enrollment.py"

CLI_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_temp_db(path: str) -> None:
    """Create the full schema in a temp SQLite-as-PG-substitute file.

    M12.0e-6b-1: replaced the ``database.DB_PATH`` swap + ``init_db()``
    scaffold with the PG-substitute pattern. Pointing ``DATABASE_URL``
    at ``path`` and building the engine triggers
    ``postgres_storage.ensure_schema`` (``_metadata.create_all``), which
    creates every mirror table (analysis_results,
    verdict_label_attributions, review_tasks, …) in the SAME file the
    ``_seed_*`` / ``_count_*`` helpers read and write directly via
    sqlite3. The create_all column set matches the raw-INSERT columns
    this fixture uses (verified against postgres_storage's table
    definitions). The env vars remain set so the enrollment calls under
    test see the substitute; the next ``_init_temp_db`` call overwrites
    ``DATABASE_URL`` for the next test's fresh DB.
    """
    os.environ["USE_POSTGRES_WRITE"] = "true"
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    import postgres_storage
    postgres_storage.reset_engine_for_tests()
    # Build the engine → ensure_schema (create_all) materialises every
    # mirror table in the substitute file before any seed/read fires.
    postgres_storage.get_engine()


def _seed_analysis_row(
    db_path: str, *, analysis_id: int, verdict_label: str = "draft_verified",
    score: int = 10, strength: str = "none",
    claim_text: str = "정부가 전세 보증금을 지원한다",
):
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO analysis_results (
                id, query, title, original_url, topic,
                policy_alert_level, policy_confidence_score,
                verification_strength, claim_text, verdict_label,
                verdict_confidence, evidence_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id, "전세 보증금", "전세 보증금 지원 발표",
                f"https://news.example/article-{analysis_id}",
                "housing", "LOW", score, strength,
                claim_text, verdict_label, score,
                "공식 검색 페이지 접근이 실패해 비교할 수 없습니다.",
                "2026-05-22T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_attribution_row(
    db_path: str, *, attribution_id: int, analysis_id: str,
    stored_verdict_label: str = "draft_verified", score: int = 10,
    strength: str = "none",
    weak: bool = True, signals=None, malformed_signals: bool = False,
):
    connection = sqlite3.connect(db_path)
    try:
        if malformed_signals:
            signals_text = "[not valid json"
        else:
            payload = signals or [
                "no_official_sources", "score_leq_30",
                "strength_none", "evidence_summary_says_failure",
            ]
            signals_text = json.dumps(payload, ensure_ascii=False)
        connection.execute(
            """
            INSERT INTO verdict_label_attributions (
                id, analysis_id, stored_verdict_label,
                stored_verdict_confidence, stored_policy_alert_level,
                stored_policy_confidence_score,
                stored_verification_strength, stored_claim_text,
                stored_evidence_summary, reconstructed_inputs,
                attributed_branch_id, attribution_confidence,
                attribution_reason, is_weak_evidence_verified,
                weak_evidence_signals, diagnostic_timestamp, notes,
                truth_claim, operator_review_required, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attribution_id, str(analysis_id), stored_verdict_label,
                score, "LOW", score, strength,
                "정부가 전세 보증금을 지원한다",
                "공식 검색 페이지 접근이 실패해 비교할 수 없습니다.",
                "{}",  # reconstructed_inputs
                "B08_direct_support_only",
                "low" if weak else "high",
                "diagnostic-test", 1 if weak else 0,
                signals_text,
                "2026-05-22T00:00:00+00:00",
                "test-fixture",
                0, 1, "2026-05-22T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _count_review_tasks(db_path: str, *, analysis_id: str = None) -> int:
    connection = sqlite3.connect(db_path)
    try:
        if analysis_id:
            row = connection.execute(
                "SELECT COUNT(*) FROM review_tasks WHERE result_id = ?",
                (str(analysis_id),),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM review_tasks"
            ).fetchone()
    finally:
        connection.close()
    return row[0] if row else 0


def _fetch_review_task(db_path: str, *, idempotency_key: str):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM review_tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def _fetch_analysis_row(db_path: str, *, analysis_id: int):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM analysis_results WHERE id = ?",
            (analysis_id,),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def _run_cli_subprocess(*args, timeout=CLI_TIMEOUT_SECONDS, env=None,
                        stdin_text=None):
    # Default stdin to an empty closed pipe so the child reliably
    # reports ``sys.stdin.isatty()`` as False. Without this, on
    # Windows the child inherits the parent test runner's stdin,
    # which can be a console TTY and would change the CLI's
    # confirmation-flow behaviour.
    effective_input = stdin_text if stdin_text is not None else ""
    completed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
        cwd=str(ROOT),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout,
        env={**os.environ, **(env or {})},
        input=effective_input,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _run_cli_inproc(argv):
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = enroll_cli.main(argv)
    except SystemExit as e:
        rc = int(e.code or 0)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Module unit tests
# ---------------------------------------------------------------------------


class FindLegacyWeakVerifiedTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp_dir.name) / "find_test.db")
        _init_temp_db(self._db)

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_returns_only_flagged_rows(self):
        _seed_attribution_row(
            self._db, attribution_id=1, analysis_id="100", weak=True,
        )
        _seed_attribution_row(
            self._db, attribution_id=2, analysis_id="200", weak=False,
        )
        rows = enrollment.find_legacy_weak_verified_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["analysis_id"], "100")

    def test_returns_empty_when_no_flagged(self):
        rows = enrollment.find_legacy_weak_verified_rows()
        self.assertEqual(rows, [])

    def test_db_error_returns_empty_list_without_raising(self):
        # M12.0e-3b: PG-only — no db_path. Point DATABASE_URL at an
        # UNPARSEABLE value so engine creation fails immediately at
        # create_engine (no network connect — fast) and raises
        # PostgresReadError; find_legacy_weak_verified_rows must swallow
        # it and return []. Restore the prior env + reset the engine in a
        # finally so this test cannot poison the others in the file.
        import postgres_storage
        prior_url = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "not-a-valid-database-url"
            postgres_storage.reset_engine_for_tests()
            result = enrollment.find_legacy_weak_verified_rows()
            self.assertEqual(result, [])
        finally:
            if prior_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prior_url
            postgres_storage.reset_engine_for_tests()


class IsAlreadyEnrolledTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp_dir.name) / "isalready.db")
        _init_temp_db(self._db)
        _seed_attribution_row(
            self._db, attribution_id=1, analysis_id="100", weak=True,
        )

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_false_when_no_task(self):
        self.assertFalse(
            enrollment.is_already_enrolled("100")
        )

    def test_true_after_enrollment(self):
        candidate = enrollment.find_legacy_weak_verified_rows()[0]
        enrollment.enroll_legacy_row(
            candidate, dry_run=False,
        )
        self.assertTrue(
            enrollment.is_already_enrolled("100")
        )

    def test_false_when_same_analysis_id_different_reason(self):
        candidate = enrollment.find_legacy_weak_verified_rows()[0]
        enrollment.enroll_legacy_row(
            candidate, dry_run=False,
        )
        # Different reason → different idempotency key → False.
        self.assertFalse(
            enrollment.is_already_enrolled(
                "100", reason="some_other_reason",
            )
        )

    def test_empty_analysis_id_returns_false(self):
        self.assertFalse(
            enrollment.is_already_enrolled("")
        )


# ---------------------------------------------------------------------------
# enroll_legacy_row behaviour
# ---------------------------------------------------------------------------


class EnrollLegacyRowTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp_dir.name) / "enroll_test.db")
        _init_temp_db(self._db)
        _seed_analysis_row(self._db, analysis_id=100)
        _seed_attribution_row(
            self._db, attribution_id=1, analysis_id="100", weak=True,
        )

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _candidate(self) -> dict:
        rows = enrollment.find_legacy_weak_verified_rows()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_dry_run_does_not_write(self):
        record = enrollment.enroll_legacy_row(
            self._candidate(), dry_run=True,
        )
        self.assertEqual(_count_review_tasks(self._db), 0)
        self.assertFalse(record.already_enrolled)
        self.assertFalse(record.wrote_to_db)
        self.assertIsNone(record.review_task_id)
        self.assertIs(record.truth_claim, False)
        self.assertIs(record.operator_review_required, True)

    def test_actual_write_creates_one_review_task(self):
        record = enrollment.enroll_legacy_row(
            self._candidate(), dry_run=False,
        )
        self.assertTrue(record.wrote_to_db)
        self.assertFalse(record.already_enrolled)
        self.assertIsNotNone(record.review_task_id)
        self.assertEqual(_count_review_tasks(self._db, analysis_id="100"), 1)

    def test_idempotency_second_call_does_not_duplicate(self):
        first = enrollment.enroll_legacy_row(
            self._candidate(), dry_run=False,
        )
        second = enrollment.enroll_legacy_row(
            self._candidate(), dry_run=False,
        )
        self.assertTrue(first.wrote_to_db)
        self.assertFalse(second.wrote_to_db)
        self.assertTrue(second.already_enrolled)
        self.assertEqual(_count_review_tasks(self._db, analysis_id="100"), 1)
        # Both records point at the same review_task_id.
        self.assertEqual(first.review_task_id, second.review_task_id)

    def test_analysis_results_row_unchanged_after_enroll(self):
        before = _fetch_analysis_row(self._db, analysis_id=100)
        enrollment.enroll_legacy_row(
            self._candidate(), dry_run=False,
        )
        after = _fetch_analysis_row(self._db, analysis_id=100)
        # Every column must be byte-identical before/after enrollment.
        self.assertEqual(
            before, after,
            "analysis_results row was modified during enrollment",
        )

    def test_review_task_status_is_pending_review(self):
        record = enrollment.enroll_legacy_row(
            self._candidate(), dry_run=False,
        )
        key = enrollment.make_enrollment_idempotency_key("100")
        task = _fetch_review_task(self._db, idempotency_key=key)
        self.assertIsNotNone(task)
        self.assertEqual(
            task["status"], review_workflow.STATUS_PENDING_REVIEW,
        )
        # And explicitly not in any forbidden auto-finalized set.
        for forbidden in (
            review_workflow.STATUS_APPROVED,
            review_workflow.STATUS_REJECTED,
            review_workflow.STATUS_PUBLISHED,
            review_workflow.STATUS_CORRECTED,
        ):
            self.assertNotEqual(task["status"], forbidden)
        self.assertEqual(int(task["human_review_required"]), 1)
        # snapshot_json carries the enrollment metadata.
        snapshot = json.loads(task["snapshot_json"])
        self.assertEqual(
            snapshot["legacy_enrollment"]["reason"],
            enrollment.ENROLLMENT_REASON,
        )
        self.assertEqual(
            snapshot["legacy_enrollment"]["source_milestone"], "M11.1",
        )
        self.assertIs(
            snapshot["legacy_enrollment"]["truth_claim"], False,
        )

    def test_record_truth_claim_always_false(self):
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                r = enrollment.enroll_legacy_row(
                    self._candidate(), dry_run=dry,
                )
                self.assertIs(r.truth_claim, False)
                d = enrollment.enrollment_to_dict(r)
                self.assertIs(d["truth_claim"], False)
                self.assertIs(d["operator_review_required"], True)

    def test_non_dict_input_returns_error_record(self):
        r = enrollment.enroll_legacy_row(None)  # type: ignore[arg-type]
        self.assertIsNotNone(r.error)
        self.assertIs(r.truth_claim, False)
        # No write happened.
        self.assertEqual(_count_review_tasks(self._db), 0)

    def test_missing_analysis_id_returns_error_record(self):
        r = enrollment.enroll_legacy_row(
            {"stored_verdict_label": "draft_verified"},
        )
        self.assertIsNotNone(r.error)
        self.assertIn("analysis_id", r.error)
        self.assertEqual(_count_review_tasks(self._db), 0)

    def test_forbidden_status_constant_set(self):
        # Sanity: ENROLLMENT_STATUS must NOT be in the forbidden set.
        self.assertNotIn(
            enrollment.ENROLLMENT_STATUS,
            enrollment._FORBIDDEN_ENROLLMENT_STATUSES,
        )


# ---------------------------------------------------------------------------
# W. Defensive: malformed signals JSON
# ---------------------------------------------------------------------------


class MalformedSignalsTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp_dir.name) / "malformed.db")
        _init_temp_db(self._db)
        _seed_attribution_row(
            self._db, attribution_id=1, analysis_id="100", weak=True,
            malformed_signals=True,
        )

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_malformed_signals_does_not_crash(self):
        candidate = enrollment.find_legacy_weak_verified_rows()[0]
        record = enrollment.enroll_legacy_row(
            candidate, dry_run=True,
        )
        # Signals collapsed to empty list — no crash.
        self.assertEqual(record.weak_evidence_signals, [])
        self.assertIs(record.truth_claim, False)


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------


class SummaryTests(unittest.TestCase):
    def test_zero_records(self):
        summary = enrollment.compute_enrollment_summary([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["enrolled_now"], 0)
        self.assertEqual(summary["already_enrolled"], 0)
        self.assertEqual(summary["dry_run_skipped"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_counts_enrolled_vs_already(self):
        records = [
            enrollment.LegacyEnrollmentRecord(
                analysis_id="1", wrote_to_db=True,
                weak_evidence_signals=["no_official_sources"],
            ),
            enrollment.LegacyEnrollmentRecord(
                analysis_id="2", wrote_to_db=True,
                weak_evidence_signals=["score_leq_30"],
            ),
            enrollment.LegacyEnrollmentRecord(
                analysis_id="3", already_enrolled=True,
                weak_evidence_signals=["no_official_sources"],
            ),
            enrollment.LegacyEnrollmentRecord(
                analysis_id="4",  # dry-run skip
                weak_evidence_signals=["strength_none"],
            ),
        ]
        summary = enrollment.compute_enrollment_summary(records)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["enrolled_now"], 2)
        self.assertEqual(summary["already_enrolled"], 1)
        self.assertEqual(summary["dry_run_skipped"], 1)
        self.assertEqual(
            summary["weak_evidence_signal_histogram"][
                "no_official_sources"
            ],
            2,
        )


# ---------------------------------------------------------------------------
# CLI smokes
# ---------------------------------------------------------------------------


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp_dir.name) / "cli_smoke.db")
        _init_temp_db(self._db)
        # Seed three weak-verified rows for the CLI to enumerate.
        for idx, aid in enumerate((105, 104, 95), start=1):
            _seed_analysis_row(self._db, analysis_id=aid)
            _seed_attribution_row(
                self._db, attribution_id=idx, analysis_id=str(aid),
                weak=True,
            )
        # Plus one non-weak row to confirm the filter holds.
        _seed_analysis_row(self._db, analysis_id=999, score=90,
                           strength="high")
        _seed_attribution_row(
            self._db, attribution_id=99, analysis_id="999", weak=False,
        )

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_help_exits_0(self):
        rc, stdout, _ = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("legacy weak-verified", stdout)
        self.assertIn("Exit codes", stdout)

    def test_list_exits_0_no_writes(self):
        before = _count_review_tasks(self._db)
        rc, stdout, _ = _run_cli_subprocess(
            "--list",
        )
        self.assertEqual(rc, 0)
        self.assertIn("Legacy Weak-Verified Candidates", stdout)
        self.assertIn("Total candidates: 3", stdout)
        # Per-row truth/operator-review reminders in the footer.
        self.assertIn("truth_claim=False", stdout)
        self.assertIn("operator_review_required=True", stdout)
        # No DB writes.
        self.assertEqual(_count_review_tasks(self._db), before)

    def test_dry_run_exits_0_no_writes(self):
        before = _count_review_tasks(self._db)
        rc, stdout, _ = _run_cli_subprocess(
            "--dry-run",
        )
        self.assertEqual(rc, 0)
        self.assertIn("Total candidates: 3", stdout)
        self.assertIn("Would enroll now: 3", stdout)
        self.assertEqual(_count_review_tasks(self._db), before)

    def test_check_status_reports_each_row(self):
        rc, stdout, _ = _run_cli_subprocess(
            "--check-status",
        )
        self.assertEqual(rc, 0)
        self.assertIn("Total candidates: 3", stdout)
        self.assertIn("Not yet enrolled: 3", stdout)
        # All three IDs appear.
        for aid in ("105", "104", "95"):
            self.assertIn(aid, stdout)

    def test_summary_exits_0_no_writes(self):
        before = _count_review_tasks(self._db)
        rc, stdout, _ = _run_cli_subprocess(
            "--summary",
        )
        self.assertEqual(rc, 0)
        self.assertIn("Enrollment Summary", stdout)
        self.assertIn("Total candidates:        3", stdout)
        self.assertEqual(_count_review_tasks(self._db), before)

    def test_enroll_no_tty_no_yes_refuses(self):
        # Subprocess inherits a non-TTY stdin → CLI must refuse without
        # writing.
        before = _count_review_tasks(self._db)
        rc, _stdout, stderr = _run_cli_subprocess(
            "--enroll",
        )
        self.assertEqual(rc, 1)
        self.assertIn("--yes", stderr)
        self.assertEqual(_count_review_tasks(self._db), before)

    def test_enroll_yes_writes_review_tasks(self):
        rc, stdout, _ = _run_cli_subprocess(
            "--enroll", "--yes",
        )
        self.assertEqual(rc, 0, msg=stdout)
        self.assertIn("enrolled=3", stdout)
        self.assertIn("already_enrolled=0", stdout)
        self.assertEqual(_count_review_tasks(self._db), 3)
        # All three review_tasks land in the pending status.
        connection = sqlite3.connect(self._db)
        try:
            statuses = {
                row[0] for row in connection.execute(
                    "SELECT status FROM review_tasks"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(
            statuses, {review_workflow.STATUS_PENDING_REVIEW},
        )

    def test_enroll_yes_is_idempotent(self):
        rc1, _, _ = _run_cli_subprocess(
            "--enroll", "--yes",
        )
        rc2, stdout2, _ = _run_cli_subprocess(
            "--enroll", "--yes",
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertIn("enrolled=0", stdout2)
        self.assertIn("already_enrolled=3", stdout2)
        # Still exactly 3 review_tasks — no duplicates.
        self.assertEqual(_count_review_tasks(self._db), 3)

    def test_no_mode_is_usage_error(self):
        rc, _, stderr = _run_cli_subprocess()
        self.assertEqual(rc, 2)
        self.assertIn("required", stderr)

    def test_two_modes_simultaneously_is_usage_error(self):
        rc, _, stderr = _run_cli_subprocess(
            "--list", "--dry-run",
        )
        self.assertEqual(rc, 2)
        self.assertIn("only one", stderr)

    def test_yes_without_enroll_is_usage_error(self):
        rc, _, stderr = _run_cli_subprocess(
            "--list", "--yes",
        )
        self.assertEqual(rc, 2)
        self.assertIn("--yes", stderr)

    def test_enroll_with_no_candidates_exits_1(self):
        # Fresh temp DB with the schema but no weak-verified rows.
        empty_db = str(Path(self._tmp_dir.name) / "empty.db")
        _init_temp_db(empty_db)
        rc, _stdout, stderr = _run_cli_subprocess(
            "--enroll", "--yes",
        )
        self.assertEqual(rc, 1)
        self.assertIn("no legacy weak-verified candidates", stderr)

    def test_list_json_carries_no_truth_claim_true(self):
        rc, stdout, _ = _run_cli_subprocess(
            "--list", "--json",
        )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["mode"], "list")
        # truth_claim never appears as True in any output mode.
        self.assertNotIn("\"truth_claim\": true", stdout.lower())


# ---------------------------------------------------------------------------
# Interactive confirmation (in-process so we can monkey-patch input)
# ---------------------------------------------------------------------------


class InteractiveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db = str(Path(self._tmp_dir.name) / "confirm.db")
        _init_temp_db(self._db)
        _seed_analysis_row(self._db, analysis_id=105)
        _seed_attribution_row(
            self._db, attribution_id=1, analysis_id="105", weak=True,
        )

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _force_tty(self, on=True):
        return patch("sys.stdin.isatty", return_value=on)

    def test_confirmation_yes_writes(self):
        with self._force_tty(True), patch.object(
            enroll_cli, "_read_confirmation", return_value="YES",
        ):
            rc, _stdout, _ = _run_cli_inproc([
                "--enroll",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(_count_review_tasks(self._db), 1)

    def test_confirmation_no_aborts(self):
        before = _count_review_tasks(self._db)
        with self._force_tty(True), patch.object(
            enroll_cli, "_read_confirmation", return_value="NO",
        ):
            rc, _stdout, stderr = _run_cli_inproc([
                "--enroll",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("confirmation aborted", stderr)
        self.assertEqual(_count_review_tasks(self._db), before)

    def test_confirmation_empty_aborts(self):
        before = _count_review_tasks(self._db)
        with self._force_tty(True), patch.object(
            enroll_cli, "_read_confirmation", return_value="",
        ):
            rc, _stdout, _ = _run_cli_inproc([
                "--enroll",
            ])
        self.assertEqual(rc, 1)
        self.assertEqual(_count_review_tasks(self._db), before)


# ---------------------------------------------------------------------------
# Static safety
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def _import_lines(self, path):
        text = path.read_text(encoding="utf-8")
        return [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]

    def test_module_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(MODULE_PATH))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"legacy_review_enrollment.py must not import {forbidden!r}",
            )

    def test_cli_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(CLI_SCRIPT))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"enroll_legacy_weak_verified.py must not import {forbidden!r}",
            )

    def test_module_not_imported_by_pipeline_entry_points(self):
        for module_name in ("main.py", "api_server.py", "scheduler.py"):
            module_path = ROOT / module_name
            if not module_path.exists():
                continue
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "legacy_review_enrollment", text,
                f"{module_name} must not import legacy_review_enrollment",
            )
            self.assertNotIn(
                "enroll_legacy_weak_verified", text,
                f"{module_name} must not import enroll_legacy_weak_verified",
            )


if __name__ == "__main__":
    unittest.main()
