# Exception Swallowing Audit (M11.7)

## Background

`claude_audit_phase1.md` §1.5 #8 listed exception-swallow sites that hide
errors from downstream code. M11.7 audits each at current line numbers
and categorizes the fix risk + scope.

**M11.7 is diagnosis only — no code is modified by this PR.** Individual
fixes will be separate, targeted PRs (M11.7a, M11.7b, …) only if the
operator approves the category-specific fix strategy.

## Categorization framework

- **Category 1 (LEGITIMATE BROAD):** the broad `except` is intentional
  and correct. Typically: optional import fallback, best-effort
  enrichment that must not crash the pipeline, cache-infrastructure
  path that must never block the live fetch. No fix needed.
- **Category 2 (SHOULD-LOG, NO-RETHROW):** the swallow is OK but the
  caller has no way to know an error happened. Fix: add `log.warning` /
  `log.error` in the `except` block, preserve return shape. **Low risk.**
- **Category 3 (SHOULD-DISTINGUISH RETURN VALUE):** the swallow returns
  a value indistinguishable from a legitimate "empty" result. Fix:
  return a distinct sentinel (e.g., `{"status": "fetch_failed", "body": ""}`
  instead of just `""`). Requires downstream caller updates.
  **Medium-high risk.**
- **Category 4 (SHOULD-NARROW EXCEPTION):** the `except` is too broad
  (bare `except:` catching even `KeyboardInterrupt` / `SystemExit`).
  Fix: narrow to specific types like `(RequestException, TimeoutError,
  json.JSONDecodeError)`. **Low risk if the specific types are
  obvious.**
- **Category 5 (LEAKY ABSTRACTION):** the swallow is in a low-level
  utility, but the right place to handle is the caller. Fix: re-raise
  and let the caller decide. **Medium risk.**

## Sites

### Site 1: `memory_store.load_policy_memory`

- **Audit line cite:** L29
- **Current line:** L29 (unchanged since audit — this file has been stable)
- **Surrounding context:**
```python
def load_policy_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {"created_at": ..., "last_updated_at": None, "topics": {}, "articles": []}

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as file:
            memory = json.load(file)
        memory.setdefault("topics", {})
        memory.setdefault("articles", [])
        return memory

    except Exception:
        return {"created_at": ..., "last_updated_at": None, "topics": {}, "articles": []}
```
- **What gets swallowed:** Bare `except Exception` — catches `json.JSONDecodeError`, `OSError`, `UnicodeDecodeError`, plus literally everything that isn't `KeyboardInterrupt` / `SystemExit`.
- **What gets returned on swallow:** Same empty-memory dict as the "first-run" path (no file yet).
- **Callers in repo:** **1 caller** — `main.py:386` inside `analyze_pipeline`, immediately followed by `move_existing_articles_to_better_topics(memory)` + `save_policy_memory(memory)`.
- **Caller behavior on the "empty" return:** Indistinguishable from "first run". The caller proceeds to rebuild topics from the (empty) articles list and writes the empty memory back to disk — **overwriting any corrupt-but-recoverable JSON on the next save**.
- **Concrete production risk:** A transient parse failure (e.g., the file was being written when read; a `\r\n` corruption from a Windows tool; a half-flushed write from a previous crash) silently resets the memory store to empty. The next `save_policy_memory` call commits the empty state, making the loss permanent. No log, no metric, no operator signal.
- **Category:** **2 (SHOULD-LOG, NO-RETHROW)** + a small enhancement: log at `error` level AND back up the unreadable file before overwriting it, so the failure can be investigated. The return shape stays identical.
- **Recommended fix sketch:** Add `log = get_logger(__name__)` at module top; in the `except` block, log `error` with the exception type + message + file path, and `os.replace(MEMORY_FILE, MEMORY_FILE + ".corrupt-<timestamp>")` to preserve the bad bytes for diagnosis before returning the empty dict.
- **Blast radius:** 0 callers need to change (return shape preserved).
- **Recommended priority:** **HIGH** — silent state loss is the worst class of bug; cheap to fix.

### Site 2: `article_extractor.fetch_article_body`

- **Audit line cite:** L364
- **Current line:** L371 (drift +7 from intermediate edits)
- **Surrounding context:**
```python
def fetch_article_body(url: str, max_chars: int = 5000) -> str:
    try:
        html_candidates = _fetch_html_candidates(url)
        ... # multi-candidate encoding probe, text quality scoring, mojibake repair
        if extracted and not _is_probably_broken(extracted) and len(extracted) >= 100:
            ...
            return extracted[:max_chars]
        log.info("[ArticleExtractor] Fallback to title")
        log.info("[ArticleExtractor] fallback to title due to encoding")
        return ""

    except Exception as error:
        log.error("[ArticleExtractor] encoding used: unknown")
        log.error("[ArticleExtractor] text quality score: -10000")
        log.error("[ArticleExtractor] text length: 0")
        log.error("[ArticleExtractor] Extracted length: 0")
        log.error("[ArticleExtractor] Fallback to title")
        log.error("[ArticleExtractor] fallback to title due to encoding")
        return ""
```
- **What gets swallowed:** Bare `except Exception` — covers `requests.RequestException` (network/timeout/SSL), `trafilatura` parse errors, `BeautifulSoup` errors, encoding errors.
- **What gets returned on swallow:** Empty string `""` — **identical** to the "legitimate empty body" return when `_is_probably_broken` rejects all candidates or the extracted text is shorter than 100 chars.
- **Callers in repo:** **1 caller** — `main.py:441`: `article_body = sanitize_text(fetch_article_body(original_url, max_chars=MAX_ARTICLE_CHARS))`. The result feeds every downstream agent (claim extraction, evidence comparison, contradiction checks, etc.).
- **Caller behavior on the "empty" return:** Downstream pipelines treat empty body as "article had no text to extract" and skip claim extraction on the body, falling back to title-only analysis. There is **no signal anywhere that a fetch actually FAILED** vs. the article being legitimately empty.
- **Concrete production risk:** Verbatim from the audit: a 503 from a news site looks identical to a real "empty body" page. The pipeline produces a verdict on title-only data and labels it indistinguishably from a successful body-fetch verdict — operator sees normal "LOW" / "WATCH" outputs that are actually built on a fetch failure.
- **Category:** **3 (SHOULD-DISTINGUISH RETURN VALUE)** — this is the textbook case. The empty return is genuinely ambiguous.
- **Recommended fix sketch:** Change the return type from `str` to `dict` with keys `{"body": str, "status": "ok"|"empty"|"fetch_failed", "error": str|None}`. Update the one caller in `main.py` to read `.body` and use `.status` to feed a new `article_body_status` field through the verification card / debug summary. Existing `sanitize_text()` wrapper stays in caller.
- **Blast radius:** 1 caller in `main.py` + downstream consumers of `article_body` (semantic analysis, evidence comparison) need to be checked for "empty string assumption" patterns. Touching the verdict producer is the riskiest part — needs the 3 verdict regression suites green before merge.
- **Recommended priority:** **MEDIUM** — high value but touches the verdict path, so it needs design review + a small follow-up PR. Until then, the existing `log.error` lines DO at least log the fact, so observability is partially present (just buried in lots of identical messages).

### Site 3a: `news_collector.resolve_google_news_url`

- **Audit line cite:** L736
- **Current line:** L920 (audit's L736 cite actually points at the **different** function `_parse_google_news_rss`; see Site 3b below. `resolve_google_news_url` lives at L907 with its except at L920. I'm covering both interpretations of the audit cite.)
- **Surrounding context (L907-922):**
```python
def resolve_google_news_url(google_news_url: str) -> str:
    parsed = urlparse(google_news_url or "")
    if parsed.netloc and "news.google.com" not in parsed.netloc:
        return google_news_url

    try:
        result = gnewsdecoder(google_news_url)
        if isinstance(result, dict) and result.get("status"):
            return result.get("decoded_url", google_news_url)
        return google_news_url

    except Exception as error:
        log.error(f'원문 URL 변환 실패: {error}')
        return google_news_url
```
- **What gets swallowed:** Bare `except Exception` around the `gnewsdecoder` third-party call.
- **What gets returned on swallow:** The original wrapped Google URL `https://news.google.com/...`.
- **Callers in repo:** **1 caller** — `main.py:433`: `original_url = resolve_google_news_url(news["google_link"])`.
- **Caller behavior on the "Google URL" return:** Treated as a normal publisher URL. The reliability classifier sees a `news.google.com` host (not a `chosun.com` / `joongang.co.kr` / etc.) — Google is not in `OFFICIAL_SOURCE_TYPES` and not in `FALLBACK_NEWS_SOURCES`, so it falls into `unknown` reliability. Downstream verdict logic penalizes the source trust score.
- **Concrete production risk:** A `gnewsdecoder` library bump or upstream API change silently demotes every Google News redirect to the Google domain. Operator sees a fleet-wide drop in source trust scores with no obvious cause (only the per-fetch `log.error` line, which is buried in tens of thousands of log lines per day).
- **Category:** **2 (SHOULD-LOG, NO-RETHROW)** — it ALREADY logs at error level, but: (a) the message is Korean (harder to grep / alert on); (b) it doesn't include the URL that failed; (c) downstream code has no machine-readable signal. A small enhancement is to expand the log into a structured field set (URL, exception type, exception message) and add a counter / metric tag, so an operator can alert on `resolve_fail_rate > N%`.
- **Recommended fix sketch:** Replace `log.error(f'원문 URL 변환 실패: {error}')` with `log.error("google_news_url_resolve_failed", extra={"url": google_news_url, "error_type": type(error).__name__, "error_message": str(error)})`. Return shape unchanged.
- **Blast radius:** 0 callers need to change.
- **Recommended priority:** **MEDIUM** — already logs; the upgrade is observability hardening, not bug-fixing.

### Site 3b: `news_collector._parse_google_news_rss` (audit's L736)

- **Audit line cite:** L736 (this is what L736 actually is — different function from Site 3a)
- **Current line:** L735
- **Surrounding context:**
```python
try:
    from http_cache import extract_domain
except Exception:  # noqa: BLE001
    return feedparser.parse(rss_url)
```
- **What gets swallowed:** Bare `except Exception` around an internal-module import.
- **What gets returned on swallow:** Cache-off fallback path — calls `feedparser.parse(rss_url)` directly, exactly like the cache-disabled path. Behavior is byte-identical to "cache is off".
- **Callers in repo:** internal to `_parse_google_news_rss`.
- **Caller behavior on swallow:** The wrapping function delegates to the cache-off path. Zero observable effect; the cache simply doesn't activate.
- **Concrete production risk:** None. This is a `noqa: BLE001`-tagged defensive guard around an optional internal import, behind two feature flags. The cache infrastructure must never break the pipeline — that is the M13.3a contract.
- **Category:** **1 (LEGITIMATE BROAD)**. Already documented via the `noqa` marker.
- **Recommended fix sketch:** None.
- **Blast radius:** N/A.
- **Recommended priority:** **N/A** — leave as-is.

### Site 4: `official_browser_crawler.fetch_rendered_page`

- **Audit line cite:** L69
- **Current line:** L69 (unchanged)
- **Surrounding context:**
```python
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        ...
        result["status_code"] = response.status if response else None
        result["title"] = sanitize_text(page.title())
        result["html"] = sanitize_text(page.content())[:500000]
        result["text"] = sanitize_text(page.locator("body").inner_text(timeout=5000))[:50000]
        result["raw_links"] = page.evaluate(...)
        result["rendered"] = True
        context.close()
        browser.close()

except Exception as exc:
    result["error"] = str(exc)

return result
```
- **What gets swallowed:** Bare `except Exception` — covers `playwright._impl._errors.TimeoutError`, `playwright._impl._errors.Error`, `playwright._impl._errors.TargetClosedError`, browser launch failures, page navigation failures, body extraction timeouts.
- **What gets returned on swallow:** The pre-initialized `result` dict with `rendered=False`, `html=""`, `text=""`, `raw_links=[]`, and `error=str(exc)`.
- **Callers in repo:** **1 internal caller** — `extract_rendered_links` at L194 wraps `fetch_rendered_page`. `extract_rendered_links` is the public name and is itself called from `official_crawler.py:1040` and `official_crawler.py:1146`.
- **Caller behavior on swallow:** `extract_rendered_links` checks `if rendered_page.get("rendered"):` (L201) — if False, the function skips the whole rendered-extraction block and returns a result with `rendered_links_count=0`, `rendered_error=<exc str>`. The outer `official_crawler` checks `rendered.get("rendered_error")` and propagates it into `result["rendered_error"]` (L1059), where downstream verdict logic and the verification card surface it.
- **Concrete production risk:** Lower than the audit implies. Unlike Site 2, the `rendered_error` field IS surfaced downstream and visible in debug output. The remaining risk is the **bare-Exception breadth**: a `KeyboardInterrupt` during a long Playwright page wait would be caught as a string and lost; the operator would have to Ctrl-C twice to actually stop.
- **Category:** **4 (SHOULD-NARROW EXCEPTION)** + minor logging improvement. The specific Playwright exception types are documented; narrowing to `(PlaywrightError, PlaywrightTimeoutError)` lets `KeyboardInterrupt` propagate correctly without changing any normal-path behavior.
- **Recommended fix sketch:** `from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError` at the top (guarded by the same `try: from playwright.sync_api import sync_playwright` pattern already in the module). Then `except (PlaywrightError, PlaywrightTimeoutError) as exc:` instead of `except Exception`. Also add a `log.warning("playwright_render_failed", extra={"url": url, "error_type": type(exc).__name__, "error_message": str(exc)})` so the failure is visible in JSON logs (currently only surfaced via the per-fetch `rendered_error` field, not in logs).
- **Blast radius:** 0 caller changes. The Playwright import guard already exists at the top of the function.
- **Recommended priority:** **MEDIUM** — visibility win + correct interrupt handling. Safe but not urgent.

### Site 5: `official_crawler` — broad except handlers

The audit cited 5 lines: **L246, L711, L1040, L1133, L1229**. After M11.6 deleted 22 lines and earlier work shifted the file further, **none of those exact line numbers map to a current broad `except`**. I enumerated every `except` in the current file and matched each to the "spirit" of the audit's cites — see the table below.

Current `except` positions in `official_crawler.py`:

| Current line | Site | Original audit cite | Notes |
| --- | --- | --- | --- |
| L26  | optional import of `extract_rendered_links` | — | not in audit |
| L234 | `except ValueError` (int parse) | — | specific exception, not in audit |
| L254 | `except (ConnectionError, Timeout, RequestException)` | — | specific tuple, already correct |
| L306 | cache infra import fallback (`noqa: BLE001`) | — | M13.3b safety guard |
| L351 | cache `put` failure (`noqa: BLE001`, already logs) | — | M13.3b safety guard |
| **L520** | `fetch_official_page` swallow | maps to audit L246 spirit | broad — see Site 5a |
| **L906** | `_extract_candidate_links` swallow | maps to audit L711 spirit | broad — see Site 5b |
| **L1213** | per-attempt retry-loop swallow | maps to audit L1040/L1133 spirit | broad — see Site 5c |
| **L1306** | per-candidate relevance scoring | maps to audit L1229 spirit | broad — see Site 5d |
| **L1402** | outermost `fetch_best_official_document` | maps to audit L1229 spirit (alt) | broad — see Site 5e |

#### Site 5a: `fetch_official_page` (L520)

- **Audit-mapped cite:** L246 (drift large)
- **Current line:** L520
- **Surrounding context:**
```python
def fetch_official_page(url: str) -> dict:
    result = {"url": url, "fetched": False, ...}
    try:
        response = _request_url(url)
        result["status_code"] = response.status_code
        response.raise_for_status()
        title, text_snippet = _extract_html_text(_response_text(response), max_chars=1500)
        result["title"] = title; result["text_snippet"] = text_snippet
        result["fetched"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result
```
- **What gets swallowed:** Bare `Exception`.
- **What gets returned on swallow:** Result with `fetched=False`, `error=str(exc)`.
- **Callers in repo:** **ZERO callers anywhere in the repo** (confirmed via grep). `fetch_official_page` is **DEAD CODE** — it was likely a helper from an earlier crawler design that nothing currently invokes.
- **Caller behavior on swallow:** N/A.
- **Concrete production risk:** None (no callers).
- **Category:** This isn't really a Category 1-5 case — **it's a dead-function case**. Properly belongs to a future dead-code cleanup pass alongside M11.5 (similar to the `fetch_official_page` discovery in M11.4 territory).
- **Recommended fix sketch:** Delete the function entirely (separate PR — would be `M11.5c` or similar, not part of M11.7).
- **Blast radius:** 0.
- **Recommended priority:** **LOW** (dead, cosmetic).

#### Site 5b: `_extract_candidate_links` (L906)

- **Audit-mapped cite:** L711 (drift large)
- **Current line:** L906
- **Surrounding context:**
```python
def _extract_candidate_links(search_html, search_url, source_name, query) -> tuple[list, str]:
    try:
        site_candidates = extract_links_for_site(
            search_html=search_html, base_url=search_url,
            source_name=source_name, query=query, max_links=5,
        )
    except Exception:
        site_candidates = []

    if site_candidates:
        return site_candidates, "site_specific"

    generic_candidates = extract_official_result_links(search_html, search_url, max_links=5)
    ...
    return generic_candidates, "generic_fallback"
```
- **What gets swallowed:** Bare `Exception` around the site-specific parser call.
- **What gets returned on swallow:** Empty `site_candidates` list. The function falls through to `extract_official_result_links` (the generic fallback parser), so the bad site-specific parser doesn't break the whole call chain.
- **Callers in repo:** 2 — inside `fetch_best_official_document` (L1028 area) and the per-attempt path.
- **Caller behavior on swallow:** Sees `parser_used = "generic_fallback"`, which IS visible in the result and logged. The behavior is intentional — a broken site-specific parser MUST not block the generic fallback.
- **Concrete production risk:** Low. The fallback path produces a result; the only loss is that the operator doesn't know WHY the site-specific parser failed (parser bug? bad HTML? upstream layout change?).
- **Category:** **2 (SHOULD-LOG, NO-RETHROW)**.
- **Recommended fix sketch:** Add `log.warning("site_specific_parser_failed", extra={"source_name": source_name, "url": search_url, "error_type": type(exc).__name__, "error_message": str(exc)})` inside the except, with `as exc`. Return shape unchanged.
- **Blast radius:** 0 callers.
- **Recommended priority:** **MEDIUM** — easy observability win; surfaces parser regressions.

#### Site 5c: per-attempt retry-loop swallow (L1213)

- **Audit-mapped cite:** L1040 / L1133 (drift large, plus M11.6 deletions)
- **Current line:** L1213
- **Surrounding context:**
```python
try:
    attempt_response = _request_url(attempt_url)
    attempt_result["status_code"] = attempt_response.status_code
    attempt_response.raise_for_status()
    ...
    attempt_candidate_links, attempt_parser_used = _extract_candidate_links(...)
    ...
    break  # success — leave the per-attempt loop
except Exception as exc:
    attempt_result["error"] = str(exc)
    if result.get("site_key") == "ibk":
        result["ibk_content_attempt_results"].append(attempt_result.copy())
    result["search_attempt_results"].append(attempt_result)
```
- **What gets swallowed:** Bare `Exception` around an entire fetch-attempt block (HTTP request + link extraction + browser rendering).
- **What gets returned on swallow:** `attempt_result` dict appended to `result["search_attempt_results"]` with `error=str(exc)`. The retry loop continues to the next query variant.
- **Callers in repo:** Internal to `fetch_best_official_document`; downstream consumers see `result["search_attempt_results"][*]["error"]`.
- **Caller behavior on swallow:** Per-attempt error is preserved in the result. The retry loop accepts attempt failures as normal (the whole point of having retries).
- **Concrete production risk:** Lower than naive reading suggests — the error IS captured per-attempt. But the breadth (`Exception`) means a `KeyboardInterrupt` mid-attempt gets eaten as a string and the loop continues; also `MemoryError` and other recoverable but exceptional conditions are misclassified as "attempt failed".
- **Category:** **4 (SHOULD-NARROW EXCEPTION)**. The expected failure modes are well-defined: `requests.exceptions.RequestException`, `Timeout`, `requests.HTTPError`. Narrow to those.
- **Recommended fix sketch:** `except (RequestException, Timeout, requests.HTTPError) as exc:`. Add a `log.warning("official_crawler_attempt_failed", extra={"site_key": ..., "attempt_url": attempt_url, "error_type": type(exc).__name__})` for observability.
- **Blast radius:** 0 callers; risk is "did we miss an exception type". A short audit of historical `attempt_result["error"]` strings in Render logs would confirm the type list before merge.
- **Recommended priority:** **MEDIUM** — non-urgent; can be batched with Site 5d.

#### Site 5d: per-candidate relevance-scoring swallow (L1306)

- **Audit-mapped cite:** L1229 (drift large, plus M11.6 deletions)
- **Current line:** L1306
- **Surrounding context:**
```python
try:
    document_response = _request_url(candidate.get("url"))
    document_status_code = document_response.status_code
    document_response.raise_for_status()
    content = _extract_document_content(_response_text(document_response), max_chars=4000)
    ...
    relevance = score_document_relevance(...)
    candidate["relevance_score"] = relevance["relevance_score"]
    candidate["relevance_level"] = relevance["relevance_level"]
    evaluated.append({"candidate": candidate, "document": document, "relevance": relevance})
except Exception as exc:
    candidate["relevance_score"] = 0
    candidate["relevance_level"] = "error_page"
    candidate["relevance_error"] = str(exc)
```
- **What gets swallowed:** Bare `Exception` around per-candidate document fetch + scoring.
- **What gets returned on swallow:** Candidate marked `relevance_score=0`, `relevance_level="error_page"`, with the exception string stored in `relevance_error`.
- **Callers in repo:** Internal.
- **Caller behavior on swallow:** The candidate is excluded from `evaluated`, which the downstream verdict path uses to decide whether to produce a verdict at all (`if not evaluated: result["error"] = "No candidate documents could be evaluated."`). So per-candidate failures DO surface as a final result-level error if every candidate fails.
- **Concrete production risk:** Same as Site 5c — bare breadth misclassifies non-fetch errors (e.g., scoring code raising on malformed data). The error string is preserved, so per-candidate diagnosis is possible, but log-level visibility is missing.
- **Category:** **4 (SHOULD-NARROW EXCEPTION)** + Category 2 logging enhancement.
- **Recommended fix sketch:** Same shape as Site 5c — narrow to `(RequestException, Timeout, requests.HTTPError, ValueError, KeyError)` based on what `score_document_relevance` can raise. Add a `log.warning("official_candidate_evaluation_failed", extra={"url": candidate.get("url"), "error_type": type(exc).__name__})`.
- **Blast radius:** 0 callers.
- **Recommended priority:** **MEDIUM** — same batch as Site 5c.

#### Site 5e: outermost `fetch_best_official_document` swallow (L1402)

- **Audit-mapped cite:** L1229 (alt mapping)
- **Current line:** L1402
- **Surrounding context (just the except block):**
```python
except Exception as exc:
    if not result.get("search_attempt_results"):
        result["search_attempt_count"] = max(result.get("search_attempt_count") or 0, 1)
        result["search_attempt_results"].append({
            "query": result.get("search_query_used"),
            "url": search_url,
            "fetched": False,
            ...
        })
```
- **What gets swallowed:** Bare `Exception` around the entire `fetch_best_official_document` body.
- **What gets returned on swallow:** The pre-built `result` dict (with `usable=False`, `weakly_usable=False`, and a synthetic search-attempt entry recording the failure URL).
- **Callers in repo:** This is the top-level entry point for official-doc fetching; called from `main.py`'s official-evidence collection step.
- **Caller behavior on swallow:** Sees `usable=False` + an error-ish `search_attempt_results` payload. Downstream agents treat the source as "no usable official evidence", which is the correct conservative behavior.
- **Concrete production risk:** Catches everything (including programmer errors like `AttributeError` from a typo in this function). Real bugs get masked as "official fetch failed" and the operator has no way to distinguish "FSS site was down" from "we introduced a regression in `_extract_candidate_links`".
- **Category:** **2 + 4** combined. The swallow IS legitimate at this outer level (the pipeline must continue), but it needs to log loudly so programmer errors are visible, AND it could plausibly be narrowed to networking + parsing exception types so genuine programmer errors propagate to the next-outer catch.
- **Recommended fix sketch:** Keep the broad `Exception` for now (this is a top-level pipeline-resilience boundary), but ADD `log.error("fetch_best_official_document_unexpected_failure", extra={"source_name": result.get("source_name"), "site_key": result.get("site_key"), "error_type": type(exc).__name__, "error_message": str(exc), "stack_trace_summary": traceback.format_exc()[:1000]})` so unexpected failures are at least surfaced in JSON logs. Future tightening (narrowing to specific types) is a Category 4 follow-up.
- **Blast radius:** 0 callers (return shape preserved).
- **Recommended priority:** **HIGH** — currently a stack-trace black hole at the top of the official-source pipeline. Logging is a free win.

## Cross-cutting observations

- **Audit line drift is severe for `official_crawler.py`.** The audit cites L246/L711/L1040/L1133/L1229 do not map cleanly to any current `except` after M11.6 (which deleted 22 lines) and earlier intervening work. I've enumerated all 5 broad-Exception sites in the current file and matched each to the "closest audit spirit" — but a future audit pass should re-baseline against the post-M11.6 file rather than trusting the old line numbers.
- **Already-correct sites:** L26 (optional import fallback), L306 / L351 (cache infrastructure, already `noqa: BLE001`-tagged with rationale), L735 (cache import fallback, already `noqa: BLE001`-tagged) — all Category 1, no action needed.
- **Pattern: bare `Exception` + return empty/default.** Sites 1, 2, 4, 5a, 5b, 5c, 5d, 5e all follow the same shape. This is a codebase-wide convention rather than per-site sloppiness; cleaning it up wholesale is plausible if the operator wants a single sweeping PR.
- **`fetch_official_page` (Site 5a) is dead code.** It belongs to a future dead-code cleanup pass alongside M11.5, not M11.7.
- **No test fixtures depend on swallow behavior.** I grep'd `tests/` for assertions about empty strings or `"error_page"` levels coming back from the swallow paths — none of them assert the exact swallow-shape, so adding logging or narrowing exceptions should not break any pin. The 3 verdict regression suites do not assert exception-handling behavior either; they assert verdict labels conditioned on documented input shapes.
- **Site 2 has the highest verdict-impact risk.** It's the only Category 3 case where the return value is genuinely ambiguous — a future fix needs the 3 verdict regression suites green before merge AND a manual Render verification because the verdict producer would see a new field.
- **Site 3a (`resolve_google_news_url`) already logs, but in Korean.** That's not wrong per se, but it makes alerting harder. A small fix to use structured logging would be a free win.

## Recommended next steps

The operator can choose any subset of the following targeted follow-up PRs. Each is independently mergeable.

| Suggested PR | Sites | Strategy | Risk | Effort |
| --- | --- | --- | --- | --- |
| **M11.7a — Logging-only sweep** | 1, 3a, 5b, 5c (logging only), 5d (logging only), 5e | Add structured `log.warning` / `log.error` to each except, preserving return shape. No control-flow changes. | LOW | Small |
| **M11.7b — Narrow Playwright broad-Exception** | 4 | Replace `except Exception` with `except (PlaywrightError, PlaywrightTimeoutError)`. | LOW | Tiny |
| **M11.7c — Narrow crawler-attempt broad-Exception** | 5c, 5d | Replace `except Exception` with `except (RequestException, Timeout, HTTPError, …)` after a brief Render-log audit to confirm the actual exception types seen in production. | LOW-MEDIUM | Small |
| **M11.7d — Distinguish article-fetch failure from empty body** | 2 | Return a status-tagged dict; update `main.py` and downstream verdict logic to read the status. Requires 3 verdict regression suites + Render verification. | MEDIUM-HIGH | Medium |
| **M11.5c (separate dead-code pass)** | 5a (`fetch_official_page`) | Delete the unused function. Not part of M11.7. | LOW | Tiny |

**Recommended ordering:**
1. **M11.7a first** (logging-only) — pure observability gain, zero behavioral risk. Lets you see whether sites are actually firing in production before deciding whether to narrow.
2. **M11.7b second** (Playwright narrow) — cheapest correctness win, restores `KeyboardInterrupt` propagation.
3. **M11.7c third** — only after a few weeks of M11.7a logs have shown what exception types actually fire in `_extract_candidate_links` and per-candidate scoring.
4. **M11.7d held for design review** — touches the verdict producer; deserves its own design discussion.

## What's NOT in M11.7

- Any code change. Pure diagnosis.
- Decisions about the global exception handling philosophy.
- New exception classes or error taxonomy.
- Retry-logic changes.
- Removal of dead `fetch_official_page` (belongs to a separate cleanup pass).

## Verification pins

- This PR adds only one file: `docs/EXCEPTION_HANDLING_AUDIT.md`.
- No tests added because no code changed.
- `regression.test.js` unchanged.
- `npm test` byte-identical.

## Resolution for Site 5a (M11.5c)

`fetch_official_page` was deleted from `official_crawler.py` as a follow-up to the M11.7 finding. M11.5c is a dead-code cleanup pass, NOT an exception-handling change.

- **Confirmed zero callers** by repo-wide grep at HEAD before deletion. The 8 occurrences of the function name inside this audit doc are documentation prose, not call sites.
- **Function body removed:** 26 lines (L500–L525 in the post-M11.6 file: the 24-line def-through-`return result` block plus the two trailing blank lines, leaving the two leading blanks at L498–L499 as the PEP 8 separator before `_empty_relevance_fields`).
- **No imports needed removal** — the deleted function used only intra-module helpers (`_request_url`, `_extract_html_text`, `_response_text`) that remain in active use elsewhere.
- **Uniqueness + non-reintroduction pinned by** `tests/test_m11_5c_fetch_official_page_removed.py` (4 cases: definition absent, no repo references, module still imports, live public surface still present).
- **Production behavior unchanged** — the function was never being called, so deleting it changes nothing at runtime. Sites 5b–5e (and the other audit-cited swallows) are explicitly NOT touched by M11.5c; they remain as documented above for future M11.7a / M11.7b / M11.7c work.
