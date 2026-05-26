"""audit §1.5 #3 re-audit (2026-05-26): pins for the LOW_* tuple
consolidation.

M11.2 consolidated several Korean keyword groups into
``korean_constants.py`` but left ``LOW_RISK_KEYWORDS`` (in
``policy_confidence.py``) and ``LOW_IMPACT_KEYWORDS`` (in
``policy_impact.py``) as local module-level lists — the M11.2 audit
treated them as belonging to separate single-source files. A fresh
re-audit found the two are SET-EQUAL (both wrap
``{행사, 발언, 제언, 설명, 전망}``) but with the trailing two items
swapped (``설명 ↔ 전망``), which causes the per-consumer first-match
keyword to differ in the human-readable reason strings.

This milestone lifts both tuples to ``korean_constants.py`` as two
separately named tuples, preserving each consumer's original order
so first-match behavior is byte-identical. These pins:

  (a) statically assert the two centralized tuples are set-equal
      (drift-detection),
  (b) assert each tuple's exact ordering matches the pre-milestone
      consumer order (so first-match keyword is unchanged),
  (c) AST-walk both consumer files to confirm no local module-level
      ``LOW_RISK_KEYWORDS`` / ``LOW_IMPACT_KEYWORDS`` literal remains,
      and
  (d) confirm both consumer files import their tuple from
      ``korean_constants`` with the documented alias.

Behavioural pins for the consumers' first-match output are covered
by the existing verdict-core regression suites (e.g.,
``tests/test_m11_0d_3b_2_prose_alignment.py``) — this file only pins
the *consolidation*.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _read(filename: str) -> str:
    return (_PROJECT_ROOT / filename).read_text(encoding="utf-8")


def _module_level_target_names_with_literal_rhs(filename: str) -> set[str]:
    """Return the set of module-level NAMES whose RHS is a literal
    list/tuple/set/dict. Bindings from ``from … import X as Y`` are
    NOT flagged (those have a Name RHS, not a Constant-containing
    collection)."""
    tree = ast.parse(_read(filename), filename=filename)
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


# Per-file pin spec: (consumer file, centralized name in korean_constants,
# local alias, pre-milestone expected order).
_LOW_KEYWORD_SITES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "policy_confidence.py",
        "LOW_RISK_KEYWORDS_POLICY_CONFIDENCE",
        "LOW_RISK_KEYWORDS",
        ("행사", "발언", "제언", "설명", "전망"),
    ),
    (
        "policy_impact.py",
        "LOW_IMPACT_KEYWORDS_POLICY_IMPACT",
        "LOW_IMPACT_KEYWORDS",
        ("행사", "발언", "제언", "전망", "설명"),
    ),
)


class LowKeywordSetEquivalencePin(unittest.TestCase):
    """The two centralized LOW_* tuples must remain set-equal. If a
    future PR adds an item to only one of them, this pin fails — the
    operator must then either add to both OR explicitly document why
    they should diverge (and update this pin)."""

    def test_low_risk_low_impact_are_set_equal(self):
        import korean_constants as kc

        self.assertEqual(
            set(kc.LOW_RISK_KEYWORDS_POLICY_CONFIDENCE),
            set(kc.LOW_IMPACT_KEYWORDS_POLICY_IMPACT),
            "LOW_RISK_KEYWORDS_POLICY_CONFIDENCE and "
            "LOW_IMPACT_KEYWORDS_POLICY_IMPACT must remain set-equal "
            "per the audit §1.5 #3 re-audit (2026-05-26) finding. If a "
            "future milestone intentionally diverges them, update this "
            "pin AND docs/KOREAN_CONSTANTS.md.",
        )


class LowKeywordOrderPreservedPin(unittest.TestCase):
    """The exact tuple ORDER for each LOW_* constant must match the
    pre-milestone consumer order so first-match behavior (and the
    resulting human-readable reason string) is byte-identical.

    The two pre-milestone orderings:

      policy_confidence.LOW_RISK_KEYWORDS  = (행사, 발언, 제언, 설명, 전망)
      policy_impact.LOW_IMPACT_KEYWORDS    = (행사, 발언, 제언, 전망, 설명)

    They differ only in the last two items (설명 ↔ 전망).
    """

    def test_low_risk_keyword_order_preserved(self):
        import korean_constants as kc

        expected = next(
            order for filename, _name, _alias, order in _LOW_KEYWORD_SITES
            if filename == "policy_confidence.py"
        )
        self.assertEqual(
            kc.LOW_RISK_KEYWORDS_POLICY_CONFIDENCE, expected,
            "LOW_RISK_KEYWORDS_POLICY_CONFIDENCE order changed — "
            "policy_confidence._risk_level relies on the (설명, 전망) "
            "trailing order for byte-identical first-match output. "
            "If you intentionally reordered, update this pin AND the "
            "audit doc.",
        )

    def test_low_impact_keyword_order_preserved(self):
        import korean_constants as kc

        expected = next(
            order for filename, _name, _alias, order in _LOW_KEYWORD_SITES
            if filename == "policy_impact.py"
        )
        self.assertEqual(
            kc.LOW_IMPACT_KEYWORDS_POLICY_IMPACT, expected,
            "LOW_IMPACT_KEYWORDS_POLICY_IMPACT order changed — "
            "policy_impact._impact_level relies on the (전망, 설명) "
            "trailing order for byte-identical first-match output. "
            "If you intentionally reordered, update this pin AND the "
            "audit doc.",
        )


class NoLocalLowKeywordsPin(unittest.TestCase):
    """``LOW_RISK_KEYWORDS`` must not be re-declared as a literal list
    in ``policy_confidence.py``; ``LOW_IMPACT_KEYWORDS`` must not be
    re-declared as a literal list in ``policy_impact.py``. Catches
    accidental "cleanup" PRs that re-inline the constants."""

    def test_no_local_low_keywords_in_consumers(self):
        for filename, _centralized, alias, _order in _LOW_KEYWORD_SITES:
            with self.subTest(filename=filename, alias=alias):
                literal_names = _module_level_target_names_with_literal_rhs(
                    filename,
                )
                self.assertNotIn(
                    alias, literal_names,
                    f"{filename} re-introduced {alias!r} as a "
                    "module-level literal — it must be imported from "
                    "korean_constants. See docs/KOREAN_CONSTANTS.md "
                    "re-audit section.",
                )


class ImportPathsValidPin(unittest.TestCase):
    """Both consumer files must import their LOW_* tuple from
    ``korean_constants`` with the documented alias, AND the imported
    name must be the same object (``is``) as the centralized
    constant — guards against accidental rebinding."""

    _IMPORT_TEMPLATE = (
        r"from\s+korean_constants\s+import\b[\s\S]*?\b"
        r"{original}\b\s+as\s+{alias}\b"
    )

    def test_consumers_import_from_korean_constants(self):
        import korean_constants as kc
        import policy_confidence
        import policy_impact

        consumer_modules = {
            "policy_confidence.py": policy_confidence,
            "policy_impact.py": policy_impact,
        }

        for filename, centralized, alias, _order in _LOW_KEYWORD_SITES:
            with self.subTest(filename=filename, alias=alias):
                source = _read(filename)
                pattern = re.compile(
                    self._IMPORT_TEMPLATE.format(
                        original=re.escape(centralized),
                        alias=re.escape(alias),
                    ),
                    re.MULTILINE,
                )
                self.assertRegex(
                    source, pattern,
                    f"{filename} must import {centralized!r} as "
                    f"{alias!r} from korean_constants.",
                )
                # The imported name must be the SAME object as the
                # centralized tuple.
                module = consumer_modules[filename]
                local = getattr(module, alias)
                central = getattr(kc, centralized)
                self.assertIs(
                    local, central,
                    f"{filename}.{alias} is not the same object as "
                    f"korean_constants.{centralized} — accidental "
                    "rebinding suspected.",
                )


if __name__ == "__main__":
    unittest.main()
