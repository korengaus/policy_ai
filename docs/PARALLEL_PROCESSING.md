# Parallel Per-News-Item Processing — M15.0d

**Status:** Phase 2 M15.0d SHIPPED. `main.analyze_pipeline`'s per-news loop is now parallelized via `concurrent.futures.ThreadPoolExecutor` while preserving every M11.0d invariant and the existing sequential ordering of LLM calls + memory mutations.

## Why M15.0d exists

`claude_audit_phase1.md §1.6` identified two latency hotspots in `analyze_pipeline`:

1. "Sequential per-news-item pipeline (main.py loop) — No parallelism across news items"
2. "Sequential per-claim official document fetch — O(claims × candidates) blocking"

For `max_news=3`, the pre-M15.0d pipeline processed news items 1 → 2 → 3 sequentially. Each item's HTTP fetches (article body, official source search, contradiction checks) blocked the next item. Total wall-clock time ≈ sum of per-item latencies (~150-180s for `max_news=3`).

M15.0d parallelizes the I/O-bound half of the per-news loop. With `MAX_PARALLEL_NEWS_ITEMS=3` (default), three news items' HTTP fetches run concurrently. The LLM half stays sequential (per spec, for OpenAI rate-limit safety and deterministic memory mutations).

## Phase A / Phase B split

The per-news loop body now splits into two phases at the LLM call boundary:

### Phase A — verdict computation (parallel)

`_process_news_item_phase_a(news, *, index, total, memory_snapshot, query, news_collection_debug, analysis_cache_key) -> dict`

Pure function — reads `memory_snapshot` but never mutates it (or anything else outside its return value). Runs the entire verdict-computation chain:

1. URL resolve (`resolve_google_news_url`)
2. Article body fetch (`fetch_article_body`)
3. Claim extraction (`extract_verifiable_claims`, `normalize_claims`, `extract_policy_claim_sentences`)
4. Preliminary topic classification (rule-based, no LLM)
5. Official source candidate generation + fetch (`fetch_official_evidence`)
6. Source retrieval enrichment + resolution
7. Evidence extraction (`extract_evidence_snippets`)
8. Contradiction checks
9. Bias-framing analysis
10. Semantic evidence summary (optional, off by default)
11. Evidence comparison (`compare_news_with_official_evidence`)
12. Policy confidence (`calculate_policy_confidence`)
13. Policy impact (`analyze_policy_impact`)
14. **P1**: `make_final_decision` (captures `p1_alert_level_raw` for M11.0d-3a)
15. **P3**: `build_verification_card`
16. Build debug summary
17. **P4 (implicit)**: official_mismatch rewrite
18. **P2**: `calibrate_final_decision` (authoritative `policy_alert_level`, M11.0d-3b)
19. M11.0d-3a `disagreement_signal` assembly + structured log
20. `sanitize_data(verification_card)` + print

Returns a dict carrying every per-item value Phase B needs.

### Phase B — LLM + memory + report (sequential)

`_apply_news_item_phase_b(phase_a, memory) -> dict`

Runs serially in submission order on the main thread:

1. **Duplicate detection** re-computed against the LATEST `memory` (preserves byte-identical behaviour to the pre-M15.0d sequential loop)
2. **`ai_reasoner.run_ai_reasoning`** (LLM call — kept sequential per spec)
3. AI-driven topic classification when `ai_available`
4. `update_memory_with_result` + `save_policy_memory` (memory mutation)
5. Report-item dict assembly

Returns `{report_item, saved_to_memory, duplicate}`.

## Concurrency control

| Env var | Default | Effect |
| --- | --- | --- |
| `MAX_PARALLEL_NEWS_ITEMS` | `3` | Max worker threads in the Phase A executor. Clamped to ≥1. Effective workers = `min(env_value, len(news_results))`. |

**Safe rollback:** `MAX_PARALLEL_NEWS_ITEMS=1` → sequential execution, byte-identical to pre-M15.0d behaviour. The code explicitly takes a different code path (`for i, news in enumerate(news_results):` loop instead of `ThreadPoolExecutor`) when `max_parallel <= 1 or total_items <= 1`, so there's no thread pool overhead and no concurrency-induced log interleaving.

## Order preservation

Results are slotted into a `phase_a_results[index]` list by submission index. Phase B then iterates the list in order. Final `report_items` is in input news order, regardless of which Phase A item completed first.

Pinned by `tests/test_parallel_news_processing.py::OrderPreservationTests::test_report_items_in_submission_order_under_parallel`.

## Error isolation

Each Phase A future is wrapped in try/except. A failed Phase A future leaves `phase_a_results[idx] = None`, and Phase B simply skips that index. Other items still complete and appear in the final report.

Pinned by `tests/test_parallel_news_processing.py::ErrorIsolationTests::test_phase_a_failure_does_not_abort_other_items`.

## Thread safety surface

| Resource | Thread-safe? | Why |
| --- | --- | --- |
| `http_cache.py` | ✅ Yes | M13.3a built in `threading.RLock()` + module-level `_default_cache_lock`. Every public method wrapped with `with self._lock`. |
| `memory_store.save_policy_memory` | ✅ Yes (in M15.0d's design) | Memory mutations happen ONLY in Phase B, which runs sequentially on the main thread. |
| SQLite (`policy_ai.db`) | ✅ Yes | Not touched inside `analyze_pipeline`. Writes happen AFTER `analyze_pipeline` returns. |
| OpenAI embeddings (when `SEMANTIC_MATCHING_ENABLED=true`) | ✅ Bounded | Up to 3 concurrent requests (default `MAX_PARALLEL_NEWS_ITEMS`). |
| `ai_reasoner.run_ai_reasoning` | ✅ Sequential | Phase B keeps these in submission order per spec. |

## Progress reporting

`analyze_pipeline` now accepts an optional keyword-only `progress_callback: Callable[[str, dict], None] | None = None`. When passed, it fires at:

| Stage | Payload | Bound percent in pipeline_worker bridge |
| --- | --- | --- |
| `news_item_parallel_started` | `{"total": N, "workers": M}` | 12 |
| `news_item_completed` | `{"index": i, "total": N}` | 15 + (i/N)×65 |

`pipeline_worker.run_analyze_pipeline_job` wires this through to its Redis pub/sub `report_progress` channel. The browser sees a smooth progress bar advancing from 10 (`pipeline_started`) through the news_item completions (15-80) to 85 (`saving_results`) to 100 (`completed`).

`V2_STAGE_LABELS_KO` in `frontend/scripts/main.js` already has Korean translations for both stages (pre-populated in M15.0c — "병렬 처리 시작" and "뉴스 N/M 처리 완료"); no frontend change needed for M15.0d.

Sync `/analyze` callers pass `progress_callback=None` (default) — no progress events fire.

## What M15.0d does NOT do

- Does NOT touch any verdict-producing logic (`verification_card._verdict_label`, `make_final_decision`, `calibrate_final_decision`).
- Does NOT change `policy_alert_level` / `verdict_label` for any input. M11.0d-1 9-case snapshot + 9 verdict regression suites (147 cases) all PASS byte-identical.
- Does NOT modify any M11.0d artifact (snapshot files, tests, docs).
- Does NOT add asyncio. `ThreadPoolExecutor` only.
- Does NOT add new dependencies — `concurrent.futures` is stdlib.
- Does NOT change LLM call ordering or batching.
- Does NOT add a Playwright pool (M15.0e territory).
- Does NOT touch `frontend/`, `web/index.html`, `tests/regression.test.js`, `render.yaml`, `requirements.txt`.

## Latency expectation

Pre-M15.0d (sequential, `max_news=3`): ~150-180s wall-clock.

Post-M15.0d (parallel, `max_news=3`, `MAX_PARALLEL_NEWS_ITEMS=3`):

- Phase A (parallel): ~max(per-item Phase A) ≈ ~50-70s
- Phase B (sequential): ~sum(per-item Phase B, mostly LLM) ≈ ~45-75s

Estimated total: **~95-145s** (1.3-1.6× speedup vs sequential). The LLM call dominates per-item Phase B and remains sequential per spec, so the speedup is bounded by Amdahl's law (sequential portion ≈ 30-40%).

Render verification should measure actual production latency before/after to confirm.

## Pins

| Test | What it pins |
| --- | --- |
| `tests/test_parallel_news_processing.py` (15 cases) | Order preservation, parallel-thread overlap, error isolation, `MAX_PARALLEL_NEWS_ITEMS=1` rollback, env-var clamping, progress_callback wiring + failure tolerance, helper signature shape, M11.0d invariant reachability. |
| `tests/test_verdict_producer_disagreement_diagnostic.py` (M11.0d-1, 9 cases) | 42-row synthetic matrix + 3 named fixtures produce IDENTICAL P1/P2/P3 labels. **STRONGEST safety signal.** |
| `tests/test_m11_0d_3a_disagreement_signal.py` (17 cases) | `disagreement_signal` contract unchanged. |
| `tests/test_m11_0d_3b_p2_authority.py` (12 cases) | P2 authoritative + Constraint #11 + #12 invariants. |
| All 9 verdict regression suites (147 cases) | Verdict labels byte-identical across all production fixtures. |
| `tests/regression.test.js` (npm test) | Korean methodology phrases + frontend export byte-identical. |
| `tests/test_log_level_reclassification.py` | `EXPECTED_TOTAL_LOG_CALLS` bumped 263 → 265 (+2 for M15.0d's new main.py log calls). |
