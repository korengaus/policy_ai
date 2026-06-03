"""M20 Phase 1 — abstract source-provider interface.

This module defines a tiny, dependency-light provider surface shaped so it
can accept BOTH search-type providers (query -> list of normalized hits) AND,
in a future milestone, primary-document-type providers (structured document
returned directly).

Design contract (mirrors ``semantic_embeddings.EmbeddingProvider``):

    * No network at import time.
    * ``search`` / ``fetch_document`` NEVER raise out to the caller. A missing
      key / disabled provider / transport error yields ``available=False``
      (or an ``error``-populated result) and an empty item list — the caller
      keeps running.
    * Providers never assert truth and never grant any reliability uplift —
      they only *provide candidates*. No ``truth_claim`` field is ever set on
      a hit.
    * Secrets are NEVER logged or echoed in any result / status dict.

Runtime values are plain ``dict``s; the ``TypedDict`` definitions below are
documentation-only (no pydantic — matches the codebase convention in
``source_registry`` and ``semantic_embeddings``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class SearchHit(TypedDict, total=False):
    """One normalized search result.

    Shaped to be drop-in compatible with the news-result dict the pipeline
    already consumes (see ``news_collector._entry_to_news`` / ``_fallback_item``):
    ``title`` / ``summary`` / ``original_url`` / ``link`` / ``published`` /
    ``published_at`` / ``source``. ``google_link`` is set equal to
    ``original_url`` so ``news_collector.resolve_google_news_url`` short-circuits
    (non-Google URLs are returned unchanged). ``publisher`` and ``raw`` are
    additive and ignored by the existing consumers.
    """

    title: str
    summary: str
    original_url: str
    link: str
    google_link: str
    published: str        # RFC-2822 / RFC-1123 — must parse via parsedate_to_datetime
    published_at: str     # ISO-8601
    source: str           # provider provenance tag, e.g. "naver_api"
    publisher: str        # resolved domain (for future reliability grading)
    raw: Dict[str, Any]   # untouched provider fields (audit / debug)


class SearchProviderResult(TypedDict, total=False):
    """Return shape of :meth:`SearchProvider.search`."""

    provider: str
    query: str
    available: bool
    items: List[SearchHit]
    total_available: int  # provider-reported total match count (Naver "total")
    fetched_count: int
    error: Optional[str]
    debug: Dict[str, Any]


class DocumentProviderResult(TypedDict, total=False):
    """Return shape for a primary-document provider.

    M21: the Policy Briefing ``pressReleaseList`` endpoint returns MANY items
    per call (a date-windowed feed), so ``documents`` (a list) is the field
    that provider populates. ``document`` (singular) is retained for a future
    fetch-one-by-id provider. Both are optional (``total=False``)."""

    provider: str
    available: bool
    document: Optional[Dict[str, Any]]
    documents: List[Dict[str, Any]]
    error: Optional[str]
    debug: Dict[str, Any]


class BaseSourceProvider:
    """Common provider surface.

    Subclasses set:
        * ``available`` — True only when the provider can actually return
          results (key present, gate on, transport importable).
        * ``configured`` — True when all required env config is present
          (regardless of reachability). Distinguishes "operator didn't set
          this up" from "set up but couldn't run".
        * ``external_calls_possible`` — True when a call could hit the network.
          False for disabled and deterministic/mock providers.
        * ``reason`` — short, JSON-safe, secret-free human summary.
        * ``error`` — short, secret-free error summary (or None).
    """

    name: str = "base"
    available: bool = False
    configured: bool = False
    external_calls_possible: bool = False
    reason: str = ""
    error: Optional[str] = None

    def provider_status(self) -> Dict[str, Any]:
        """JSON-safe status snapshot. Never includes secrets."""
        return {
            "provider": self.name,
            "available": bool(self.available),
            "configured": bool(self.configured),
            "external_calls_possible": bool(self.external_calls_possible),
            "reason": self.reason or "",
            "error": self.error,
        }


class SearchProvider(BaseSourceProvider):
    """Search-type provider: ``query -> list[SearchHit]``."""

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        sort: str = "sim",
    ) -> SearchProviderResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def _empty_result(
        self,
        query: str,
        *,
        error: Optional[str] = None,
        debug: Optional[Dict[str, Any]] = None,
    ) -> SearchProviderResult:
        """Build an empty, never-raising result. Shared by the disabled
        provider and every error path so the shape stays uniform."""
        return {
            "provider": self.name,
            "query": query or "",
            "available": bool(self.available),
            "items": [],
            "total_available": 0,
            "fetched_count": 0,
            "error": error,
            "debug": debug or {},
        }


class PrimaryDocumentProvider(BaseSourceProvider):
    """Primary-document-type provider.

    Unlike a ``SearchProvider`` (query -> candidate hits), this returns the
    original document text directly. M21 implements the first concrete
    subclass (Policy Briefing press releases) via ``fetch_press_releases``;
    the abstract ``fetch_document`` (one-by-id) stays reserved."""

    def fetch_document(
        self, identifier: Any,
    ) -> DocumentProviderResult:  # pragma: no cover - reserved
        raise NotImplementedError

    def _empty_result(
        self,
        *,
        error: Optional[str] = None,
        debug: Optional[Dict[str, Any]] = None,
    ) -> DocumentProviderResult:
        """Build an empty, never-raising document result. Shared by the
        disabled provider and every error path so the shape stays uniform."""
        return {
            "provider": self.name,
            "available": bool(self.available),
            "document": None,
            "documents": [],
            "error": error,
            "debug": debug or {},
        }
