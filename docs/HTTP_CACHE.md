# Shared HTTP Cache (M13.3)

## Why this exists

The Phase 1 audit identified that several modules in `analyze_pipeline`
fetch the same URL 3–5× per analysis:

> Same URL refetched 3–5× across modules.
> `official_crawler._request_url` lines 316/796/928/1096,
> `official_source_body` line 228, second-pass enrichment.
> Multiplied latency + bandwidth.

A shared HTTP cache can eliminate this duplication, realistically halving
end-to-end analyze latency (current Render baseline: 124–139s per
`smoke_async_job`). But naive integration risks:

- **Stale evidence.** A cached government notice served despite a policy
  update — could affect a verdict label.
- **Verdict drift.** A cached page that contradicts a newer one would
  cause the same claim to be judged differently on different runs.
- **Site-policy violations.** Ignoring `Cache-Control` headers /
  `robots.txt` directives.

M13.3 is a deliberate three-phase rollout: build the safe infrastructure
first, then integrate carefully, then enable in production after
measurement.

## Three-phase rollout

| Phase   | What it does                                              | Status     |
|---------|-----------------------------------------------------------|------------|
| M13.3a  | Cache module + CLI + tests; NOT integrated; default off   | this PR    |
| M13.3b  | Integrate behind feature flag for specific modules        | future     |
| M13.3c  | Enable in production after measurement                    | future     |

## What M13.3a adds

- `http_cache.py` — in-memory cache module (stdlib only).
- `scripts/check_http_cache.py` — diagnostic CLI with `--status` and
  `--simulate-{hit,deny,expired}` flags.
- `tests/test_http_cache.py` — 70 tests covering feature flag, URL
  normalization, cache key stability, Cache-Control parsing, store /
  retrieve, TTL precedence, refusal paths, LRU eviction, LRU touch on
  read, thread safety, lifecycle, singleton, the CLI, and the
  pipeline-isolation pin.
- `docs/HTTP_CACHE.md` — this document.
- Three new env vars (all default-safe):
  - `HTTP_CACHE_ENABLED` (default `"false"`)
  - `HTTP_CACHE_DEFAULT_TTL_SECONDS` (default `3600`)
  - `HTTP_CACHE_MAX_ENTRIES` (default `500`)

## What M13.3a does NOT do

- Does NOT touch `official_crawler.py`, `official_source_body.py`,
  `news_collector.py`, `article_extractor.py`, or any other
  HTTP-making module.
- Does NOT add cache lookups around any existing fetch.
- Does NOT change `render.yaml` or any Render env var.
- Does NOT add a pip dependency. `http_cache.py` imports only
  stdlib (and `logging`); the pipeline-isolation pin in
  `tests/test_http_cache.py::PipelineIsolationPin` enforces this
  contract on every CI run.

The module is **dormant in production**. Setting
`HTTP_CACHE_ENABLED=true` does nothing observable until M13.3b wires up
specific call sites.

## Safety invariants

- `get` and `put` NEVER raise. Internal errors are logged at WARNING
  and the function returns `None` / `False`.
- Cache disabled by default.
- Cache respects `Cache-Control: no-store`, `no-cache`, `private`
  (M13.3a refuses to store any response carrying these directives).
- TTL precedence: explicit `ttl_seconds` arg > `Cache-Control: max-age`
  > env default.
- LRU eviction past `max_entries`.
- LRU touch on read — frequently-accessed entries stay warm.
- Thread-safe (RLock).
- Domain allow-list / deny-list available; both empty in M13.3a so an
  operator running the CLI can exercise it without preconfiguring
  domains. M13.3b will populate per integration step.
- Cache key includes only content-affecting headers (`Accept`,
  `Accept-Language`, `User-Agent`). `Authorization` / `Cookie` /
  internal trace IDs are deliberately excluded.

## What M13.3b will integrate (preview — not in this PR)

Most likely first targets, ordered by audit impact:

1. `official_crawler._request_url` — currently called 4× for the same
   URL across a single analysis.
2. `official_source_body` fetch — caches the parsed body.
3. Google News RSS feed (`news_collector`) — caches per-query for ~5
   minutes.

Each integration in M13.3b will:

- Add a single env var to allow per-module opt-in
  (`OFFICIAL_CRAWLER_CACHE_ENABLED` etc.) so the rollout is
  one-module-at-a-time.
- Be measurable via the operational checks runner (latency before /
  after each integration).
- Be reversible by unsetting the env var.

## Diagnostic CLI

```
python scripts/check_http_cache.py --help
python scripts/check_http_cache.py                  # human-readable status
python scripts/check_http_cache.py --json
python scripts/check_http_cache.py --status         # alias
HTTP_CACHE_ENABLED=true python scripts/check_http_cache.py --simulate-hit
HTTP_CACHE_ENABLED=true python scripts/check_http_cache.py --simulate-deny
HTTP_CACHE_ENABLED=true python scripts/check_http_cache.py --simulate-expired
```

Simulations construct private `HttpCache` instances; they do not touch
the process singleton and never make real HTTP traffic.

## Rollback

To revert M13.3a:

1. Delete `http_cache.py`, `scripts/check_http_cache.py`,
   `tests/test_http_cache.py`, `docs/HTTP_CACHE.md`.
2. Remove the `validate.py` + `run_operational_checks.py` additions
   (`http_cache.py` from compileall, the CLI smokes, the test step,
   the `http-cache` profile).
3. No other production code depends on these files in M13.3a — the
   pipeline-isolation pin guarantees this.

## CI

`scripts/validate.py` runs `tests/test_http_cache.py` and
`scripts/check_http_cache.py --help` / `--status` on every CI run. No
actual network or real HTTP call is involved.
