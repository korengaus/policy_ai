# HTTP Cache Activation Guide (M13.3c)

## What you're activating

M13.3b integrated an HTTP cache into `official_crawler._request_url`,
behind two feature flags, restricted to 20 Korean government domains,
with a 10-minute default TTL.

**M13.3c does NOT activate it.** This guide tells YOU how to activate
it on Render and verify it's working.

## Pre-activation checklist

Before enabling on Render, confirm:

- [ ] Latest commit on `main` includes M13.3b ("Integrate HTTP cache
      into official_crawler").
- [ ] Latest CI run on `main` is green.
- [ ] M13.3c measurement tooling is committed.
- [ ] You have ~30 minutes for measurement (10 min baseline +
      activation + 10 min cache-on).
- [ ] You're prepared to roll back if needed (instructions below).

## Step 1 — Establish baseline (before enabling)

Run the measurement script with the current Render config (cache OFF):

```
python scripts/measure_cache_impact.py \
    --base-url https://policy-ai-q5ax.onrender.com \
    --query 전세사기 \
    --runs 3 \
    --warmup 1 \
    --baseline-only
```

This writes `reports/cache_measurement_<timestamp>.{json,md}`. Save
the report. You'll compare against the post-activation result.

**Expected output:**
- Pass rate 3/3.
- Mean elapsed: 60–130s (depending on Render instance warmth).

If pass rate is NOT 3/3, **STOP**. Investigate the smoke failure
before enabling the cache.

## Step 2 — Enable on Render

Go to Render dashboard → policy-ai service → Environment.

Add these env vars (do NOT remove any existing ones):

| Key                              | Value | Purpose                                              |
|----------------------------------|-------|------------------------------------------------------|
| `HTTP_CACHE_ENABLED`             | `true` | M13.3a master flag                                  |
| `OFFICIAL_CRAWLER_CACHE_ENABLED` | `true` | M13.3b crawler flag                                 |
| `LOG_FORMAT`                     | `json` | (Optional) Enable JSON logs for cache_event grepping |

Save. Render auto-redeploys (~2 minutes).

Wait for the deploy to complete (Render dashboard shows "Live").

## Step 3 — Verify activation

Run the activation check:

```
python scripts/check_cache_activation.py \
    --base-url https://policy-ai-q5ax.onrender.com \
    --query 금융위
```

**Expected output:**
- Run 1 (cold) takes ~60–120s.
- Run 2 (warm) takes significantly less (target: 40–70% faster).
- Verdict: `[OK] Cache appears effective`.

If verdict is `[WARN]` or speedup < 20%:
- Check Render env vars are actually set (Render dashboard).
- Check the Render deploy completed.
- Try a different query that involves more government URLs.
- Proceed to Step 4 anyway for statistical confidence.

## Step 4 — Measure with statistical confidence

```
python scripts/measure_cache_impact.py \
    --base-url https://policy-ai-q5ax.onrender.com \
    --query 전세사기 \
    --runs 3 \
    --warmup 1 \
    --cache-on-only
```

This writes another `reports/cache_measurement_<timestamp>.{json,md}`.

Compare Mean elapsed against the baseline saved in Step 1.

### Expected results

| Scenario                                         | Mean speedup    | Action                              |
|--------------------------------------------------|-----------------|-------------------------------------|
| Same query analyzed twice within 10 min          | 30–60%          | Cache is working as designed        |
| Different query each run                         | 5–15%           | Limited cache reuse — expected      |
| No speedup at all                                | 0%              | Cache not active OR query has no gov URLs |
| Slower than baseline                             | negative        | **ROLLBACK** — see Step 6           |

The script's verdict labels map to the same thresholds:

- `PASS` — speedup ≥ 20% AND pass rate maintained.
- `MARGINAL` — speedup 5–20%. Verify Render logs.
- `INVESTIGATE` — speedup < 5%. Cache may not be active or
  query has no gov URLs.
- `ROLLBACK_RECOMMENDED` — pass rate dropped. Disable and
  investigate.

## Step 5 — Inspect Render logs (optional but recommended)

If you set `LOG_FORMAT=json`:

In Render dashboard → Logs, search for `official_crawler_cache_event`.

You should see entries like:

```json
{"ts":"...","level":"INFO","module":"official_crawler","msg":"official_crawler_cache_event","extra":{"url":"https://www.fsc.go.kr/...","status_code":200,"cache_hit":false,"body_bytes":48291}}
```

After the first run, subsequent runs within 10 minutes should show
`cache_hit:true`.

If you see only `cache_hit:false` and never `cache_hit:true`, the
cache is being checked but never hitting. Likely cause: the query
produces different URLs each time, or the TTL is shorter than the
gap between runs.

## Step 6 — Rollback (if needed)

If anything looks wrong:

1. Go to Render dashboard → Environment.
2. Change `OFFICIAL_CRAWLER_CACHE_ENABLED` from `true` to `false`
   (or delete the key).
3. Save. Render auto-redeploys.
4. Verify: `python scripts/check_cache_activation.py ...` should now
   show no speedup (`[AMBIGUOUS]` or `[WARN]`).

Optionally, also disable `HTTP_CACHE_ENABLED` if you don't want the
master flag set.

Note: `HTTP_CACHE_ENABLED=true` alone does NOT activate the cache
because `OFFICIAL_CRAWLER_CACHE_ENABLED=true` is also required
(both-flags-required by design, pinned by
`tests/test_official_crawler_cache.py::FlagPrecedenceTests`).

## Long-term: what to watch for

After enabling, monitor for:

- **Stale evidence reports.** If a government notice is updated, the
  cache will serve the old version for up to 10 minutes. This is
  generally acceptable for policy verification, but flag any user
  complaints.
- **Memory growth.** Cache caps at 500 entries; should plateau
  quickly.
- **Cache-related errors in Render logs.** Search for `WARNING` from
  `http_cache` or `official_crawler`.

## What stays disabled (until M13.3d or later)

The cache is wired ONLY into `official_crawler._request_url`. These
remain uncached:

- `official_source_body.py` — fetches official document bodies.
- `news_collector.py` — Google News RSS, Naver/Daum fallbacks.
- `article_extractor.py` — article body extraction.
- LLM Judge API calls (M13.1b territory).

Each of these is a candidate for M13.3d expansion. Don't enable
additional caching modules until measurement shows the existing
cache is stable.

## Cost

No cost. The cache is in-memory, single-process. No Render plan
upgrade, no external service.

## Reverting M13.3b entirely

If you want to remove the cache code entirely (not just disable):

1. Revert the M13.3b commit ("Integrate HTTP cache into
   official_crawler").
2. `npm test && python scripts/validate.py` to confirm green.
3. Push and verify Render deploys cleanly.

This is a destructive operation. Disable via env var first (Step 6
above) before considering code reversion.
