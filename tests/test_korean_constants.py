"""Phase 2 M11.2: tests for the centralized Korean keyword constants.

The tests pin three properties of ``korean_constants.py``:

    1. **Immutability** — every main constant is a ``frozenset`` or
       ``tuple``; attempting to mutate raises ``TypeError`` /
       ``AttributeError``.
    2. **Regression-safety pins** — every ``TEST_*_MIN`` constant is
       a strict subset of its corresponding main constant. A future
       edit that removes a pinned keyword fails immediately.
    3. **Import-graph wiring** — every source file the M11.2
       refactor moved away from a local literal continues to import
       its replacement from ``korean_constants``. Re-introducing a
       local copy of a centralized keyword set would be caught by
       this scan.

The tests also pin a handful of hygiene properties: no empty
constants, no whitespace-padded keywords, all strings decode as
valid UTF-8, and ``korean_constants.py`` has no import-time side
effects (no network, no I/O, no logging).
"""

from __future__ import annotations

import importlib
import re
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import korean_constants as kc  # noqa: E402


KC_PATH = ROOT / "korean_constants.py"


# Map main constant → its TEST_*_MIN pin (collection or Mapping).
_PINNED_FROZENSETS = (
    ("STOPWORDS_OFFICIAL_BODY", kc.STOPWORDS_OFFICIAL_BODY,
     kc.TEST_STOPWORDS_OFFICIAL_BODY_MIN),
    ("STOPWORDS_COMPARATOR", kc.STOPWORDS_COMPARATOR,
     kc.TEST_STOPWORDS_COMPARATOR_MIN),
    ("HOUSING_QUERY_TERMS", kc.HOUSING_QUERY_TERMS,
     kc.TEST_HOUSING_QUERY_TERMS_MIN),
    ("HOUSING_DOCUMENT_TERMS", kc.HOUSING_DOCUMENT_TERMS,
     kc.TEST_HOUSING_DOCUMENT_TERMS_MIN),
)

_PINNED_TUPLES = (
    ("MOJIBAKE_MARKERS_TEXT_UTILS", kc.MOJIBAKE_MARKERS_TEXT_UTILS,
     kc.TEST_MOJIBAKE_MARKERS_TEXT_UTILS_MIN),
    ("MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR",
     kc.MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR,
     kc.TEST_MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR_MIN),
    ("POLICY_ACTION_KEYWORDS", kc.POLICY_ACTION_KEYWORDS,
     kc.TEST_POLICY_ACTION_KEYWORDS_MIN),
    # audit §1.5 #3 re-audit (2026-05-26): the two LOW_* tuples are
    # set-equal but ordered differently per consumer. Each gets its
    # own subset pin so a future edit removing items from either
    # tuple fails immediately.
    ("LOW_RISK_KEYWORDS_POLICY_CONFIDENCE",
     kc.LOW_RISK_KEYWORDS_POLICY_CONFIDENCE,
     kc.TEST_LOW_RISK_KEYWORDS_POLICY_CONFIDENCE_MIN),
    ("LOW_IMPACT_KEYWORDS_POLICY_IMPACT",
     kc.LOW_IMPACT_KEYWORDS_POLICY_IMPACT,
     kc.TEST_LOW_IMPACT_KEYWORDS_POLICY_IMPACT_MIN),
)

_PINNED_MAPPINGS = (
    ("CONCEPT_SYNONYMS_RELEVANCE", kc.CONCEPT_SYNONYMS_RELEVANCE,
     kc.TEST_CONCEPT_SYNONYMS_RELEVANCE_MIN),
    ("CONCEPT_SYNONYMS_COMPARATOR", kc.CONCEPT_SYNONYMS_COMPARATOR,
     kc.TEST_CONCEPT_SYNONYMS_COMPARATOR_MIN),
    ("CONCEPT_GROUPS_OFFICIAL_BODY", kc.CONCEPT_GROUPS_OFFICIAL_BODY,
     kc.TEST_CONCEPT_GROUPS_OFFICIAL_BODY_MIN),
)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class ImmutabilityTests(unittest.TestCase):
    def test_frozensets_are_frozensets(self):
        for name, main, _pin in _PINNED_FROZENSETS:
            with self.subTest(name=name):
                self.assertIsInstance(main, frozenset, name)

    def test_tuples_are_tuples(self):
        for name, main, _pin in _PINNED_TUPLES:
            with self.subTest(name=name):
                self.assertIsInstance(main, tuple, name)

    def test_mappings_are_mapping_with_tuple_values(self):
        for name, main, _pin in _PINNED_MAPPINGS:
            with self.subTest(name=name):
                self.assertIsInstance(main, Mapping, name)
                for key, value in main.items():
                    self.assertIsInstance(
                        key, str, f"{name}.{key!r} key must be str",
                    )
                    self.assertIsInstance(
                        value, tuple,
                        f"{name}.{key!r} value must be tuple (immutable)",
                    )

    def test_frozenset_cannot_be_mutated(self):
        for name, main, _ in _PINNED_FROZENSETS:
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    main.add("__bogus__")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Regression-safety pins
# ---------------------------------------------------------------------------


class PinSubsetTests(unittest.TestCase):
    def test_pinned_frozensets_are_subsets(self):
        for name, main, pin in _PINNED_FROZENSETS:
            with self.subTest(name=name):
                self.assertTrue(
                    pin <= main,
                    f"TEST_{name}_MIN not a subset of {name}: "
                    f"missing {sorted(pin - main)!r}",
                )

    def test_pinned_tuples_are_subsets(self):
        for name, main, pin in _PINNED_TUPLES:
            with self.subTest(name=name):
                self.assertTrue(
                    set(pin) <= set(main),
                    f"TEST_{name}_MIN not a subset of {name}: "
                    f"missing {sorted(set(pin) - set(main))!r}",
                )

    def test_pinned_mappings_are_subsets(self):
        for name, main, pin in _PINNED_MAPPINGS:
            with self.subTest(name=name):
                for key, required in pin.items():
                    self.assertIn(
                        key, main,
                        f"TEST_{name}_MIN: key {key!r} missing from {name}",
                    )
                    self.assertTrue(
                        set(required) <= set(main[key]),
                        f"TEST_{name}_MIN[{key!r}] not a subset of "
                        f"{name}[{key!r}]: missing "
                        f"{sorted(set(required) - set(main[key]))!r}",
                    )


# ---------------------------------------------------------------------------
# Minimum size pins
# ---------------------------------------------------------------------------


# Floors derived from the actual sizes at M11.2 time. A future
# regression that drops items below these floors fails immediately.
_MINIMUM_SIZES = {
    "STOPWORDS_OFFICIAL_BODY": 15,
    "STOPWORDS_COMPARATOR": 15,
    "HOUSING_QUERY_TERMS": 8,
    "HOUSING_DOCUMENT_TERMS": 10,
    "MOJIBAKE_MARKERS_TEXT_UTILS": 10,
    "MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR": 10,
    "POLICY_ACTION_KEYWORDS": 15,
    # audit §1.5 #3 re-audit (2026-05-26): both LOW_* tuples have
    # 5 items at audit time. Floor at 5 keeps the regression-safety
    # contract tight — a future removal will fail immediately.
    "LOW_RISK_KEYWORDS_POLICY_CONFIDENCE": 5,
    "LOW_IMPACT_KEYWORDS_POLICY_IMPACT": 5,
}

_MINIMUM_MAPPING_KEYS = {
    "CONCEPT_SYNONYMS_RELEVANCE": 8,
    "CONCEPT_SYNONYMS_COMPARATOR": 7,
    "CONCEPT_GROUPS_OFFICIAL_BODY": 6,
}


class MinimumSizeTests(unittest.TestCase):
    def test_frozensets_meet_minimum(self):
        for name, main, _ in _PINNED_FROZENSETS:
            with self.subTest(name=name):
                self.assertGreaterEqual(
                    len(main), _MINIMUM_SIZES[name],
                    f"{name} dropped below floor {_MINIMUM_SIZES[name]}",
                )

    def test_tuples_meet_minimum(self):
        for name, main, _ in _PINNED_TUPLES:
            with self.subTest(name=name):
                self.assertGreaterEqual(
                    len(main), _MINIMUM_SIZES[name],
                    f"{name} dropped below floor {_MINIMUM_SIZES[name]}",
                )

    def test_mappings_meet_minimum_key_count(self):
        for name, main, _ in _PINNED_MAPPINGS:
            with self.subTest(name=name):
                self.assertGreaterEqual(
                    len(main), _MINIMUM_MAPPING_KEYS[name],
                    f"{name} key count dropped below floor "
                    f"{_MINIMUM_MAPPING_KEYS[name]}",
                )


# ---------------------------------------------------------------------------
# No-empty / no-whitespace / UTF-8 hygiene
# ---------------------------------------------------------------------------


def _all_strings(name, main):
    """Yield ``(label, value)`` for every string in ``main`` regardless
    of whether ``main`` is a frozenset, tuple, or Mapping[str, tuple]."""
    if isinstance(main, (frozenset, tuple)):
        for item in main:
            yield f"{name}::{item!r}", item
    elif isinstance(main, Mapping):
        for key, value in main.items():
            yield f"{name}.{key} (key)", key
            for inner in value:
                yield f"{name}.{key}::{inner!r}", inner


class HygieneTests(unittest.TestCase):
    def _all_constants(self):
        for name, main, _ in _PINNED_FROZENSETS:
            yield name, main
        for name, main, _ in _PINNED_TUPLES:
            yield name, main
        for name, main, _ in _PINNED_MAPPINGS:
            yield name, main

    def test_no_constant_is_empty(self):
        for name, main in self._all_constants():
            with self.subTest(name=name):
                self.assertTrue(main, f"{name} is empty")

    def test_no_keyword_has_leading_or_trailing_whitespace(self):
        for name, main in self._all_constants():
            for label, value in _all_strings(name, main):
                with self.subTest(label=label):
                    self.assertEqual(
                        value, value.strip(),
                        f"{label} has padding whitespace",
                    )

    def test_every_keyword_is_decodable_utf8(self):
        for name, main in self._all_constants():
            for label, value in _all_strings(name, main):
                with self.subTest(label=label):
                    # Round-trip: encode and decode. Anything that
                    # can survive that is a valid Python str.
                    try:
                        value.encode("utf-8").decode("utf-8")
                    except (UnicodeEncodeError, UnicodeDecodeError) as e:
                        self.fail(f"{label} not valid UTF-8: {e}")

    def test_no_blank_keyword(self):
        for name, main in self._all_constants():
            for label, value in _all_strings(name, main):
                with self.subTest(label=label):
                    self.assertTrue(value, f"{label} is empty string")


# ---------------------------------------------------------------------------
# Cross-file equivalence: source files re-export the centralized names
# ---------------------------------------------------------------------------


class CrossFileEquivalenceTests(unittest.TestCase):
    def test_text_utils_mojibake_markers_is_central(self):
        import text_utils
        self.assertIs(
            text_utils.MOJIBAKE_MARKERS,
            kc.MOJIBAKE_MARKERS_TEXT_UTILS,
        )

    def test_article_extractor_mojibake_markers_is_central(self):
        import article_extractor
        self.assertIs(
            article_extractor.MOJIBAKE_MARKERS,
            kc.MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR,
        )

    def test_official_relevance_concept_synonyms_is_central(self):
        import official_relevance
        self.assertIs(
            official_relevance.CONCEPT_SYNONYMS,
            kc.CONCEPT_SYNONYMS_RELEVANCE,
        )

    def test_evidence_comparator_concept_synonyms_is_central(self):
        import evidence_comparator
        self.assertIs(
            evidence_comparator.CONCEPT_SYNONYMS,
            kc.CONCEPT_SYNONYMS_COMPARATOR,
        )

    def test_evidence_comparator_stopwords_is_central(self):
        import evidence_comparator
        self.assertIs(
            evidence_comparator.STOPWORDS, kc.STOPWORDS_COMPARATOR,
        )

    def test_official_source_body_stopwords_is_central(self):
        import official_source_body
        self.assertIs(
            official_source_body.STOPWORDS, kc.STOPWORDS_OFFICIAL_BODY,
        )

    def test_official_source_body_concept_groups_is_central(self):
        import official_source_body
        self.assertIs(
            official_source_body.CONCEPT_GROUPS,
            kc.CONCEPT_GROUPS_OFFICIAL_BODY,
        )

    def test_verification_card_housing_terms_are_central(self):
        import verification_card
        self.assertIs(
            verification_card.HOUSING_QUERY_TERMS, kc.HOUSING_QUERY_TERMS,
        )
        self.assertIs(
            verification_card.HOUSING_DOCUMENT_TERMS,
            kc.HOUSING_DOCUMENT_TERMS,
        )

    def test_verification_card_policy_action_keywords_is_central(self):
        import verification_card
        self.assertIs(
            verification_card.POLICY_ACTION_KEYWORDS,
            kc.POLICY_ACTION_KEYWORDS,
        )


# ---------------------------------------------------------------------------
# Import-graph pin: each source file imports korean_constants
# ---------------------------------------------------------------------------


_REQUIRED_IMPORTS = {
    "text_utils.py":          ("korean_constants",
                                "MOJIBAKE_MARKERS_TEXT_UTILS"),
    "article_extractor.py":   ("korean_constants",
                                "MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR"),
    "official_relevance.py":  ("korean_constants",
                                "CONCEPT_SYNONYMS_RELEVANCE"),
    "evidence_comparator.py": ("korean_constants",
                                "CONCEPT_SYNONYMS_COMPARATOR"),
    "official_source_body.py": ("korean_constants",
                                 "STOPWORDS_OFFICIAL_BODY"),
    "verification_card.py":   ("korean_constants",
                                "HOUSING_QUERY_TERMS"),
    # audit §1.5 #3 re-audit (2026-05-26): policy_confidence.py and
    # policy_impact.py now import their LOW_* tuples from
    # korean_constants instead of declaring them locally.
    "policy_confidence.py":   ("korean_constants",
                                "LOW_RISK_KEYWORDS_POLICY_CONFIDENCE"),
    "policy_impact.py":       ("korean_constants",
                                "LOW_IMPACT_KEYWORDS_POLICY_IMPACT"),
}


class ImportGraphTests(unittest.TestCase):
    def test_each_source_file_imports_from_korean_constants(self):
        for filename, (module, name) in _REQUIRED_IMPORTS.items():
            with self.subTest(filename=filename, expected=name):
                path = ROOT / filename
                self.assertTrue(
                    path.exists(),
                    f"audit pointed at {filename} but file is missing",
                )
                source = path.read_text(encoding="utf-8")
                # The import line might be ``from korean_constants
                # import X`` or ``from korean_constants import X as Y``
                # — either form contains the substring below.
                pattern = re.compile(
                    rf"from\s+{re.escape(module)}\s+import\b[\s\S]*?\b{re.escape(name)}\b",
                    re.MULTILINE,
                )
                self.assertRegex(
                    source, pattern,
                    f"{filename} does not import {name!r} from {module}",
                )


# ---------------------------------------------------------------------------
# Anti-reintroduction scan: no large Korean string set literal in a
# source file that was supposed to have moved to korean_constants.
# ---------------------------------------------------------------------------


# A Korean character — used to identify "Korean string" literals.
_HANGUL_RE = re.compile(r"[가-힣]")

class AntiReintroductionTests(unittest.TestCase):
    """Detect re-introduction of MODULE-LEVEL Korean keyword set
    literals in files M11.2 already moved away from. Function-local
    inline lists (e.g., the `["정부", "국토부", ...]` actor lists
    inside ``verification_card._sentence_score``) are NOT in scope —
    the audit only targeted module-level constants, and tightening
    further would regress existing pre-M11.2 inline code paths."""

    @staticmethod
    def _module_level_korean_assignments(path: Path) -> list[str]:
        """Walk the source AST and return any module-level
        ``NAME = {…}`` / ``NAME = […]`` / ``NAME = (…)`` whose RHS
        contains Hangul string literals AND has more than 3 items.
        Only ``ast.Module.body``-level statements are checked, so
        inline function lists are silently passed."""
        import ast

        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []

        out: list[str] = []
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value if isinstance(node, ast.Assign) else node.value
            if value is None:
                continue
            if not isinstance(value, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
                continue

            # Collect every string constant the RHS contains, plus
            # the values inside dict-of-lists style RHS.
            string_constants: list[str] = []

            def _collect(node_):
                if isinstance(node_, ast.Constant) and isinstance(
                    node_.value, str
                ):
                    string_constants.append(node_.value)
                else:
                    for child in ast.iter_child_nodes(node_):
                        _collect(child)

            _collect(value)
            if len(string_constants) <= 3:
                continue
            if not any(_HANGUL_RE.search(s) for s in string_constants):
                continue
            target_name = ""
            if isinstance(node, ast.Assign) and node.targets:
                first = node.targets[0]
                if isinstance(first, ast.Name):
                    target_name = first.id
            elif isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name
            ):
                target_name = node.target.id
            out.append(
                f"{target_name or '<unnamed>'} ({len(string_constants)} items)"
            )
        return out

    # M11.2 centralized exactly these names (per docs/KOREAN_CONSTANTS.md).
    # If any of them re-appears as a module-level LITERAL assignment
    # in one of the moved files, the centralization regressed.
    # Single-source constants that the audit did NOT touch
    # (OFFICIAL_NAME_HINTS, INSTITUTION_TERMS, ERROR_PAGE_PATTERNS,
    # POSITIVE_KEYWORDS, GROUP_RULES, …) are out of scope.
    _CENTRALIZED_NAMES_PER_FILE = {
        "text_utils.py":          {"MOJIBAKE_MARKERS"},
        "article_extractor.py":   {"MOJIBAKE_MARKERS"},
        "official_relevance.py":  {"CONCEPT_SYNONYMS"},
        "evidence_comparator.py": {"CONCEPT_SYNONYMS", "STOPWORDS"},
        "official_source_body.py": {
            "STOPWORDS", "CONCEPT_GROUPS",
        },
        "verification_card.py": {
            "HOUSING_QUERY_TERMS", "HOUSING_DOCUMENT_TERMS",
            "POLICY_ACTION_KEYWORDS",
        },
        # audit §1.5 #3 re-audit (2026-05-26): policy_confidence.py
        # and policy_impact.py centralized only LOW_RISK_KEYWORDS /
        # LOW_IMPACT_KEYWORDS. HIGH/MEDIUM/POSITIVE/etc. constants in
        # these files remain intentionally local (MAJOR DIVERGENCE
        # from any look-alike in other files — see audit doc).
        "policy_confidence.py":   {"LOW_RISK_KEYWORDS"},
        "policy_impact.py":       {"LOW_IMPACT_KEYWORDS"},
    }

    @staticmethod
    def _module_level_literal_assignment_names(path: Path) -> set[str]:
        """Return the set of module-level target names where the RHS
        is a literal ``{…}`` / ``[…]`` / ``(…)`` containing string
        constants. Assignments where the RHS is a Name (the
        ``from … import X`` rebinding pattern we use) are NOT
        flagged."""
        import ast

        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()

        out: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not isinstance(value, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
        return out

    def test_centralized_names_are_not_literal_in_moved_files(self):
        for filename, names in self._CENTRALIZED_NAMES_PER_FILE.items():
            with self.subTest(filename=filename):
                path = ROOT / filename
                literals = self._module_level_literal_assignment_names(path)
                offenders = sorted(names & literals)
                self.assertEqual(
                    offenders, [],
                    f"{filename} re-introduced module-level literal "
                    f"assignment(s) for centralized name(s): "
                    f"{offenders!r}",
                )


# ---------------------------------------------------------------------------
# Import-time side effects
# ---------------------------------------------------------------------------


class ImportSafetyTests(unittest.TestCase):
    def test_korean_constants_has_no_side_effects(self):
        # Re-import the module fresh and confirm it doesn't write
        # anywhere, doesn't open files, and doesn't import network
        # libraries. We scan the source for forbidden import lines.
        text = KC_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
            "logging",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"korean_constants.py must not import {forbidden!r}",
            )
        # Confirm re-import works without raising.
        importlib.reload(kc)


if __name__ == "__main__":
    unittest.main()
