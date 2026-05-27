"""M14.0-print-b (2026-05-26) — operational scripts print() audit + migration.

Final phase of the audit §1.5 #10 (print-based logging) cleanup.
Migrates the remaining 25 `print()` calls across the 2 operational
scripts (timeline.py, scheduler.py) to structured logging.

Phase 1 categorized all 25 calls as CATEGORY A (migrate). Phase 2
applied the conversions:

  * timeline.py     — 13 prints → 13 log.info (`print_timeline_summary`)
  * scheduler.py    — 11 prints → 11 log.info + 1 print → 1 log.error
                      (the log.error sits inside
                      `except Exception as error:` — M14.4
                      NoFalsePositiveErrorsPin passes via the
                      inside-except path)

Closes audit §1.5 #10 completely:

  * M11.7 series:       exception swallowing logging
  * M14.0-print-a:      9 pipeline production files (26 prints)
  * M14.0-print-b:      2 operational scripts (25 prints, all CATEGORY A)

Pins:

  1. NoBarePrintInMigratedFilesPin — AST-walk: zero `print()` calls
     remain in timeline.py and scheduler.py.
  2. LoggerImportPresentPin — each file imports `get_logger` from
     `structured_logging` AND assigns `log = get_logger(__name__)`.
  3. MigratedFilesContainsBothFilesPin — both files appear in
     `tests/test_log_level_reclassification.MIGRATED_FILES`.
  4. PinValueMatches323Pin — `EXPECTED_TOTAL_LOG_CALLS == 323` AND
     `EXPECTED_TOTAL_LOG_ERRORS == 14`. Drift detector; the actual
     count-vs-source cross-check is done by the existing M14.4
     `TotalLogCallCountInvariant`.
  5. Audit_1_5_10_FullyResolvedPin — combined-scope membership check
     across both M14.0-print-a's 9 files AND M14.0-print-b's 2 files
     (all 11 must be in MIGRATED_FILES).

Scope is fully disjoint from `tests/test_print_migration.py` (M14.0b,
5 files), `tests/test_print_migration_m14_0c.py` (M14.0c, 8 files),
and `tests/test_m14_0_print_migration.py` (M14.0-print-a, 9 files).
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


M14_0_PRINT_B_FILES: tuple[str, ...] = (
    "timeline.py",
    "scheduler.py",
)


# M14.0-print-a's 9 files. Combined-scope membership check ensures
# every file across both -a and -b milestones is in MIGRATED_FILES.
M14_0_PRINT_A_FILES: tuple[str, ...] = (
    "official_source_search.py",
    "memory_store.py",
    "source_reliability_agent.py",
    "worker.py",
    "source_retrieval_agent.py",
    "claim_extractor.py",
    "official_evidence_resolution.py",
    "claim_normalizer.py",
    "pipeline_debug.py",
)


def _parse(path: Path) -> ast.AST:
    """Parse a Python source file, stripping a leading BOM if present."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return ast.parse(raw.decode("utf-8"))


def _count_print_calls(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            count += 1
    return count


def _has_get_logger_import(tree: ast.AST) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "structured_logging":
            for alias in node.names:
                if alias.name == "get_logger":
                    return True
    return False


def _has_module_logger_init(tree: ast.AST) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and func.id == "get_logger":
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "log", "logger",
                ):
                    return True
    return False


# ---------------------------------------------------------------------------
# 1. Zero bare print() calls in the 2 migrated files
# ---------------------------------------------------------------------------


class NoBarePrintInMigratedFilesPin(unittest.TestCase):
    """timeline.py and scheduler.py must contain zero `print()` Call
    nodes after M14.0-print-b. Subtests per file so a failure points
    at the exact offending source."""

    def test_no_print_calls_in_target_files(self):
        for filename in M14_0_PRINT_B_FILES:
            with self.subTest(filename=filename):
                path = _PROJECT_ROOT / filename
                count = _count_print_calls(_parse(path))
                self.assertEqual(
                    count, 0,
                    f"{filename} still contains {count} print() call(s) "
                    f"after M14.0-print-b — every print should have "
                    f"been converted to log.info or log.error.",
                )


# ---------------------------------------------------------------------------
# 2. Both files import + initialise the logger
# ---------------------------------------------------------------------------


class LoggerImportPresentPin(unittest.TestCase):
    """Each of the 2 files must (a) import `get_logger` from
    `structured_logging` AND (b) bind a module-level
    `log = get_logger(__name__)`."""

    def test_get_logger_imported_in_target_files(self):
        for filename in M14_0_PRINT_B_FILES:
            with self.subTest(filename=filename):
                tree = _parse(_PROJECT_ROOT / filename)
                self.assertTrue(
                    _has_get_logger_import(tree),
                    f"{filename} missing "
                    "'from structured_logging import get_logger'",
                )
                self.assertTrue(
                    _has_module_logger_init(tree),
                    f"{filename} missing module-level "
                    "'log = get_logger(__name__)'",
                )


# ---------------------------------------------------------------------------
# 3. Both files appear in MIGRATED_FILES
# ---------------------------------------------------------------------------


class MigratedFilesContainsBothFilesPin(unittest.TestCase):
    """`tests/test_log_level_reclassification.MIGRATED_FILES` must
    contain timeline.py and scheduler.py post-migration."""

    def test_both_files_in_migrated_files(self):
        from tests.test_log_level_reclassification import (
            MIGRATED_FILES as REPO_MIGRATED_FILES,
        )

        for filename in M14_0_PRINT_B_FILES:
            with self.subTest(filename=filename):
                self.assertIn(
                    filename, REPO_MIGRATED_FILES,
                    f"{filename} not in MIGRATED_FILES — its log calls "
                    f"will not be counted by the M14.4 pin.",
                )


# ---------------------------------------------------------------------------
# 4. Pin values match 323 / 14
# ---------------------------------------------------------------------------


class PinValueMatches323Pin(unittest.TestCase):
    """Both `EXPECTED_TOTAL_LOG_CALLS` (324) and
    `EXPECTED_TOTAL_LOG_ERRORS` (14) must reflect the M14.0-print-b
    additions. Lineage:
        ... → 298 (M14.0-print-a) → 323 (M14.0-print-b) → 324 (M15-dedup-1)
    """

    EXPECTED_TOTAL = 324
    EXPECTED_ERRORS = 14

    def test_expected_pin_values_after_m14_0_print_b(self):
        from tests.test_log_level_reclassification import (
            EXPECTED_TOTAL_LOG_CALLS,
            EXPECTED_TOTAL_LOG_ERRORS,
        )

        self.assertEqual(
            EXPECTED_TOTAL_LOG_CALLS, self.EXPECTED_TOTAL,
            f"EXPECTED_TOTAL_LOG_CALLS = {EXPECTED_TOTAL_LOG_CALLS}, "
            f"expected {self.EXPECTED_TOTAL} after M14.0-print-b. "
            "If a future milestone legitimately changes the count, "
            "update both this pin AND the lineage comment in "
            "tests/test_log_level_reclassification.py.",
        )
        self.assertEqual(
            EXPECTED_TOTAL_LOG_ERRORS, self.EXPECTED_ERRORS,
            f"EXPECTED_TOTAL_LOG_ERRORS = {EXPECTED_TOTAL_LOG_ERRORS}, "
            f"expected {self.EXPECTED_ERRORS} after M14.0-print-b "
            "(scheduler.py adds 1 log.error inside "
            "`except Exception as error:`).",
        )


# ---------------------------------------------------------------------------
# 5. audit §1.5 #10 closed — combined-scope membership
# ---------------------------------------------------------------------------


class Audit_1_5_10_FullyResolvedPin(unittest.TestCase):
    """audit §1.5 #10 (print-based logging) is closed when both
    M14.0-print-a's 9 files AND M14.0-print-b's 2 files appear in
    MIGRATED_FILES. This combined-scope membership pin captures the
    "closed" state — if any of the 11 files is removed, this pin
    fails and surfaces the regression."""

    def test_all_eleven_files_in_migrated_files(self):
        from tests.test_log_level_reclassification import (
            MIGRATED_FILES as REPO_MIGRATED_FILES,
        )

        all_files = M14_0_PRINT_A_FILES + M14_0_PRINT_B_FILES
        for filename in all_files:
            with self.subTest(filename=filename):
                self.assertIn(
                    filename, REPO_MIGRATED_FILES,
                    f"{filename} dropped from MIGRATED_FILES — audit "
                    "§1.5 #10 'closed' invariant violated.",
                )
        # Sanity: assert all 11 files are listed (defense against an
        # accidental partial deletion that happens to keep at least
        # one entry per call site).
        self.assertGreaterEqual(
            len([f for f in all_files if f in REPO_MIGRATED_FILES]),
            11,
            "Combined M14.0-print-a + M14.0-print-b scope is 11 files; "
            "MIGRATED_FILES is missing some.",
        )


if __name__ == "__main__":
    unittest.main()
