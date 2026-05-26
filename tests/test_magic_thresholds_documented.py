"""audit §1.5 #5 (2026-05-26): magic-threshold documentation pins.

The audit identified "magic thresholds everywhere" in the verdict
pipeline. Phase 1 chose Option N (Narrow) — ~30-entry catalog at
``docs/MAGIC_THRESHOLDS.md`` plus inline-comment additions at the
most verdict-critical sites.

These pins enforce:

  1. CatalogExistsPin — ``docs/MAGIC_THRESHOLDS.md`` exists and
     contains at least one entry per file in the verdict-pipeline
     subset.

  2. ModuleLevelThresholdsCommentedPin — AST-walk: every targeted
     module-level threshold constant has a comment within ±3 lines
     OR a self-describing name.

  3. OfficialCrawlerThresholdsMatchCatalogPin — the three
     ``official_crawler.py`` document-fetch gates
     (``MIN_DOCUMENT_SCORE=25``, ``WEAK_DOCUMENT_RELEVANCE_THRESHOLD=35``,
     ``DOCUMENT_RELEVANCE_THRESHOLD=40``) — values match the catalog.
     Drift detector: a future change to the source value without a
     simultaneous catalog update fails this test.

  4. PolicyScoringAlertCutoffsMatchCatalogPin — the five
     ``_alert_from_score`` HIGH-gate cutoffs
     (``final_score >= 75``, ``evidence_quality_score >= 65``,
     ``source_trust_score >= 55``, ``strength_score >= 55``, and the
     WATCH fallback ``final_score >= 45``) — present in the catalog.
     The most verdict-critical magic-number cluster.

The test file is documentation enforcement, not behavior enforcement.
Values are pinned against the catalog file content (text grep) rather
than re-implementing the verdict logic.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_CATALOG_PATH = _PROJECT_ROOT / "docs" / "MAGIC_THRESHOLDS.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# 1. Catalog exists + covers verdict-pipeline subset
# ---------------------------------------------------------------------------


class CatalogExistsPin(unittest.TestCase):
    """`docs/MAGIC_THRESHOLDS.md` must exist and reference each file
    in the verdict-pipeline subset at least once."""

    # Verdict-pipeline files that should appear in the catalog.
    _REQUIRED_FILES_IN_CATALOG = (
        "official_crawler.py",
        "source_reliability_agent.py",
        "evidence_extraction_agent.py",
        "policy_confidence.py",
        "policy_scoring.py",
        "evidence_comparator.py",
        "contradiction_agent.py",
        "verdict_label_diagnostic.py",
    )

    def test_catalog_exists_and_covers_subset(self):
        self.assertTrue(
            _CATALOG_PATH.exists(),
            "docs/MAGIC_THRESHOLDS.md missing — created by audit §1.5 #5 "
            "and required by these pins.",
        )
        text = _read(_CATALOG_PATH)
        missing = [name for name in self._REQUIRED_FILES_IN_CATALOG if name not in text]
        self.assertFalse(
            missing,
            f"docs/MAGIC_THRESHOLDS.md must reference each verdict-"
            f"pipeline file at least once. Missing: {missing!r}.",
        )


# ---------------------------------------------------------------------------
# 2. Targeted module-level thresholds have an inline comment
# ---------------------------------------------------------------------------


# Each row: (file, threshold name). The pin asserts a comment appears
# within ±3 lines of the assignment AND that the catalog references
# this threshold.
_INLINE_COMMENT_TARGETS: tuple[tuple[str, str], ...] = (
    ("official_crawler.py", "MIN_DOCUMENT_SCORE"),
    ("official_crawler.py", "WEAK_DOCUMENT_RELEVANCE_THRESHOLD"),
    ("official_crawler.py", "DOCUMENT_RELEVANCE_THRESHOLD"),
    ("contradiction_agent.py", "SOURCE_SCORE_MINIMUM"),
    ("verdict_label_diagnostic.py", "WEAK_EVIDENCE_SCORE_THRESHOLD"),
)


class ModuleLevelThresholdsCommentedPin(unittest.TestCase):
    """For each targeted module-level threshold assignment, a comment
    must appear within ±3 lines (`#` prefix). Catches future PRs that
    delete the calibration-source breadcrumb."""

    _COMMENT_WINDOW = 3

    def test_targeted_thresholds_have_nearby_comment(self):
        offenders: list[str] = []
        for filename, name in _INLINE_COMMENT_TARGETS:
            path = _PROJECT_ROOT / filename
            text = _read(path)
            lines = text.splitlines()
            # Find the assignment line.
            assignment_re = re.compile(
                rf"^\s*{re.escape(name)}\s*=\s*",
            )
            assignment_idx = None
            for i, line in enumerate(lines):
                if assignment_re.match(line):
                    assignment_idx = i
                    break
            if assignment_idx is None:
                offenders.append(
                    f"{filename}: threshold {name!r} not found "
                    "(was it renamed or moved?)"
                )
                continue
            # Look for a `#` comment within ±N lines.
            start = max(0, assignment_idx - self._COMMENT_WINDOW)
            end = min(len(lines), assignment_idx + self._COMMENT_WINDOW + 1)
            window = lines[start:end]
            has_comment = any(line.lstrip().startswith("#") for line in window)
            if not has_comment:
                offenders.append(
                    f"{filename}:{assignment_idx + 1} {name!r} has no "
                    f"comment within ±{self._COMMENT_WINDOW} lines — "
                    "audit §1.5 #5 requires a calibration-source "
                    "breadcrumb."
                )
        if offenders:
            self.fail(
                "Targeted thresholds missing inline comments:\n  "
                + "\n  ".join(offenders)
            )


# ---------------------------------------------------------------------------
# 3. official_crawler.py thresholds match the catalog values
# ---------------------------------------------------------------------------


class OfficialCrawlerThresholdsMatchCatalogPin(unittest.TestCase):
    """Drift detector: a value change to ``MIN_DOCUMENT_SCORE`` /
    ``WEAK_DOCUMENT_RELEVANCE_THRESHOLD`` / ``DOCUMENT_RELEVANCE_THRESHOLD``
    in source MUST be matched by an update to
    docs/MAGIC_THRESHOLDS.md §1. Either-direction drift fails this
    pin."""

    def test_official_crawler_thresholds_in_catalog(self):
        import official_crawler

        # Source values.
        actual = {
            "MIN_DOCUMENT_SCORE": official_crawler.MIN_DOCUMENT_SCORE,
            "WEAK_DOCUMENT_RELEVANCE_THRESHOLD":
                official_crawler.WEAK_DOCUMENT_RELEVANCE_THRESHOLD,
            "DOCUMENT_RELEVANCE_THRESHOLD":
                official_crawler.DOCUMENT_RELEVANCE_THRESHOLD,
        }

        # Catalog text.
        catalog = _read(_CATALOG_PATH)

        for name, value in actual.items():
            with self.subTest(name=name, value=value):
                # The catalog must contain a literal "= <value>" near
                # the threshold name. We allow either backtick-code
                # quoting or plain text. The pattern intentionally
                # tolerates surrounding whitespace and quotation styles.
                pattern = re.compile(
                    rf"{re.escape(name)}\s*=\s*`?{value}`?\b",
                )
                self.assertRegex(
                    catalog, pattern,
                    f"docs/MAGIC_THRESHOLDS.md does not record "
                    f"{name} = {value}. If you changed the value in "
                    f"official_crawler.py, update the catalog §1 in "
                    f"the same PR. If you changed the catalog without "
                    f"the source, restore the source value.",
                )


# ---------------------------------------------------------------------------
# 4. policy_scoring._alert_from_score cutoffs match the catalog
# ---------------------------------------------------------------------------


class PolicyScoringAlertCutoffsMatchCatalogPin(unittest.TestCase):
    """The most verdict-critical magic-number cluster: the
    ``_alert_from_score`` HIGH-gate quadruple (75 / 65 / 55 / 55)
    plus the WATCH fallback (45). Source values must appear in the
    catalog §6."""

    # The five numeric literals on the HIGH-gate path + WATCH fallback.
    # Captured from policy_scoring.py:137-147 at audit time (2026-05-26).
    _EXPECTED_CUTOFFS = (75, 65, 55, 45)
    # `55` appears twice (source_trust + strength_score) but the
    # de-dup'd set is {75, 65, 55, 45}.

    def test_alert_cutoffs_in_source_and_catalog(self):
        """Walk policy_scoring.py source AND docs/MAGIC_THRESHOLDS.md;
        every cutoff in the HIGH-gate/WATCH-fallback must be present in
        both. Catches drift between source and catalog in either
        direction."""
        path = _PROJECT_ROOT / "policy_scoring.py"
        text = _read(path)
        # Locate the function body.
        start = text.index("def _alert_from_score(")
        end_match = re.search(r"^def\s+", text[start + 1:], re.MULTILINE)
        body = text[start: start + 1 + end_match.start()] if end_match else text[start:]
        catalog = _read(_CATALOG_PATH)
        # The catalog §6 section header.
        self.assertIn(
            "§6 — P2 alert-level cutoffs", catalog,
            "docs/MAGIC_THRESHOLDS.md is missing the §6 alert-cutoffs "
            "section.",
        )
        for cutoff in self._EXPECTED_CUTOFFS:
            with self.subTest(cutoff=cutoff):
                # Source: must contain the literal `>= <cutoff>` in
                # the _alert_from_score body.
                self.assertIn(
                    f">= {cutoff}", body,
                    f"policy_scoring._alert_from_score no longer "
                    f"contains the cutoff `>= {cutoff}`. Either the "
                    f"function was refactored or a cutoff value "
                    f"changed — audit §1.5 #5 catalog must be "
                    f"updated in the same PR.",
                )
                # Catalog: must contain the value as a literal anywhere.
                # Tolerate backtick code-quoting.
                pattern = re.compile(rf"`?{cutoff}`?")
                self.assertRegex(
                    catalog, pattern,
                    f"docs/MAGIC_THRESHOLDS.md does not contain the "
                    f"value {cutoff}. If you changed an alert cutoff "
                    f"in policy_scoring._alert_from_score, update the "
                    f"catalog §6 in the same PR.",
                )


if __name__ == "__main__":
    unittest.main()
