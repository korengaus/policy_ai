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

## audit §1.5 #3 re-audit (2026-05-26)

A fresh codebase scan re-checked whether duplicate Korean keyword
lists had accumulated since M11.2 and whether any constants that
M11.2 classified as "single-source" had developed twin copies in
other files. The re-audit:

- **Confirmed M11.2's centralization is intact** — every constant
  M11.2 lifted is still imported correctly by the same set of
  consumers, with the import-graph pin in `tests/test_korean_constants.py`
  still passing.
- **Found one new IDENTICAL-set duplicate** that M11.2 missed
  because the two copies lived in single-source files that M11.2
  treated as independent: `policy_confidence.LOW_RISK_KEYWORDS` and
  `policy_impact.LOW_IMPACT_KEYWORDS` are set-equal (both wrap
  `{행사, 발언, 제언, 설명, 전망}`) but with the trailing two items
  swapped in tuple order. This milestone lifts both to
  `korean_constants.py` as two separately named tuples:
  `LOW_RISK_KEYWORDS_POLICY_CONFIDENCE` (order ending `…설명, 전망`)
  and `LOW_IMPACT_KEYWORDS_POLICY_IMPACT` (order ending `…전망, 설명`).
  Each consumer imports its own tuple under the original local name
  via `from korean_constants import LOW_*_POLICY_* as LOW_*_KEYWORDS`,
  preserving byte-identical first-match behavior in the human-readable
  reason strings.
- **Confirmed every other "near-duplicate" is intentionally
  separate.** The MAJOR DIVERGENCE table below documents each pair
  that was reviewed and explicitly retained as separate constants
  with an inline comment in the source file.

### MAJOR DIVERGENCE pairs (audit §1.5 #3 re-audit, kept separate)

| Constants | Files | Overlap | Reason for separation |
| --- | --- | --- | --- |
| `HIGH_RISK_KEYWORDS` vs `HIGH_IMPACT_KEYWORDS` | `policy_confidence.py`, `policy_impact.py` | 4 items (규제, 차단, 금지, 대출 제한) | `HIGH_RISK` measures *risk signaling*; `HIGH_IMPACT` measures *impact magnitude*. Unifying would conflate two distinct scoring axes. |
| `MEDIUM_RISK_KEYWORDS` vs `MEDIUM_IMPACT_KEYWORDS` | `policy_confidence.py`, `policy_impact.py` | 1 item (실행 감소) | Same axis distinction as the HIGH pair. |
| `POSITIVE_KEYWORDS` vs `PRO_POLICY_TERMS` | `policy_impact.py`, `bias_framing_agent.py` | ~4 items | `POSITIVE_KEYWORDS` scores impact direction; `PRO_POLICY_TERMS` scores framing bias. |
| `NEGATIVE_KEYWORDS` vs `ANTI_POLICY_TERMS` | `policy_impact.py`, `bias_framing_agent.py` | ~4 items | Same as above. |
| `ERROR_SIGNALS` / `HARD_ERROR_SIGNALS` / `NAVIGATION_ERROR_SIGNALS` vs `ERROR_PAGE_PATTERNS` | `official_relevance.py`, `official_source_body.py` | ~7 items | `official_relevance` variants are scored against *document relevance* with load-bearing subset structure (HARD vs NAVIGATION penalties differ); `ERROR_PAGE_PATTERNS` is a flat body-text filter. |
| `STOP_TERMS` vs `STOPWORDS_*` | `official_relevance.py` vs `korean_constants.STOPWORDS_OFFICIAL_BODY` / `STOPWORDS_COMPARATOR` | ~6 items | M11.2 already addressed this — `STOP_TERMS` is for query-term extraction (a narrower tokenization context), `STOPWORDS_*` are for sentence-level analysis. |
| `CONSUMER_HOUSING_FINANCE_KEYWORDS` (flat list) vs `CONCEPT_SYNONYMS_*` (grouped dict) | `policy_impact.py`, `korean_constants.py` | shared housing terms | Different data shapes; flat-list vs grouped-by-concept mapping. |
| `config.STAGE_ORDER` (dict) vs `UNCERTAINTY_TERMS` (list) | `config.py`, `bias_framing_agent.py` | 검토 / 추진 / 논의 / 전망 | `STAGE_ORDER` is a stage-name→integer ordering dict (policy-stage rank); `UNCERTAINTY_TERMS` is a flat list scored for rhetorical hedging. |
| `verification_card._sentence_score` inline actor + target lists vs `INSTITUTION_TERMS` / `CONCEPT_SYNONYMS_*` | `verification_card.py`, `official_source_body.py`, `korean_constants.py` | shared institution names + target tokens | Sentence-relevance scoring vs official-document fetching vs concept matching — three distinct downstream uses. Centralizing would broaden match surfaces and change sentence scores. |
| `MARKET_KEYWORDS` (module) vs inline market list in `analyze_policy_impact` | `policy_impact.py` (both) | 6 of 8 overlap | Module-level is a boolean "if any → promote score" check; inline is a hit-count base for `_score_sensitivity`. Both intentionally narrow / broaden for their specific scoring purpose. |
| `BUSINESS_KEYWORDS` (module) vs inline business list in `analyze_policy_impact` | `policy_impact.py` (both) | 7 of 8 overlap | Module-level includes the bare token `기업`; inline omits it (would over-match in hit-count). Each scoring shape needs its own granularity. |
| `OFFICIAL_DOMAINS` local extras vs `OFFICIAL_AUTHORITY_DOMAINS` | `official_source_body.py`, `official_metadata.py` | unioned at runtime | Local copy adds `mofa.go.kr` + `hrdkorea.or.kr` not in the metadata set. Already mitigated by the additive union pattern; broadening the metadata set would change detection at every consumer. |
| `OFFICIAL_NAME_HINTS` local vs `official_metadata.OFFICIAL_NAME_HINTS` | `official_source_body.py`, `official_metadata.py` | partial (10 of 27 overlap) | M11.2 noted this as "already partially centralized"; local copy adds `국세청`. OR-ed with the imported set, so detection is union-of-both. Same additive pattern as above. |

### What stayed inline / single-source after this re-audit

The re-audit explicitly checked these single-source constants and
confirmed each remains correct in its original location (no twin
copy detected elsewhere):

- `evidence_comparator.CONFLICT_PHRASES` — single-source.
- `article_extractor.BAD_KEYWORDS` — single-source (article-body
  boilerplate filter).
- `official_source_body.INSTITUTION_TERMS` — single-source.
- `contradiction_agent.EXPLICIT_CONTRADICTION_KEYWORDS`,
  `OPPOSING_ACTIONS` — single-source.
- `bias_framing_agent.SENSATIONAL_TERMS`,
  `PRO_MARKET_TERMS` / `ANTI_MARKET_TERMS` /
  `PRO_GOVERNMENT_TERMS` / `ANTI_GOVERNMENT_TERMS` — single-source.
- `news_collector.MEDIA_ONLY_TITLES`,
  `LOW_QUALITY_TITLE_PHRASES`, `UI_ONLY_TITLES` — single-source.
- `verification_card.OFFICIAL_GOVERNMENT_TYPES`,
  `PUBLIC_INSTITUTION_TYPES`, `EXCLUDED_TOP_SOURCE_TYPES`,
  `MATERIAL_OFFICIAL_CONCEPTS`, `FALLBACK_NEWS_SOURCES` — typed-tag
  sets, single-source.

### Verification after this re-audit

- `tests/test_keyword_consolidation.py` — 5 pins covering set-equality,
  per-consumer tuple order, AST anti-reintroduction, and
  import-alias-with-`is`-identity for the two LOW_* tuples.
- `tests/test_korean_constants.py` — extended pin tables
  (`_PINNED_TUPLES`, `_MINIMUM_SIZES`, `_REQUIRED_IMPORTS`,
  `_CENTRALIZED_NAMES_PER_FILE`) automatically cover the two new
  centralized constants via the existing subTest-iterating tests.
- All pre-existing tests pass unchanged; `npm test` byte-identical;
  six M11.0d-1 snapshot fixtures byte-identical.

### Limits of this re-audit

The same M11.2 out-of-scope items still apply (no fuzzy matching,
no morphological analysis, no merging of kept-separate variants).
Additionally:

- The MINOR DIVERGENCE pairs for `MARKET_KEYWORDS`,
  `BUSINESS_KEYWORDS`, `OFFICIAL_DOMAINS` local extras, and
  `OFFICIAL_NAME_HINTS` local extras were explicitly **left
  separate** in this milestone. Merging any of them would change
  scoring at a consumer; each requires its own behaviour-impact
  analysis and operator approval. None are urgent — the additive
  union pattern at the domain / name-hint sites already prevents
  silent drift.
