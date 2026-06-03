"""M23 — National Law Information (법제처 국가법령정보) PrimaryDocumentProvider.

Second primary-document (1차원문) source, mirroring the M21 Policy Briefing
provider (providers/policy_briefing.py) and feeding the same Option-A injection
→ resolve/evaluate → M22 Lane-B capped verdict join.

CONFIRMED LIVE SPEC (verified via Render Worker probes on two statutes):
    * Search: GET {BASE}/lawSearch.do
        params OC=<LAW_OC>&target=law&query=<statute name>&type=XML
        -> root <LawSearch> with <resultCode>00</resultCode>, <totalCnt>,
           repeated <law id="..."> nodes. Per-law identifiers:
           <법령일련번호> (= MST, e.g. 276291), <법령ID> (e.g. 001248),
           <법령명한글> (CDATA), <시행일자>, <소관부처명>, <법령상세링크>.
    * Body: GET {BASE}/lawService.do
        params OC=<LAW_OC>&target=law&MST=<법령일련번호>&type=XML
        -> root <법령> with <기본정보> and <조문> containing repeated
           <조문단위> nodes; each has <조문번호>, <조문제목> (CDATA),
           <조문내용> (CDATA = article text). UTF-8, CDATA-wrapped.
    * Auth = OC from env LAW_OC (NOT serviceKey; DATAGOKR_SERVICE_KEY stays
      with M21). A BROWSER User-Agent is MANDATORY — law.go.kr silently
      bot-blocks default Python UAs from cloud IPs.
    * Missing/invalid request returns HTTP 200 with an error envelope
      <Response><result>...</result><msg>...</msg></Response> (NOT <LawSearch>).

Fail-closed contract (mirrors providers/policy_briefing.py):
    * No network at import time.
    * search_laws / fetch_law_body NEVER raise out to the caller.
    * Disabled gate / missing OC -> DisabledNationalLawProvider: available=False,
      empty results, ZERO network.
    * non-200 / wrong root / resultCode!=00 / <Response> envelope / HTML /
      malformed-XML / transport error -> empty result, never raises.
    * The OC is NEVER logged or echoed in any result / status / log line.

Operator-approved budgets: <= 3 searches + <= 5 body fetches per run; keep
top-K=3 (hard max 5) laws by token overlap (rank-to-fill, never exclude).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config

from structured_logging import get_logger


log = get_logger(__name__)


# ONE place for the base URL: HTTPS-preferred with HTTP fallback (operator
# decision b). Both confirmed working; https tried first, http on failure.
LAW_DRF_BASE_HTTPS = "https://www.law.go.kr/DRF"
LAW_DRF_BASE_HTTP = "http://www.law.go.kr/DRF"
_SEARCH_PATH = "/lawSearch.do"
_BODY_PATH = "/lawService.do"

# ONE browser User-Agent reused by EVERY GET so it can never be forgotten
# (law.go.kr bot-blocks default Python UAs from cloud IPs — operator-verified).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# Operator-approved call budget (decision a).
MAX_SEARCHES = 3
MAX_KEPT_LAWS = 3          # K
MAX_BODY_FETCHES = 5       # hard ceiling on body GETs
MAX_ARTICLE_CHARS = 5000   # mirrors config.MAX_ARTICLE_CHARS discipline

_SOURCE_TAG = "national_law"
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9.%]+")

# Body tags that hold human-readable article text (shallow-gather, decision d).
_ARTICLE_TEXT_TAGS = ("조문제목", "조문내용", "항내용", "호내용")


def _sanitize(text: Optional[str]) -> str:
    from text_utils import sanitize_text

    if not text:
        return ""
    return sanitize_text(text)


def _source_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _localname(tag: Any) -> str:
    """Strip any XML namespace from a tag for lenient matching."""
    t = str(tag or "")
    return t.split("}", 1)[1] if "}" in t else t


# ---------------------------------------------------------------------------
# Pure XML parsers (never raise; fail-closed). Shared by real + mock providers.
# ---------------------------------------------------------------------------


def parse_law_search_xml(text: str) -> tuple[str, bool, List[Dict[str, Any]]]:
    """Parse a lawSearch.do response. Returns (resultCode, ok, [law dicts]).
    ok is True only for root <LawSearch> AND resultCode == "00". Any error
    envelope / wrong root / malformed XML -> ("", False, [])."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text or "")
    except Exception:
        return "", False, []

    if _localname(root.tag) != "LawSearch":
        # Includes the <Response><result>/<msg> error envelope and HTML.
        return "", False, []

    result_code = (root.findtext("resultCode") or "").strip()
    if result_code != "00":
        return result_code, False, []

    laws: List[Dict[str, Any]] = []
    for law in root.iter("law"):
        mst = (law.findtext("법령일련번호") or "").strip()
        if not mst:
            continue
        laws.append(
            {
                "mst": mst,
                "law_id": (law.findtext("법령ID") or "").strip(),
                "name": _sanitize(law.findtext("법령명한글")),
                "effective_date": (law.findtext("시행일자") or "").strip(),
                "ministry": _sanitize(law.findtext("소관부처명")),
                "detail_link": (law.findtext("법령상세링크") or "").strip(),
            }
        )
    return result_code, True, laws


def _gather_article_text(unit_elem) -> str:
    """Shallow-gather (decision d): concatenate text from 조문제목/조문내용 and
    any one-level 항내용/호내용 descendants. No deep recursion."""
    parts: List[str] = []
    for el in unit_elem.iter():
        if _localname(el.tag) in _ARTICLE_TEXT_TAGS:
            chunk = (el.text or "").strip()
            if chunk:
                parts.append(chunk)
    return _sanitize(" ".join(parts))


def parse_law_body_xml(text: str) -> tuple[bool, List[Dict[str, Any]]]:
    """Parse a lawService.do response. Returns (ok, [article dicts]).
    ok is True only for root <법령>. Error envelope / wrong root / malformed
    XML -> (False, [])."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text or "")
    except Exception:
        return False, []

    if _localname(root.tag) != "법령":
        return False, []

    articles: List[Dict[str, Any]] = []
    for unit in root.iter("조문단위"):
        articles.append(
            {
                "article_no": (unit.findtext("조문번호") or "").strip(),
                "title": _sanitize(unit.findtext("조문제목")),
                "text": _gather_article_text(unit),
            }
        )
    return True, articles


def _law_detail_url(detail_link: str, mst: str) -> str:
    """Build a FULL law.go.kr detail URL. resolve_official_evidence scores the
    URL via is_official_domain (+ numeric-id / detail signals); a relative or
    empty link would score as a weak/search page and block the strong
    classification (hence the M19-3 uplift). law.go.kr is a recognized official
    .go.kr domain, so a full absolute URL with the numeric MST clears the
    'weak_or_search_page' bar."""
    dl = (detail_link or "").strip()
    if dl.startswith("http://") or dl.startswith("https://"):
        return dl
    if dl.startswith("/"):
        return "https://www.law.go.kr" + dl
    if mst:
        return f"https://www.law.go.kr/lsInfoP.do?lsiSeq={mst}"
    return ""


def _assemble_body_text(articles: List[Dict[str, Any]]) -> str:
    """Concatenate per-article text (title + body) into one raw_text, capped."""
    parts: List[str] = []
    for art in articles or []:
        title = art.get("title") or ""
        body = art.get("text") or ""
        joined = f"{title} {body}".strip()
        if joined:
            parts.append(joined)
    return _sanitize(" ".join(parts))[:MAX_ARTICLE_CHARS]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class DisabledNationalLawProvider:
    """Returned when the gate is off or the OC is absent. Pure no-op; zero
    network so callers never special-case the disabled state."""

    name = "national_law"
    external_calls_possible = False

    def __init__(self, *, reason: str = "national law provider disabled") -> None:
        self.available = False
        self.configured = False
        self.reason = reason
        self.error = reason

    def search_laws(self, query: str) -> Dict[str, Any]:
        return {"available": False, "laws": [], "error": self.reason, "result_code": ""}

    def fetch_law_body(self, mst: str) -> Dict[str, Any]:
        return {"available": False, "articles": [], "error": self.reason}


class NationalLawProvider:
    """Real 법제처 law.go.kr DRF provider.

    ``available`` is True only when the gate is on AND LAW_OC is present.
    ``configured`` is True when the OC is present regardless of the gate.
    Constructed without any network call. The OC is held privately and NEVER
    logged / echoed.
    """

    name = "national_law"
    external_calls_possible = True

    def __init__(self) -> None:
        self._oc = config.law_oc()
        self._timeout = config.national_law_timeout_seconds()
        self.error = None
        self.configured = bool(self._oc)

        if not config.national_law_enabled():
            self.available = False
            self.reason = "NATIONAL_LAW_ENABLED=false"
            self.error = self.reason
            return
        if not self._oc:
            self.available = False
            self.reason = "LAW_OC missing"
            self.error = self.reason
            return
        self.available = True
        self.reason = "national law provider ready"

    def _get(self, path: str, params: Dict[str, Any]):
        """HTTPS-preferred GET with HTTP fallback + mandatory browser UA. OC is
        injected here ONLY (never logged). Returns the response or None; never
        raises."""
        try:
            import requests
        except Exception:  # pragma: no cover - requests is a dep
            return None
        full_params = {**params, "OC": self._oc}
        last = None
        for base in (LAW_DRF_BASE_HTTPS, LAW_DRF_BASE_HTTP):
            try:
                return requests.get(
                    base + path,
                    params=full_params,
                    headers=_BROWSER_HEADERS,
                    timeout=self._timeout,
                )
            except Exception as exc:
                last = exc
                continue
        if last is not None:
            log.warning(
                "national_law.request_failed",
                extra={"path": path, "error_type": type(last).__name__,
                       "error_message": str(last)[:200]},
            )
        return None

    def search_laws(self, query: str) -> Dict[str, Any]:
        if not self.available:
            return {"available": False, "laws": [], "error": self.reason, "result_code": ""}
        if not query or not str(query).strip():
            return {"available": True, "laws": [], "error": "empty query", "result_code": ""}

        resp = self._get(_SEARCH_PATH, {"target": "law", "query": str(query), "type": "XML"})
        if resp is None:
            return {"available": True, "laws": [], "error": "transport failure", "result_code": ""}
        status = getattr(resp, "status_code", None)
        if status != 200:
            log.warning("national_law.search_non_200", extra={"status_code": status})
            return {"available": True, "laws": [], "error": f"http status {status}", "result_code": ""}

        code, ok, laws = parse_law_search_xml(getattr(resp, "text", "") or "")
        if not ok:
            log.warning("national_law.search_non_ok", extra={"result_code": code})
            return {"available": True, "laws": [], "error": "non-ok search", "result_code": code}
        return {"available": True, "laws": laws, "error": None, "result_code": code}

    def fetch_law_body(self, mst: str) -> Dict[str, Any]:
        if not self.available:
            return {"available": False, "articles": [], "error": self.reason}
        if not mst or not str(mst).strip():
            return {"available": True, "articles": [], "error": "missing mst"}

        resp = self._get(_BODY_PATH, {"target": "law", "MST": str(mst), "type": "XML"})
        if resp is None:
            return {"available": True, "articles": [], "error": "transport failure"}
        status = getattr(resp, "status_code", None)
        if status != 200:
            log.warning("national_law.body_non_200", extra={"status_code": status})
            return {"available": True, "articles": [], "error": f"http status {status}"}

        ok, articles = parse_law_body_xml(getattr(resp, "text", "") or "")
        if not ok:
            log.warning("national_law.body_non_ok", extra={"article_count": 0})
            return {"available": True, "articles": [], "error": "non-ok body"}
        return {"available": True, "articles": articles, "error": None}


class MockNationalLawProvider:
    """Deterministic, network-free provider for tests/local dev. Runs the SAME
    parsers as the real provider over canned XML, so shapes can't drift. Mirrors
    MockPolicyBriefingProvider."""

    name = "national_law"
    external_calls_possible = False

    def __init__(
        self,
        *,
        search_xml: str = "",
        body_xml_by_mst: Optional[Dict[str, str]] = None,
        available: bool = True,
    ) -> None:
        self.available = available
        self.configured = True
        self.reason = "deterministic mock provider: no network"
        self.error = None
        self._search_xml = search_xml
        self._body_xml_by_mst = dict(body_xml_by_mst or {})

    def search_laws(self, query: str) -> Dict[str, Any]:
        code, ok, laws = parse_law_search_xml(self._search_xml)
        return {
            "available": True,
            "laws": laws if ok else [],
            "error": None if ok else "non-ok search",
            "result_code": code,
        }

    def fetch_law_body(self, mst: str) -> Dict[str, Any]:
        ok, articles = parse_law_body_xml(self._body_xml_by_mst.get(str(mst), ""))
        return {
            "available": True,
            "articles": articles if ok else [],
            "error": None if ok else "non-ok body",
        }


def get_law_provider(name: str = "national_law"):
    """Return the national-law provider for the current environment. Never
    raises. Mirrors providers.get_document_provider for Policy Briefing."""
    key = (name or "").strip().lower()
    if key in ("national_law", "national-law", "nationallaw", "law"):
        provider = NationalLawProvider()
        if provider.available:
            return provider
        return DisabledNationalLawProvider(reason=provider.reason)
    return DisabledNationalLawProvider(reason=f"unsupported provider: {name}")


# ---------------------------------------------------------------------------
# Option A: claims -> law search/body -> official source candidates
# ---------------------------------------------------------------------------


def _claim_tokens(normalized_claims: List[Dict[str, Any]]) -> set:
    tokens: set = set()
    for claim in normalized_claims or []:
        for field in (
            "claim_text", "actor", "action", "target", "object",
            "quantity", "location", "status",
        ):
            value = str(claim.get(field) or "")
            for token in _TOKEN_RE.findall(value):
                if len(token) >= 2 and not token.isdigit():
                    tokens.add(token)
    return tokens


def _derive_queries(normalized_claims: List[Dict[str, Any]]) -> List[str]:
    """Derive <= MAX_SEARCHES distinct statute-like query strings from claims.
    Keyword-based (law search matches 법령명), deduped, capped."""
    queries: List[str] = []
    for claim in normalized_claims or []:
        toks: List[str] = []
        for field in ("actor", "object", "target"):
            value = str(claim.get(field) or "").strip()
            if value and value.lower() != "unknown":
                toks.append(value)
        if not toks:
            ct = str(claim.get("claim_text") or "")
            cand = [t for t in _TOKEN_RE.findall(ct) if len(t) >= 2 and not t.isdigit()]
            toks = cand[:2]
        query = " ".join(dict.fromkeys(toks)).strip()[:60]
        if query and query not in queries:
            queries.append(query)
        if len(queries) >= MAX_SEARCHES:
            break
    return queries[:MAX_SEARCHES]


def _rank_laws(
    laws: List[Dict[str, Any]], normalized_claims: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank-to-fill (never exclude): order by 법령명 token overlap with claims
    desc, then MST for determinism."""
    claim_tokens = _claim_tokens(normalized_claims)

    def overlap(law: Dict[str, Any]) -> int:
        name_tokens = {
            t for t in _TOKEN_RE.findall(law.get("name") or "")
            if len(t) >= 2 and not t.isdigit()
        }
        return len(name_tokens & claim_tokens)

    return sorted(laws, key=lambda law: (-overlap(law), law.get("mst") or ""))


def to_official_source_candidates(
    laws_with_body: List[Dict[str, Any]],
    normalized_claims: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Shape body-fetched laws as official source candidates (Option A). Emits
    one candidate per (claim_index x law). ``official_body_match`` is NEVER set
    here — computed only by resolve_official_evidence (M19-3 guard).

    Each candidate carries the STABLE gating marker ``national_law_mst`` (never
    overwritten by resolve/evaluate), mirroring M21's marker discipline."""
    if not normalized_claims or not laws_with_body:
        return [], 0

    retrieved_at = datetime.now(timezone.utc).isoformat()
    candidates: List[Dict[str, Any]] = []
    for index in range(len(normalized_claims)):
        for law in laws_with_body:
            mst = law.get("mst") or ""
            detail = _law_detail_url(law.get("detail_link") or "", mst)
            candidates.append(
                {
                    "source_id": _source_id(str(index), _SOURCE_TAG, mst),
                    "claim_index": index,
                    "title": law.get("name") or "",
                    "url": detail,
                    "official_detail_url": detail,
                    "publisher": law.get("ministry") or "",
                    "source_type": "official_government",
                    # body lives in raw_text -> read by resolve_official_evidence
                    "raw_text": law.get("raw_text") or "",
                    "raw_text_available": True,
                    "official_body_fetched": True,
                    # official_body_match is NOT set — computed downstream only.
                    "retrieval_method": "national_law_api",  # informational; resolve overwrites
                    "purpose": "primary_source",
                    "retrieved_at": retrieved_at,
                    # STABLE marker (M22-1b lesson) + provenance:
                    "national_law_mst": mst,
                    "national_law_id": law.get("law_id") or "",
                    "national_law_effective_date": law.get("effective_date") or "",
                }
            )
    return candidates, len(laws_with_body)


def fetch_and_build_national_law_candidates(
    normalized_claims: List[Dict[str, Any]],
    *,
    provider=None,
    max_kept: int = MAX_KEPT_LAWS,
) -> tuple[List[Dict[str, Any]], int]:
    """Top-level entry called by the pipeline (Option A). 2-step: search (<=3)
    -> rank-to-fill top-K -> body fetch (<=5) -> inject only laws whose body
    text was retrieved. Never raises; returns ([], 0) on failure / empty.

    The CALLER gates this behind ``config.national_law_enabled()`` so the
    disabled path constructs nothing and hits no network."""
    provider = provider or get_law_provider("national_law")
    if not getattr(provider, "available", False):
        return [], 0

    laws_by_mst: Dict[str, Dict[str, Any]] = {}
    searches = 0
    for query in _derive_queries(normalized_claims):
        if searches >= MAX_SEARCHES:
            break
        result = provider.search_laws(query)
        searches += 1
        for law in result.get("laws") or []:
            mst = law.get("mst")
            if mst and mst not in laws_by_mst:
                laws_by_mst[mst] = law

    ranked = _rank_laws(list(laws_by_mst.values()), normalized_claims)
    kept = ranked[: min(max_kept, MAX_BODY_FETCHES)]

    laws_with_body: List[Dict[str, Any]] = []
    for law in kept:
        body = provider.fetch_law_body(law.get("mst") or "")
        raw_text = _assemble_body_text(body.get("articles") or [])
        if raw_text.strip():
            laws_with_body.append({**law, "raw_text": raw_text})

    return to_official_source_candidates(laws_with_body, normalized_claims)


__all__ = [
    "NationalLawProvider",
    "DisabledNationalLawProvider",
    "MockNationalLawProvider",
    "get_law_provider",
    "fetch_and_build_national_law_candidates",
    "to_official_source_candidates",
    "parse_law_search_xml",
    "parse_law_body_xml",
    "LAW_DRF_BASE_HTTPS",
    "LAW_DRF_BASE_HTTP",
    "MAX_SEARCHES",
    "MAX_KEPT_LAWS",
    "MAX_BODY_FETCHES",
]
