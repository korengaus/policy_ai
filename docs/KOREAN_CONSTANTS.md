# Korean Keyword Constants — Centralization Notes

## Why this module exists

The Phase 1 audit identified that Korean keyword lists were
declared as module-level literals in multiple files. A drift between
two near-identical lists is silent — a fix added to one file does
not propagate. M11.2 lifts the affected lists into a single source
of truth (`korean_constants.py`) and pins them with regression-safety
subsets so accidental removals fail tests immediately.

## What was actually duplicated (M11.2 audit)

The audit claimed five-file duplication. The full scan found that
only a smaller subset was truly duplicated **with differing
contents** — most lists were single-source. The actual duplicates
and the choice for each:

| Constant | Source files (lines) | Contents identical? | M11.2 choice |
| --- | --- | --- | --- |
| `CONCEPT_SYNONYMS` | `official_relevance.py:4`, `evidence_comparator.py:39` | **No** — overlapping but different keys + value lists | **Kept separate**: `CONCEPT_SYNONYMS_RELEVANCE`, `CONCEPT_SYNONYMS_COMPARATOR` |
| `CONCEPT_GROUPS` | `official_source_body.py:95` | (single source, different shape from CONCEPT_SYNONYMS) | Lifted as `CONCEPT_GROUPS_OFFICIAL_BODY` |
| `MOJIBAKE_MARKERS` | `text_utils.py:10`, `article_extractor.py:44` | **No** — intersection is six markers; each has six unique | **Kept separate**: `MOJIBAKE_MARKERS_TEXT_UTILS`, `MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR` |
| `STOPWORDS` | `official_source_body.py:74`, `evidence_comparator.py:18` | **No** — different sets of common Korean noise tokens | **Kept separate**: `STOPWORDS_OFFICIAL_BODY`, `STOPWORDS_COMPARATOR` |
| `OFFICIAL_NAME_HINTS` | `official_metadata.py:51`, `official_source_body.py:47` | (already partially centralized — `official_source_body` already imports the `official_metadata` version as `SHARED_OFFICIAL_NAME_HINTS`) | **Left as-is** (no action needed) |
| `HOUSING_QUERY_TERMS` / `HOUSING_DOCUMENT_TERMS` | `verification_card.py:47/60` | (single source) | Lifted for discoverability |
| `POLICY_ACTION_KEYWORDS` | `verification_card.py:93` | (single source) | Lifted for discoverability |

Single-source constants explicitly **not** centralized in M11.2
(remain in their original files because there is no duplication to
fix): `policy_decision.tightening_keywords`,
`policy_decision.support_pressure_keywords`, the entire
`policy_impact.HIGH/MEDIUM/LOW_IMPACT_KEYWORDS` family,
`policy_impact.GROUP_RULES`, `policy_impact.SECTOR_RULES`,
`policy_confidence.HIGH/MEDIUM/LOW_RISK_KEYWORDS`,
`topic_classifier`'s function-local keyword lists,
`official_metadata.OFFICIAL_NAME_HINTS`,
`official_metadata.NAME_TO_DOMAIN`,
`official_source_body.INSTITUTION_TERMS`,
`official_source_body.ERROR_PAGE_PATTERNS`,
`evidence_comparator.CONFLICT_PHRASES`,
`source_reliability_agent.VERY_HIGH_DOMAINS` /
`HIGH_DOMAINS` / `NEWS_DOMAINS` (these are domain lists, not Korean
keywords, despite the audit's wording).

A separate milestone would be required to centralize any of these
— each move requires its own behaviour-preservation check.

## What was unioned vs kept separate

**Unioned: none.** Every pair of "near-duplicate" constants in the
audit had differing contents, so unioning would have broadened
matches at one or both call sites — a semantic change. The
conservative rule from the spec is followed throughout: when in
doubt, keep separate.

This means `korean_constants.py` exposes several
similarly-named constants (e.g., `CONCEPT_SYNONYMS_RELEVANCE` vs
`CONCEPT_SYNONYMS_COMPARATOR`). Operators reviewing them should
compare side-by-side and decide whether a future milestone should
merge them — that decision is **out of scope for M11.2**.

## Maintenance rules

1. Add keywords only after operator review.
2. Never remove keywords without confirming no call site relied on
   the keyword being present. The `TEST_*_MIN` constants in
   `korean_constants.py` are the minimum-required subsets — they
   are checked by `tests/test_korean_constants.py` and will fail
   the test suite if a pinned keyword is removed.
3. Adding a new keyword group requires:
   - A new section in `korean_constants.py` with a docstring
   - A new `TEST_*_MIN` constant (subset for regression safety)
   - A new test in `tests/test_korean_constants.py`
   - Operator review
4. Adding a keyword to an existing centralized constant is allowed
   without bumping a milestone, as long as the existing
   `TEST_*_MIN` pin remains a subset.
5. Renaming a centralized constant requires updating the
   import-graph pin in `tests/test_korean_constants.py
   (ImportGraphTests._REQUIRED_IMPORTS)` and the cross-file
   equivalence tests.

## Verification after M11.2

- `npm test` (`tests/regression.test.js`): **PASS unchanged** — the
  fixture-driven JS export tests don't read Korean keyword
  constants directly.
- All existing Python tests: **PASS unchanged**.
- `scripts/validate.py`: **PASS**.
- `tests/test_korean_constants.py`: **26 cases PASS** —
  immutability, subset pins, minimum sizes, hygiene, cross-file
  equivalence (`is` identity assertions), import-graph wiring, and
  the AST-level anti-reintroduction guard.

## Hygiene contract pinned by tests

- Every main constant is a `frozenset` or `tuple` (no `list` or
  bare `dict[str, list[str]]` at module level).
- Mapping values are tuples (the old `dict[str, list[str]]` shape
  was preserved semantically; the values are just immutable now —
  every call site only iterated them, so this is a pure
  read-mostly upgrade).
- No constant is empty.
- No keyword has leading or trailing whitespace.
- Every string is valid UTF-8.
- `korean_constants.py` has no import-time side effects (no
  `logging` / `requests` / `httpx` / `urllib.request` / `socket` /
  `openai` / `anthropic` / `playwright` / `browser_use` /
  `openclaw` / `selenium`).

## Limits

This module contains **data only**. No semantic logic was changed.
The following are explicitly **out of scope** for M11.2:

- Fuzzy / approximate Korean keyword matching
- Look-alike Korean phrase detection (`전세 사기` vs `전세사기`)
- Operator-driven keyword editing UI
- Stemming / morphological-analysis pipelines
- Merging the kept-separate variants (`CONCEPT_SYNONYMS_RELEVANCE` ↔
  `CONCEPT_SYNONYMS_COMPARATOR`, etc.)

Each of those is its own milestone with its own behaviour-impact
analysis.
