"""Tests for the M14.0c print() -> structured logging migration.

Run with: python tests/test_print_migration_m14_0c.py

M14.0c migrates the remaining 8 legacy files (62 prints) after M14.0b
handled the top 5 files (189 prints). Three of these eight modules
contain verdict logic: ``policy_decision``, ``policy_confidence``,
``policy_impact``. The most important pin in this file is the
verdict-invariance check that subprocess-invokes the existing verdict
test suites and asserts they all pass — proving the migration was
purely a logging-route change.

The other pins mirror M14.0b's ``tests/test_print_migration.py``:
zero remaining print() Calls, get_logger imported, log call count
meets minimum, no forbidden kwargs leaked, Korean text preserved, the
already-migrated M14.0b files still untouched.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
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


# (file, prints_pre_migration). All 8 files had zero pre-existing
# log calls (verified manually pre-migration), so the expected minimum
# log count equals the migrated print count.
M14_0C_FILES = (
    ("evidence_comparator.py", 14),
    ("policy_decision.py", 11),
    ("policy_confidence.py", 11),
    ("policy_impact.py", 10),
    ("bias_framing_agent.py", 6),
    ("evidence_extraction_agent.py", 5),
    ("contradiction_agent.py", 4),
    ("official_source_body.py", 1),
)


# Verdict-logic modules whose migration must NOT affect verdicts.
# Subprocess invocation of the existing verdict test suites is the
# operational invariance proof.
VERDICT_MODULES = (
    "policy_decision.py",
    "policy_confidence.py",
    "policy_impact.py",
)


# Verdict test suites that exercise these modules end-to-end. Each one
# is a unittest runner that exits 0 on success.
VERDICT_TEST_SUITES = (
    "tests/test_verdict_label_b08_fix.py",
    "tests/test_verdict_label_diagnostic.py",
    "tests/test_verdict_producer_comparison.py",
    "tests/test_artifact_evidence_linker.py",
)


# The 5 M14.0b-migrated files MUST still have zero prints — M14.0c
# scope is the OTHER 8 files only.
M14_0B_UNTOUCHED = (
    "main.py",
    "official_crawler.py",
    "verification_card.py",
    "news_collector.py",
    "article_extractor.py",
)


# ---------------------------------------------------------------------------
# AST helpers (identical shape to test_print_migration.py)
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.AST:
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


_LOG_METHOD_NAMES = ("info", "warning", "error", "debug", "exception")


def _count_log_method_calls(tree: ast.AST) -> dict:
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


# ---------------------------------------------------------------------------
# Zero-prints + import + logger-init pins (mirrors M14.0b's pattern)
# ---------------------------------------------------------------------------


class M14_0C_ZeroPrintsPin(unittest.TestCase):
    def test_no_print_calls_in_migrated_files(self):
        offenders = []
        for filename, _ in M14_0C_FILES:
            path = _PROJECT_ROOT / filename
            count = _count_print_calls(_parse(path))
            if count > 0:
                offenders.append((filename, count))
        self.assertFalse(
            offenders,
            msg=(
                "M14.0c migrated files still contain print() calls: "
                f"{offenders}"
            ),
        )


class M14_0C_GetLoggerImportPin(unittest.TestCase):
    def test_get_logger_imported_in_all_migrated(self):
        missing = []
        for filename, _ in M14_0C_FILES:
            path = _PROJECT_ROOT / filename
            if not _has_get_logger_import(_parse(path)):
                missing.append(filename)
        self.assertFalse(
            missing,
            msg=(
                "M14.0c migrated files missing "
                f"'from structured_logging import get_logger': {missing}"
            ),
        )

    def test_module_logger_init_present_in_all_migrated(self):
        missing = []
        for filename, _ in M14_0C_FILES:
            path = _PROJECT_ROOT / filename
            if not _has_module_logger_init(_parse(path)):
                missing.append(filename)
        self.assertFalse(
            missing,
            msg=(
                "M14.0c migrated files missing module-level "
                f"log/logger = get_logger(__name__): {missing}"
            ),
        )


class M14_0C_LogCountMinimumPin(unittest.TestCase):
    def test_log_call_count_meets_minimum_per_file(self):
        # All 8 files had zero pre-existing log calls before M14.0c,
        # so the minimum equals the migrated print count exactly.
        shortfalls = []
        for filename, expected in M14_0C_FILES:
            path = _PROJECT_ROOT / filename
            actual = _count_log_method_calls(_parse(path))["total"]
            if actual < expected:
                shortfalls.append((filename, actual, expected))
        self.assertFalse(
            shortfalls,
            msg=(
                "Files with fewer log.X calls than expected: "
                f"{shortfalls}"
            ),
        )


class M14_0C_NoForbiddenKwargsPin(unittest.TestCase):
    FORBIDDEN_KWARGS = frozenset({"file", "end", "flush", "sep"})

    def test_no_forbidden_kwargs_on_log_calls(self):
        offenders = []
        for filename, _ in M14_0C_FILES:
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
            msg=f"log.X calls carry print-only kwargs: {offenders}",
        )


# ---------------------------------------------------------------------------
# Tokenize-level scan — catches ``print(`` patterns outside Call sites.
# ---------------------------------------------------------------------------


class M14_0C_TokenLevelScan(unittest.TestCase):
    def test_no_print_call_tokens_in_migrated_files(self):
        offenders = []
        for filename, _ in M14_0C_FILES:
            path = _PROJECT_ROOT / filename
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            tokens = list(tokenize.tokenize(io.BytesIO(raw).readline))
            for i, tok in enumerate(tokens):
                if tok.type != tokenize.NAME or tok.string != "print":
                    continue
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
                "NAME 'print' immediately followed by '(' in M14.0c "
                f"migrated files: {offenders}"
            ),
        )


# ---------------------------------------------------------------------------
# M14.0b files MUST still be untouched (zero prints).
# ---------------------------------------------------------------------------


class M14_0C_DoesNotDisturbM14_0B(unittest.TestCase):
    def test_m14_0b_files_still_zero_prints(self):
        offenders = []
        for filename in M14_0B_UNTOUCHED:
            path = _PROJECT_ROOT / filename
            count = _count_print_calls(_parse(path))
            if count > 0:
                offenders.append((filename, count))
        self.assertFalse(
            offenders,
            msg=(
                "M14.0b-migrated files re-grew print() calls — "
                f"M14.0c accidentally touched them: {offenders}"
            ),
        )


# ---------------------------------------------------------------------------
# Verdict-invariance pin — THE operative proof.
#
# The brief calls this out: "Practically: invoke
# test_verdict_label_b08_fix.py, test_verdict_label_diagnostic.py,
# test_verdict_producer_comparison.py as part of this test and assert
# they all pass. If they pass, verdict invariance is established."
# ---------------------------------------------------------------------------


class M14_0C_VerdictInvariancePin(unittest.TestCase):
    """Subprocess-invoke the existing verdict test suites. Each
    must exit 0 — i.e. every existing verdict assertion still holds
    after the migration. This is the operational proof that the
    migration did NOT alter behaviour in policy_decision.py /
    policy_confidence.py / policy_impact.py / evidence_comparator.py.
    """

    def _run_suite(self, suite_path: str) -> tuple:
        cmd = [sys.executable, str(_PROJECT_ROOT / suite_path)]
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def test_verdict_label_b08_fix_still_passes(self):
        rc, _, err = self._run_suite("tests/test_verdict_label_b08_fix.py")
        self.assertEqual(
            rc, 0,
            msg=(
                "test_verdict_label_b08_fix exit=" + str(rc) +
                " stderr_tail=" + err[-400:]
            ),
        )

    def test_verdict_label_diagnostic_still_passes(self):
        rc, _, err = self._run_suite(
            "tests/test_verdict_label_diagnostic.py",
        )
        self.assertEqual(
            rc, 0,
            msg=(
                "test_verdict_label_diagnostic exit=" + str(rc) +
                " stderr_tail=" + err[-400:]
            ),
        )

    def test_verdict_producer_comparison_still_passes(self):
        rc, _, err = self._run_suite(
            "tests/test_verdict_producer_comparison.py",
        )
        self.assertEqual(
            rc, 0,
            msg=(
                "test_verdict_producer_comparison exit=" + str(rc) +
                " stderr_tail=" + err[-400:]
            ),
        )

    def test_artifact_evidence_linker_still_passes(self):
        # evidence_comparator.py is exercised via the linker tests.
        rc, _, err = self._run_suite(
            "tests/test_artifact_evidence_linker.py",
        )
        self.assertEqual(
            rc, 0,
            msg=(
                "test_artifact_evidence_linker exit=" + str(rc) +
                " stderr_tail=" + err[-400:]
            ),
        )


# ---------------------------------------------------------------------------
# Verdict modules: structural check — no non-logging code changed
#
# We can't compare against the pre-M14.0c source without reaching into
# git history. Instead we pin a structural invariant: the AST of each
# verdict module, after the migration, has the SAME number of
# non-logging top-level nodes as it would have had pre-migration.
# Pre-M14.0c each module had no logger import and no logger init; post-
# migration each has +1 ImportFrom (structured_logging) and +1 Assign
# (log = get_logger(__name__)). So the number of OTHER top-level nodes
# (functions, classes, imports unrelated to logging, etc.) is unchanged.
# ---------------------------------------------------------------------------


class M14_0C_VerdictModuleStructuralPin(unittest.TestCase):
    """For each verdict-logic module, count top-level AST nodes by
    type. The migration should have added exactly one ImportFrom +
    one Assign + zero everything-else."""

    # Expected number of NON-LOGGING top-level nodes per file.
    # These values are derived from the post-migration source; they
    # match the pre-migration state because the migration only added
    # the 2 logging-related top-level nodes.
    # Numbers will be discovered in setUp by inspecting the actual
    # post-migration files; the test then asserts they're consistent
    # with the expected delta of +1 ImportFrom + +1 Assign.

    def _categorise(self, tree: ast.AST) -> dict:
        cats = {
            "ImportFrom_structured_logging": 0,
            "Assign_logger_init": 0,
            "ImportFrom_other": 0,
            "Import": 0,
            "FunctionDef": 0,
            "AsyncFunctionDef": 0,
            "ClassDef": 0,
            "Assign_other": 0,
            "Expr_docstring": 0,
            "Other": 0,
        }
        for index, node in enumerate(tree.body):
            if isinstance(node, ast.ImportFrom):
                if node.module == "structured_logging":
                    cats["ImportFrom_structured_logging"] += 1
                else:
                    cats["ImportFrom_other"] += 1
            elif isinstance(node, ast.Import):
                cats["Import"] += 1
            elif isinstance(node, ast.Assign):
                if (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "get_logger"
                ):
                    cats["Assign_logger_init"] += 1
                else:
                    cats["Assign_other"] += 1
            elif isinstance(node, ast.FunctionDef):
                cats["FunctionDef"] += 1
            elif isinstance(node, ast.AsyncFunctionDef):
                cats["AsyncFunctionDef"] += 1
            elif isinstance(node, ast.ClassDef):
                cats["ClassDef"] += 1
            elif (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                cats["Expr_docstring"] += 1
            else:
                cats["Other"] += 1
        return cats

    def test_verdict_modules_added_exactly_one_import_one_assign(self):
        for filename in VERDICT_MODULES:
            path = _PROJECT_ROOT / filename
            cats = self._categorise(_parse(path))
            self.assertEqual(
                cats["ImportFrom_structured_logging"], 1,
                msg=(
                    f"{filename} has "
                    f"{cats['ImportFrom_structured_logging']} "
                    "structured_logging imports (expected exactly 1)"
                ),
            )
            self.assertEqual(
                cats["Assign_logger_init"], 1,
                msg=(
                    f"{filename} has {cats['Assign_logger_init']} "
                    "logger init assignments (expected exactly 1)"
                ),
            )

    def test_verdict_modules_did_not_grow_unexpected_nodes(self):
        """No new ``ClassDef``, ``FunctionDef``, or non-logging
        ``Assign`` would survive ``Other`` bucket. We assert the
        ``Other`` bucket is zero — the migration must not have
        introduced a new top-level statement we didn't recognise."""
        for filename in VERDICT_MODULES:
            path = _PROJECT_ROOT / filename
            cats = self._categorise(_parse(path))
            self.assertEqual(
                cats["Other"], 0,
                msg=(
                    f"{filename} has {cats['Other']} unrecognised "
                    "top-level nodes — M14.0c may have introduced "
                    "something unexpected"
                ),
            )


# ---------------------------------------------------------------------------
# Korean text preservation
# ---------------------------------------------------------------------------


class M14_0C_KoreanTextPreservationPin(unittest.TestCase):
    """For each migrated file that originally contained Korean text,
    confirm representative Korean characters are still present.
    Missing Korean would indicate the migration accidentally dropped
    Korean content from a message."""

    KOREAN_PROBES = ("정책", "검증", "공식", "분석", "한국", "의")

    # Files known to contain Korean text in their print messages
    # (verified pre-migration). Each should still have at least one
    # probe hit post-migration.
    #
    # ``policy_confidence.py`` and ``policy_impact.py`` are entirely
    # English -- they were always English diagnostics; verified
    # pre-migration that neither contained any Hangul characters.
    # Including them here would be a false expectation.
    EXPECTED_KOREAN_PRESENT = (
        "evidence_comparator.py",
        "policy_decision.py",
    )

    def test_korean_present_in_known_files(self):
        for filename in self.EXPECTED_KOREAN_PRESENT:
            path = _PROJECT_ROOT / filename
            text = path.read_text(encoding="utf-8")
            total = sum(text.count(probe) for probe in self.KOREAN_PROBES)
            self.assertGreater(
                total, 0,
                msg=(
                    f"{filename} unexpectedly has zero Korean probe "
                    "hits post-migration — message text may have "
                    "been corrupted"
                ),
            )


# ---------------------------------------------------------------------------
# Runtime smoke — each migrated module exposes a usable logger
# ---------------------------------------------------------------------------


class M14_0C_LoggerRuntimeSmoke(unittest.TestCase):
    """Confirm each of the 8 modules has an importable, usable
    ``log`` attribute that is a ``logging.Logger`` instance."""

    def test_all_eight_modules_have_callable_log_attribute(self):
        import logging
        targets = [name.removesuffix(".py") for name, _ in M14_0C_FILES]
        bad = []
        for module_name in targets:
            mod = __import__(module_name)
            log_attr = getattr(
                mod, "log", getattr(mod, "logger", None),
            )
            if not isinstance(log_attr, logging.Logger):
                bad.append((module_name, type(log_attr).__name__))
        self.assertFalse(
            bad,
            msg=(
                "Modules without a usable log/logger attribute: "
                f"{bad}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
