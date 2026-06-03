"""M20 Phase 1 — external source providers package.

Public surface:

    BaseSourceProvider, SearchProvider, PrimaryDocumentProvider
    SearchHit, SearchProviderResult, DocumentProviderResult
    NaverNewsSearchProvider, DisabledSearchProvider, MockNaverSearchProvider
    get_search_provider(name)

Nothing in the live pipeline imports this package yet — wiring is a later
milestone. Adding the package is a no-op until then (the Naver provider is
disabled by default via ``NAVER_SEARCH_ENABLED``).
"""

from __future__ import annotations

from .base import (
    BaseSourceProvider,
    DocumentProviderResult,
    PrimaryDocumentProvider,
    SearchHit,
    SearchProvider,
    SearchProviderResult,
)
from .naver_search import (
    DisabledSearchProvider,
    MockNaverSearchProvider,
    NaverNewsSearchProvider,
)
from .policy_briefing import (
    DisabledPolicyBriefingProvider,
    MockPolicyBriefingProvider,
    PolicyBriefingProvider,
    fetch_and_build_policy_briefing_candidates,
    get_document_provider,
)
from .national_law import (
    DisabledNationalLawProvider,
    MockNationalLawProvider,
    NationalLawProvider,
    fetch_and_build_national_law_candidates,
    get_law_provider,
)


def get_search_provider(name: str = "naver") -> SearchProvider:
    """Return the search provider matching ``name`` and the current
    environment. Never raises.

    Resolution (mirrors ``semantic_embeddings.get_active_provider``):
        * ``"naver"`` -> ``NaverNewsSearchProvider`` when the gate is on AND
          both credentials are present; otherwise a ``DisabledSearchProvider``
          carrying the precise reason (gate off / id missing / secret missing).
        * anything else -> ``DisabledSearchProvider`` with an
          ``unsupported provider`` reason.

    The returned provider may report ``available=False`` — callers treat that
    exactly like disabled (empty results, no network).
    """
    key = (name or "").strip().lower()
    if key in ("naver", "naver_api", "naver_news"):
        provider = NaverNewsSearchProvider()
        if provider.available:
            return provider
        return DisabledSearchProvider(name="naver_api", reason=provider.reason)
    return DisabledSearchProvider(reason=f"unsupported provider: {name}")


__all__ = [
    "BaseSourceProvider",
    "SearchProvider",
    "PrimaryDocumentProvider",
    "SearchHit",
    "SearchProviderResult",
    "DocumentProviderResult",
    "NaverNewsSearchProvider",
    "DisabledSearchProvider",
    "MockNaverSearchProvider",
    "get_search_provider",
    "PolicyBriefingProvider",
    "DisabledPolicyBriefingProvider",
    "MockPolicyBriefingProvider",
    "get_document_provider",
    "fetch_and_build_policy_briefing_candidates",
    "NationalLawProvider",
    "DisabledNationalLawProvider",
    "MockNationalLawProvider",
    "get_law_provider",
    "fetch_and_build_national_law_candidates",
]
