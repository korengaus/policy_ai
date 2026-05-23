"""Shared HTTP cache (M13.3a) — infrastructure only, NOT yet integrated.

Provides an opt-in cache layer for HTTP GET responses, designed to
reduce duplicate URL fetches across ``analyze_pipeline``. M13.3a
builds the module + CLI + tests. M13.3b integrates it with specific
call sites behind a feature flag.

Design
------

* In-memory ``dict`` (Python 3.7+ preserves insertion order, used for
  LRU eviction). No filesystem persistence in M13.3a.
* TTL per entry (default 1 hour, configurable per call and per env).
* Cache key = ``sha256(normalized_url + content-affecting-headers)``.
* Respects ``Cache-Control: no-store`` / ``no-cache`` / ``private`` /
  ``max-age`` (basic parsing only).
* Domain allow-list and deny-list (both empty by default — operator
  opts in per domain in M13.3b).
* Counters for hits / misses / expired / refused-by-domain.
* Single-process only. NOT shared across worker processes.

Safety
------

* ``get`` returns ``None`` if cache disabled OR domain not allowed OR
  entry expired.
* ``put`` returns ``False`` if cache disabled OR domain not allowed OR
  a refusing ``Cache-Control`` header is present.
* Every public method NEVER raises — errors are logged and swallowed.
* The module imports only stdlib. No ``requests`` / ``httpx`` /
  ``socket`` / network I/O. The cache stores bytes the caller has
  already fetched; it does not fetch anything itself.
* No pipeline module imports this in M13.3a — the
  ``test_pipeline_isolation_pin`` test pins that contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature flag and configuration
# ---------------------------------------------------------------------------


def is_http_cache_enabled() -> bool:
    """Returns True iff env var ``HTTP_CACHE_ENABLED`` equals ``"true"``
    (case-insensitive, leading/trailing whitespace stripped).

    Any other value — including unset, empty, ``"false"``, ``"0"``,
    ``"no"``, ``"yes"``, ``"1"`` — returns False. Strict equality with
    ``"true"`` was chosen so an operator who typed a typo does not
    silently enable the cache.
    """
    return os.environ.get("HTTP_CACHE_ENABLED", "").strip().lower() == "true"


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_nonneg_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def get_default_ttl_seconds() -> int:
    """Default TTL when neither the caller nor Cache-Control specifies.
    Default 3600s (1 hour). Configurable via env
    ``HTTP_CACHE_DEFAULT_TTL_SECONDS``."""
    return _env_nonneg_int("HTTP_CACHE_DEFAULT_TTL_SECONDS", 3600)


def get_max_entries() -> int:
    """Maximum in-memory entries before LRU eviction kicks in. Default 500.
    Configurable via env ``HTTP_CACHE_MAX_ENTRIES``."""
    return _env_positive_int("HTTP_CACHE_MAX_ENTRIES", 500)


# Domains the cache is willing to store. Empty in M13.3a; M13.3b
# populates per integration step. Empty allow-list means "allow any
# domain not in the deny-list".
DEFAULT_ALLOWED_DOMAINS: frozenset = frozenset()

# Domains the cache will refuse to store regardless of allow-list.
# Empty in M13.3a; operator may add e.g. internal admin endpoints in
# M13.3b.
DEFAULT_DENIED_DOMAINS: frozenset = frozenset()


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


# Only these request-header keys are folded into the cache key. Other
# headers (Authorization, Cookie, custom internals) are deliberately
# excluded — caching keyed on them would either defeat sharing or risk
# leaking private content to a different requester.
HEADERS_AFFECTING_CONTENT = (
    "accept",
    "accept-language",
    "user-agent",
)


def _normalize_url(url: str) -> str:
    """Lowercase scheme and host, strip trailing slash for non-root
    paths, drop fragment. Invalid input returns unchanged so the
    function never raises."""
    if not isinstance(url, str) or not url:
        return url or ""
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        netloc = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if path.endswith("/") and path != "/":
            path = path.rstrip("/")
        query = parsed.query or ""
        rebuilt = f"{scheme}://{netloc}{path}"
        if query:
            rebuilt = f"{rebuilt}?{query}"
        return rebuilt
    except Exception:  # noqa: BLE001 — defensive; urlparse is robust
        return url


def _canonical_headers(headers: Optional[dict]) -> str:
    """Serialize content-affecting headers with lowercased keys, sorted."""
    if not headers:
        return ""
    norm = {}
    for k, v in headers.items():
        if not k:
            continue
        lk = str(k).lower()
        if lk in HEADERS_AFFECTING_CONTENT:
            norm[lk] = str(v)
    if not norm:
        return ""
    return json.dumps(norm, sort_keys=True, separators=(",", ":"))


def compute_cache_key(url: str, headers: Optional[dict] = None) -> str:
    """SHA256 hex digest of normalized URL + canonical-headers JSON."""
    normalized = _normalize_url(url)
    headers_str = _canonical_headers(headers)
    payload = f"{normalized}|{headers_str}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def extract_domain(url: str) -> str:
    """Lowercase host without port. Empty string on parse failure."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    return netloc


# ---------------------------------------------------------------------------
# Cache-Control header parsing (minimal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheControl:
    """Just enough of RFC 9111 to make the right decisions in M13.3a."""

    no_store: bool = False
    no_cache: bool = False
    max_age_seconds: Optional[int] = None
    # ``private`` is treated like no_store for this shared cache — we
    # are not a per-user cache so we should not store responses marked
    # private to a single user.
    private: bool = False


_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)", re.IGNORECASE)


def parse_cache_control(header_value: Optional[str]) -> CacheControl:
    """Best-effort parse. Unknown directives are ignored; malformed
    ``max-age=`` values surface as ``None`` rather than 0."""
    if not header_value:
        return CacheControl()
    text = str(header_value).lower()
    no_store = "no-store" in text
    no_cache = "no-cache" in text
    private = "private" in text
    max_age = None
    match = _MAX_AGE_RE.search(text)
    if match:
        try:
            max_age = int(match.group(1))
        except ValueError:
            max_age = None
    return CacheControl(
        no_store=no_store,
        no_cache=no_cache,
        max_age_seconds=max_age,
        private=private,
    )


# ---------------------------------------------------------------------------
# Cache entry + stats
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    key: str
    url: str
    body: bytes
    status_code: int
    headers: dict
    fetched_at: float
    expires_at: float
    bytes_size: int = 0

    def is_expired(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.time()
        return now >= self.expires_at


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    expired: int = 0
    refused_by_domain: int = 0
    refused_by_cache_control: int = 0
    evicted: int = 0
    stored: int = 0
    disabled_calls: int = 0


# ---------------------------------------------------------------------------
# HttpCache
# ---------------------------------------------------------------------------


class HttpCache:
    """Thread-safe in-memory cache with LRU eviction.

    Typical usage::

        cache = get_default_cache()
        entry = cache.get(url, request_headers)
        if entry is None:
            body, status, headers = real_fetch(url, request_headers)
            cache.put(url, body, status, headers,
                      request_headers=request_headers)
        else:
            body = entry.body

    M13.3a does NOT wire this into any production module. The block
    above is illustrative for M13.3b.
    """

    def __init__(
        self,
        max_entries: Optional[int] = None,
        default_ttl_seconds: Optional[int] = None,
        allowed_domains: Optional[Iterable[str]] = None,
        denied_domains: Optional[Iterable[str]] = None,
    ):
        self._lock = threading.RLock()
        self._max_entries = (
            max_entries if max_entries is not None else get_max_entries()
        )
        self._default_ttl = (
            default_ttl_seconds
            if default_ttl_seconds is not None
            else get_default_ttl_seconds()
        )
        self._allowed = frozenset(
            d.lower() for d in (allowed_domains or DEFAULT_ALLOWED_DOMAINS)
        )
        self._denied = frozenset(
            d.lower() for d in (denied_domains or DEFAULT_DENIED_DOMAINS)
        )
        # dict, not OrderedDict — Python 3.7+ preserves insertion order
        # which is enough for the simple LRU eviction we need here.
        self._store: dict = {}
        self.stats = CacheStats()

    # ----------------- internal helpers -----------------

    def _domain_allowed(self, url: str) -> bool:
        domain = extract_domain(url)
        if not domain:
            return False
        if domain in self._denied:
            return False
        if not self._allowed:
            return True
        return domain in self._allowed

    @staticmethod
    def _extract_cache_control(headers: Optional[dict]) -> CacheControl:
        if not headers:
            return CacheControl()
        for k, v in headers.items():
            if k and str(k).lower() == "cache-control":
                return parse_cache_control(v)
        return CacheControl()

    # ----------------- public API -----------------

    def get(self, url: str, headers: Optional[dict] = None) -> Optional[CacheEntry]:
        """Return the cached entry, or ``None`` on miss / expired /
        disabled / refused. NEVER raises."""
        try:
            if not is_http_cache_enabled():
                with self._lock:
                    self.stats.disabled_calls += 1
                return None
            if not self._domain_allowed(url):
                with self._lock:
                    self.stats.refused_by_domain += 1
                return None
            key = compute_cache_key(url, headers)
            with self._lock:
                entry = self._store.get(key)
                if entry is None:
                    self.stats.misses += 1
                    return None
                if entry.is_expired():
                    # Leave the expired entry in place; future ``put``
                    # calls will evict by LRU order. Counting it under
                    # ``expired`` separates "you got nothing because the
                    # cache had nothing" from "you got nothing because
                    # the entry timed out".
                    self.stats.expired += 1
                    return None
                # LRU touch: move to most-recent.
                del self._store[key]
                self._store[key] = entry
                self.stats.hits += 1
                return entry
        except Exception as exc:  # noqa: BLE001 — never propagate
            log.warning("HttpCache.get unexpected error: %s", exc)
            return None

    def put(
        self,
        url: str,
        body: bytes,
        status_code: int = 200,
        headers: Optional[dict] = None,
        ttl_seconds: Optional[int] = None,
        request_headers: Optional[dict] = None,
    ) -> bool:
        """Store a response. Returns ``True`` on success, ``False`` if
        refused or disabled. NEVER raises.

        TTL precedence:

        1. Explicit ``ttl_seconds`` argument.
        2. ``Cache-Control: max-age=N`` on the response.
        3. ``HTTP_CACHE_DEFAULT_TTL_SECONDS`` env (or the constructor
           default).

        ``Cache-Control: no-store`` / ``no-cache`` / ``private`` all
        cause the call to return ``False`` without storing. M13.3a
        does not implement revalidation, so ``no-cache`` is treated as
        "do not store"; M13.3b may add ETag/If-Modified-Since support.
        """
        try:
            if not is_http_cache_enabled():
                with self._lock:
                    self.stats.disabled_calls += 1
                return False
            if not self._domain_allowed(url):
                with self._lock:
                    self.stats.refused_by_domain += 1
                return False

            cc = self._extract_cache_control(headers)
            if cc.no_store or cc.private or cc.no_cache:
                with self._lock:
                    self.stats.refused_by_cache_control += 1
                return False

            # Resolve TTL.
            effective_ttl = ttl_seconds
            if effective_ttl is None and cc.max_age_seconds is not None:
                effective_ttl = cc.max_age_seconds
            if effective_ttl is None:
                effective_ttl = self._default_ttl
            if effective_ttl <= 0:
                with self._lock:
                    self.stats.refused_by_cache_control += 1
                return False

            key = compute_cache_key(url, request_headers)
            now = time.time()
            entry = CacheEntry(
                key=key,
                url=url,
                body=body or b"",
                status_code=status_code,
                headers=dict(headers or {}),
                fetched_at=now,
                expires_at=now + float(effective_ttl),
                bytes_size=len(body or b""),
            )

            with self._lock:
                # If a colliding key already exists, drop it so the
                # replacement becomes the newest. Otherwise eviction
                # bookkeeping below could prematurely remove it.
                if key in self._store:
                    del self._store[key]
                while len(self._store) >= self._max_entries:
                    oldest_key = next(iter(self._store))
                    del self._store[oldest_key]
                    self.stats.evicted += 1
                self._store[key] = entry
                self.stats.stored += 1
                return True
        except Exception as exc:  # noqa: BLE001 — never propagate
            log.warning("HttpCache.put unexpected error: %s", exc)
            return False

    def clear(self) -> int:
        """Remove all entries. Returns count removed. NEVER raises."""
        try:
            with self._lock:
                n = len(self._store)
                self._store.clear()
                return n
        except Exception as exc:  # noqa: BLE001
            log.warning("HttpCache.clear unexpected error: %s", exc)
            return 0

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def snapshot(self) -> dict:
        """Diagnostic snapshot for the CLI. Body bytes are NOT
        included — operators should not see raw response bodies via
        the diagnostic CLI."""
        with self._lock:
            entries_preview = [
                {
                    "url": e.url,
                    "status_code": e.status_code,
                    "bytes": e.bytes_size,
                    "fetched_at": e.fetched_at,
                    "expires_at": e.expires_at,
                    "expired": e.is_expired(),
                }
                for e in list(self._store.values())[:20]
            ]
            return {
                "enabled": is_http_cache_enabled(),
                "max_entries": self._max_entries,
                "default_ttl_seconds": self._default_ttl,
                "allowed_domains": sorted(self._allowed),
                "denied_domains": sorted(self._denied),
                "current_size": len(self._store),
                "stats": {
                    "hits": self.stats.hits,
                    "misses": self.stats.misses,
                    "expired": self.stats.expired,
                    "refused_by_domain": self.stats.refused_by_domain,
                    "refused_by_cache_control":
                        self.stats.refused_by_cache_control,
                    "evicted": self.stats.evicted,
                    "stored": self.stats.stored,
                    "disabled_calls": self.stats.disabled_calls,
                },
                "entries_preview": entries_preview,
            }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_default_cache: Optional[HttpCache] = None
_default_cache_lock = threading.Lock()


def get_default_cache() -> HttpCache:
    """Process-wide singleton. Lazily constructed."""
    global _default_cache
    if _default_cache is None:
        with _default_cache_lock:
            if _default_cache is None:
                _default_cache = HttpCache()
    return _default_cache


def reset_default_cache_for_tests() -> None:
    """Test helper: force a fresh singleton on the next call to
    :func:`get_default_cache`. Clears the previous instance's store
    so cross-test pollution is impossible."""
    global _default_cache
    with _default_cache_lock:
        if _default_cache is not None:
            _default_cache.clear()
        _default_cache = None


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """Small diagnostic dict used by ``scripts/check_http_cache.py``.
    Does not modify state. NEVER raises."""
    try:
        cache = get_default_cache()
        snap = cache.snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("http_cache.health_check unexpected error: %s", exc)
        return {
            "enabled": is_http_cache_enabled(),
            "current_size": 0,
            "max_entries": get_max_entries(),
            "default_ttl_seconds": get_default_ttl_seconds(),
            "allowed_domains": sorted(DEFAULT_ALLOWED_DOMAINS),
            "denied_domains": sorted(DEFAULT_DENIED_DOMAINS),
            "stats": {
                "hits": 0, "misses": 0, "expired": 0,
                "refused_by_domain": 0, "refused_by_cache_control": 0,
                "evicted": 0, "stored": 0, "disabled_calls": 0,
            },
            "error": str(exc),
        }
    return {
        "enabled": snap["enabled"],
        "current_size": snap["current_size"],
        "max_entries": snap["max_entries"],
        "default_ttl_seconds": snap["default_ttl_seconds"],
        "allowed_domains": snap["allowed_domains"],
        "denied_domains": snap["denied_domains"],
        "stats": snap["stats"],
    }
