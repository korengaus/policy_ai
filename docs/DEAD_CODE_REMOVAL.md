# Dead Code Removal — M11.5

## Background

`claude_audit_phase1.md` §1.5 #5 identified four dead code paths that had
accumulated in the codebase. M11.5 re-audits each path at current line
numbers, deletes those that are confirmed unreachable, and pins the
resulting behavior so regressions surface immediately.

The cleanup is per-item and STOP-first: an item is only deleted when
control-flow analysis or a repo-wide grep proves the path produces no
observable effect. Items whose audit classification turned out to be
wrong are documented and left alone.

## Diagnosis Results

### Item 1 — `evidence_comparator._make_summary` duplicate branch — SAFE

The function contained two consecutive `if verification_level == "excluded_non_policy_page":` blocks. The first (kept) branch always returns inside its body, so the second (deleted) branch was unreachable. The deleted branch was a strict subset of the kept one — same data extraction, but without the `has_detail_url` discrimination, so its only possible output was already produced by the kept branch's `return` on the no-detail path.

### Item 2 — `extract_evidence_snippets` double-build of `claim_evidence_map` — SAFE

The function built the map twice: an initial `claim_evidence_map = {}` plus a per-iteration `claim_evidence_map[str(index)] = claim_snippet_ids` write, then a post-loop `claim_evidence_map = {}` followed by a rebuild from the sorted `evidence_snippets`. Nothing between the two constructions read the map, so the per-iteration writes (and the initial `= {}`) were dead. After M11.5 the post-loop rebuild is the sole construction.

### Item 3 — `renderResultsLegacy` + `buildReportTextLegacy` — SAFE

A repo-wide grep confirms both function names appear only at their own definition sites in `frontend/scripts/main.js` and the rebuilt `web/index.html`. All callers of the report-builder and result-renderer use the live `buildReportText()` / `renderResults(...)` functions. The legacy variants are dead JS.

Deletion required rebuilding the served artifact: `frontend/scripts/main.js` is the source-of-truth and `web/index.html` is produced by `python frontend/build_index.py`. The build automatically refreshes `frontend/dist_checksum.txt`, which is pinned byte-for-byte by `tests/test_frontend_build.py::RepoLevelIntegrationTest`.

### Item 4 — `source_retrieval_agent.OFFICIAL_DOMAIN_QUERY_HINTS` — DEFERRED

The audit asserts that no Google query is ever issued for the `site:fsc.go.kr`-style operators this dict produces. That may be true at the search-issuance layer, but the constant is **not** dead from a reachability standpoint:

- `_official_site_query` reads it on every claim (`source_retrieval_agent.py:177`).
- The helper is called six times inside `generate_source_queries`.
- Its return values become entries in `source_queries`, which flow through `build_source_retrieval_context` → `main.py:analyze_pipeline` (`L502, L524, L596`) → into `verification_card`, the API JSON response, and the DB rows.

Removing the constant would change the produced query strings (the `site:...` prefix would vanish), altering visible output in API responses and persisted verification cards. M11.5 is scoped to **provably unreachable** code; verifying whether the audit's deeper claim ("the strings are produced but never consulted") holds requires tracing the full search lifecycle through `contradiction_agent`, the Google client, and downstream consumers — which exceeds the safety envelope of this campaign.

Action: leave the constant in place. Reopen as a separate scoped task ("audit downstream consumers of `source_queries[*].query`") if a future cleanup wants to act on it.

## Resolution

| Item | Status | Notes |
| --- | --- | --- |
| 1. `_make_summary` dup branch | APPLIED | Deleted 15 lines in `evidence_comparator.py`. |
| 2. `claim_evidence_map` double-build | APPLIED | Deleted 2 lines in `evidence_extraction_agent.py` (one pre-loop init, one in-loop write). |
| 3. `renderResultsLegacy` + `buildReportTextLegacy` | APPLIED | Deleted ~324 lines from `frontend/scripts/main.js`; rebuilt `web/index.html`; refreshed `frontend/dist_checksum.txt`. |
| 4. `OFFICIAL_DOMAIN_QUERY_HINTS` | DEFERRED | Audit appears to be a "no-effect" claim, not a reachability claim. Constant is read and produces user-visible output. |

## What's NOT in M11.5

- Verdict producer unification (audit §1.5 #1 — future M11.0d)
- Korean keyword duplication beyond M11.2 (future M11.5b)
- Mojibake sentinels in `official_crawler` (future M11.6)
- Exception swallowing fixes (future M11.7)

## Verification pins

- `tests/test_dead_code_removal.py` (M11.5 — 12 cases across the four items, including the deferred-item documentation pin)
- `tests/test_verdict_label_b08_fix.py` (24 — regression)
- `tests/test_verdict_label_diagnostic.py` (42 — regression)
- `tests/test_verdict_producer_comparison.py` (37 — regression)
- `tests/test_frontend_build.py` (38 — frontend SHA pin; new checksum committed)
- `tests/test_verification_card_dedup.py` (11 — M11.4b regression)
- `npm test` (regression unchanged)
