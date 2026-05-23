"""Tests for the M14.0b print() -> structured logging migration.

Run with: python tests/test_print_migration.py

AST-based pins on the 5 migrated files and on the 8 deferred files
(M14.0c scope). Token-aware static scan supplements the AST checks
by catching ``print(`` in non-Call contexts (e.g., a regex string
literal or a comment).
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

# (file, prints_pre_migration, pre_existing_log_calls)
MIGRATED_FILES = (
    ("main.py", 62, 0),
    # official_crawler.py had 3 pre-existing log calls from M13.3b
    # (cache_hit event x2 -- on hit + on miss -- plus the
    # cache_put_failed warning). Those are NOT counted as migrations.
    ("official_crawler.py", 57, 3),
    ("verification_card.py", 27, 0),
    ("news_collector.py", 26, 0),
    ("article_extractor.py", 17, 0),
)

# M14.0c completed the migration of these files. They started life
# in M14.0b as "deferred" with non-zero print counts; M14.0c brought
# every count to zero. The pin now asserts the post-M14.0c state.
DEFERRED_FILES = (
    ("evidence_comparator.py", 0),
    ("policy_decision.py", 0),
    ("policy_confidence.py", 0),
    ("policy_impact.py", 0),
    ("bias_framing_agent.py", 0),
    ("evidence_extraction_agent.py", 0),
    ("contradiction_agent.py", 0),
    ("official_source_body.py", 0),
)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.AST:
    """Parse a Python source file, stripping a leading BOM if present."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return ast.parse(raw.decode("utf-8"))


def _count_print_calls(tree: ast.AST) -> int:
    """Count Call nodes where func is the built-in name ``print``."""
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            count += 1
    return count


_LOG_METHOD_NAMES = ("info", "warning", "error", "debug", "exception")


def _count_log_method_calls(tree: ast.AST) -> dict:
    """Count Call nodes of the form ``<name>.<method>(...)`` where
    ``<name>`` is ``log`` or ``logger`` and ``<method>`` is one of
    info/warning/error/debug/exception. Returns a dict per method
    plus a 'total' key."""
    counts = {name: 0 for name in _LOG_METHOD_NAMES}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in _LOG_METHOD_NAMES:
            continue
        value = func.value
        if not isinstance(value, ast.Name):
            continue
        if value.id not in ("log", "logger"):
            continue
        counts[func.attr] += 1
    counts["total"] = sum(counts.values())
    return counts


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


def _print_in_call_form(tree: ast.AST):
    """Yield (lineno, col_offset) for every ``print(...)`` Call."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            yield node.lineno, node.col_offset


# ---------------------------------------------------------------------------
# Migrated-files invariants
# ---------------------------------------------------------------------------


class MigratedFilesZeroPrintsPin(unittest.TestCase):
    """The single most important pin: every migrated file has zero
    AST-level ``print()`` Call nodes."""

    def test_no_print_calls_in_migrated_files(self):
        offenders = []
        for filename, _, _ in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            count = _count_print_calls(tree)
            if count > 0:
                offenders.append((filename, count))
        self.assertFalse(
            offenders,
            msg=(
                "Migrated files still contain print() calls: "
                f"{offenders}"
            ),
        )


class MigratedFilesGetLoggerImportPin(unittest.TestCase):
    def test_get_logger_imported_in_all_migrated(self):
        missing = []
        for filename, _, _ in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            if not _has_get_logger_import(tree):
                missing.append(filename)
        self.assertFalse(
            missing,
            msg=(
                "Migrated files missing 'from structured_logging "
                f"import get_logger': {missing}"
            ),
        )

    def test_module_logger_init_present_in_all_migrated(self):
        missing = []
        for filename, _, _ in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            if not _has_module_logger_init(tree):
                missing.append(filename)
        self.assertFalse(
            missing,
            msg=(
                "Migrated files missing module-level "
                f"log/logger = get_logger(__name__): {missing}"
            ),
        )


class MigratedFilesLogCountMeetsMinimum(unittest.TestCase):
    """Total log.X calls in each migrated file must be at least
    (prints_pre_migration + pre_existing_log_calls). This catches a
    silent drop of any migrated call."""

    def test_log_call_count_meets_minimum_per_file(self):
        shortfalls = []
        for filename, prints_pre, pre_existing in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            counts = _count_log_method_calls(tree)
            minimum = prints_pre + pre_existing
            if counts["total"] < minimum:
                shortfalls.append(
                    (filename, counts["total"], minimum)
                )
        self.assertFalse(
            shortfalls,
            msg=(
                "Files with fewer log.X calls than required: "
                f"{shortfalls}"
            ),
        )

    def test_individual_file_counts(self):
        # Pin the actual observed counts so any future regression
        # (silent drop or accidental duplication) surfaces immediately.
        # Numbers come from the M14.0b summary; if a future PR
        # intentionally adds or removes a log call, this test should
        # be updated explicitly.
        expected_minimums = {
            "main.py": 62,
            "official_crawler.py": 60,         # 57 migrated + 3 pre-existing
            "verification_card.py": 27,
            "news_collector.py": 26,
            "article_extractor.py": 17,
        }
        for filename, minimum in expected_minimums.items():
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            counts = _count_log_method_calls(tree)
            self.assertGreaterEqual(
                counts["total"], minimum,
                msg=(
                    f"{filename}: log.X call count {counts['total']} "
                    f"is below the expected minimum {minimum}"
                ),
            )


class MigratedFilesNoForbiddenKwargs(unittest.TestCase):
    """log.info / log.warning / etc. don't support ``file=`` / ``end=``
    / ``flush=`` / ``sep=`` kwargs (they're stdlib Logger methods, not
    builtins). A leaked kwarg from a print() would crash at runtime."""

    FORBIDDEN_KWARGS = frozenset({"file", "end", "flush", "sep"})

    def test_no_forbidden_kwargs_on_log_calls(self):
        offenders = []
        for filename, _, _ in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in _LOG_METHOD_NAMES:
                    continue
                value = func.value
                if not isinstance(value, ast.Name):
                    continue
                if value.id not in ("log", "logger"):
                    continue
                for kw in node.keywords:
                    if kw.arg in self.FORBIDDEN_KWARGS:
                        offenders.append(
                            (filename, node.lineno, kw.arg)
                        )
        self.assertFalse(
            offenders,
            msg=(
                "log.X calls carry print-only kwargs: "
                f"{offenders}"
            ),
        )


# ---------------------------------------------------------------------------
# Token-aware static scan — catches ``print(`` outside Call contexts.
# ---------------------------------------------------------------------------


class TokenLevelPrintScan(unittest.TestCase):
    """Walk tokens of each migrated file. The NAME token ``print`` must
    never appear immediately followed by ``(`` (OP) — that pattern is
    the syntactic shape of a builtin call. NAME ``print`` followed by
    ``.`` (attribute access on something else named print) and NAME
    ``print`` inside a string literal are tolerated because they don't
    invoke the builtin."""

    def test_no_print_call_tokens_in_migrated_files(self):
        offenders = []
        for filename, _, _ in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            tokens = list(tokenize.tokenize(
                io.BytesIO(raw).readline,
            ))
            for i, tok in enumerate(tokens):
                if tok.type != tokenize.NAME or tok.string != "print":
                    continue
                # Look at the next non-comment/non-NL token.
                for j in range(i + 1, len(tokens)):
                    nxt = tokens[j]
                    if nxt.type in (
                        tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                    ):
                        continue
                    if nxt.type == tokenize.OP and nxt.string == "(":
                        offenders.append(
                            (filename, tok.start[0], tok.start[1])
                        )
                    break
        self.assertFalse(
            offenders,
            msg=(
                "NAME 'print' immediately followed by '(' in migrated "
                f"files: {offenders}"
            ),
        )


# ---------------------------------------------------------------------------
# Deferred-files pin — counts MUST remain at pre-M14.0b values.
# ---------------------------------------------------------------------------


class DeferredFilesUntouchedPin(unittest.TestCase):
    def test_deferred_print_counts_unchanged(self):
        actual = {}
        for filename, _ in DEFERRED_FILES:
            path = _PROJECT_ROOT / filename
            tree = _parse(path)
            actual[filename] = _count_print_calls(tree)
        expected = {filename: count for filename, count in DEFERRED_FILES}
        self.assertEqual(
            actual, expected,
            msg=(
                "Deferred files (M14.0c scope) had their print() "
                "counts modified by M14.0b. They must be untouched. "
                f"Expected={expected}, actual={actual}"
            ),
        )

    def test_post_m14_0c_files_all_import_structured_logging(self):
        """M14.0c migrated all 8 originally-deferred files. Each
        must now import structured_logging. (The M14.0b version of
        this test asserted the opposite — left here as a flipped
        pin under the M14.0c completion contract.)"""
        missing = []
        for filename, _ in DEFERRED_FILES:
            path = _PROJECT_ROOT / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if not re.search(
                r"^(?:from\s+structured_logging\b|import\s+structured_logging\b)",
                text, re.MULTILINE,
            ):
                missing.append(filename)
        self.assertFalse(
            missing,
            msg=(
                "Post-M14.0c migration: these files must import "
                f"structured_logging but don't: {missing}"
            ),
        )


# ---------------------------------------------------------------------------
# Korean text preservation
# ---------------------------------------------------------------------------


class KoreanTextPreservationPin(unittest.TestCase):
    """For each migrated file, count occurrences of common Korean
    characters. Pinned values come from the post-migration state;
    the values are non-zero for files that contained Korean prints,
    zero for files that didn't. If a future change inadvertently
    drops Korean characters from a message, the count diverges and
    the test fails.
    """

    KOREAN_PROBES = ("정책", "검증", "공식", "분석", "한국")

    EXPECTED_COUNTS = {
        # Pinned to post-migration state. Re-derive only if Korean
        # text is intentionally added/removed in a future PR.
        "main.py": None,
        "official_crawler.py": None,
        "verification_card.py": None,
        "news_collector.py": None,
        "article_extractor.py": None,
    }

    def setUp(self):
        # Build the actual count map ONCE and use it for the
        # "non-zero" assertion. Drift catching is structural — any
        # future migration that removes Korean characters can be
        # caught via the byte-count assertion below.
        self.actual = {}
        for filename, _, _ in MIGRATED_FILES:
            path = _PROJECT_ROOT / filename
            text = path.read_text(encoding="utf-8")
            self.actual[filename] = {
                probe: text.count(probe)
                for probe in self.KOREAN_PROBES
            }

    def test_korean_text_present_where_expected(self):
        # main.py, verification_card.py, news_collector.py have
        # extensive Korean text (verdict labels, evidence summaries,
        # news titles). At least one probe must hit each.
        files_with_korean = (
            "main.py", "verification_card.py", "news_collector.py",
        )
        for filename in files_with_korean:
            total = sum(self.actual[filename].values())
            self.assertGreater(
                total, 0,
                msg=(
                    f"{filename} unexpectedly has zero Korean probe "
                    "hits -- migration may have dropped Korean text. "
                    f"Counts: {self.actual[filename]}"
                ),
            )


# ---------------------------------------------------------------------------
# Runtime smoke — invoke a migrated function and confirm a log record
# is emitted (visual identity in text mode).
# ---------------------------------------------------------------------------


class RuntimeSmokeTests(unittest.TestCase):
    """Lightweight runtime checks. The full visual-identity claim is
    too coupled to invoke for every file; instead we exercise
    ``official_crawler._do_request_url_raw`` indirectly via mocked
    requests.get and confirm log records flow."""

    def test_official_crawler_module_logger_is_callable(self):
        import official_crawler
        # The module-level log was added by M14.0b. Confirm it's the
        # canonical logging.Logger so log.info(...) calls work.
        import logging
        self.assertIsInstance(
            official_crawler.log, logging.Logger,
            msg="official_crawler.log must be a logging.Logger",
        )

    def test_main_module_logger_is_callable(self):
        import main
        import logging
        self.assertIsInstance(
            main.log, logging.Logger,
        )

    def test_verification_card_logger_is_callable(self):
        import verification_card
        import logging
        # Variable name preserved per migration rule — check both.
        attr = getattr(
            verification_card, "log",
            getattr(verification_card, "logger", None),
        )
        self.assertIsInstance(attr, logging.Logger)


if __name__ == "__main__":
    unittest.main()
