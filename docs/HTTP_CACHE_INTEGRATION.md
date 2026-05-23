# HTTP Cache Integration (M13.3b)

## What this milestone adds

M13.3a built the cache module. M13.3b connects it to
`official_crawler._request_url` behind a feature flag, restricted to
Korean government domains, with a conservative 10-minute TTL.

All four internal call sites of `_request_url` (lines 316, 796, 928,
1096 in the pre-M13.3b file) benefit transparently — no caller code
changed.

## Activation requires ALL of these

1. `HTTP_CACHE_ENABLED=true` (M13.3a's master flag).
2. `OFFICIAL_CRAWLER_CACHE_ENABLED=true` (this milestone's flag).
3. The fetched URL's domain is in `GOV_CACHE_ALLOWED_DOMAINS`.
4. Response status is HTTP 200.
5. Response body ≤ 5 MB.
6. Response does not have `Cache-Control: no-store`, `no-cache`, or
   `private`.

Any one missing → cache is bypassed, behaviour is identical to
pre-M13.3b. Pinned by
`tests/test_official_crawler_cache.py::FlagPrecedenceTests` and
`CacheOffByteIdentityTests`.

## Allowed government domains (M13.3b initial list)

| Domain | Notes |
|---|---|
| fsc.go.kr | Financial Services Commission |
| fss.or.kr | Financial Supervisory Service |
| court.go.kr | Korean Courts |
| gov.kr | Government portal |
| korea.kr | Korea.kr news / official |
| moel.go.kr | Ministry of Employment and Labour |
| mohw.go.kr | Ministry of Health and Welfare |
| moef.go.kr | Ministry of Economy and Finance |
| molit.go.kr | Ministry of Land, Infrastructure and Transport |
| msit.go.kr | Ministry of Science and ICT |
| moe.go.kr | Ministry of Education |
| me.go.kr | Ministry of Environment |
| moj.go.kr | Ministry of Justice |
| mois.go.kr | Ministry of the Interior and Safety |
| mfds.go.kr | Ministry of Food and Drug Safety |
| kostat.go.kr | Statistics Korea |
| law.go.kr | Korea Law Information Center |
| assembly.go.kr | National Assembly |
| epeople.go.kr | e-People portal |
| data.go.kr | Open data portal |

Conservative subset. M13.3c will expand only after measurement.

## TTL

Default **600 seconds (10 minutes)**. Override via
`OFFICIAL_CRAWLER_CACHE_TTL_SECONDS` (positive integer; invalid /
non-positive values fall back to 600).

Why short: government notices may update; we never want to serve a
notice that's been retracted or amended. The Cache-Control max-age
header, if present, can extend the TTL to the server's stated
lifetime, but the M13.3b default deliberately stays well below
M13.3a's 1-hour module default.

## Expected impact (to be measured in M13.3c)

Audit baseline:
- Same URL fetched 3–5× per analysis across modules.
- `smoke_async_job` runs 124–139s on Render.

With M13.3b active:
- Same URL within 10 minutes: served from memory.
- Expected first-analysis: no observable change (cold cache).
- Expected subsequent analyses for the same query: 40–60% faster.

Measurement plan (M13.3c):
1. Run `smoke_async_job` 3× with flags off; record average end-to-end
   time and per-step latencies.
2. Set both flags on Render. Run 3× more.
3. Compare. If improvement < 20%, investigate (likely cause: the
   four call sites resolve to different URLs more often than the
   audit estimated).

## Safety invariants

- Cache-off path is **byte-identical** to pre-M13.3b. The original
  `_request_url` body was hoisted verbatim into
  `_do_request_url_raw`; when either feature flag is unset, the
  wrapper delegates straight to it. Pinned by
  `CacheOffByteIdentityTests::test_both_flags_unset_return_object_identical_to_requests_get`
  which asserts the returned object is the *same instance*
  (`assertIs`) that `requests.get` produced.
- Network exceptions propagate unchanged. The retry loop still
  attempts twice; if both attempts raise, the last exception is
  re-raised. Pinned by `test_network_exception_propagates`.
- Cache miss never affects fetch behaviour. The fetch happens
  exactly as it did pre-M13.3b; the cache `put` happens *after* the
  response is in hand and wrapped in try/except (a `put` failure
  logs a warning and the response is still returned).
- Only `_request_url` is wrapped. `official_source_body.py`,
  `news_collector.py`, and `article_extractor.py` are untouched
  (M13.3c may extend).
- No `truth_claim`, no verdict, no analysis stored in cache — only
  HTTP response bytes.
- Cache module's `get` / `put` already never raise; an additional
  defensive try/except guards the `put` call inside `_request_url`
  so even a hypothetical cache-module regression cannot break a
  fetch.

## Synthetic Response on cache hit

On a cache hit, the wrapper constructs a fresh `requests.Response()`
populated with the cached body / status / headers and the original
URL. The four call sites all access these attributes:

- `response.status_code` — set directly.
- `response.raise_for_status()` — works (only raises for 4xx/5xx
  and we never cache those).
- `response.content` — set via the private `_content` attribute.
- `response.headers` — set to a `CaseInsensitiveDict`.
- `response.url` — set to the request URL.
- `_response_text(response)` (in `text_utils.py`) accesses
  `.apparent_encoding`, `.encoding`, `.content`, `.text` — all of
  which are properties on `requests.Response` that compute from
  `_content` and work on synthetic instances.

Pinned by `ResponseShapeOnHitTests::test_synthetic_response_supports_caller_attributes`
which exercises every attribute the production callers touch,
including UTF-8 Korean text decoded via `.text`.

## Rollback

Set `OFFICIAL_CRAWLER_CACHE_ENABLED=false` (or unset it).
Application reverts to pre-M13.3b behaviour on the next request.
No data migration needed; the in-memory cache is dropped when the
process restarts anyway.

## What M13.3b does NOT do

- Does NOT enable the flag on Render (operator decision after local
  measurement).
- Does NOT touch `official_source_body.py`, `news_collector.py`, or
  `article_extractor.py`.
- Does NOT add Postgres / Redis / external cache (M13.3a is
  in-memory only; M13.3c may revisit if process restarts erase too
  much warm state).
- Does NOT cache POST requests (none made by `_request_url`).
- Does NOT cache responses with authentication headers.
- Does NOT replace any existing `print()` or log statement in
  `official_crawler.py` (M14.0b territory).

## CI

`scripts/validate.py` runs both `tests/test_http_cache.py` (83 cases
including `FetchWithCacheTests`) and
`tests/test_official_crawler_cache.py` (21 cases including
`CacheOffByteIdentityTests`) on every run. No real network is
involved — `requests.get` is patched with a fake in every test.
