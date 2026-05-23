# HTTP Cache Expansion (M13.3d)

## Background

M13.3b integrated HTTP caching into `official_crawler._request_url`.
Render verification showed a 51% latency reduction (174s → 85s cold)
for the canonical `금융위` query. M13.3d extends the same pattern to
two more modules where the safety profile is similar and the latency
benefit is significant.

## What M13.3d caches

| Module                          | TTL    | Domain / host gate                          | Module flag                              |
|---------------------------------|--------|---------------------------------------------|------------------------------------------|
| official_crawler (M13.3b)       | 10 min | 20 Korean `.go.kr` / `.or.kr` domains       | `OFFICIAL_CRAWLER_CACHE_ENABLED`         |
| **official_source_body (M13.3d)** | **30 min** | **Same 20 Korean `.go.kr` / `.or.kr` domains** | **`OFFICIAL_SOURCE_BODY_CACHE_ENABLED`** |
| **news_collector (M13.3d)**     | **5 min**  | **`news.google.com` only**                   | **`NEWS_COLLECTOR_CACHE_ENABLED`**       |

All three module flags require `HTTP_CACHE_ENABLED=true` (M13.3a master flag)
to take effect.

## What M13.3d does NOT cache

- `article_extractor.py` — individual articles can be updated; caching risks
  stale content for the user.
- Naver / Daum news fallbacks in `news_collector` — rare error paths;
  freshness matters more than latency. Pinned by
  `tests/test_news_collector_cache.py::NaverDaumFallbacksNotCached`.
- PDF fetches in `official_source_body` — handled by an early
  `if "pdf" in content_type` branch, so PDF bodies never enter the
  cache.
- LLM calls (M13.1b territory).

## Cache-off byte-identicality

The most important M13.3d invariant is that with the new flags unset
(default everywhere except where the operator opts in on Render),
both modules behave exactly as before:

- `official_source_body.fetch_official_source_body` delegates to
  `_do_fetch_official_source_body_raw` which is the original
  `requests.get(...)` invocation hoisted into a helper, byte-identical
  to the pre-M13.3d code.
- `news_collector._parse_google_news_rss` delegates to
  `feedparser.parse(rss_url)`, exactly the call that used to live
  inline in `search_google_news_rss_with_meta`.

These contracts are pinned by `CacheOffByteIdentityTests` in each of:

- `tests/test_official_source_body_cache.py`
- `tests/test_news_collector_cache.py`

## Activation procedure (operator)

Pre-activation:

- [ ] M13.3d commit deployed to Render.
- [ ] CI green.
- [ ] M13.3b cache stable for at least 24h
      (`scripts/measure_cache_impact.py` shows expected hit-rate).

**Step 1: Enable `OFFICIAL_SOURCE_BODY_CACHE_ENABLED=true` on Render.**
Save → auto-redeploy (~2 min).

**Step 2: Run smoke. Expected: 5–15% additional latency reduction
on top of the M13.3b baseline.**

```bash
python scripts/run_operational_checks.py --profile render-baseline \
  --base-url https://policy-ai-q5ax.onrender.com
```

**Step 3: Confirm cache events.** In Render logs, search for
`"official_source_body_cache_event"`. The first request after deploy
shows `"cache_hit":false`; subsequent runs against the same .go.kr
URL show `"cache_hit":true`. If the cache_hit pattern is correct,
the module cache is working.

**Step 4: Enable `NEWS_COLLECTOR_CACHE_ENABLED=true` on Render.**
Save → auto-redeploy.

**Step 5: Run smoke. Expected: another 5–10% reduction.**
RSS hits are small but happen on every analysis run.

**Step 6: Final measurement.**

```bash
python scripts/measure_cache_impact.py \
  --base-url https://policy-ai-q5ax.onrender.com \
  --query 금융위 --runs 3 --warmup 0 --cache-on-only
```

Expected: cold-cache run ~60–75s (vs M13.3b baseline ~85s).

## Rollback

Disable any of the new flags (or both):

- Remove `OFFICIAL_SOURCE_BODY_CACHE_ENABLED` from Render env vars.
- Remove `NEWS_COLLECTOR_CACHE_ENABLED` from Render env vars.
- Save → auto-redeploy.

The existing M13.3b cache (`OFFICIAL_CRAWLER_CACHE_ENABLED`) is
independent and stays active during M13.3d rollback. The master flag
`HTTP_CACHE_ENABLED` is also unaffected.

## Feature-flag matrix

| HTTP_CACHE_ENABLED | OFFICIAL_SOURCE_BODY_CACHE_ENABLED | NEWS_COLLECTOR_CACHE_ENABLED | Result |
|---|---|---|---|
| unset / false | * | * | All caches OFF. Byte-identical to pre-M13.3a. |
| true | unset / false | unset / false | Only M13.3b crawler cache (if its flag is on). |
| true | true | unset / false | Crawler + body caches active. RSS path unchanged. |
| true | unset / false | true | Crawler + RSS caches active. Body path unchanged. |
| true | true | true | All three caches active. |

## What's still NOT cached (future)

- `article_extractor.py` — article HTML scraping (M13.3e? — needs
  careful TTL design because article content can be edited).
- LLM provider responses (M13.1b — depends on prompt caching design).
- Postgres query results (M12.0e — separate cache layer at storage tier).

## Verification pins

- `tests/test_official_source_body_cache.py` (M13.3d — 17 cases)
- `tests/test_news_collector_cache.py` (M13.3d — 19 cases)
- M13.3a/b/c regression tests still pass (`extended-cache` ops profile
  re-runs `test_http_cache.py` and `test_official_crawler_cache.py`).
- npm regression test unchanged byte-identical.

## Cost

Zero financial cost. The two new caches sit in the same Render free
instance as the M13.3b crawler cache:

| Cache                   | Max entries | Avg body | Worst-case RAM |
|-------------------------|-------------|----------|----------------|
| official_crawler        | 500         | ~50 KB   | ~25 MB         |
| official_source_body    | 500         | ~50 KB   | ~25 MB         |
| news_collector (RSS)    | 200         | ~30 KB   | ~6 MB          |
| **Total**               |             |          | **~56 MB**     |

Render free has plenty of RAM headroom; no upgrade required.
