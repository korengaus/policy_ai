from __future__ import annotations

from urllib.parse import urlparse


OFFICIAL_AUTHORITY_DOMAINS = {
    "fsc.go.kr",
    "fss.or.kr",
    "molit.go.kr",
    "moef.go.kr",
    "bok.or.kr",
    "korea.kr",
    "gov.kr",
    "mss.go.kr",
    "msit.go.kr",
    "kdi.re.kr",
    "kif.re.kr",
    "hf.go.kr",
    "khug.or.kr",
    "lh.or.kr",
    "hfn.go.kr",
    "nts.go.kr",
    "customs.go.kr",
    "kosis.kr",
    "stat.go.kr",
    "law.go.kr",
    "epeople.go.kr",
    "assembly.go.kr",
}


PUBLIC_INSTITUTION_DOMAINS = {
    "kdi.re.kr",
    "kif.re.kr",
    "hf.go.kr",
    "khug.or.kr",
    "lh.or.kr",
    "hfn.go.kr",
    "kosis.kr",
    "stat.go.kr",
}


OFFICIAL_NAME_HINTS = {
    "financial services commission",
    "financial supervisory service",
    "ministry of land, infrastructure and transport",
    "ministry of economy and finance",
    "bank of korea",
    "hug",
    "lh",
    "korea housing finance corporation",
    "korea housing & urban guarantee corporation",
    "금융위원회",
    "금융위",
    "금융감독원",
    "금감원",
    "국토교통부",
    "국토부",
    "기획재정부",
    "기재부",
    "한국은행",
    "한은",
    "중소벤처기업부",
    "과학기술정보통신부",
    "주택도시보증공사",
    "한국주택금융공사",
    "한국토지주택공사",
}


NAME_TO_DOMAIN = {
    "financial services commission": "fsc.go.kr",
    "금융위원회": "fsc.go.kr",
    "금융위": "fsc.go.kr",
    "financial supervisory service": "fss.or.kr",
    "금융감독원": "fss.or.kr",
    "금감원": "fss.or.kr",
    "ministry of land, infrastructure and transport": "molit.go.kr",
    "국토교통부": "molit.go.kr",
    "국토부": "molit.go.kr",
    "ministry of economy and finance": "moef.go.kr",
    "기획재정부": "moef.go.kr",
    "기재부": "moef.go.kr",
    "bank of korea": "bok.or.kr",
    "한국은행": "bok.or.kr",
    "한은": "bok.or.kr",
    "주택도시보증공사": "khug.or.kr",
    "hug": "khug.or.kr",
    "한국주택금융공사": "hf.go.kr",
    "한국토지주택공사": "lh.or.kr",
    "lh": "lh.or.kr",
}


def normalize_domain(url: str = "") -> str:
    try:
        return urlparse(url or "").netloc.lower().replace("www.", "")
    except Exception:
        return ""


def domain_matches(domain: str, patterns: set[str] | list[str]) -> bool:
    domain = (domain or "").lower()
    return any(domain == pattern or domain.endswith("." + pattern) for pattern in patterns)


def is_official_domain(url: str = "") -> bool:
    domain = normalize_domain(url)
    return bool(
        domain.endswith(".go.kr")
        or domain_matches(domain, OFFICIAL_AUTHORITY_DOMAINS)
        or domain_matches(domain, PUBLIC_INSTITUTION_DOMAINS)
    )


def is_public_institution_domain(url: str = "") -> bool:
    return domain_matches(normalize_domain(url), PUBLIC_INSTITUTION_DOMAINS)


def name_implies_official(name: str = "") -> bool:
    lowered = (name or "").lower()
    return any(hint.lower() in lowered for hint in OFFICIAL_NAME_HINTS)


def canonical_official_domain(name: str = "", url: str = "") -> str:
    domain = normalize_domain(url)
    if domain:
        return domain
    lowered = (name or "").lower()
    for hint, mapped_domain in NAME_TO_DOMAIN.items():
        if hint.lower() in lowered:
            return mapped_domain
    return ""


def official_source_type_from_identity(name: str = "", url: str = "") -> str | None:
    if is_public_institution_domain(url):
        return "public_institution"
    if is_official_domain(url) or name_implies_official(name):
        return "official_government"
    return None


def looks_like_official_search_or_index_url(url: str = "") -> bool:
    lowered = (url or "").lower().rstrip("/")
    if not lowered:
        return True
    markers = [
        "search",
        "srchtxt",
        "query=",
        "keyword=",
        "kwd=",
        "/list",
        "listall",
        "portal/list",
        "service/list",
        "main.do",
        "/index",
    ]
    if any(marker in lowered for marker in markers):
        return True
    domain = normalize_domain(url)
    if domain and lowered in {f"https://{domain}", f"http://{domain}", f"https://{domain}/", f"http://{domain}/"}:
        return True
    return False
