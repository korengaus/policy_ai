"""M21 Phase 2b — Policy Briefing press-release PrimaryDocumentProvider.

Implements a primary-document-type provider against the data.go.kr Policy
Briefing integrated press-release feed (org code 1371000, an aggregated feed of
press releases from all central ministries):

    GET http://apis.data.go.kr/1371000/pressReleaseService/pressReleaseList
    params: serviceKey, startDate, endDate (YYYYMMDD), pageNo, numOfRows

Unlike the Naver provider (a *finder* that returns article URLs), this is a
*primary-document source*: it returns the government's own text directly
(title / body / ministry / original URL), so the result carries normalized
``documents`` rather than search hits.

CONFIRMED LIVE SPEC (verified via Worker Shell against the real API):
    * Auth + params via ``requests.get(url, params={...})`` (dict form
      single-encodes the serviceKey correctly).
    * Date window is capped at 3 days — a wider range returns
      THREE_DAYS_OVER_ERROR. Always request a <=3-day window.
    * Response is XML, NOT JSON (the ``type=json`` param is ignored by this
      service). Parse with ``xml.etree.ElementTree``.
    * Shape: <response><header><resultCode>0</resultCode>
      <resultMsg>NORMAL_SERVICE</resultMsg></header>
      <body><NewsItem>...</NewsItem>...</body></response>
    * Per-<NewsItem> fields: NewsItemId, Title (CDATA, HTML entities),
      SubTitle1/2/3, DataContents (CDATA, HTML markup), MinisterCode (actually
      the ministry NAME in plain Korean despite the suffix), OriginalUrl
      (canonical korea.kr URL), ApproveDate (MM/DD/YYYY HH:MM:SS), EmbargoDate
      (may be empty), FileName/FileUrl (attachment pairs — captured but NOT
      parsed this milestone).
    * Error signaling: any resultCode != 0 / resultMsg != NORMAL_SERVICE
      (e.g. THREE_DAYS_OVER_ERROR, NO_MANDATORY_REQUEST_PARAMETERS_ERROR,
      SERVICE_KEY_IS_NOT_REGISTERED_ERROR) -> fail-closed empty result.

Fail-closed contract (mirrors ``providers.naver_search``):
    * No network at import time.
    * ``fetch_press_releases`` NEVER raises out to the caller.
    * Missing key / disabled gate -> ``DisabledPolicyBriefingProvider``:
      ``available=False`` + populated ``reason`` + empty result + ZERO network.
    * Transport / non-200 / non-NORMAL / malformed-XML -> empty ``documents``
      + ``error`` set, never raises.
    * The serviceKey is NEVER logged or echoed in any result / status dict.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config

from structured_logging import get_logger

from .base import DocumentProviderResult, PrimaryDocumentProvider


log = get_logger(__name__)


POLICY_BRIEFING_ENDPOINT = (
    "http://apis.data.go.kr/1371000/pressReleaseService/pressReleaseList"
)

# data.go.kr hard cap: a date window wider than 3 days returns
# THREE_DAYS_OVER_ERROR. Inclusive 3-day window => endDate - 2 days.
DATE_WINDOW_DAYS = 3

# Korea Standard Time — press releases are KST-dated, so the window is computed
# in KST to avoid an off-by-one near midnight UTC.
_KST = timezone(timedelta(hours=9))

# How many releases (from the full multi-ministry feed) to inject. See
# ``to_official_source_candidates`` for the ranking-not-filtering contract.
MAX_PRESS_RELEASES = 15

# M34 — minimum claim-token overlap for a release to be injected. 1 = drop
# only releases sharing ZERO meaningful tokens with the claim set (kills
# off-topic-ministry noise; keeps anything with any overlap — recall-safe).
MIN_CLAIM_TOKEN_OVERLAP = 1

# Single page is fetched in the wiring; the param is plumbed for future use.
DEFAULT_NUM_OF_ROWS = 100

# FIN-5/FIN-7 — legacy per-window page-cap constant. SUPERSEDED by
# config.policy_briefing_max_pages() (default 1) after FIN-7 proved the API
# ignores pageNo (page 1 == page 2). Retained only as a documented upper-bound
# reference; no longer read by the wiring.
MAX_PAGES_PER_WINDOW = 5

_SOURCE_TAG = "policy_briefing"

_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9.%]+")

# M36 — PROVIDER-LOCAL relevance-token cleanup. Used ONLY by _claim_tokens /
# _doc_tokens (the M34 _select_documents precision filter), NEVER by the verdict
# matcher (official_evidence_resolution._tokens is a separate tokenizer in a
# different file). Removing these junk tokens only reduces noise overlap so
# off-topic-ministry releases stop passing the MIN_CLAIM_TOKEN_OVERLAP gate; it
# can never strengthen a match. CONSERVATIVE: particles / endings / quotative /
# generic reporting + time words ONLY — NO finance/policy domain nouns.
STOPWORDS_RELEVANCE: frozenset = frozenset({
    "라고", "이라고", "이라며", "라며", "라는", "이라는",
    "따르면", "따라", "때문에", "것이다", "것", "데", "대로",
    "등", "및", "관계자", "관계자는", "제시한", "제시",
    "유지할", "유지", "방향", "방향을",
    "지난해", "올해", "내년", "작년", "금년",
    "이른", "없다는", "있다는", "아닌", "인데",
    "위해", "통해", "대한", "관련", "한편", "다만",
    # M36b — generic administrative / predicate / quantity-shape terms shared
    # across all ministries' press releases (observed leaking off-topic
    # ministries past the >=1 gate). NO finance/policy domain noun here:
    # 가계대출/대출/규제/총량/금융위/은행/예금/차주/소상공인/... stay OUT of this set.
    "대비", "계획", "증가", "증가해", "목표", "목표로", "폭", "폭이",
    "등은", "제한", "제한될수록", "바꾸는", "했다", "늘었다",
    "집중하기", "몰려갈", "이야기", "너무", "수밖에", "우량",
    # English claim-status artifacts (from normalized-claim status fields), not
    # topical. No English domain word added.
    "proposed", "uncertain", "unknown",
})

# Digit-led numeric/quantity/time token (optionally a Korean unit/counter or
# percent), e.g. 4조 / 8조 / 1분기 / 4분기 / 2026년 / 3월 / 100억 / 50% / 1.5%.
# Anchored so it only matches tokens that are ENTIRELY numeric+unit — a digit
# inside a meaningful word (already rare given _TOKEN_RE) is not dropped.
_NUMBER_UNIT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:%|조|억|만|천|원|년|월|일|분기|개월|건|명|차|위|호|위안|달러)?$"
)


def _is_number_or_unit(token: str) -> bool:
    """True iff ``token`` is a pure numeric/quantity/time token (digit-led with
    an optional Korean unit/counter or percent). Provider-local; relevance
    filter only."""
    return bool(_NUMBER_UNIT_RE.match(token or ""))


# M36b — leading/trailing punctuation only (NOT internal). _TOKEN_RE keeps "."
# for 1.5% style numbers, so sentence-ending tokens arrive as 것이다. / 했다. /
# 늘었다. and slip past the stoplist. Stripping EDGE punctuation lets them match
# the stoplist; an internal period (1.5%) is untouched because it is not at an
# edge (and the trailing "%" further guards it).
_TOKEN_STRIP_CHARS = ".,。"


def _clean_token(token: str) -> Optional[str]:
    """Return the relevance-cleaned form of a single token, or None if it should
    be dropped. Strips EDGE punctuation, then applies the len>=2 / pure-digit /
    stopword / number-unit drops to the stripped form. Provider-local; used ONLY
    by the M34 _select_documents precision filter — never by the verdict
    matcher. Removal-only: it can only reduce junk overlap, never strengthen a
    match."""
    cleaned = (token or "").strip(_TOKEN_STRIP_CHARS)
    if (
        len(cleaned) >= 2
        and not cleaned.isdigit()
        and cleaned not in STOPWORDS_RELEVANCE
        and not _is_number_or_unit(cleaned)
    ):
        return cleaned
    return None


def _strip_tags(text: Optional[str]) -> str:
    """Strip HTML tags from a DataContents CDATA body and unescape entities.
    ``sanitize_text`` then repairs mojibake / collapses whitespace."""
    from text_utils import sanitize_text

    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    return sanitize_text(html.unescape(no_tags))


def _unescape(text: Optional[str]) -> str:
    """Unescape HTML entities in a Title (e.g. ``&middot;`` -> ``·``)."""
    from text_utils import sanitize_text

    if not text:
        return ""
    return sanitize_text(html.unescape(text))


def _source_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now_kst() -> datetime:
    return datetime.now(_KST)


def date_window(reference: Optional[datetime] = None) -> tuple[str, str]:
    """Return (startDate, endDate) as YYYYMMDD for the inclusive last
    ``DATE_WINDOW_DAYS`` days in KST. ``reference`` is for testability."""
    end = (reference or _now_kst()).date()
    start = end - timedelta(days=DATE_WINDOW_DAYS - 1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _approve_date_sort_key(approve_date: str) -> float:
    """Best-effort recency key from an ApproveDate ('MM/DD/YYYY HH:MM:SS').
    Returns 0.0 when unparseable — used only for tie-break ordering."""
    if not approve_date:
        return 0.0
    try:
        return datetime.strptime(approve_date.strip(), "%m/%d/%Y %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return 0.0


def _normalize_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw <NewsItem> field dict to a normalized press-release dict.
    Shared by the real and mock providers so their shapes can't drift."""
    subtitles = [
        _unescape(raw.get(key))
        for key in ("SubTitle1", "SubTitle2", "SubTitle3")
    ]
    subtitle = " ".join(part for part in subtitles if part).strip()
    file_urls = [url for url in (raw.get("FileUrlList") or []) if url]
    return {
        "id": (raw.get("NewsItemId") or "").strip(),
        "title": _unescape(raw.get("Title")),
        "subtitle": subtitle,
        "body": _strip_tags(raw.get("DataContents")),
        "ministry": (raw.get("MinisterCode") or "").strip(),
        "original_url": (raw.get("OriginalUrl") or "").strip(),
        "approve_date": (raw.get("ApproveDate") or "").strip(),
        "embargo_date": (raw.get("EmbargoDate") or "").strip(),
        "file_urls": file_urls,
        "raw": dict(raw),
    }


def _newsitem_to_raw(elem) -> Dict[str, Any]:
    """Collect a <NewsItem> ElementTree element into a flat raw dict.
    Multiple <FileUrl>/<FileName> children are gathered into lists."""
    raw: Dict[str, Any] = {}
    for child in list(elem):
        tag = child.tag
        if tag in ("FileUrl", "FileName"):
            continue
        raw[tag] = child.text if child.text is not None else ""
    raw["FileUrlList"] = [
        (node.text or "").strip() for node in elem.findall("FileUrl") if (node.text or "").strip()
    ]
    raw["FileNameList"] = [
        (node.text or "").strip() for node in elem.findall("FileName") if (node.text or "").strip()
    ]
    return raw


def parse_press_release_xml(text: str) -> tuple[str, str, List[Dict[str, Any]]]:
    """Parse the XML body. Returns (resultCode, resultMsg, [raw NewsItem dicts]).
    Never raises — a malformed body yields ('', 'XML_PARSE_ERROR', [])."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text or "")
    except Exception:
        return "", "XML_PARSE_ERROR", []

    header = root.find("header")
    result_code = (header.findtext("resultCode") if header is not None else "") or ""
    result_msg = (header.findtext("resultMsg") if header is not None else "") or ""
    result_code = result_code.strip()
    result_msg = result_msg.strip()

    body = root.find("body")
    items: List[Dict[str, Any]] = []
    if body is not None:
        for elem in body.findall("NewsItem"):
            items.append(_newsitem_to_raw(elem))
    return result_code, result_msg, items


def _is_normal_service(result_code: str, result_msg: str) -> bool:
    return result_msg == "NORMAL_SERVICE" and result_code in ("0", "00", "")


class DisabledPolicyBriefingProvider(PrimaryDocumentProvider):
    """Returned when the gate is off or the serviceKey is absent. Every call is
    a pure no-op so callers never special-case the disabled state."""

    external_calls_possible = False

    def __init__(
        self,
        *,
        name: str = "policy_briefing",
        reason: str = "policy briefing provider disabled",
    ) -> None:
        self.name = name
        self.available = False
        self.configured = False
        self.reason = reason
        self.error = reason

    def fetch_press_releases(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        page_no: int = 1,
        num_of_rows: int = DEFAULT_NUM_OF_ROWS,
    ) -> DocumentProviderResult:
        return self._empty_result(error=self.reason)


class PolicyBriefingProvider(PrimaryDocumentProvider):
    """Real Policy Briefing press-release provider.

    ``available`` is True only when the gate is on AND the serviceKey is
    present. ``configured`` is True when the key is present regardless of the
    gate. Constructed without any network call.
    """

    name = "policy_briefing"
    external_calls_possible = True

    def __init__(self) -> None:
        self._service_key = config.datagokr_service_key()
        self._timeout = config.policy_briefing_timeout_seconds()
        self.error = None
        self.configured = bool(self._service_key)

        if not config.policy_briefing_enabled():
            self.available = False
            self.reason = "POLICY_BRIEFING_ENABLED=false"
            self.error = self.reason
            return
        if not self._service_key:
            self.available = False
            self.reason = "DATAGOKR_SERVICE_KEY missing"
            self.error = self.reason
            return
        self.available = True
        self.reason = "policy briefing provider ready"

    def fetch_press_releases(
        self,
        *,
        start_date: str,
        end_date: str,
        page_no: int = 1,
        num_of_rows: int = DEFAULT_NUM_OF_ROWS,
    ) -> DocumentProviderResult:
        if not self.available:
            return self._empty_result(error=self.reason)
        if not start_date or not end_date:
            return self._empty_result(error="missing date window")

        # serviceKey lives ONLY in the params dict; never logged. Dict form
        # lets requests single-encode the key (confirmed correct live).
        params = {
            "serviceKey": self._service_key,
            "startDate": start_date,
            "endDate": end_date,
            "pageNo": page_no,
            "numOfRows": num_of_rows,
        }
        debug_base = {
            "start_date": start_date,
            "end_date": end_date,
            "page_no": page_no,
            "num_of_rows": num_of_rows,
        }

        try:
            import requests
        except Exception as import_error:  # pragma: no cover - requests is a dep
            return self._empty_result(
                error=f"requests not importable: {type(import_error).__name__}",
                debug=debug_base,
            )

        try:
            response = requests.get(
                POLICY_BRIEFING_ENDPOINT,
                params=params,
                timeout=self._timeout,
            )
        except Exception as call_error:
            # Never log params (serviceKey). Type + short message only.
            log.warning(
                "policy_briefing.request_failed",
                extra={
                    "error_type": type(call_error).__name__,
                    "error_message": str(call_error)[:200],
                    **debug_base,
                },
            )
            return self._empty_result(
                error=f"request failed: {type(call_error).__name__}",
                debug=debug_base,
            )

        status_code = getattr(response, "status_code", None)
        debug = {"status_code": status_code, **debug_base}
        if status_code != 200:
            log.warning("policy_briefing.non_200", extra={"status_code": status_code, **debug_base})
            return self._empty_result(error=f"http status {status_code}", debug=debug)

        result_code, result_msg, raw_items = parse_press_release_xml(
            getattr(response, "text", "") or ""
        )
        debug["result_code"] = result_code
        debug["result_msg"] = result_msg
        if not _is_normal_service(result_code, result_msg):
            # Includes THREE_DAYS_OVER_ERROR / SERVICE_KEY_IS_NOT_REGISTERED_ERROR
            # / NO_MANDATORY_REQUEST_PARAMETERS_ERROR / XML_PARSE_ERROR.
            log.warning(
                "policy_briefing.non_normal_service",
                extra={"result_code": result_code, "result_msg": result_msg, **debug_base},
            )
            return self._empty_result(error=result_msg or "non-normal service", debug=debug)

        documents = [_normalize_item(item) for item in raw_items]
        debug["fetched_count"] = len(documents)
        return {
            "provider": self.name,
            "available": True,
            "document": None,
            "documents": documents,
            "error": None,
            "debug": debug,
        }


class MockPolicyBriefingProvider(PrimaryDocumentProvider):
    """Deterministic, network-free provider for tests and local development.

    Returns canned raw <NewsItem>-shaped dicts run through the SAME
    ``_normalize_item`` as the real provider, so downstream code can be
    exercised without a key or the network. Mirrors ``MockNaverSearchProvider``.
    """

    name = "policy_briefing"
    external_calls_possible = False

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self.available = True
        self.configured = True
        self.reason = "deterministic mock provider: no network"
        self.error = None
        self._items = list(items) if items is not None else list(_DEFAULT_MOCK_ITEMS)

    def fetch_press_releases(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        page_no: int = 1,
        num_of_rows: int = DEFAULT_NUM_OF_ROWS,
    ) -> DocumentProviderResult:
        documents = [_normalize_item(item) for item in self._items if isinstance(item, dict)]
        return {
            "provider": self.name,
            "available": True,
            "document": None,
            "documents": documents,
            "error": None,
            "debug": {
                "mock": True,
                "start_date": start_date,
                "end_date": end_date,
                "fetched_count": len(documents),
            },
        }


def get_document_provider(name: str = "policy_briefing") -> PrimaryDocumentProvider:
    """Return the document provider matching ``name`` and the current
    environment. Never raises. Mirrors ``providers.get_search_provider``.

        * ``"policy_briefing"`` -> ``PolicyBriefingProvider`` when the gate is
          on AND the serviceKey is present; otherwise a
          ``DisabledPolicyBriefingProvider`` carrying the precise reason.
        * anything else -> disabled with an ``unsupported provider`` reason.
    """
    key = (name or "").strip().lower()
    if key in ("policy_briefing", "policy-briefing", "policybriefing", "press_release"):
        provider = PolicyBriefingProvider()
        if provider.available:
            return provider
        return DisabledPolicyBriefingProvider(name="policy_briefing", reason=provider.reason)
    return DisabledPolicyBriefingProvider(reason=f"unsupported provider: {name}")


# --- Option A: map normalized press releases -> official source candidates ---


def _claim_tokens(normalized_claims: List[Dict[str, Any]]) -> set:
    tokens: set = set()
    for claim in normalized_claims or []:
        for field in (
            "claim_text", "actor", "action", "target", "object",
            "quantity", "location", "status",
        ):
            value = str(claim.get(field) or "")
            for token in _TOKEN_RE.findall(value):
                cleaned = _clean_token(token)
                if cleaned is not None:
                    tokens.add(cleaned)
    return tokens


def _doc_tokens(document: Dict[str, Any]) -> set:
    text = f"{document.get('title') or ''} {document.get('body') or ''}"
    return {
        cleaned
        for token in _TOKEN_RE.findall(text)
        if (cleaned := _clean_token(token)) is not None
    }


def _select_documents(
    documents: List[Dict[str, Any]],
    normalized_claims: List[Dict[str, Any]],
    *,
    max_releases: int,
) -> List[Dict[str, Any]]:
    """Pick which releases to inject from the full multi-ministry feed.

    M34 — RANK, then DROP off-topic releases (reverses the prior M21
    rank-to-fill/never-exclude behavior):
      (a) releases sharing ZERO claim-token overlap are EXCLUDED
          (``MIN_CLAIM_TOKEN_OVERLAP``); a release with any overlap (>=1)
          survives — recall-safe. Overlap is measured on STOPWORD/NUMBER-CLEANED
          token sets (M36) so junk words (라고/올해/4조) no longer count as
          overlap; this is still input-selection only — the body-matcher
          remains the sole judge of evidence STRENGTH for survivors;
      (b) survivors keep overlap-desc order; this selection affects ONLY
          which candidates are injected — it never touches any reliability
          score or verdict, and the body-matcher (resolve_official_evidence,
          M19-3 official_body_match guard) remains the SOLE judge of evidence
          STRENGTH for survivors;
      (c) on a topic-dry feed every release may be excluded -> empty result,
          which the pipeline handles as a clean no-official-candidate state.
    Order: by claim-token overlap desc, then recency (ApproveDate) desc,
    then id for determinism.
    """
    claim_tokens = _claim_tokens(normalized_claims)
    relevant = [
        doc
        for doc in documents
        if len(_doc_tokens(doc) & claim_tokens) >= MIN_CLAIM_TOKEN_OVERLAP
    ]
    ranked = sorted(
        relevant,
        key=lambda doc: (
            -len(_doc_tokens(doc) & claim_tokens),
            -_approve_date_sort_key(doc.get("approve_date") or ""),
            doc.get("id") or "",
        ),
    )
    return ranked[:max_releases]


def to_official_source_candidates(
    documents: List[Dict[str, Any]],
    normalized_claims: List[Dict[str, Any]],
    *,
    max_releases: int = MAX_PRESS_RELEASES,
) -> tuple[List[Dict[str, Any]], int]:
    """Shape selected press releases as official source candidates for Option A
    injection. Emits one candidate per (claim_index x release).

    ``official_body_match`` is intentionally NEVER set here — it is computed
    only by ``resolve_official_evidence`` (the M19-3 guard is the sole path to
    the reliability uplift). Returns (candidates, injected_release_count)."""
    if not normalized_claims or not documents:
        return [], 0

    selected = _select_documents(documents, normalized_claims, max_releases=max_releases)
    if not selected:
        return [], 0

    retrieved_at = datetime.now(timezone.utc).isoformat()
    window_start, window_end = date_window()
    query_used = f"{window_start}-{window_end}"

    candidates: List[Dict[str, Any]] = []
    for index in range(len(normalized_claims)):
        for doc in selected:
            original_url = doc.get("original_url") or ""
            candidates.append(
                {
                    "source_id": _source_id(str(index), _SOURCE_TAG, doc.get("id") or original_url),
                    "claim_index": index,
                    "title": doc.get("title") or "",
                    "url": original_url,
                    "official_detail_url": original_url,
                    "publisher": doc.get("ministry") or "",
                    "source_type": "official_government",
                    # body lives in raw_text -> read by resolve_official_evidence
                    "raw_text": doc.get("body") or "",
                    "raw_text_available": True,
                    "official_body_fetched": True,
                    "official_body_length": len(doc.get("body") or ""),
                    # official_body_match is NOT set — computed downstream only.
                    "retrieval_method": "policy_briefing_api",
                    "purpose": "primary_source",
                    "query_used": query_used,
                    "retrieved_at": retrieved_at,
                    "policy_briefing_news_item_id": doc.get("id") or "",
                    "policy_briefing_approve_date": doc.get("approve_date") or "",
                    "policy_briefing_file_urls": list(doc.get("file_urls") or []),
                }
            )
    return candidates, len(selected)


def _fetch_window_paginated(
    provider: PrimaryDocumentProvider,
    start_date: str,
    end_date: str,
    *,
    num_of_rows: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    """Fetch ONE 3-day window, paging until a SHORT page (fewer than
    ``num_of_rows`` items, including zero) or ``max_pages`` is reached. Calls the
    unchanged ``fetch_press_releases`` single-page primitive once per page.

    ``max_pages=1`` reproduces the pre-FIN-5 single-page fetch EXACTLY (one call,
    page 1) — the short-page check never even runs a second iteration."""
    documents: List[Dict[str, Any]] = []
    for page_no in range(1, max_pages + 1):
        result = provider.fetch_press_releases(
            start_date=start_date,
            end_date=end_date,
            page_no=page_no,
            num_of_rows=num_of_rows,
        )
        page_docs = result.get("documents") or []
        documents.extend(page_docs)
        if len(page_docs) < num_of_rows:  # short-page-stop (incl. empty/error)
            break
    return documents


def fetch_and_build_policy_briefing_candidates(
    normalized_claims: List[Dict[str, Any]],
    *,
    max_releases: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """Top-level entry called by the pipeline (Option A). Covers the last
    ``POLICY_BRIEFING_LOOKBACK_DAYS`` days via looped non-overlapping 3-day KST
    windows (FIN-5), paginating each window, then shapes the merged + deduped
    releases into official source candidates. Never raises; returns ([], 0) on
    any failure / empty.

    DEFAULT (lookback=3, max=15): exactly ONE window (today-2..today), PAGE 1
    ONLY (``max_pages=1``), dedup is a no-op on a single page, top-15 selection
    — byte-identical to pre-FIN-5. Pagination engages ONLY when the window is
    widened (``lookback_days > DATE_WINDOW_DAYS``), so the default fetch never
    pulls page 2 even on a >=100-item window.

    The CALLER gates this behind ``config.policy_briefing_enabled()`` so the
    disabled path constructs nothing and hits no network."""
    provider = get_document_provider("policy_briefing")

    if max_releases is None:
        max_releases = config.policy_briefing_max_releases()
    lookback_days = config.policy_briefing_lookback_days()
    windows = max(1, math.ceil(lookback_days / DATE_WINDOW_DAYS))
    # FIN-7 — per-window pages from config (default 1). The data.go.kr
    # pressReleaseList API IGNORES pageNo (proven 2026-06: page 1 == page 2,
    # byte-identical items), so page 1 already holds the whole window; pages 2+
    # were duplicates that dedup discarded while occasionally paying a 10s
    # read-timeout. Capping at 1 removes those no-op calls — zero data lost. The
    # real recall lever is the multi-window loop below (windows), untouched.
    max_pages = max(1, config.policy_briefing_max_pages())

    reference = _now_kst()
    seen_ids: set = set()
    documents: List[Dict[str, Any]] = []
    for window_index in range(windows):
        window_ref = reference - timedelta(days=DATE_WINDOW_DAYS * window_index)
        start_date, end_date = date_window(reference=window_ref)
        for doc in _fetch_window_paginated(
            provider,
            start_date,
            end_date,
            num_of_rows=DEFAULT_NUM_OF_ROWS,
            max_pages=max_pages,
        ):
            dedup_key = doc.get("id") or doc.get("original_url") or ""
            if dedup_key and dedup_key in seen_ids:
                continue
            if dedup_key:
                seen_ids.add(dedup_key)
            documents.append(doc)

    return to_official_source_candidates(
        documents, normalized_claims, max_releases=max_releases
    )


_DEFAULT_MOCK_ITEMS: List[Dict[str, Any]] = [
    {
        "NewsItemId": "mock-0001",
        "Title": "전세대출 규제 강화 &middot; 실수요자 보호",
        "SubTitle1": "금융위원회 보도자료",
        "SubTitle2": "",
        "SubTitle3": "",
        "DataContents": (
            "<p>정부는 <b>전세대출</b> 규제를 강화한다고 "
            "밝혔다. 금융위원회는 실수요자 보호를 위해 "
            "전세대출 한도와 DSR 규제를 조정한다. 이번 "
            "대책은 가계부채 관리와 주택시장 안정을 "
            "목표로 한다. 전세대출 규제는 수도권 규제"
            "지역에 우선 적용되며, 실수요자에 대한 "
            "예외 규정도 함께 마련된다. 금융당국은 "
            "시행 시기를 추가로 안내할 예정이다.</p>"
        ),
        "MinisterCode": "금융위원회",
        "OriginalUrl": "https://www.korea.kr/news/policyNewsView.do?newsId=148900001",
        "ApproveDate": "06/02/2026 09:30:00",
        "EmbargoDate": "",
        "FileName": "전세대출규제.hwp",
        "FileUrl": "https://www.korea.kr/file/0001.hwp",
    },
    {
        "NewsItemId": "mock-0002",
        "Title": "주택담보대출 금리 동향 점검",
        "SubTitle1": "",
        "SubTitle2": "",
        "SubTitle3": "",
        "DataContents": (
            "<p>한국은행은 주택담보대출 금리 동향을 "
            "점검했다고 밝혔다. 기준금리와 연계된 "
            "주담대 금리가 소폭 상승했으며, 가계부채 "
            "관리 필요성이 제기됐다. 통화정책 방향은 "
            "물가와 경기 상황을 종합 고려해 결정된다. "
            "한은은 향후 시장 영향을 면밀히 모니터링"
            "할 계획이다.</p>"
        ),
        "MinisterCode": "한국은행",
        "OriginalUrl": "https://www.korea.kr/news/policyNewsView.do?newsId=148900002",
        "ApproveDate": "06/01/2026 18:00:00",
        "EmbargoDate": "",
        "FileName": "",
        "FileUrl": "",
    },
]


__all__ = [
    "PolicyBriefingProvider",
    "DisabledPolicyBriefingProvider",
    "MockPolicyBriefingProvider",
    "get_document_provider",
    "fetch_and_build_policy_briefing_candidates",
    "to_official_source_candidates",
    "parse_press_release_xml",
    "date_window",
    "POLICY_BRIEFING_ENDPOINT",
    "MAX_PRESS_RELEASES",
]
