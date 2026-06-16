"""FSS-PROVIDER — FSS 보도자료 (bodoInfo) press-release PrimaryDocumentProvider.

Third primary-document (1차원문) source, mirroring the M21 Policy Briefing
provider (providers/policy_briefing.py) and feeding the SAME Lane-A injection
path (source_candidates -> resolve_official_evidence -> matcher).

WHY: the M37/BODY-2 floor diagnosis found ~46 floor financial rows that carry
ZERO policy_briefing candidate because their issuing body is the 금융감독원 (FSS),
which is OUTSIDE the policy_briefing aggregated feed (data.go.kr org 1371000).
FSS press releases are the official-document supply those rows lacked (e.g.
"2026년 5월 가계대출 동향(잠정)"). This provider supplies those bodies; the EXISTING
matcher decides whether they match — bodies must still MATCH to drop the floor
(no-gain is possible and is NOT a regression).

★ LANE-A ONLY (absolute project rule): FSS candidates attach the stable marker
``fss_bodo_content_id`` for DEDUP/PROVENANCE ONLY. That marker is deliberately
NOT a member of official_evidence_resolution._PRIMARY_DOCUMENT_MARKER_FIELDS, so
FSS gets NO Lane-B verdict-raise uplift (extract_primary_document_match never
recognizes it). official_evidence_resolution.py is NOT modified by this provider.
Lane-B marker generalization is PROHIBITED until the upstream-supply fixes land.

CONFIRMED LIVE SPEC (verified by scripts/fss_key_probe.py + a live test — build
against THIS, not the screen docs):
    * Endpoint: GET https://www.fss.or.kr/fss/kr/openApi/api/bodoInfo.jsp
    * Params (ALL required): apiType=json, startDate, endDate, authKey.
    * ★ Date format is YYYY-MM-DD WITH HYPHENS (YYYYMMDD returns ZERO items).
    * authKey from env FSS_API_KEY (32-char). NEVER hard-coded, NEVER logged.
    * A BROWSER User-Agent is used (FSS/gov sites often require it; M23 lesson).
    * Response JSON top-level key is "reponse" (the API literally misspells it).
      Under it: resultCode ("1" = success), resultMsg, resultCnt, result (array).
    * ★ Per-item body field is "contentKor" (NO trailing s; the screen doc says
      "contentsKor" but the LIVE response uses "contentKor").
    * Other item fields: subject, publishOrg (always "금융감독원"), originUrl,
      contentId, regDate, viewCnt, atchfileUrl, atchfileNm.
    * ★ Bodies carry HTML/entity/noise: HTML tags (<p>/<br>), entities
      (&lt; &gt; &quot; &#39; &amp;), literal "nn" newline artifacts, and the
      literal "u203B" (※) escape artifact — all cleaned before the body is used.

Fail-closed contract (mirrors providers/policy_briefing.py):
    * No network at import time.
    * fetch_press_releases NEVER raises out to the caller.
    * Disabled gate / missing key -> DisabledFssProvider: available=False, empty
      result, ZERO network.
    * non-200 / resultCode!="1" / malformed-JSON / transport error -> empty
      documents + error, never raises.
    * The authKey is NEVER logged or echoed in any result / status / log line.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config

from structured_logging import get_logger

from .base import DocumentProviderResult, PrimaryDocumentProvider


log = get_logger(__name__)


FSS_ENDPOINT = "https://www.fss.or.kr/fss/kr/openApi/api/bodoInfo.jsp"

# ★ Date format MUST use hyphens (probe-confirmed: YYYYMMDD returns zero items).
_DATE_FMT = "%Y-%m-%d"

# Korea Standard Time — FSS press releases are KST-dated, so the window is
# computed in KST to avoid an off-by-one near midnight UTC (mirrors M21).
_KST = timezone(timedelta(hours=9))

# Browser User-Agent — FSS/gov sites often require it (M23 law.go.kr lesson).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# How many releases (from the full FSS feed for the window) to inject. Mirrors
# M21's MAX_PRESS_RELEASES / rank-not-filter contract.
MAX_PRESS_RELEASES = 15

# Minimum claim-token overlap for a release to be injected (mirrors M21 M34).
# 1 = drop only releases sharing ZERO meaningful tokens with the claim set
# (kills off-topic-window noise; keeps anything with any overlap — recall-safe).
MIN_CLAIM_TOKEN_OVERLAP = 1

_SOURCE_TAG = "fss_press_release"

_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9.%]+")

# Provider-local relevance-token cleanup (mirrors M36/M36b doctrine; kept DISTINCT
# from the verdict matcher's tokenizer). Used ONLY by _claim_tokens / _doc_tokens
# (the MIN_CLAIM_TOKEN_OVERLAP precision filter), NEVER by the verdict matcher.
# Removal-only: it can only reduce junk overlap so off-topic releases stop passing
# the >=1 gate; it can never strengthen a match. CONSERVATIVE: particles /
# endings / quotatives / generic reporting + time words ONLY — NO finance/policy
# domain nouns (가계대출/대출/금융/은행/감독 stay OUT so they remain topic signal).
STOPWORDS_RELEVANCE: frozenset = frozenset({
    "라고", "이라고", "이라며", "라며", "라는", "이라는",
    "따르면", "따라", "때문에", "것이다", "것", "데", "대로",
    "등", "및", "관계자", "관계자는", "제시한", "제시",
    "지난해", "올해", "내년", "작년", "금년",
    "위해", "통해", "대한", "관련", "한편", "다만",
    "대비", "계획", "증가", "증가해", "목표", "목표로", "폭", "폭이",
    "제한", "했다", "늘었다",
})

# Digit-led numeric/quantity/time token (mirrors M21 _NUMBER_UNIT_RE): e.g.
# 9.3조 / 4분기 / 2026년 / 5월 / 100억 / 50%. Anchored so only ENTIRELY numeric+unit
# tokens are dropped (a digit inside a meaningful word is not).
_NUMBER_UNIT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:%|조|억|만|천|원|년|월|일|분기|개월|건|명|차|위|호|위안|달러)?$"
)


def _is_number_or_unit(token: str) -> bool:
    return bool(_NUMBER_UNIT_RE.match(token or ""))


def _clean_token(token: str) -> Optional[str]:
    """Relevance-cleaned form of a single token, or None if it should be dropped.
    Provider-local; used ONLY by the MIN_CLAIM_TOKEN_OVERLAP precision filter,
    never by the verdict matcher. Removal-only."""
    cleaned = (token or "").strip(".,。")
    if (
        len(cleaned) >= 2
        and not cleaned.isdigit()
        and cleaned not in STOPWORDS_RELEVANCE
        and not _is_number_or_unit(cleaned)
    ):
        return cleaned
    return None


def _sanitize(text: Optional[str]) -> str:
    """text_utils.sanitize_text: html.unescape + zero-width strip + mojibake
    repair + whitespace collapse. Imported lazily (mirrors M21)."""
    from text_utils import sanitize_text

    if not text:
        return ""
    return sanitize_text(text)


def _clean_body(text: Optional[str]) -> str:
    """Clean an FSS contentKor body for the matcher. Order:
      (1) strip HTML tags (<p>/<br>/...);
      (2) remove FSS-specific literal noise observed in LIVE bodies — the literal
          "u203B" (※) escape artifact and literal "nn" newline artifacts;
      (3) sanitize_text -> html.unescape (&lt; &gt; &quot; &#39; &amp;) +
          zero-width/mojibake strip + whitespace collapse.
    Without this the matcher would ingest tag/entity/noise-polluted text."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    no_noise = no_tags.replace("u203B", " ").replace("nn", " ")
    return _sanitize(html.unescape(no_noise))


def _clean_inline(text: Optional[str]) -> str:
    """Clean a short inline field (subject) — unescape entities + normalize."""
    if not text:
        return ""
    return _sanitize(html.unescape(text))


def _source_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now_kst() -> datetime:
    return datetime.now(_KST)


def date_window(reference: Optional[datetime] = None) -> tuple[str, str]:
    """Return (startDate, endDate) as YYYY-MM-DD for the inclusive last
    ``config.fss_lookback_days()`` days in KST. ``reference`` is for testability.
    The FSS API allows up to a 1-month range; the default 7-day window is one
    GET/run, far under the 30-calls/day limit."""
    lookback = max(1, config.fss_lookback_days())
    end = (reference or _now_kst()).date()
    start = end - timedelta(days=lookback - 1)
    return start.strftime(_DATE_FMT), end.strftime(_DATE_FMT)


def _reg_date_sort_key(reg_date: str) -> str:
    """Best-effort recency key from a regDate string. ISO-ish dates sort
    correctly as strings; unparseable -> '' (sorts last). Tie-break ordering
    only — never touches any verdict score."""
    return (reg_date or "").strip()


def _normalize_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw FSS result item to a normalized press-release dict. Shared by
    the real and mock providers so their shapes can't drift (mirrors M21)."""
    return {
        "id": str(raw.get("contentId") or "").strip(),
        "title": _clean_inline(raw.get("subject")),
        "body": _clean_body(raw.get("contentKor")),
        "publisher": (raw.get("publishOrg") or "").strip(),
        "original_url": (raw.get("originUrl") or "").strip(),
        "reg_date": (raw.get("regDate") or "").strip(),
        "raw": dict(raw),
    }


def parse_bodo_json(text: str) -> tuple[str, int, List[Dict[str, Any]]]:
    """Parse the FSS bodoInfo JSON body. Returns (resultCode, resultCnt, [raw
    item dicts]). Never raises — malformed/empty/unexpected -> ('', 0, []).

    ★ Top-level envelope key is the API's literal misspelling "reponse"; the
    correctly-spelled "response" is also accepted defensively."""
    import json

    try:
        data = json.loads(text or "")
    except Exception:
        return "", 0, []
    if not isinstance(data, dict):
        return "", 0, []

    env = data.get("reponse")
    if not isinstance(env, dict):
        alt = data.get("response")
        env = alt if isinstance(alt, dict) else None
    if not isinstance(env, dict):
        return "", 0, []

    result_code = str(env.get("resultCode") or "").strip()
    try:
        result_cnt = int(env.get("resultCnt") or 0)
    except (TypeError, ValueError):
        result_cnt = 0
    result = env.get("result")
    items = [x for x in result if isinstance(x, dict)] if isinstance(result, list) else []
    return result_code, result_cnt, items


def _is_success(result_code: str) -> bool:
    """FSS bodoInfo signals success with resultCode == "1"."""
    return (result_code or "").strip() == "1"


class DisabledFssProvider(PrimaryDocumentProvider):
    """Returned when the gate is off or the authKey is absent. Every call is a
    pure no-op so callers never special-case the disabled state."""

    external_calls_possible = False

    def __init__(
        self,
        *,
        name: str = "fss_press_release",
        reason: str = "fss provider disabled",
    ) -> None:
        self.name = name
        self.available = False
        self.configured = False
        self.reason = reason
        self.error = reason

    def fetch_press_releases(
        self, *, start_date: str = "", end_date: str = "",
    ) -> DocumentProviderResult:
        return self._empty_result(error=self.reason)


class FssPressReleaseProvider(PrimaryDocumentProvider):
    """Real FSS bodoInfo press-release provider.

    ``available`` is True only when the gate is on AND FSS_API_KEY is present.
    ``configured`` is True when the key is present regardless of the gate.
    Constructed without any network call. The authKey is held privately and
    NEVER logged / echoed.
    """

    name = "fss_press_release"
    external_calls_possible = True

    def __init__(self) -> None:
        self._api_key = config.fss_api_key()
        self._timeout = config.fss_timeout_seconds()
        self.error = None
        self.configured = bool(self._api_key)

        if not config.fss_enabled():
            self.available = False
            self.reason = "FSS_ENABLED=false"
            self.error = self.reason
            return
        if not self._api_key:
            self.available = False
            self.reason = "FSS_API_KEY missing"
            self.error = self.reason
            return
        self.available = True
        self.reason = "fss provider ready"

    def fetch_press_releases(
        self, *, start_date: str, end_date: str,
    ) -> DocumentProviderResult:
        if not self.available:
            return self._empty_result(error=self.reason)
        if not start_date or not end_date:
            return self._empty_result(error="missing date window")

        # authKey lives ONLY in the params dict; never logged.
        params = {
            "apiType": "json",
            "startDate": start_date,
            "endDate": end_date,
            "authKey": self._api_key,
        }
        debug_base = {"start_date": start_date, "end_date": end_date}

        try:
            import requests
        except Exception as import_error:  # pragma: no cover - requests is a dep
            return self._empty_result(
                error=f"requests not importable: {type(import_error).__name__}",
                debug=debug_base,
            )

        try:
            response = requests.get(
                FSS_ENDPOINT,
                params=params,
                headers=_BROWSER_HEADERS,
                timeout=self._timeout,
            )
        except Exception as call_error:
            # Never log params (authKey). Type + short message only.
            log.warning(
                "fss.request_failed",
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
            log.warning("fss.non_200", extra={"status_code": status_code, **debug_base})
            return self._empty_result(error=f"http status {status_code}", debug=debug)

        result_code, result_cnt, raw_items = parse_bodo_json(
            getattr(response, "text", "") or ""
        )
        debug["result_code"] = result_code
        debug["result_cnt"] = result_cnt
        if not _is_success(result_code):
            log.warning(
                "fss.non_success",
                extra={"result_code": result_code, **debug_base},
            )
            return self._empty_result(error=f"result_code {result_code}", debug=debug)

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


class MockFssProvider(PrimaryDocumentProvider):
    """Deterministic, network-free provider for tests and local development.

    Returns canned raw FSS item dicts run through the SAME ``_normalize_item`` as
    the real provider, so downstream code can be exercised without a key or the
    network. Mirrors MockPolicyBriefingProvider."""

    name = "fss_press_release"
    external_calls_possible = False

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self.available = True
        self.configured = True
        self.reason = "deterministic mock provider: no network"
        self.error = None
        self._items = list(items) if items is not None else list(_DEFAULT_MOCK_ITEMS)

    def fetch_press_releases(
        self, *, start_date: str = "", end_date: str = "",
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


def get_fss_provider(name: str = "fss_press_release") -> PrimaryDocumentProvider:
    """Return the FSS provider matching ``name`` and the current environment.
    Never raises. Mirrors providers.get_document_provider for Policy Briefing.

        * ``"fss_press_release"`` -> ``FssPressReleaseProvider`` when the gate is
          on AND the authKey is present; otherwise a ``DisabledFssProvider``
          carrying the precise reason.
        * anything else -> disabled with an ``unsupported provider`` reason.
    """
    key = (name or "").strip().lower()
    if key in ("fss_press_release", "fss", "fss_bodo", "fss-press-release"):
        provider = FssPressReleaseProvider()
        if provider.available:
            return provider
        return DisabledFssProvider(name="fss_press_release", reason=provider.reason)
    return DisabledFssProvider(reason=f"unsupported provider: {name}")


# --- Option A: map normalized FSS releases -> official source candidates -----


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
    """Pick which releases to inject from the full window feed (mirrors M21 M34):
      (a) releases sharing ZERO claim-token overlap are EXCLUDED
          (MIN_CLAIM_TOKEN_OVERLAP); any overlap (>=1) survives — recall-safe.
          Overlap is measured on STOPWORD/NUMBER-cleaned token sets; this is
          input-selection only — the body-matcher (resolve_official_evidence)
          remains the SOLE judge of evidence STRENGTH for survivors.
      (b) survivors keep overlap-desc order, then recency (regDate) desc, then id
          for determinism.
      (c) on a topic-dry window every release may be excluded -> empty result,
          handled by the pipeline as a clean no-official-candidate state."""
    claim_tokens = _claim_tokens(normalized_claims)
    relevant = [
        doc
        for doc in documents
        if len(_doc_tokens(doc) & claim_tokens) >= MIN_CLAIM_TOKEN_OVERLAP
    ]
    # Order: claim-token overlap DESC, then recency (regDate) DESC, then id DESC
    # for determinism. All three keys ascend under reverse=True (id is a stable
    # tiebreak only — it never touches any verdict score).
    ranked = sorted(
        relevant,
        key=lambda doc: (
            len(_doc_tokens(doc) & claim_tokens),
            _reg_date_sort_key(doc.get("reg_date") or ""),
            doc.get("id") or "",
        ),
        reverse=True,
    )
    return ranked[:max_releases]


def to_official_source_candidates(
    documents: List[Dict[str, Any]],
    normalized_claims: List[Dict[str, Any]],
    *,
    max_releases: int = MAX_PRESS_RELEASES,
) -> tuple[List[Dict[str, Any]], int]:
    """Shape selected FSS releases as official source candidates for Option A
    (Lane-A) injection. Emits one candidate per (claim_index x release).

    ``official_body_match`` is intentionally NEVER set here — it is computed only
    by ``resolve_official_evidence`` (the M19-3 guard). The STABLE marker
    ``fss_bodo_content_id`` is for DEDUP/PROVENANCE ONLY and is NOT a member of
    official_evidence_resolution._PRIMARY_DOCUMENT_MARKER_FIELDS, so it grants NO
    Lane-B verdict-raise uplift. Returns (candidates, injected_release_count)."""
    if not normalized_claims or not documents:
        return [], 0

    selected = _select_documents(documents, normalized_claims, max_releases=max_releases)
    if not selected:
        return [], 0

    retrieved_at = datetime.now(timezone.utc).isoformat()

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
                    "publisher": doc.get("publisher") or "",
                    "source_type": "official_government",
                    # body lives in raw_text -> read by resolve_official_evidence
                    "raw_text": doc.get("body") or "",
                    "raw_text_available": True,
                    "official_body_fetched": True,
                    "official_body_length": len(doc.get("body") or ""),
                    # official_body_match is NOT set — computed downstream only.
                    "retrieval_method": "fss_bodo_api",  # informational; resolve overwrites
                    "purpose": "primary_source",
                    "retrieved_at": retrieved_at,
                    # STABLE marker (dedup/provenance ONLY; NOT a Lane-B marker):
                    "fss_bodo_content_id": doc.get("id") or "",
                    "fss_bodo_publish_org": doc.get("publisher") or "",
                    "fss_bodo_reg_date": doc.get("reg_date") or "",
                }
            )
    return candidates, len(selected)


def fetch_and_build_fss_candidates(
    normalized_claims: List[Dict[str, Any]],
    *,
    max_releases: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """Top-level entry called by the pipeline (Option A / Lane-A). Fetches the
    recent FSS window (one GET), dedups by contentId, then shapes the releases
    into official source candidates. Never raises; returns ([], 0) on any
    failure / empty.

    The CALLER gates this behind ``config.fss_enabled()`` so the disabled path
    constructs nothing and hits no network."""
    provider = get_fss_provider("fss_press_release")
    if not getattr(provider, "available", False):
        return [], 0

    if max_releases is None:
        max_releases = config.fss_max_releases()

    start_date, end_date = date_window()
    result = provider.fetch_press_releases(start_date=start_date, end_date=end_date)
    documents = result.get("documents") or []

    # Global dedup by stable contentId (one window -> typically already unique).
    seen_ids: set = set()
    deduped: List[Dict[str, Any]] = []
    for doc in documents:
        dedup_key = doc.get("id") or doc.get("original_url") or ""
        if dedup_key and dedup_key in seen_ids:
            continue
        if dedup_key:
            seen_ids.add(dedup_key)
        deduped.append(doc)

    return to_official_source_candidates(
        deduped, normalized_claims, max_releases=max_releases
    )


_DEFAULT_MOCK_ITEMS: List[Dict[str, Any]] = [
    {
        "contentId": "fss-mock-0001",
        "subject": "2026년 5월 가계대출 동향(잠정)",
        "publishOrg": "금융감독원",
        "originUrl": "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=fss-mock-0001",
        "regDate": "2026-06-05",
        "viewCnt": "123",
        "contentKor": (
            "<p>26.5월 全 금융권 가계대출은 +9.3조원 증가하였다.</p>nn"
            "<p>주택담보대출이 증가세를 주도하였으며, 금융감독원은 가계부채 "
            "관리 기조를 유지한다고 밝혔다. u203B 자세한 내용은 첨부파일 참고.</p>"
        ),
        "atchfileNm": "가계대출동향.hwp",
        "atchfileUrl": "https://www.fss.or.kr/file/fss-mock-0001.hwp",
    },
    {
        "contentId": "fss-mock-0002",
        "subject": "보험회사 건전성 감독 강화 방안",
        "publishOrg": "금융감독원",
        "originUrl": "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=fss-mock-0002",
        "regDate": "2026-06-03",
        "viewCnt": "45",
        "contentKor": (
            "<p>금융감독원은 보험회사 지급여력비율(K-ICS) 관리를 강화한다.</p>"
        ),
        "atchfileNm": "",
        "atchfileUrl": "",
    },
]


__all__ = [
    "FssPressReleaseProvider",
    "DisabledFssProvider",
    "MockFssProvider",
    "get_fss_provider",
    "fetch_and_build_fss_candidates",
    "to_official_source_candidates",
    "parse_bodo_json",
    "date_window",
    "FSS_ENDPOINT",
    "MAX_PRESS_RELEASES",
]
