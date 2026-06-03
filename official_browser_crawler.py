from official_site_parsers import (
    extract_fsc_rendered_links,
    extract_fss_rendered_links,
    extract_gov24_rendered_links,
    extract_ibk_rendered_links,
    extract_links_for_site,
    get_site_key,
)
from urllib.parse import urljoin, urlparse
import atexit
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import config
from text_utils import sanitize_data, sanitize_text

from structured_logging import get_logger

log = get_logger(__name__)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _max_parallel_playwright() -> int:
    """Return max concurrent Playwright browsers.

    M26.2 (LESSON 1 footgun fix): in-code default hardened 3 -> 1. On this
    1-CPU worker, N concurrent Chromium = ~60x slowdown (M16-speed-2b:
    16s -> 16min); the Render dashboard already pins MAX_PARALLEL_PLAYWRIGHT=1,
    so this is a no-op in production but prevents a missing/cleared env var
    from silently reintroducing the regression. Override via the env var.
    A Semaphore(1) is equivalent to a Lock for single-acquire patterns.
    """
    try:
        value = int(os.environ.get("MAX_PARALLEL_PLAYWRIGHT", "1"))
        return max(1, value)
    except (TypeError, ValueError):
        return 1


# M16-speed-2b: bounded Playwright concurrency.
# Was a threading.Lock() (M16-speed-2a) to serialize Playwright on the
# 512MB Starter tier (concurrent Chromium would OOM). Worker is now Standard
# (2GB), so we allow up to MAX_PARALLEL_PLAYWRIGHT (default 3) concurrent
# browsers. Work is I/O-bound (networkidle page loads dominate), so concurrency
# gives ~2-2.5x speedup on the Playwright portion of Phase A even with 1 CPU.
# Semaphore(1) reproduces the old Lock behavior exactly.
_PLAYWRIGHT_SEMAPHORE = threading.Semaphore(_max_parallel_playwright())


def _fetch_rendered_page_cold(url: str, timeout_ms: int = 15000) -> dict:
    # M26.2: this is the original pre-M26.2 fetch_rendered_page body, hoisted
    # VERBATIM into a helper. It is the gate-off path AND the permanent
    # fallback when the warm path fails. Do NOT modify — the warm path
    # (`_WarmBrowserHolder._render_on_thread`) must stay byte-identical to it.
    result = {
        "url": url,
        "rendered": False,
        "status_code": None,
        "title": None,
        "html": "",
        "text": "",
        "raw_links": [],
        "error": None,
    }

    try:
        from playwright.sync_api import (
            sync_playwright,
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
        )
    except ImportError as exc:
        result["error"] = f"Playwright is not installed: {exc}"
        return result

    # M16-speed-2b: bound Playwright lifecycle to MAX_PARALLEL_PLAYWRIGHT.
    # The semaphore permit is released on success AND on any exception path
    # below — `with` guarantees release before the function returns.
    with _PLAYWRIGHT_SEMAPHORE:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ko-KR",
                    extra_http_headers={
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                )
                page = context.new_page()
                response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_timeout(1000)

                result["status_code"] = response.status if response else None
                result["title"] = sanitize_text(page.title())
                result["html"] = sanitize_text(page.content())[:500000]
                result["text"] = sanitize_text(page.locator("body").inner_text(timeout=5000))[:50000]
                result["raw_links"] = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                        href: a.href || a.getAttribute('href') || '',
                        text: (a.innerText || a.textContent || '').trim()
                    }))"""
                )
                result["raw_links"] = sanitize_data(result["raw_links"])
                result["rendered"] = True

                context.close()
                browser.close()

        except PlaywrightTimeoutError as exc:
            # Page navigation / body extraction exceeded the budgeted
            # timeout. Common on slow gov.kr sites under load. Sentinel
            # return preserved — the caller (`extract_rendered_links`)
            # gates downstream work on `result["rendered"]` being True.
            log.warning(
                "playwright.page_timeout",
                extra={
                    "url": url,
                    "timeout_ms": timeout_ms,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:500],
                    "fallback_returned": "unrendered_result_dict",
                },
            )
            result["error"] = str(exc)
        except PlaywrightError as exc:
            # Any other Playwright API failure: target closed, navigation
            # error, content extraction error, browser launch failure.
            # Catching the broader `Error` base class covers everything
            # the sync_playwright API can raise other than the timeout
            # case caught above.
            log.warning(
                "playwright.api_error",
                extra={
                    "url": url,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:500],
                    "fallback_returned": "unrendered_result_dict",
                },
            )
            result["error"] = str(exc)
        except Exception as exc:
            # Last-resort fallback for genuinely unexpected non-Playwright
            # errors (e.g., a programmer bug like NameError introduced by
            # a refactor, or an OSError from the headless browser launcher
            # that surfaces outside Playwright's own exception hierarchy).
            # Kept as a separate distinct-event-name path so the operator
            # can alert on `playwright.unexpected_error` specifically: any
            # firing of this branch is either a Playwright version drift
            # or a real bug introduced upstream. Sentinel return preserved
            # so the pipeline stays available.
            log.warning(
                "playwright.unexpected_error",
                extra={
                    "url": url,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:500],
                    "fallback_returned": "unrendered_result_dict",
                },
            )
            result["error"] = str(exc)

    return result


# =========================================================================
# M26.2 — Persistent warm Chromium via a single dedicated render thread.
#
# WHY A DEDICATED THREAD (not a bare module-level Browser): fetch_rendered_page
# is invoked from up to 9 nondeterministic worker threads (anyio threadpool ->
# Phase A pool MAX_PARALLEL_NEWS_ITEMS=3 -> official-candidate pool
# MAX_PARALLEL_OFFICIAL_CANDIDATES=3). Playwright's sync API is THREAD-BOUND:
# a Browser launched in thread X cannot be driven from thread Y (greenlet /
# event-loop error). _PLAYWRIGHT_SEMAPHORE(1) serializes but does NOT pin the
# thread. So the persistent sync_playwright + Browser are confined to ONE
# worker thread (a max_workers=1 ThreadPoolExecutor); every render is submitted
# there and the caller blocks on .result(). This is simultaneously (a)
# thread-safe (single owning thread) and (b) single + sequential by
# construction (LESSON 1: never more than one Chromium, never concurrent
# renders). The Semaphore(1) is retained as defense-in-depth.
#
# FORK SAFETY (worker.py runs RQ, which forks a child per job on POSIX; threads
# do NOT survive fork): the holder is lazy (never launched at import / before
# fork) and stamps the creating PID. On the first render in a forked child,
# os.getpid() != creator pid -> the stale executor/browser references are
# DROPPED (never driven — they belong to the parent) and a fresh executor +
# browser are created in the child.
#
# GATE: config.warm_browser_enabled() (default false, lazy per-call). Gate off
# OR any warm launch/relaunch/submit failure -> the verbatim cold path
# (_fetch_rendered_page_cold) runs as the permanent fallback.
# =========================================================================


_RENDER_EXECUTOR_MAX_WORKERS = 1  # LESSON 1: exactly one render thread.


def _start_playwright():
    """Start a persistent Playwright driver on the CURRENT thread and return
    it. Isolated in its own function so tests can inject a fake without a live
    Chromium. Uses the persistent ``.start()`` form (not the ``with`` context
    manager) so the driver survives across renders; it is stopped on shutdown.
    """
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


class _WarmBrowserHolder:
    """Owns the dedicated render thread + persistent Playwright/Browser.

    All Playwright objects are created and driven ONLY on the single executor
    thread. Submitting threads only call ``executor.submit`` (thread-safe) and
    block on the future. Normal page errors (timeout / nav failure) return the
    same sentinel dict the cold path returns; only infrastructure failures
    (cannot launch/relaunch the browser, dead executor) propagate so the shim
    can fall back to cold.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._executor = None
        self._playwright = None
        self._browser = None
        self._creator_pid = None

    # ---- executor lifecycle (submitting-thread side) ----

    def _executor_for_this_process(self):
        """Return a live executor owned by THIS process, (re)creating it after
        a fork. Caller must hold ``self._lock``."""
        if self._executor is not None and self._creator_pid == os.getpid():
            return self._executor
        # Stale (post-fork) or first use. Drop stale refs WITHOUT driving them
        # — a parent's executor thread / browser does not exist in this child,
        # and Playwright objects are thread-bound so we must not touch them
        # from here. The OS reaps the parent's resources.
        self._executor = None
        self._playwright = None
        self._browser = None
        self._executor = ThreadPoolExecutor(
            max_workers=_RENDER_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="m26-2-warm-render",
        )
        self._creator_pid = os.getpid()
        log.info(
            "warm_browser.render_thread_started",
            extra={"pid": self._creator_pid, "max_workers": _RENDER_EXECUTOR_MAX_WORKERS},
        )
        return self._executor

    def render(self, url: str, timeout_ms: int) -> dict:
        """Submit one render to the dedicated thread and block on the result.
        Raises only on infrastructure failure (launch/relaunch/submit) so the
        caller falls back to cold."""
        with self._lock:
            executor = self._executor_for_this_process()
        future = executor.submit(self._render_on_thread, url, timeout_ms)
        return future.result()

    # ---- render-thread side (single owning thread) ----

    def _ensure_browser(self):
        """Launch the persistent browser if missing/disconnected. Runs ONLY on
        the executor thread. Raises on launch failure (-> cold fallback)."""
        browser = self._browser
        if browser is not None:
            try:
                if browser.is_connected():
                    return browser
            except Exception:  # noqa: BLE001 — treat as disconnected
                pass
            # Disconnected / crashed — drop and relaunch once.
            self._close_browser_on_thread()
            log.warning("warm_browser.relaunch_after_disconnect")
        self._playwright = _start_playwright()
        self._browser = self._playwright.chromium.launch(headless=True)
        return self._browser

    def _render_on_thread(self, url: str, timeout_ms: int) -> dict:
        # NOTE: extraction below is kept byte-identical to
        # _fetch_rendered_page_cold so warm and cold return the same dict for
        # the same page. The ONLY difference is browser/driver reuse.
        result = {
            "url": url,
            "rendered": False,
            "status_code": None,
            "title": None,
            "html": "",
            "text": "",
            "raw_links": [],
            "error": None,
        }

        try:
            from playwright.sync_api import (
                Error as PlaywrightError,
                TimeoutError as PlaywrightTimeoutError,
            )
        except ImportError:  # pragma: no cover - playwright installed in prod
            class PlaywrightError(Exception):
                pass

            class PlaywrightTimeoutError(PlaywrightError):
                pass

        # Defense-in-depth: one render at a time even if a cold render is also
        # somehow active during a gate flip (LESSON 1).
        with _PLAYWRIGHT_SEMAPHORE:
            browser = self._ensure_browser()  # may raise -> cold fallback
            context = None
            page = None
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ko-KR",
                    extra_http_headers={
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                )
                page = context.new_page()
                response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_timeout(1000)

                result["status_code"] = response.status if response else None
                result["title"] = sanitize_text(page.title())
                result["html"] = sanitize_text(page.content())[:500000]
                result["text"] = sanitize_text(page.locator("body").inner_text(timeout=5000))[:50000]
                result["raw_links"] = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                        href: a.href || a.getAttribute('href') || '',
                        text: (a.innerText || a.textContent || '').trim()
                    }))"""
                )
                result["raw_links"] = sanitize_data(result["raw_links"])
                result["rendered"] = True
            except PlaywrightTimeoutError as exc:
                log.warning(
                    "playwright.page_timeout",
                    extra={
                        "url": url,
                        "timeout_ms": timeout_ms,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:500],
                        "fallback_returned": "unrendered_result_dict",
                    },
                )
                result["error"] = str(exc)
            except PlaywrightError as exc:
                log.warning(
                    "playwright.api_error",
                    extra={
                        "url": url,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:500],
                        "fallback_returned": "unrendered_result_dict",
                    },
                )
                result["error"] = str(exc)
            except Exception as exc:
                log.warning(
                    "playwright.unexpected_error",
                    extra={
                        "url": url,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:500],
                        "fallback_returned": "unrendered_result_dict",
                    },
                )
                result["error"] = str(exc)
            finally:
                # Per-render context/page are ALWAYS closed (even on
                # exception/timeout). Only the Browser + driver persist — this
                # is the key 2GB leak guard against accumulating contexts.
                try:
                    if page is not None:
                        page.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if context is not None:
                        context.close()
                except Exception:  # noqa: BLE001
                    pass

        return result

    def _close_browser_on_thread(self) -> None:
        """Close the persistent browser + stop the driver. Runs ONLY on the
        executor thread (Playwright objects are thread-bound). Best-effort."""
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._playwright = None

    # ---- shutdown (atexit) ----

    def shutdown(self) -> None:
        """Close the browser on the render thread and shut the executor.
        Registered with atexit; safe to call when never used (no-op). Only
        drives the browser when this process owns the executor (same PID) so
        a forked child never touches the parent's objects."""
        with self._lock:
            executor = self._executor
            same_process = self._creator_pid == os.getpid()
            self._executor = None
        if executor is None:
            return
        if same_process:
            try:
                executor.submit(self._close_browser_on_thread).result(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        try:
            executor.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


_WARM_BROWSER = _WarmBrowserHolder()
atexit.register(_WARM_BROWSER.shutdown)


def fetch_rendered_page(url: str, timeout_ms: int = 15000) -> dict:
    """Render ``url`` and return the page dict.

    Gate-off (default) or any warm-path infrastructure failure -> the verbatim
    cold path :func:`_fetch_rendered_page_cold` (a fresh cold launch/teardown).
    Gate-on -> reuse the persistent warm Chromium via the dedicated render
    thread. Both paths return byte-identical extraction for the same page, so
    candidate selection and the downstream verdict are unchanged.
    """
    if not config.warm_browser_enabled():
        return _fetch_rendered_page_cold(url, timeout_ms)
    try:
        return _WARM_BROWSER.render(url, timeout_ms)
    except Exception as exc:  # noqa: BLE001 — infra failure -> cold fallback
        log.warning(
            "warm_browser.fallback_to_cold",
            extra={
                "url": url,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:500],
                "fallback_returned": "cold_path",
            },
        )
        return _fetch_rendered_page_cold(url, timeout_ms)


def _is_bad_raw_link(url: str, text: str) -> bool:
    normalized_url = (url or "").lower().strip()
    normalized_text = (text or "").lower().strip()

    if len(normalized_url) <= 10 or len(normalized_text) <= 5:
        return True
    if normalized_url.startswith("#") or normalized_url == "#":
        return True
    if any(
        keyword in normalized_url
        for keyword in ["javascript:", "login", "sitemap", "main.do", "portal", "home"]
    ):
        return True
    if any(keyword in normalized_text for keyword in ["로그인", "사이트맵", "홈", "메인"]):
        return True

    return False


def _score_raw_link(url: str, text: str, query: str, base_url: str) -> tuple[int, str]:
    normalized_url = (url or "").lower()
    normalized_text = text or ""
    combined = f"{url} {text}".lower()
    score = 0
    reasons = []

    if urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower():
        score += 15
        reasons.append("same domain")
    if any(part in normalized_url for part in ["detail", "view", "dtl"]):
        score += 20
        reasons.append("detail/view/dtl url")
    if any(part in normalized_url for part in ["news", "press", "bbs"]):
        score += 15
        reasons.append("news/press/bbs url")
    if re.search(r"/no01010[12]/\d{4,}", normalized_url):
        score += 35
        reasons.append("fsc detail press url")
    if re.search(r"\d{4,}", normalized_url):
        score += 10
        reasons.append("numeric id")
    if len(normalized_text) > 15:
        score += 10
        reasons.append("descriptive text")
    if any(part in normalized_url for part in ["list", "search", "paging", "pagination"]):
        score -= 35
        reasons.append("list/search page penalty")
    if normalized_text.strip().lower() in {"\ubcf4\ub3c4\uc790\ub8cc", "\ub354\ubcf4\uae30", "\ubaa9\ub85d", "list", "more"}:
        score -= 35
        reasons.append("generic list text penalty")

    query_hits = 0
    for token in (query or "").split():
        token = token.strip().lower()
        if len(token) >= 2 and token in combined:
            query_hits += 1

    if query_hits:
        score += min(20, query_hits * 10)
        reasons.append("query keyword")

    return score, "; ".join(reasons) if reasons else "raw rendered link"


def _extract_raw_rendered_links(raw_links: list[dict], base_url: str, query: str, site_key: str, max_links: int) -> dict:
    seen_urls = set()
    filtered = []
    rejected = 0

    for raw_link in raw_links or []:
        href = (raw_link.get("href") or "").strip()
        text = (raw_link.get("text") or "").strip()
        absolute_url = urljoin(base_url, href)

        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        if _is_bad_raw_link(absolute_url, text):
            rejected += 1
            continue

        score, reason = _score_raw_link(absolute_url, text, query, base_url)
        if score <= 0:
            rejected += 1
            continue

        filtered.append(
            {
                "url": absolute_url,
                "text": text[:200],
                "score": score,
                "link_score": score,
                "reason": reason,
                "link_reason": reason,
                "same_domain": urlparse(absolute_url).netloc.lower() == urlparse(base_url).netloc.lower(),
                "site_key": site_key,
                "selector": "page.evaluate:a[href]",
            }
        )

    filtered.sort(
        key=lambda item: (item.get("score", 0), item.get("same_domain", False), len(item.get("text", ""))),
        reverse=True,
    )

    return {
        "links": filtered[:max_links],
        "filtered_links_count": len(filtered),
        "rejected_links_count": rejected,
    }


def extract_rendered_links(
    url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
) -> dict:
    rendered_page = fetch_rendered_page(url)
    links = []
    rejected_links_count = 0
    raw_links_count = len(rendered_page.get("raw_links") or [])
    filtered_links_count = 0
    parser_used = None

    if rendered_page.get("rendered"):
        try:
            site_key = get_site_key(url, source_name)
            rendered_extractors = {
                "fsc": extract_fsc_rendered_links,
                "fss": extract_fss_rendered_links,
                "gov24": extract_gov24_rendered_links,
                "ibk": extract_ibk_rendered_links,
            }
            extractor = rendered_extractors.get(site_key)

            if extractor:
                parsed = extractor(
                    rendered_page.get("html") or "",
                    url,
                    source_name=source_name,
                    query=query,
                    max_links=max_links,
                )
                links = parsed.get("links") or []
                rejected_links_count = parsed.get("rejected_links_count", 0)
                parser_used = parsed.get("parser_used")

            if not links:
                raw_parsed = _extract_raw_rendered_links(
                    rendered_page.get("raw_links") or [],
                    url,
                    query=query,
                    site_key=site_key,
                    max_links=max_links,
                )
                links = raw_parsed.get("links") or []
                filtered_links_count = raw_parsed.get("filtered_links_count", 0)
                rejected_links_count += raw_parsed.get("rejected_links_count", 0)
                parser_used = "rendered_raw_a_href"

            if not links:
                links = extract_links_for_site(
                    rendered_page.get("html") or "",
                    url,
                    source_name=source_name,
                    query=query,
                    max_links=max_links,
                )
                parser_used = parser_used or "rendered_generic_fallback"
        except Exception as exc:
            rendered_page["error"] = str(exc)

    return sanitize_data({
        "rendered_used": bool(rendered_page.get("rendered")),
        "rendered_status_code": rendered_page.get("status_code"),
        "rendered_title": rendered_page.get("title"),
        "rendered_text_snippet": (rendered_page.get("text") or "")[:1000],
        "rendered_html_snippet": (rendered_page.get("html") or "")[:2000],
        "rendered_links": links,
        "rendered_links_count": len(links),
        "rendered_candidate_links_count": len(links),
        "raw_links_count": raw_links_count,
        "filtered_links_count": filtered_links_count or len(links),
        "final_candidate_links_count": len(links),
        "rendered_rejected_links_count": rejected_links_count,
        "rendered_parser_used": parser_used,
        "rendered_error": rendered_page.get("error"),
    })
