import re

from structured_logging import get_logger


# M14.0-print-a (2026-05-26): module logger replaces the
# [ClaimExtractor] print() diagnostic.
log = get_logger(__name__)


POLICY_KEYWORDS = [
    "정부",
    "금융당국",
    "금융위원회",
    "금감원",
    "국토부",
    "한국은행",
    "국회",
    "지자체",
    "은행",
    "기업은행",
    "규제",
    "제한",
    "차단",
    "금지",
    "검토",
    "추진",
    "조사",
    "착수",
    "시행",
    "운영",
    "지원",
    "확대",
    "축소",
    "감면",
    "인하",
    "인상",
    "동결",
    "대출",
    "전세대출",
    "주택담보대출",
    "주담대",
    "금리",
    "전세",
    "주택",
    "부동산",
    "청년",
    "중소기업",
]

OPINION_KEYWORDS = [
    "전망이다",
    "예상된다",
    "분석된다",
    "관측된다",
    "관측이",
    "관측도",
    "전망했다",
    "예상했다",
    "분석했다",
    "지적했다",
    "강조했다",
    "밝혔다",
    "주장했다",
    "평가했다",
    "의견",
    "칼럼",
    "사설",
]

WEAK_ENDINGS = [
    "것으로 보인다",
    "가능성도 있다",
    "필요가 있다",
    "해야 한다",
]


def _normalize_text(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ARTICLE-CHROME (71-APPLY) \u2014 bylines, datelines, edit/input timestamps, wire
# footers and photo captions were entering the stored claim field. Measured
# over 1,300 real rows (ids 12059-15795): 63 rows (4.8%) / 111 claim texts
# carried chrome; the shapes are (a) wire dateline+byline runs
# "(\ub300\uc804=\ub274\uc2a4\ucda9\uccad\uc778) \uae40\uc218\ud658 \uae30\uc790 =", (b) bare bylines "\ubc15\uc7ac\ucc2c \ubcf4\ud5d8\uc804\ubb38\uae30\uc790 =",
# (c) edit/input stamps "\ud3b8\uc9d1 2026.08.10 [23:13]" / "\uae30\uc0ac\uc785\ub825 2026/08/10
# [21:14]" / "\uc785\ub825 : 2026-08-12 17:19:44", (d) \uc5f0\ud569\ub274\uc2a4 footers "\uc81c\ubcf4\ub294
# \uce74\uce74\uc624\ud1a1 okjebo <\uc800\uc791\uad8c\uc790(c) \u2026 \uae08\uc9c0> 2026\ub14408\uc6d417\uc77c 06\uc2dc04\ubd84 \uc1a1\uace0", and
# (e) leading photo captions "\uc0ac\uc9c4=\uc0bc\uc131\uc0dd\uba85". The pipeline had NO detector \u2014
# only the display layer carried one (main.js ARTICLE_NOISE_SENTENCE_PATTERNS)
# and its five patterns sentence-reject just 27 of the 111 stored texts.
#
# EVERY rule below STRIPS the matched chrome and keeps the remainder \u2014 none
# rejects a whole sentence. False positives are worse than misses: a glued
# wire run (id 15721) carries a REAL claim after its footer, so rejection
# would destroy data that stripping recovers; a sentence that was pure chrome
# strips to nothing and the existing >=18-char floor drops it. Each pattern
# was run against all 3,927 sampled claim texts: zero legitimate claims
# matched (tests/test_claim_extractor_noise.py pins specimens + look-alike
# legitimate claims). Chrome only \u2014 no verdict field, score, label or
# threshold is read or written here.
_CHROME_DATE = r"\d{4}\s*[년./\-]\s*\d{1,2}\s*[월./\-]\s*\d{1,2}\s*일?\.?"
_CHROME_TIME = r"(?:\s*\[?\d{1,2}\s*[:시]\s*\d{2}(?:\s*:\s*\d{2})?\s*분?\]?)?"
_CHROME_CLOCK = r"\d{1,2}\s*[:시]\s*\d{2}(?:\s*:\s*\d{2})?\s*분?"
_CHROME_PATTERNS = [
    # (a) wire dateline "(지역=매체)" / "[세종=뉴시스]" — paren or square
    # bracket, optionally followed by the inline byline "이름 기자 =".
    re.compile(
        r"[(\[]\s*[가-힣A-Za-z][가-힣A-Za-z0-9·\s]{0,14}=\s*[가-힣A-Za-z][가-힣A-Za-z0-9·\s]{0,18}[)\]]\s*"
        r"(?:[가-힣]{2,4}\s*[가-힣]{0,8}(?:기자|특파원)\s*=\s*)?"),
    # (b) inline byline "이름 [전문]기자 = " anywhere ("박재찬 보험전문기자 =",
    # "…뉴스임창용 기자=") — the trailing "=" is what makes it a byline.
    re.compile(r"[가-힣]{2,4}\s*[가-힣]{0,8}\s*(?:기자|특파원)\s*=\s*"),
    # (b2) byline glued to a mangled e-mail/domain: "이정민기자 ljm7damdilbo.com".
    re.compile(r"[가-힣]{2,6}\s*기자\s*[A-Za-z0-9._%-]{2,30}\.(?:com|co\.kr|kr|net)"),
    # (b3) pipe-terminated outlet byline: "매일일보 = 서정욱 기자 |".
    re.compile(r"[가-힣A-Za-z0-9]{2,14}\s*=\s*[가-힣]{2,4}\s*기자\s*[|｜]"),
    # (b4) trailing byline after a finished sentence: "…지원하고 있다. 정근산 기자".
    re.compile(r"(?<=[.!?])\s*[가-힣]{2,4}\s*(?:기자|특파원)\s*$"),
    # (c1) strong stamp words + full date anywhere (편집/기사입력/기사수정) —
    # 승인/업데이트 deliberately NOT shipped (unmeasured; "국무회의 승인
    # 2026년 1월 5일…" is plausible prose).
    re.compile(r"(?:편집|기사\s*입력|기사\s*수정)\s*[:：]?\s*" + _CHROME_DATE + _CHROME_TIME),
    # (c2) 등록 needs date AND clock ("등록 2026.07.31 09:13:55") so registry
    # prose ("법인 등록 2026년…") can never match.
    re.compile(r"등록\s*[:：]?\s*" + _CHROME_DATE + r"\s*" + _CHROME_CLOCK),
    # (c3) bare 입력/수정 stamps need a colon anywhere ("입력 : 2026-08-12
    # 17:19:44") — colonless only at claim start (c4), so prose like
    # "…개정안 수정 2026년 3월 12일 공포" can never match.
    re.compile(r"[|｜]?\s*(?:입력|수정)\s*[:：]\s*" + _CHROME_DATE + _CHROME_TIME),
    re.compile(r"^(?:입력|수정)\s*" + _CHROME_DATE + _CHROME_TIME),
    # (d) wire footers: 카카오톡 handle, 기사문의 제보 line, angle-bracketed
    # copyright block, the copyright phrases, and "<date> <clock> 송고".
    re.compile(r"제보는\s*카카오톡\s*[A-Za-z0-9_]{0,20}"),
    re.compile(r"기사문의\s*및\s*제보\s*[:：]?\s*(?:카톡|카카오톡|라인)[/A-Za-z0-9\s]{0,24}"),
    re.compile(r"[<〈][^<>〈〉]{0,80}(?:저작권자|무단\s*전재|재배포)[^<>〈〉]{0,80}[>〉]"),
    re.compile(r"무단\s*전재\s*[-·,와및\s]*재배포(?:\s*금지)?"),
    re.compile(r"AI\s*학습\s*및\s*활용\s*금지"),
    re.compile(_CHROME_DATE + r"\s*" + _CHROME_CLOCK + r"\s*송고"),
    # (e) photo captions: paren-bounded anywhere; bare "사진=…" bounded to one
    # token unless it ends in caption vocabulary (제공/캡처/무관).
    re.compile(r"[(（]\s*(?:자료)?사진\s*=[^()（）]{1,30}[)）]"),
    re.compile(r"/?\s*(?:자료)?사진\s*=\s*(?:\S{1,24}(?:\s+\S{1,16}){0,2}\s*(?:제공|캡처|무관)|\S{1,24})"),
]


def _strip_article_chrome(text: str) -> str:
    """Remove article chrome, keep everything else. Iterates to a fixpoint so
    glued repeats ("\u202605:49 \uc1a1\uace02026\ub14408\uc6d416\uc77c 05\uc2dc49\ubd84 \uc1a1\uace0") fully clear."""
    previous = None
    while previous != text:
        previous = text
        for pattern in _CHROME_PATTERNS:
            text = pattern.sub(" ", text)
        text = _normalize_text(text)
    return text


# PERSONNEL-GATE (92-APPLY) — personnel-roster articles were having their
# NAME LISTS selected as the claim: department names contain policy keywords
# as substrings (운영지원과 → 운영 + 지원, 국회사무처 → 국회), so a roster
# passes _is_verifiable and then out-scores real sentences on keyword count,
# length and digits. Measured over the full 16,026-row corpus: 20 rows carry
# a personnel marker in the title and 11 of them render a roster claim; every
# roster-claim row carries one of the markers below. The gate recognises the
# DOCUMENT TYPE by the wire services' own title label and routes the article
# to the existing summary/title fallback (the path 9 sibling rows already
# took) — the row is still collected, clustered and counted; only the claim
# sentence changes. Nothing here judges what a claim is worth, and no verdict
# field, score threshold or chrome pattern is touched.
_PERSONNEL_TITLE_MARKERS = [
    # "[인사]", "[인사] 국가데이터처", "[8월18일 인사종합] …" — a leading
    # bracket tag whose content ENDS in 인사/인사종합. Agency names merely
    # containing 인사 ("[인사혁신처] …") do not end in the marker and pass.
    re.compile(r"^\s*[\[［][^\]］]{0,12}인사(?:종합)?\s*[\]］]"),
    # "…첫 5급 이상 인사(종합)" — 인사 immediately before the wire (종합)
    # roundup tag. 인사청문회(종합) has 회 before the paren and passes.
    re.compile(r"인사\s*[(（]\s*종합\s*[)）]"),
]


def _is_personnel_notice_title(title: str) -> bool:
    normalized = _normalize_text(title or "")
    return any(pattern.search(normalized) for pattern in _PERSONNEL_TITLE_MARKERS)


# CLAIM-DISPLAY-2 FIX B: the old pattern split on a BARE Korean ender
# (다|요|죠|음|임|됨|함) + whitespace, with no punctuation required. Ordinary
# mid-sentence words end in those syllables — 보다, 부터, 이다, 마다 — so a
# sentence was severed mid-clause and the fragment became the 핵심 주장
# (verified: "…지난해(1.1%)보다" cut loose as a 47-char stub). A genuine Korean
# sentence end carries terminal punctuation ("…기록했다."), so require it. The
# second alternative lets a closing quote/bracket sit between the punctuation
# and the space ('…말했다." 정부는') without being eaten by the split.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?？！．])\s+|(?<=[.!?？！．][\"'”’」』)\]])\s+")
# Retained ONLY as a recall net for bodies with no terminal punctuation at all
# (some wire copy), where the strict pattern would yield one over-long blob that
# the length filter drops, leaving zero claims. Never used when the strict split
# already produces a usable sentence.
_SENTENCE_SPLIT_LEGACY = re.compile(r"(?<=[.!?다요죠음임됨함])\s+")


def _split_sentences(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    sentences = _collect_sentences(_SENTENCE_SPLIT.split(normalized))
    if not sentences:
        sentences = _collect_sentences(_SENTENCE_SPLIT_LEGACY.split(normalized))
    return sentences


def _collect_sentences(parts: list[str]) -> list[str]:
    sentences = []
    for part in parts:
        sentence = part.strip(" -•·\t\r\n")
        sentence = re.sub(r"\s+", " ", sentence)
        # ARTICLE-CHROME (71-APPLY): strip bylines/datelines/stamps/footers
        # BEFORE the length filter, so a pure-chrome sentence shrinks below
        # the 18-char floor and drops, while a chrome-prefixed real claim
        # keeps its claim text (and is scored on the clean text).
        sentence = _strip_article_chrome(sentence)
        # CLAIM-QUALITY FIX 1: the old 260 ceiling silently DROPPED well-formed
        # long policy sentences at extraction, so a shorter fragment won the
        # ranking and rendered as a stub 핵심 주장. The ceiling is raised to 400
        # so it sits ABOVE the 360-char display cap (_CLAIM_MAX_CHARS) — a good
        # sentence is never rejected at extraction only to be wanted at display.
        # Lower bound unchanged: <18 chars is still a fragment, not a claim.
        if 18 <= len(sentence) <= 400:
            sentences.append(sentence)
    return sentences


def _is_opinion(sentence: str) -> bool:
    opinion_hits = sum(1 for keyword in OPINION_KEYWORDS if keyword in sentence)
    has_policy_signal = any(keyword in sentence for keyword in POLICY_KEYWORDS)
    has_number = bool(re.search(r"\d", sentence))
    has_official_action = _has_official_actor_action(sentence)

    if opinion_hits and not has_official_action:
        return True
    if opinion_hits >= 2 and not has_number:
        return True
    if any(ending in sentence for ending in WEAK_ENDINGS) and not has_policy_signal:
        return True
    return False


def _has_official_actor_action(sentence: str) -> bool:
    return bool(
        re.search(
            r"(정부|당국|금융당국|금융위|금융위원회|금감원|금융감독원|국토부|한국은행|국회|은행|기업은행).{0,45}"
            r"(검토|추진|조사|착수|시행|운영|지원|제한|차단|금지|감면|인하|인상|동결|결정|발표)",
            sentence,
        )
    )


def _is_verifiable(sentence: str) -> bool:
    if not sentence:
        return False
    if _is_opinion(sentence):
        return False

    has_policy = any(keyword in sentence for keyword in POLICY_KEYWORDS)
    has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|%p|원|억원|조원|명|건|일|년|개월|주택자)?", sentence))
    has_actor_action = _has_official_actor_action(sentence)

    return has_actor_action or (has_policy and has_number) or sum(keyword in sentence for keyword in POLICY_KEYWORDS) >= 3


def _claim_score(sentence: str) -> int:
    score = 0
    score += min(len(sentence), 140)
    score += sum(18 for keyword in POLICY_KEYWORDS if keyword in sentence)
    if re.search(r"\d", sentence):
        score += 30
    if re.search(r"(검토|추진|조사|착수|시행|운영|지원|제한|차단|금지|감면|인하|인상|동결)", sentence):
        score += 35
    if re.search(r"(정부|금융당국|금융위|금감원|국토부|한국은행|국회|기업은행)", sentence):
        score += 30
    if _is_opinion(sentence):
        score -= 80
    return score


# CLAIM-QUALITY FIX 2: display/storage cap for a single claim. Raised 220 -> 360
# and kept in lockstep with limitClaimSentences() in frontend/scripts/main.js so
# the two independent truncation layers agree instead of each shaving the text.
_CLAIM_MAX_CHARS = 360
# Sentence enders: latin punctuation, or a Korean terminal syllable before space.
_CLAIM_SENTENCE_END = re.compile(r"[.!?…]|[다요죠음임됨함](?=\s)")


def _truncate_on_boundary(sentence: str, limit: int) -> str:
    """Cut at a sentence boundary when possible, else a word boundary.

    The old ``[:217] + "..."`` sliced mid-word/mid-syllable, which is what the
    reader saw as 문장이 끊김. Prefer the last sentence end inside the cap (a
    complete sentence needs no ellipsis); fall back to the last whitespace.
    """
    if len(sentence) <= limit:
        return sentence
    window = sentence[:limit]
    last_end = 0
    for match in _CLAIM_SENTENCE_END.finditer(window):
        last_end = match.end()
    # Only accept the sentence boundary if it keeps at least half the budget —
    # otherwise an early period would gut the claim.
    if last_end >= limit // 2:
        return window[:last_end].rstrip()
    head = window.rsplit(" ", 1)[0].rstrip() if " " in window else window.rstrip()
    return f"{head}..."


def _clean_claim(sentence: str) -> str:
    sentence = _normalize_text(sentence)
    # ARTICLE-CHROME (71-APPLY): also covers the summary/title FALLBACK path,
    # which never goes through _collect_sentences (RSS summaries carry the
    # same chrome). Idempotent for ranked sentences already stripped there.
    sentence = _strip_article_chrome(sentence)
    sentence = re.sub(r"[^\w\s가-힣.,!?%·…~()\[\]{}<>:;\"'“”‘’/\-+_=|]", "", sentence)
    sentence = _normalize_text(sentence)
    sentence = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", sentence)
    return _truncate_on_boundary(sentence, _CLAIM_MAX_CHARS)


def extract_verifiable_claims(
    article_body: str,
    title: str = "",
    summary: str = "",
    max_claims: int = 5,
) -> list[str]:
    source_text = article_body if article_body and len(article_body) >= 100 else ""
    fallback_text = summary or title or ""
    # PERSONNEL-GATE (92-APPLY): a personnel-roster article's body is a name
    # list, never ranked — the summary/title fallback below records the
    # circulation instead. Everything downstream is unchanged.
    if _is_personnel_notice_title(title):
        sentences = []
    else:
        sentences = _split_sentences(source_text) if source_text else []

    ranked = sorted(
        (sentence for sentence in sentences if _is_verifiable(sentence)),
        key=_claim_score,
        reverse=True,
    )

    claims = []
    seen = set()
    for sentence in ranked:
        claim = _clean_claim(sentence)
        dedupe_key = re.sub(r"\W+", "", claim)[:80]
        if not claim or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        claims.append(claim)
        if len(claims) >= max_claims:
            break

    if not claims and fallback_text:
        fallback_claim = _clean_claim(fallback_text)
        if fallback_claim:
            claims.append(fallback_claim)

    # M14.0-print-a (2026-05-26): print → log.info conversion.
    log.info(
        f"[ClaimExtractor] extracted {len(claims)} claims",
        extra={"claims_count": len(claims)},
    )
    return claims
