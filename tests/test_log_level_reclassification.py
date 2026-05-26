"""M14.4 — Log level reclassification pins.

After M14.0b/c's print() → logger migration, a too-greedy rule classified
several status-field reporting lines as log.error even though they were
not real errors. M14.4 reclassified the false positives to log.info.

This test pins the contract going forward:

1. Every remaining log.error in the 13 migrated files is EITHER inside an
   except block OR contains a real-error keyword. No bare field-name
   reporting like ``rendered_error: None`` may be log.error.

2. Known false-positive patterns (rendered_error, error_page_detected,
   error_page_reason, the bare ``error:`` reporter, the OfficialBody
   summary line) must be log.info, not log.error.

3. Known real-error patterns ("원문 URL 변환 실패", "Cache read failed",
   "Cache write failed", "Google RSS failed") must still be log.error.

4. The total log-call count across the 13 files is invariant — M14.4
   only shifts levels, it never adds or removes calls.

The forward-looking pin in :class:`NoFalsePositiveErrorsPin` will catch
any future migration that re-introduces a field-name log.error.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Iterable, Optional


ROOT = Path(__file__).resolve().parent.parent


MIGRATED_FILES: tuple[str, ...] = (
    "main.py",
    "official_crawler.py",
    "verification_card.py",
    "news_collector.py",
    "article_extractor.py",
    "evidence_comparator.py",
    "policy_decision.py",
    "policy_confidence.py",
    "policy_impact.py",
    "bias_framing_agent.py",
    "evidence_extraction_agent.py",
    "contradiction_agent.py",
    "official_source_body.py",
    # M14.0-print-a (2026-05-26) — 9 pipeline files newly migrated
    # from print() to structured logging. See lineage block below.
    "official_source_search.py",
    "memory_store.py",
    "source_reliability_agent.py",
    "worker.py",
    "source_retrieval_agent.py",
    "claim_extractor.py",
    "official_evidence_resolution.py",
    "claim_normalizer.py",
    "pipeline_debug.py",
    # M14.0-print-b (2026-05-26) — operational scripts migrated.
    # timeline.py runs inside analyze_pipeline (main.py:1260);
    # scheduler.py is operator-run CLI. Closes audit §1.5 #10.
    "timeline.py",
    "scheduler.py",
)


# Strong-error keywords — case-insensitive substring match on the
# message text exempts a log.error from the "must be in except" rule.
# These are the markers of *actual* failures: exception class names,
# Korean failure verbs, network failure verbs.
_STRONG_ERROR_KEYWORDS: tuple[str, ...] = (
    "fail",
    "failed",
    "exception",
    "timeout",
    "abort",
    "reset by peer",
    "refused",
    "traceback",
    "typeerror",
    "valueerror",
    "keyerror",
    "attributeerror",
    "connection aborted",
    "connection reset",
    "connection refused",
    "unable to",
    "could not",
    "cannot fetch",
    "실패",
    "예외",
    "오류 발생",
)


# Field-name patterns that must NEVER be log.error. These look like
# normal status reporting where "error" is a key name, not an actual
# error condition.
_FIELD_NAME_ERROR_RE = re.compile(r"^\s*(\w+_error|error_\w+)\s*[:=]")


# Known false-positive patterns that M14.4 reclassified. Each must
# appear in source under a log.info call (not log.error).
RECLASSIFIED_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (file, substring of message that uniquely identifies the line, log level expected)
    ("official_crawler.py", "rendered_error:", "info"),
    ("official_crawler.py", "error_page_detected:", "info"),
    ("official_crawler.py", "error_page_reason:", "info"),
    ("official_crawler.py", "  error: ", "info"),
    ("official_source_body.py", "[OfficialBody] ", "info"),
)


# Known real-error patterns that must REMAIN as log.error post-M14.4.
PRESERVED_REAL_ERRORS: tuple[tuple[str, str], ...] = (
    ("news_collector.py", "원문 URL 변환 실패"),
    ("news_collector.py", "[NewsCollector] Cache read failed"),
    ("news_collector.py", "[NewsCollector] Cache write failed"),
    ("news_collector.py", "Google RSS failed"),
    ("main.py", "[AnalysisCache] read failed"),
    ("main.py", "[AnalysisCache] write failed"),
)


# Total log call count across the 13 migrated files.
#
# Baseline lineage:
#   * M14.0c (after print() migration): 254
#   * M14.4 (reclassification only): 254 (invariant — only levels shifted)
#   * M13.3d (cache instrumentation added 2× log.info + 1× log.warning
#     in official_source_body and 2× log.info + 2× log.warning in
#     news_collector): 254 + 7 = 261
#   * M11.7a (Category 2 logging sweep added 1× log.warning in
#     official_crawler.fetch_best_official_document's outer except;
#     memory_store.py is NOT in MIGRATED_FILES so its +1 warning
#     does not count toward this pin): 261 + 1 = 262
#   * M11.0d-3a (Strategy C: disagreement_signal added 1× log.info
#     "verdict.disagreement_signal" in main.analyze_pipeline; main.py
#     IS in MIGRATED_FILES so this counts): 262 + 1 = 263
#   * M15.0d (parallel per-news-item processing added 2× log.info in
#     main.analyze_pipeline: one "M15.0d parallel phase start" and
#     one per-item "Phase A item complete"; main.py IS in
#     MIGRATED_FILES so both count): 263 + 2 = 265
#   * M13.1b (OpenAI LLM judge activation added 1× log.warning
#     "llm_judge.failed" in main._process_news_item_phase_a's
#     except-block around the judge invocation; main.py IS in
#     MIGRATED_FILES so this counts. The other M13.1b log call
#     "llm_judge.completed" lives in llm_judge.py which is NOT in
#     MIGRATED_FILES and so does not bump the pin): 265 + 1 = 266
#   * M11.7a-2 (Category 2 logging sweep — 7 audit cites mapped to
#     5 distinct code-sites; 4 new log.warning calls landed in
#     MIGRATED_FILES: Site 2 article_extractor.fetch_article_body
#     +1 "article_extractor.fetch_failed"; Sites 5b/5c/5d
#     official_crawler.py +3 — "site_specific_parser_failed",
#     "attempt_failed", "candidate_evaluation_failed". Site 3a
#     news_collector.resolve_google_news_url was a structured upgrade
#     of an existing log.error — same call with extra={} added — so
#     +0 to the count. Sites 1, 4, 5a, 5e are already resolved by
#     M11.7a / M11.7b / M11.5c.): 266 + 4 = 270
#   * M13.1b-obs (LLM judge + ai_reasoner operational observability —
#     adds llm_observability.py aggregator module (NEW file, NOT in
#     MIGRATED_FILES) + instruments ai_reasoner.py with 1 log.info
#     "ai_reasoner.completed" on success + 1 log.warning
#     "ai_reasoner.failed" on the broad-except path. ai_reasoner.py
#     is NOT in MIGRATED_FILES so its 2 new log calls do not count.
#     The new aggregator hook inside llm_judge._emit_cost_log adds
#     a record_llm_call() invocation but no new log call. Both
#     touched files are outside MIGRATED_FILES, so the pin stays
#     at 270): 270 + 0 = 270
#   * M14.0-print-a (pipeline print() → structured logging — 26 new
#     log calls across 9 files newly added to MIGRATED_FILES.
#     Per-file contribution to the +28 pin bump:
#       official_source_search.py     +8 new (was 0 existing; now 8)
#       memory_store.py               +6 new + 2 existing = 8
#         (existing = memory_store.load_corrupt_or_missing warning
#         + memory_store.save_tmp_cleanup_failed warning from M11.7a
#         / M12.2 — these were already emitted, just outside scope)
#       source_reliability_agent.py   +3 new (was 0; now 3)
#       worker.py                     +3 new (was 0; now 3) — 1 log.error
#         + 2 log.info
#       source_retrieval_agent.py     +2 new (was 0; now 2)
#       claim_extractor.py            +1 new (was 0; now 1)
#       official_evidence_resolution.py +1 new (was 0; now 1)
#       claim_normalizer.py           +1 new (was 0; now 1)
#       pipeline_debug.py             +1 new (was 0; now 1)
#     Total entering scope: 26 NEW + 2 EXISTING = 28: 270 + 28 = 298
#   * M14.0-print-b (operational scripts print() → structured logging
#     — 25 new log calls across 2 files newly added to MIGRATED_FILES.
#     timeline.py runs inside analyze_pipeline (main.py:1260) on every
#     Render request — its prior print() output bypassed the JSON
#     log aggregator. scheduler.py is operator-run CLI (render.yaml
#     never runs it). Per-file contribution to the +25 pin bump:
#       timeline.py     +13 new log.info (was 0 existing; now 13)
#       scheduler.py    +11 new log.info + 1 new log.error = 12
#                       (was 0 existing; now 12). The 1 log.error
#                       lives inside `except Exception as error:`
#                       so it's added to EXPECTED_EXCEPT_ERRORS
#                       below as well.
#     Closes audit §1.5 #10 (print-based logging) completely.
#     298 + 25 = 323
#
# Any future milestone that legitimately adds log calls bumps this
# expected count; the contract M14.4 actually pins is the *level
# distribution*, not the absolute count.
EXPECTED_TOTAL_LOG_CALLS = 323

# Post-M14.4: 12 (down from 17 pre-M14.4 — 5 reclassifications).
# M13.3d added log.info / log.warning calls only — no new log.error.
# M14.0-print-a (2026-05-26): worker.py joined MIGRATED_FILES with
# 1 log.error in `_fail` (worker startup failure path). The other
# 8 newly-migrated files added log.info / log.warning only. Bump
# from 12 → 13.
# M14.0-print-b (2026-05-26): scheduler.py joined MIGRATED_FILES
# with 1 log.error in `run_once`'s `except Exception as error:`
# block (per-query failure path). timeline.py added log.info only.
# Bump from 13 → 14.
EXPECTED_TOTAL_LOG_ERRORS = 14


def _read(filename: str) -> str:
    return (ROOT / filename).read_text(encoding="utf-8")


def _parse(filename: str) -> ast.AST:
    return ast.parse(_read(filename), filename=filename)


def _is_log_method_call(node: ast.AST, method: Optional[str] = None) -> bool:
    """Return True if node is a Call to ``log.<method>(...)``.

    If ``method`` is None, matches any of info/warning/error/debug.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    value = func.value
    if not isinstance(value, ast.Name) or value.id != "log":
        return False
    if method is None:
        return func.attr in ("info", "warning", "error", "debug")
    return func.attr == method


def _extract_call_message(node: ast.Call) -> str:
    """Best-effort extraction of the message argument's text.

    Handles:
      - plain string literal: log.error("foo")
      - f-string: log.error(f"foo {bar}") — returns concatenated literal parts
      - other expressions: returns ast.unparse(node.args[0]) for substring matching

    The result is what we'll grep for keywords; it is sufficient for the
    M14.4 contract because real-error keywords appear in the literal
    portions of f-strings (e.g., ``f"원문 URL 변환 실패: {error}"``).
    """
    if not node.args:
        return ""
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        # f-string — concatenate the literal portions.
        parts: list[str] = []
        for value in arg.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts)
    # Fallback: full unparse for non-literal expressions (e.g., a
    # concatenated string at module-scope). We still want to scan it
    # for substring markers.
    try:
        return ast.unparse(arg)
    except Exception:
        return ""


def _walk_with_parents(tree: ast.AST) -> Iterable[tuple[ast.AST, list[ast.AST]]]:
    """Yield (node, ancestor_stack) pairs, where ancestor_stack[0] is the
    immediate parent and ancestor_stack[-1] is the Module."""

    def _walk(node: ast.AST, stack: list[ast.AST]):
        yield node, list(stack)
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            yield from _walk(child, stack)
        stack.pop()

    yield from _walk(tree, [])


def _is_inside_except(ancestor_stack: list[ast.AST]) -> bool:
    """True if any ancestor is an ExceptHandler. The except-block rule
    wins over any text-based classification (per M14.4 brief)."""
    return any(isinstance(ancestor, ast.ExceptHandler) for ancestor in ancestor_stack)


def _message_has_strong_error_keyword(message: str) -> bool:
    lower = message.lower()
    return any(keyword.lower() in lower for keyword in _STRONG_ERROR_KEYWORDS)


def _message_is_error_label_prefix(message: str) -> bool:
    """Exception-formatted messages often start with ``Error:``."""
    return message.lstrip().startswith("Error:")


def _collect_log_error_calls(filename: str) -> list[tuple[ast.Call, list[ast.AST]]]:
    tree = _parse(filename)
    found: list[tuple[ast.Call, list[ast.AST]]] = []
    for node, stack in _walk_with_parents(tree):
        if _is_log_method_call(node, "error"):
            assert isinstance(node, ast.Call)
            found.append((node, stack))
    return found


def _collect_all_log_calls(filename: str) -> list[tuple[ast.Call, str]]:
    """Return (Call, method) for every log.X call in the file."""
    tree = _parse(filename)
    found: list[tuple[ast.Call, str]] = []
    for node in ast.walk(tree):
        if _is_log_method_call(node, None):
            assert isinstance(node, ast.Call)
            assert isinstance(node.func, ast.Attribute)
            found.append((node, node.func.attr))
    return found


class NoFalsePositiveErrorsPin(unittest.TestCase):
    """Forward-looking detection pin. Every log.error in the 13 migrated
    files must satisfy at least one of:

      1. The call is inside an except block.
      2. The message contains a strong-error keyword.
      3. The message literally starts with "Error:" (CPython exception
         repr convention).

    A future migration mistake that re-introduces a field-name log.error
    (e.g., ``log.error(f"  rendered_error: {x}")``) will fail this pin.
    """

    def test_no_false_positive_errors(self):
        offenders: list[str] = []
        for filename in MIGRATED_FILES:
            for call, stack in _collect_log_error_calls(filename):
                message = _extract_call_message(call)
                inside_except = _is_inside_except(stack)
                has_keyword = _message_has_strong_error_keyword(message)
                has_label = _message_is_error_label_prefix(message)
                if not (inside_except or has_keyword or has_label):
                    offenders.append(
                        f"{filename}:{call.lineno}  log.error({message!r}) "
                        "— not inside except and no strong-error keyword. "
                        "Reclassify to log.info or add to the keyword "
                        "list if it is a real error."
                    )
        if offenders:
            self.fail(
                "M14.4 detection pin: false-positive log.error calls found:\n"
                + "\n".join(offenders)
            )


class FieldNameErrorPatternPin(unittest.TestCase):
    """No log.error call may have a message matching the ``\\w+_error:``
    or ``error_\\w+:`` field-name regex. These are field-name reporters,
    not error events.

    This is a stricter, more targeted check than the strong-error
    keyword pin: it specifically guards against the Render log examples
    that motivated M14.4.
    """

    def test_no_field_name_error_patterns(self):
        offenders: list[str] = []
        for filename in MIGRATED_FILES:
            for call, _stack in _collect_log_error_calls(filename):
                message = _extract_call_message(call)
                if _FIELD_NAME_ERROR_RE.match(message):
                    offenders.append(
                        f"{filename}:{call.lineno}  log.error({message!r}) "
                        "— message matches field-name regex "
                        "^\\s*(\\w+_error|error_\\w+):"
                    )
        if offenders:
            self.fail(
                "M14.4 field-name pin: field-name reporters logged as ERROR:\n"
                + "\n".join(offenders)
            )


class ReclassifiedPatternsArInfoNow(unittest.TestCase):
    """Each known false-positive pattern that M14.4 reclassified must
    now appear as log.info (not log.error) in the source.

    This pin protects against an accidental revert that re-tags one
    of these lines as ERROR.
    """

    def test_reclassified_patterns_use_info(self):
        unresolved: list[str] = []
        for filename, marker, expected_level in RECLASSIFIED_PATTERNS:
            calls = _collect_all_log_calls(filename)
            found = False
            for call, level in calls:
                message = _extract_call_message(call)
                if marker in message:
                    found = True
                    if level != expected_level:
                        unresolved.append(
                            f"{filename}: pattern {marker!r} is "
                            f"log.{level} but should be log.{expected_level}"
                        )
                    break
            if not found:
                unresolved.append(
                    f"{filename}: pattern {marker!r} not found in any "
                    f"log call (was the message text changed?)"
                )
        if unresolved:
            self.fail("M14.4 reclassification pin:\n" + "\n".join(unresolved))


class PreservedRealErrorsStillErrorPin(unittest.TestCase):
    """Each known real-error pattern must still be log.error post-M14.4.
    These were not touched by M14.4; this pin protects against an
    over-broad future "silence the noise" pass.
    """

    def test_real_errors_still_logged_as_error(self):
        regressions: list[str] = []
        for filename, marker in PRESERVED_REAL_ERRORS:
            calls = _collect_all_log_calls(filename)
            found = False
            for call, level in calls:
                message = _extract_call_message(call)
                if marker in message:
                    found = True
                    if level != "error":
                        regressions.append(
                            f"{filename}: real-error pattern {marker!r} "
                            f"is now log.{level} (must remain log.error)"
                        )
                    break
            if not found:
                regressions.append(
                    f"{filename}: real-error marker {marker!r} not found "
                    "— message text was changed (forbidden by M14.4)"
                )
        if regressions:
            self.fail(
                "M14.4 real-error preservation pin:\n" + "\n".join(regressions)
            )


class TotalLogCallCountInvariant(unittest.TestCase):
    """M14.4 must not add or remove any log call. Pin the total."""

    def test_total_log_call_count_unchanged(self):
        total = 0
        per_file: dict[str, int] = {}
        for filename in MIGRATED_FILES:
            calls = _collect_all_log_calls(filename)
            per_file[filename] = len(calls)
            total += len(calls)
        self.assertEqual(
            total,
            EXPECTED_TOTAL_LOG_CALLS,
            f"M14.4 invariant violated: total log calls is {total} "
            f"but must be {EXPECTED_TOTAL_LOG_CALLS}. M14.4 reclassifies, "
            f"it does not add/remove calls. Per-file breakdown: {per_file!r}",
        )

    def test_total_log_error_count_post_m14_4(self):
        total_errors = 0
        for filename in MIGRATED_FILES:
            for _call, level in _collect_all_log_calls(filename):
                if level == "error":
                    total_errors += 1
        self.assertEqual(
            total_errors,
            EXPECTED_TOTAL_LOG_ERRORS,
            f"Post-M14.4 log.error count is {total_errors}, expected "
            f"{EXPECTED_TOTAL_LOG_ERRORS}. If you intentionally moved "
            f"another call's level, update EXPECTED_TOTAL_LOG_ERRORS.",
        )


class ExceptBlockErrorsPreserved(unittest.TestCase):
    """Every log.error inside an except block in the 13 files must stay
    log.error. M14.4 must not silence except-block diagnostics.

    We pin the per-file count of "log.error inside except".
    """

    # Pre- and post-M14.4 this is unchanged: M14.4 only modified
    # log.error calls that were NOT inside except blocks.
    EXPECTED_EXCEPT_ERRORS: dict[str, int] = {
        "main.py": 2,                # 2 cache except blocks
        "news_collector.py": 3,      # 2 cache except blocks + URL decoder except
        "article_extractor.py": 6,   # all 6 inside extract except block
        # M14.0-print-b: scheduler.run_once catches per-query failures
        # in `except Exception as error:` and logs via log.error.
        "scheduler.py": 1,
    }

    def test_except_block_errors_pinned(self):
        actual: dict[str, int] = {}
        for filename in MIGRATED_FILES:
            count = 0
            for _call, stack in _collect_log_error_calls(filename):
                if _is_inside_except(stack):
                    count += 1
            if count:
                actual[filename] = count
        self.assertEqual(
            actual,
            self.EXPECTED_EXCEPT_ERRORS,
            "M14.4 except-block preservation pin: counts of "
            "log.error inside except blocks differ from expected. "
            "Did a real-error log get silenced?",
        )


class SmokeOfficialCrawlerStatusLines(unittest.TestCase):
    """Direct smoke pin for the four official_crawler.py lines named in
    the M14.4 brief. These were the Render production smoking-gun
    examples. Each one must be log.info now.
    """

    def test_official_crawler_status_lines_are_info(self):
        text = _read("official_crawler.py")
        for marker in (
            "rendered_error:",
            "error_page_detected:",
            "error_page_reason:",
        ):
            # The line should appear once, and on a log.info, not log.error.
            self.assertIn(
                f'log.info(f"  {marker}',
                text,
                f"official_crawler.py: expected log.info for {marker!r}",
            )
            self.assertNotIn(
                f'log.error(f"  {marker}',
                text,
                f"official_crawler.py: still log.error for {marker!r} "
                "— M14.4 reclassification regressed",
            )
        # The bare ``error:`` field has slightly different quoting.
        self.assertIn(
            'log.info(f"  error: {result.get(\'error\')}',
            text,
            "official_crawler.py: expected log.info for bare 'error:' field",
        )
        self.assertNotIn(
            'log.error(f"  error: {result.get(\'error\')}',
            text,
            "official_crawler.py: still log.error for bare 'error:' field",
        )

    def test_official_source_body_summary_is_info(self):
        text = _read("official_source_body.py")
        # The [OfficialBody] summary line crosses multiple lines, so
        # we just confirm the log.error variant is no longer present
        # adjacent to the marker.
        marker = "[OfficialBody] "
        idx = text.find(marker)
        self.assertGreaterEqual(
            idx, 0, "[OfficialBody] summary marker not found in source"
        )
        # Find the log call wrapping this string.
        # The "log.error(" or "log.info(" appears before the marker.
        before = text[:idx]
        last_error = before.rfind("log.error(")
        last_info = before.rfind("log.info(")
        self.assertGreater(
            last_info,
            last_error,
            "[OfficialBody] summary line: nearest preceding log call is "
            "log.error, but M14.4 requires log.info",
        )


if __name__ == "__main__":
    unittest.main()
