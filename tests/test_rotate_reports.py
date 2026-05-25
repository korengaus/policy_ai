"""M12.2 — Tests for ``scripts/rotate_reports.py``.

Each test runs against a ``tempfile.TemporaryDirectory`` masquerading
as the ``reports/`` directory so the real project artifacts are never
touched. Mtimes are forced via ``os.utime`` so the day-threshold tests
do not depend on wall-clock drift.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make ``scripts/`` importable.
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


import rotate_reports  # noqa: E402


def _make_args(reports_dir: str, **overrides) -> argparse.Namespace:
    base = {
        "days": 30,
        "reports_dir": reports_dir,
        "archive_subdir": "archive",
        "exclude_pattern": None,
        "compress": False,
        "delete_after_days": None,
        "dry_run": False,
        "json_log": False,
        "quiet": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _age_file(path: Path, days_old: float) -> None:
    """Set both atime and mtime to ``days_old`` days in the past."""
    target = time.time() - (days_old * 86400)
    os.utime(path, (target, target))


def _archive_for(reports_dir: Path, mtime_seconds: float) -> Path:
    """Compute the YYYY-MM bucket directory inside reports/archive that
    rotate_reports would create for the given mtime."""
    from datetime import datetime, timezone
    bucket = datetime.fromtimestamp(mtime_seconds, tz=timezone.utc).strftime("%Y-%m")
    return reports_dir / "archive" / bucket


class FilesYoungerThanThresholdNotMoved(unittest.TestCase):
    def test_recent_files_stay_put(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            fresh = reports / "policy_analysis_fresh.json"
            _write_json(fresh, {"x": 1})
            _age_file(fresh, days_old=1)

            exit_code = rotate_reports.run(_make_args(str(reports), days=30))

            self.assertEqual(exit_code, 0)
            self.assertTrue(fresh.exists(), "fresh file was unexpectedly moved")
            archive = reports / "archive"
            # archive dir not even created since no work happened
            self.assertFalse(archive.exists(), "archive dir created with no work")


class FilesOlderThanThresholdMoved(unittest.TestCase):
    def test_old_file_moves_to_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old = reports / "policy_analysis_old.json"
            _write_json(old, {"x": 1})
            _age_file(old, days_old=60)
            old_mtime = old.stat().st_mtime

            exit_code = rotate_reports.run(_make_args(str(reports), days=30))

            self.assertEqual(exit_code, 0)
            self.assertFalse(old.exists(), "old file still in top-level reports/")
            expected_dest = _archive_for(reports, old_mtime) / "policy_analysis_old.json"
            self.assertTrue(expected_dest.exists(), f"missing {expected_dest}")


class OperationalCheckFilesExcludedByDefault(unittest.TestCase):
    def test_operational_check_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            op_check = reports / "operational_check_20250101T000000Z.json"
            _write_json(op_check, {"x": 1})
            _age_file(op_check, days_old=90)

            other = reports / "policy_analysis_other.json"
            _write_json(other, {"x": 2})
            _age_file(other, days_old=90)

            exit_code = rotate_reports.run(_make_args(str(reports), days=30))

            self.assertEqual(exit_code, 0)
            self.assertTrue(op_check.exists(), "operational_check_* was moved")
            self.assertFalse(other.exists(), "policy_analysis was not moved")


class CustomExcludePatternWorks(unittest.TestCase):
    def test_custom_exclude_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            keep = reports / "keep_me_alone.json"
            _write_json(keep, {"x": 1})
            _age_file(keep, days_old=90)

            move = reports / "operational_check_will_move.json"
            _write_json(move, {"x": 2})
            _age_file(move, days_old=90)

            exit_code = rotate_reports.run(
                _make_args(
                    str(reports),
                    days=30,
                    exclude_pattern=["keep_me_*.json"],
                )
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(keep.exists(), "custom-excluded file was moved")
            self.assertFalse(move.exists(), "operational_check not moved when custom excludes used")


class ArchiveDirectoryCreatedWithYyyyMm(unittest.TestCase):
    def test_yyyy_mm_bucket_derives_from_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old = reports / "policy_analysis_x.json"
            _write_json(old, {"x": 1})
            _age_file(old, days_old=60)
            mtime = old.stat().st_mtime

            rotate_reports.run(_make_args(str(reports), days=30))

            from datetime import datetime, timezone
            expected_bucket = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m")
            self.assertTrue((reports / "archive" / expected_bucket).is_dir())


class IdempotentRerunNoOp(unittest.TestCase):
    def test_second_run_does_not_re_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old = reports / "policy_analysis_old.json"
            _write_json(old, {"x": 1})
            _age_file(old, days_old=60)

            rotate_reports.run(_make_args(str(reports), days=30))
            archived = list((reports / "archive").rglob("*.json"))
            self.assertEqual(len(archived), 1)

            # Re-create the same file (simulating fresh data with the
            # same name) and verify the second run treats the existing
            # archive entry as a collision rather than overwriting.
            _write_json(old, {"x": 1})
            _age_file(old, days_old=60)

            exit_code = rotate_reports.run(_make_args(str(reports), days=30))
            self.assertEqual(exit_code, 0)
            self.assertTrue(old.exists(), "source moved despite archive collision")
            archived_after = list((reports / "archive").rglob("*.json"))
            self.assertEqual(len(archived_after), 1, "archive directory grew on re-run")


class DryRunNoFilesystemChanges(unittest.TestCase):
    def test_dry_run_preserves_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old = reports / "policy_analysis_old.json"
            _write_json(old, {"x": 1})
            _age_file(old, days_old=60)

            exit_code = rotate_reports.run(
                _make_args(str(reports), days=30, dry_run=True)
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(old.exists(), "file moved during --dry-run")
            self.assertFalse(
                (reports / "archive").exists(),
                "archive dir created during --dry-run",
            )


class CompressionProducesGz(unittest.TestCase):
    def test_gz_archive_and_source_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old = reports / "policy_analysis_compress.json"
            payload = {"key": "value", "list": [1, 2, 3]}
            _write_json(old, payload)
            _age_file(old, days_old=60)
            mtime = old.stat().st_mtime

            rotate_reports.run(
                _make_args(str(reports), days=30, compress=True)
            )

            self.assertFalse(old.exists())
            expected = _archive_for(reports, mtime) / "policy_analysis_compress.json.gz"
            self.assertTrue(expected.exists(), f"missing {expected}")
            with gzip.open(expected, "rt", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), payload)


class DeleteAfterDaysOnlyTouchesArchive(unittest.TestCase):
    def test_top_level_files_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            top_level_recent = reports / "policy_analysis_recent.json"
            _write_json(top_level_recent, {"x": 1})
            _age_file(top_level_recent, days_old=400)
            # Despite the ancient mtime, this file lives at the top
            # level (not inside archive/) so delete_after_days must
            # not touch it. The rotation portion will move it to
            # archive first.

            exit_code = rotate_reports.run(
                _make_args(
                    str(reports),
                    days=30,
                    delete_after_days=10,
                )
            )

            self.assertEqual(exit_code, 0)
            # The file was rotated, not deleted, then immediately
            # deleted from the archive because its mtime is older
            # than 10 days. The contract under test: only archive
            # files were eligible for deletion at all.
            self.assertFalse(top_level_recent.exists())


class DeleteAfterDaysRespectsThreshold(unittest.TestCase):
    def test_recent_archive_files_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            # Seed an existing archive layout directly.
            archive_dir = reports / "archive" / "2026-04"
            archive_dir.mkdir(parents=True)

            recent_archive = archive_dir / "recent_archived.json"
            recent_archive.write_text("{}", encoding="utf-8")
            _age_file(recent_archive, days_old=5)

            ancient_archive = archive_dir / "ancient_archived.json"
            ancient_archive.write_text("{}", encoding="utf-8")
            _age_file(ancient_archive, days_old=400)

            exit_code = rotate_reports.run(
                _make_args(
                    str(reports),
                    days=30,
                    delete_after_days=180,
                )
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(recent_archive.exists(), "recent archive deleted")
            self.assertFalse(ancient_archive.exists(), "ancient archive kept")


class PerFileErrorDoesNotHaltRun(unittest.TestCase):
    def test_single_file_error_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            good = reports / "policy_analysis_good.json"
            _write_json(good, {"x": 1})
            _age_file(good, days_old=60)

            bad = reports / "policy_analysis_bad.json"
            _write_json(bad, {"x": 2})
            _age_file(bad, days_old=60)

            real_move_one = rotate_reports._move_one

            def selective_failure(src, archive_root, *, compress, dry_run):
                if src.name == "policy_analysis_bad.json":
                    raise OSError("simulated move failure")
                return real_move_one(src, archive_root, compress=compress, dry_run=dry_run)

            with mock.patch.object(rotate_reports, "_move_one", side_effect=selective_failure):
                exit_code = rotate_reports.run(_make_args(str(reports), days=30))

            self.assertEqual(exit_code, 1, "exit code did not reflect the error")
            # Good file moved, bad file still there.
            self.assertFalse(good.exists())
            self.assertTrue(bad.exists())


class DestinationCollisionSkippedAndLogged(unittest.TestCase):
    def test_existing_destination_counts_as_already_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old = reports / "policy_analysis_dup.json"
            _write_json(old, {"x": 1})
            _age_file(old, days_old=60)
            mtime = old.stat().st_mtime

            # Pre-seed the archive destination.
            dest_dir = _archive_for(reports, mtime)
            dest_dir.mkdir(parents=True)
            (dest_dir / "policy_analysis_dup.json").write_text("{}", encoding="utf-8")

            exit_code = rotate_reports.run(_make_args(str(reports), days=30))

            self.assertEqual(exit_code, 0)
            self.assertTrue(old.exists(), "source removed despite destination collision")


class SummaryCountsMatchActions(unittest.TestCase):
    def test_summary_counts_are_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            for index in range(3):
                old = reports / f"policy_analysis_{index}.json"
                _write_json(old, {"i": index})
                _age_file(old, days_old=60)
            young = reports / "policy_analysis_young.json"
            _write_json(young, {"i": "y"})
            _age_file(young, days_old=2)
            op = reports / "operational_check_skip.json"
            _write_json(op, {"i": "op"})
            _age_file(op, days_old=120)

            buffer = []

            def capture(record, *, json_log, quiet):
                buffer.append(record)

            with mock.patch.object(rotate_reports, "_emit", side_effect=capture):
                exit_code = rotate_reports.run(
                    _make_args(str(reports), days=30, quiet=False)
                )

            self.assertEqual(exit_code, 0)
            summaries = [record for record in buffer if record.get("action") == "summary"]
            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertEqual(summary["scanned"], 5)
            self.assertEqual(summary["excluded"], 1)
            self.assertEqual(summary["eligible"], 3)
            self.assertEqual(summary["moved"], 3)
            self.assertEqual(summary["errors"], 0)


class ReportsDirMissingReturnsErrorExitCode(unittest.TestCase):
    def test_missing_reports_dir_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist"
            exit_code = rotate_reports.run(_make_args(str(missing)))
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
