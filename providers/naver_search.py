"""M20 Phase 1 — Naver news SearchProvider.

Implements a search-type provider against the Naver news search API:

    GET https://openapi.naver.com/v1/search/news.json
    headers: X-Naver-Client-Id / X-Naver-Client-Secret
    params:  query (UTF-8), display (<=100), start (<=1000), sort (sim|date)

Response items carry metadata + summary only (title / originallink / link /
description / pubDate) — copyright-safe. The free tier is 25,000 calls/day,
max 100 results per call.

Fail-closed contract (mirrors ``semantic_embeddings.OpenAIEmbeddingProvider``):
    * No network at import time.
    * ``search`` NEVER raises out to the caller.
    * Missing key / disabled gate -> ``DisabledSearchProvider``:
      ``available=False`` + populated ``reason`` + empty result + ZERO network.
    * Transport / HTTP-error / malformed-shape -> empty ``items`` + ``error``
      set, never raises.
    * Secrets (client id / secret) are NEVER logged or echoed in any result.

NOTE (live-spec caveat, M20 §7): the official Naver developer docs were
unreachable when this was authored, so error handling here branches on
``status_code`` + presence-of-``items`` ONLY — it does NOT switch on specific
Naver error codes (e.g. SE01 / 010 / 024 / 028). A future milestone can harden
this once the live spec (exact error codes + quota tier) is confirmed.
"""

from __future__ import annotations

import html
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import config

from structured_logging import get_logger

from .base import SearchHit, SearchProvider, SearchProviderResult


log = get_logger(__name__)


NAVER_NEWS_ENDPOINT = "https://openapi.naver.com/v1/search/news.json"

# Naver-documented maxima (confirmed via spec mirror; see module docstring).
MAX_DISPLAY = 100
MAX_START = 1000

_SOURCE_TAG = "naver_api"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: Optional[str]) -> str:
    """Strip HTML tags (Naver embeds <b>/</b> around matched terms) and
    unescape entities. ``sanitize_text`` already does ``html.unescape`` +
    mojibake repair + whitespace collapse; we strip tags first.

    We deliberately do NOT import ``news_collector.clean_html`` (a MIGRATED_FILES
    member) — a tiny local helper keeps the provider package decoupled."""
    from text_utils import sanitize_text

    if not text:
        return ""
    no_tags = _TAG_RE.sub("", text)
    return sanitize_text(html.unescape(no_tags))


def _publisher_from_url(url: str) -> str:
    netloc = urlparse(url or "").netloc
    return netloc.replace("www.", "")


def _pubdate_to_iso(pub_date: str) -> str:
    """Convert a Naver RFC-1123 ``pubDate`` to an ISO-8601 UTC string.
    Returns "" when unparseable — never raises."""
    if not pub_date:
        return ""
    try:
        parsed = parsedate_to_datetime(pub_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _clamp(value: int, lo: int, hi: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = lo
    return max(lo, min(hi, value))


class DisabledSearchProvider(SearchProvider):
    """Returned when the gate is off or a credential is absent. Every call is
    a pure no-op so callers never special-case the disabled state."""

    external_calls_possible = False

    def __init__(self, *, name: str = "naver_api", reason: str = "search provider disabled") -> None:
        self.name = name
        self.available = False
        self.configured = False
        self.reason = reason
        self.error = reason

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        sort: str = "sim",
    ) -> SearchProviderResult:
        return self._empty_result(query, error=self.reason)


class NaverNewsSearchProvider(SearchProvider):
    """Real Naver news search provider.

    ``available`` is True only when the gate is on AND both credentials are
    present. ``configured`` is True when both credentials are present
    regardless of the gate. Constructed without any network call.
    """

    name = "naver_api"
    external_calls_possible = True

    def __init__(self) -> None:
        self._client_id = config.naver_client_id()
        self._client_secret = config.naver_client_secret()
        self._timeout = config.naver_search_timeout_seconds()
        self.error = None
        self.configured = bool(self._client_id and self._client_secret)

        if not config.naver_search_enabled():
            self.available = False
            self.reason = "NAVER_SEARCH_ENABLED=false"
            self.error = self.reason
            return
        if not self._client_id:
            self.available = False
            self.reason = "NAVER_CLIENT_ID missing"
            self.error = self.reason
            return
        if not self._client_secret:
            self.available = False
            self.reason = "NAVER_CLIENT_SECRET missing"
            self.error = self.reason
            return
        self.available = True
        self.reason = "naver news search provider ready"

    def _headers(self) -> Dict[str, str]:
        # Secrets live ONLY in the request headers; never logged.
        return {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
        }

    def _normalize_item(self, item: Dict[str, Any]) -> SearchHit:
        original_url = (item.get("originallink") or "").strip()
        naver_link = (item.get("link") or "").strip()
        pub_date = item.get("pubDate") or ""
        return {
            "title": _strip_html(item.get("title")),
            "summary": _strip_html(item.get("description")),
            "original_url": original_url,
            "link": naver_link,
            # google_link == original_url so resolve_google_news_url short-circuits
            # (it returns non-Google URLs unchanged).
            "google_link": original_url,
            "published": pub_date,
            "published_at": _pubdate_to_iso(pub_date),
            "source": _SOURCE_TAG,
            "publisher": _publisher_from_url(original_url),
            "raw": dict(item),
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        sort: str = "sim",
    ) -> SearchProviderResult:
        if not self.available:
            return self._empty_result(query, error=self.reason)
        if not query or not str(query).strip():
            return self._empty_result(query, error="empty query")

        display = _clamp(limit, 1, MAX_DISPLAY)
        start = _clamp(start, 1, MAX_START)
        sort = sort if sort in ("sim", "date") else "sim"
        params = {"query": str(query), "display": display, "start": start, "sort": sort}

        # Local import keeps the app importable without requests in odd envs
        # and keeps "no network at import time" trivially true.
        try:
            import requests
        except Exception as import_error:  # pragma: no cover - requests is a dep
            return self._empty_result(
                query,
                error=f"requests not importable: {type(import_error).__name__}",
            )

        try:
            response = requests.get(
                NAVER_NEWS_ENDPOINT,
                headers=self._headers(),
                params=params,
                timeout=self._timeout,
            )
        except Exception as call_error:
            # Never log the headers (secrets). Type + short message only.
            log.warning(
                "naver_search.request_failed",
                extra={
                    "error_type": type(call_error).__name__,
                    "error_message": str(call_error)[:200],
                    "display": display,
                    "start": start,
                    "sort": sort,
                },
            )
            return self._empty_result(
                query,
                error=f"request failed: {type(call_error).__name__}",
                debug={"display": display, "start": start, "sort": sort},
            )

        status_code = getattr(response, "status_code", None)
        debug: Dict[str, Any] = {
            "status_code": status_code,
            "display": display,
            "start": start,
            "sort": sort,
        }

        # Defensive: branch on status_code + presence-of-items only (NOT on
        # Naver-specific error codes — see module docstring caveat).
        if status_code != 200:
            log.warning(
                "naver_search.non_200",
                extra={"status_code": status_code, "display": display, "start": start},
            )
            return self._empty_result(
                query, error=f"http status {status_code}", debug=debug,
            )

        try:
            payload = response.json()
        except Exception as parse_error:
            return self._empty_result(
                query,
                error=f"json parse failed: {type(parse_error).__name__}",
                debug=debug,
            )

        if not isinstance(payload, dict):
            return self._empty_result(query, error="unexpected response shape", debug=debug)

        items_raw = payload.get("items")
        if not isinstance(items_raw, list):
            return self._empty_result(query, error="missing items array", debug=debug)

        hits: List[SearchHit] = []
        for item in items_raw:
            if isinstance(item, dict):
                hits.append(self._normalize_item(item))

        total_available = payload.get("total")
        try:
            total_available = int(total_available)
        except (TypeError, ValueError):
            total_available = len(hits)

        return {
            "provider": self.name,
            "query": str(query),
            "available": True,
            "items": hits,
            "total_available": total_available,
            "fetched_count": len(hits),
            "error": None,
            "debug": debug,
        }


class MockNaverSearchProvider(SearchProvider):
    """Deterministic, network-free provider for tests and local development.

    Returns canned ``items`` (raw Naver-shaped dicts) run through the SAME
    normalization as the real provider, so downstream code can be exercised
    without a key or the network. Mirrors
    ``DeterministicHashEmbeddingProvider``.
    """

    name = "naver_api"
    external_calls_possible = False

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, *, total: Optional[int] = None) -> None:
        self.available = True
        self.configured = True
        self.reason = "deterministic mock provider: no network"
        self.error = None
        self._items = list(items) if items is not None else list(_DEFAULT_MOCK_ITEMS)
        self._total = total

    # Reuse the real normalizer so the mock and live shapes can't drift.
    _normalize_item = NaverNewsSearchProvider._normalize_item

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        sort: str = "sim",
    ) -> SearchProviderResult:
        display = _clamp(limit, 1, MAX_DISPLAY)
        start = _clamp(start, 1, MAX_START)
        hits: List[SearchHit] = [
            self._normalize_item(item) for item in self._items[:display] if isinstance(item, dict)
        ]
        total = self._total if self._total is not None else len(hits)
        return {
            "provider": self.name,
            "query": str(query or ""),
            "available": True,
            "items": hits,
            "total_available": total,
            "fetched_count": len(hits),
            "error": None,
            "debug": {"mock": True, "display": display, "start": start, "sort": sort},
        }


_DEFAULT_MOCK_ITEMS: List[Dict[str, Any]] = [
    {
        "title": "<b>전세대출</b> 규제 강화 &quot;실수요자 보호&quot;",
        "originallink": "https://www.example-press.co.kr/article/123",
        "link": "https://n.news.naver.com/mnews/article/001/0000000123",
        "description": "정부가 <b>전세대출</b> 규제를 강화한다고 밝혔다. &lt;관계부처&gt; 협의 결과.",
        "pubDate": "Mon, 02 Jun 2025 09:30:00 +0900",
    },
    {
        "title": "주택담보대출 금리 동향",
        "originallink": "https://www.another-press.com/news/456",
        "link": "https://n.news.naver.com/mnews/article/002/0000000456",
        "description": "주담대 금리가 소폭 상승했다.",
        "pubDate": "Sun, 01 Jun 2025 18:00:00 +0900",
    },
]
