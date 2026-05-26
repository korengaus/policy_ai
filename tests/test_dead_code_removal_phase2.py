"""audit §1.5 #6 + #7 re-audit (2026-05-26): dead-code confirmation pins.

The audit cited:

  #6 — Broken encoding sentinels in ``official_crawler.py`` at L815/938
      ("?먮윭?섏씠吏", "?癒?쑎??륁뵠筌왖") that would never match anything.
      Fully resolved by M11.6 (commit 5d5e1824a). Existing pin in
      ``tests/test_mojibake_cleanup.py`` covers official_crawler.py
      specifically. This file generalises that scan to the whole
      repo as a forward-looking guard.

  #7 — Three dead-code paths:
      (a) ``evidence_comparator._make_summary`` duplicated
          ``excluded_non_policy_page`` branch at L362-381 vs L383-397
      (b) ``evidence_extraction_agent.extract_evidence_snippets``
          double-build of ``claim_evidence_map`` at L528 vs L539-542
      (c) ``source_retrieval_agent.OFFICIAL_DOMAIN_QUERY_HINTS`` —
          unused site: operators

All three #7 audit cites turned out to be STALE — the current code
no longer matches the audit's claim. These pins codify the
"audit-was-wrong" finding so a future re-audit catches genuine
regression rather than re-flagging the same stale cite.

(d) is the audit's ``index.html`` legacy functions item — explicitly
out of scope (Phase 5 frontend migration deferred).
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _read(filename: str) -> str:
    return (_PROJECT_ROOT / filename).read_text(encoding="utf-8-sig")


def _iter_repo_root_python_files() -> list[Path]:
    """Repo-root *.py files only. Matches the scope of
    tests/test_no_duplicate_definitions.py."""
    return sorted(
        path
        for path in _PROJECT_ROOT.glob("*.py")
        if path.is_file()
    )


# ---------------------------------------------------------------------------
# 1. ITEM B (re-audit): codebase-wide mojibake-absence pin
# ---------------------------------------------------------------------------


class NoResidualMojibakePin(unittest.TestCase):
    """Generalises ``tests/test_mojibake_cleanup.py``'s
    ``official_crawler.py``-only scan to every repo-root ``*.py``
    file. The fingerprint is the classic CP949→UTF-8 misdecode
    pattern: literal ``?`` immediately followed by a Hangul syllable
    inside a quoted string.

    Files that LEGITIMATELY contain mojibake markers as detection
    targets (``korean_constants.py``, ``text_utils.py``,
    ``article_extractor.py``) are whitelisted because they define
    the markers used to DETECT mojibake elsewhere — they must
    themselves contain the byte signatures for the detector to
    work. The whitelist is narrow to keep the guard meaningful."""

    _WHITELIST: frozenset[str] = frozenset({
        "korean_constants.py",
        "text_utils.py",
        "article_extractor.py",
    })

    def test_no_residual_mojibake_in_codebase(self):
        offenders: list[str] = []
        for path in _iter_repo_root_python_files():
            if path.name in self._WHITELIST:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except Exception as exc:
                self.fail(f"{path.name} could not be decoded as UTF-8: {exc}")

            # Walk character by character. The fingerprint is `?<Hangul>`
            # inside a string literal. False-positive sources we filter:
            #   * Comment lines (may discuss mojibake without containing it)
            #   * Raw-string regex literals (``r"..."`` / ``r'...'``) —
            #     `?` is regex syntax (`(?:...)?`, `[.!?...]`, `*?`, etc.)
            #   * `?` preceded by a regex-special char on the same line
            #     (covers non-raw regex strings used by `re.compile`)
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                # Skip lines that contain a raw-string literal — these
                # are regex patterns where `?` is syntax, not mojibake.
                if re.search(r"\br[\"']", line):
                    continue
                for i in range(len(line) - 1):
                    if line[i] != "?":
                        continue
                    nxt = line[i + 1]
                    if not ("가" <= nxt <= "힣"):
                        continue
                    # Skip regex syntax: `(?` or `]?` or `*?` / `+?` /
                    # `??` or escaped `\?`.
                    if i > 0 and line[i - 1] in "(]*+?\\":
                        continue
                    # Is the `?` inside a string literal?
                    before = line[:i]
                    single = before.count("'") - before.count("\\'")
                    double = before.count('"') - before.count('\\"')
                    if single % 2 == 1 or double % 2 == 1:
                        snippet = line[max(0, i - 20): i + 30]
                        offenders.append(
                            f"{path.name}:{lineno} `?<Hangul>` in string "
                            f"literal: ...{snippet!r}..."
                        )

        if offenders:
            self.fail(
                "Suspected mojibake `?<Hangul>` pattern(s) in string "
                "literals (audit §1.5 #6 fingerprint). M11.6 already "
                "cleaned official_crawler.py; new findings here mean a "
                "fresh copy-paste from a wrongly-encoded source.\n  "
                + "\n  ".join(offenders)
            )


# ---------------------------------------------------------------------------
# 2. ITEM C(a) re-audit: evidence_comparator._make_summary uniqueness
# ---------------------------------------------------------------------------


class EvidenceComparatorMakeSummaryPin(unittest.TestCase):
    """Audit §1.5 #7 (a) claimed
    ``evidence_comparator._make_summary`` had a duplicated
    ``excluded_non_policy_page`` branch at L362-381 vs L383-397. The
    re-audit found this is a STALE cite — the function contains the
    branch exactly once.

    This pin asserts the single-branch state remains."""

    def test_excluded_non_policy_branch_appears_once_in_make_summary(self):
        text = _read("evidence_comparator.py")
        # Locate the _make_summary function body.
        start = text.index("def _make_summary(")
        end_match = re.search(r"^def\s+", text[start + 1:], re.MULTILINE)
        body = text[start: start + 1 + end_match.start()] if end_match else text[start:]
        # Count `if verification_level == "excluded_non_policy_page":`.
        pattern = re.compile(
            r'if\s+verification_level\s*==\s*"excluded_non_policy_page"\s*:',
        )
        matches = pattern.findall(body)
        self.assertEqual(
            len(matches), 1,
            f"evidence_comparator._make_summary must contain the "
            f"`excluded_non_policy_page` branch exactly once. Found "
            f"{len(matches)}. If the audit §1.5 #7 (a) regression has "
            f"returned, restore the single-branch state.",
        )


# ---------------------------------------------------------------------------
# 3. ITEM C(b) re-audit: evidence_extraction_agent claim_evidence_map single build
# ---------------------------------------------------------------------------


class EvidenceExtractionAgentClaimMapPin(unittest.TestCase):
    """Audit §1.5 #7 (b) claimed
    ``evidence_extraction_agent.extract_evidence_snippets`` built
    ``claim_evidence_map`` twice (L528 then overwritten at L539-542).
    The re-audit found the map is built EXACTLY once at L540-543.
    Pin the single-build state."""

    def test_claim_evidence_map_assigned_once(self):
        text = _read("evidence_extraction_agent.py")
        # `claim_evidence_map = {}` is the literal-empty-dict assignment.
        # Allow optional whitespace inside the braces.
        pattern = re.compile(r"^\s*claim_evidence_map\s*=\s*\{\s*\}\s*$", re.MULTILINE)
        matches = pattern.findall(text)
        self.assertEqual(
            len(matches), 1,
            f"evidence_extraction_agent must contain exactly one "
            f"`claim_evidence_map = {{}}` literal assignment. Found "
            f"{len(matches)}. If the audit §1.5 #7 (b) regression has "
            f"returned, restore the single-build state.",
        )


# ---------------------------------------------------------------------------
# 4. ITEM C(c) re-audit: source_retrieval_agent OFFICIAL_DOMAIN_QUERY_HINTS usage
# ---------------------------------------------------------------------------


class OfficialDomainQueryHintsIsUsedPin(unittest.TestCase):
    """Audit §1.5 #7 (c) claimed
    ``source_retrieval_agent.OFFICIAL_DOMAIN_QUERY_HINTS`` was an
    unused module-level constant emitting ``site:`` operators that
    no Google query was ever issued for. The re-audit found it IS
    used at L177 in ``_official_site_query`` which is called by
    ``generate_source_queries`` at L243.

    This pin asserts there is at least one non-definition use-site
    in the source file. Catches a future "cleanup" PR that deletes
    the use sites without deleting the constant (which would silently
    re-make the audit's claim true)."""

    def test_official_domain_query_hints_has_use_site(self):
        text = _read("source_retrieval_agent.py")
        # Definition line: `OFFICIAL_DOMAIN_QUERY_HINTS = {` (or `dict(...)`)
        # Use sites: any other occurrence of the name.
        all_occurrences = re.findall(
            r"\bOFFICIAL_DOMAIN_QUERY_HINTS\b", text,
        )
        # Definition site counts as ONE occurrence; if there are not
        # MORE than one, the constant is defined but never read.
        self.assertGreater(
            len(all_occurrences), 1,
            f"source_retrieval_agent.OFFICIAL_DOMAIN_QUERY_HINTS appears "
            f"only {len(all_occurrences)} time(s) — definition only, no "
            f"use site. The audit §1.5 #7 (c) regression has returned; "
            f"either restore the call site in _official_site_query or "
            f"delete the constant definition.",
        )


if __name__ == "__main__":
    unittest.main()
