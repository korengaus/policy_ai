"""M14.0-print-a (2026-05-26) — pipeline print() → structured logging.

The brief identified 26 `print()` calls across 9 pipeline files that
bypassed the JSON log format on Render. This milestone migrates each
to structured logging (`log.info` for 25 sites; `log.error` for the
single worker.py startup-failure helper). All Korean / English content
preserved; interpolated values exposed via `extra={...}`.

Pins:

  1. NoBarePrintInMigratedFilesPin — AST-walk: zero `print()` calls
     remain across the 9 migrated files.
  2. LoggerImportPresentPin — each of the 9 files imports
     `get_logger` from `structured_logging` AND assigns
     `log = get_logger(__name__)` at module level.
  3. MigratedFilesContainsAllNinePin — all 9 files appear in
     `tests/test_log_level_reclassification.MIGRATED_FILES`.
  4. PinValueMatches298Pin — `EXPECTED_TOTAL_LOG_CALLS == 298`
     (drift detector; the count-against-source check in
     `test_log_level_reclassification.TotalLogCallCountInvariant`
     does the actual cross-verification against AST-counted log
     calls).

Scope is fully disjoint from `tests/test_print_migration.py` (M14.0b,
5 files) and `tests/test_print_migration_m14_0c.py` (M14.0c, 8 files);
those suites continue to pin their own scoped file sets without
overlap.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


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
    """Parse a Python source file, stripping a leading BOM if present.
    Matches the helper shape in tests/test_print_migration.py /
    test_print_migration_m14_0c.py for consistency."""
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
# 1. Zero bare print() calls in the migrated files
# ---------------------------------------------------------------------------


class NoBarePrintInMigratedFilesPin(unittest.TestCase):
    """All 9 target files must contain zero `print()` Call nodes after
    the M14.0-print-a migration. Subtests per file so a failure points
    at the exact offending source."""

    def test_no_print_calls_in_target_files(self):
        offenders: list[tuple[str, int]] = []
        for filename in M14_0_PRINT_A_FILES:
            with self.subTest(filename=filename):
                path = _PROJECT_ROOT / filename
                count = _count_print_calls(_parse(path))
                if count > 0:
                    offenders.append((filename, count))
                self.assertEqual(
                    count, 0,
                    f"{filename} still contains {count} print() call(s) "
                    f"after M14.0-print-a — every print should have been "
                    f"converted to log.info / log.error.",
                )
        self.assertFalse(
            offenders,
            f"M14.0-print-a migrated files still contain print(): "
            f"{offenders}",
        )


# ---------------------------------------------------------------------------
# 2. Every migrated file imports + initialises the logger
# ---------------------------------------------------------------------------


class LoggerImportPresentPin(unittest.TestCase):
    """Each of the 9 files must (a) import `get_logger` from
    `structured_logging` AND (b) bind a module-level
    `log = get_logger(__name__)` (or `logger = ...`)."""

    def test_get_logger_imported_in_target_files(self):
        missing: list[str] = []
        for filename in M14_0_PRINT_A_FILES:
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
                    "'log = get_logger(__name__)' (or 'logger = ...')",
                )


# ---------------------------------------------------------------------------
# 3. All 9 files appear in MIGRATED_FILES
# ---------------------------------------------------------------------------


class MigratedFilesContainsAllNinePin(unittest.TestCase):
    """`tests/test_log_level_reclassification.MIGRATED_FILES` must
    contain all 9 files post-migration. If a future PR removes one
    of them, the M14.4 invariant test would silently stop counting
    its log calls — this pin catches that drift."""

    def test_all_nine_files_in_migrated_files(self):
        from tests.test_log_level_reclassification import (
            MIGRATED_FILES as REPO_MIGRATED_FILES,
        )

        for filename in M14_0_PRINT_A_FILES:
            with self.subTest(filename=filename):
                self.assertIn(
                    filename, REPO_MIGRATED_FILES,
                    f"{filename} not in MIGRATED_FILES — its log calls "
                    f"will not be counted by the M14.4 pin.",
                )


# ---------------------------------------------------------------------------
# 4. Pin value matches 298
# ---------------------------------------------------------------------------


class PinValueMatches298Pin(unittest.TestCase):
    """`EXPECTED_TOTAL_LOG_CALLS` must be 298 after M14.0-print-a.
    The lineage trace is:
        ... → 270 (M11.7a-2) → 270 (M13.1b-obs) → 298 (M14.0-print-a)

    This is a constant check; the actual count-vs-source cross-check
    is done by `TotalLogCallCountInvariant` in
    `test_log_level_reclassification.py`."""

    EXPECTED_PIN_VALUE = 298

    def test_expected_total_log_calls_is_298(self):
        from tests.test_log_level_reclassification import (
            EXPECTED_TOTAL_LOG_CALLS,
        )

        self.assertEqual(
            EXPECTED_TOTAL_LOG_CALLS, self.EXPECTED_PIN_VALUE,
            f"EXPECTED_TOTAL_LOG_CALLS = {EXPECTED_TOTAL_LOG_CALLS}, "
            f"expected {self.EXPECTED_PIN_VALUE} after M14.0-print-a. "
            "If a future milestone legitimately changes the count, "
            "update both this pin AND the lineage comment in "
            "tests/test_log_level_reclassification.py.",
        )


if __name__ == "__main__":
    unittest.main()
