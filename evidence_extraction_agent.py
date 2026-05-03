from datetime import datetime, timezone
import hashlib
import re

from text_utils import sanitize_text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize(text: str) -> str:
    text = sanitize_text(text)
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    normalized = _normalize(text)
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?。])\s+|(?<=[다요죠음함])\.\s*|(?<=다)\s+", normalized)
    sentences = []
    for part in parts:
        sentence = part.strip(" -•·\t\r\n")
        if 25 <= len(sentence) <= 350:
            sentences.append(sentence)
    return sentences


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\uac00-\ud7a3A-Za-z0-9.%]+", text or "")
        if len(token) >= 2
    }


def _claim_keywords(claim: dict) -> set[str]:
    fields = [
        claim.get("claim_text") or "",
        claim.get("actor") or "",
        claim.get("action") or "",
        claim.get("target") or "",
        claim.get("object") or "",
        claim.get("quantity") or "",
        claim.get("date_or_time") or "",
        claim.get("location") or "",
    ]
    return set().union(*(_tokens(field) for field in fields))


def _score_text_against_claim(claim: dict, text: str) -> tuple[int, str]:
    claim_terms = _claim_keywords(claim)
    text_terms = _tokens(text)
    if not claim_terms or not text_terms:
        return 0, "no matched text"

    overlap = claim_terms & text_terms
    score = min(65, len(overlap) * 12)
    reasons = []
    if overlap:
        reasons.append("matched terms: " + ", ".join(sorted(overlap)[:6]))

    for field in ["actor", "action", "target", "object"]:
        value = claim.get(field) or ""
        if value and value != "unknown" and value in text:
            score += 10
            reasons.append(f"{field} overlap")

    quantity = claim.get("quantity") or ""
    if quantity and (_tokens(quantity) & text_terms):
        score += 15
        reasons.append("quantity overlap")

    if not reasons:
        reasons.append("query overlap too low")
    return min(score, 100), "; ".join(reasons)


def _evidence_type(score: int, source_type: str) -> str:
    if source_type in {"official_government", "public_institution"}:
        return "official_reference"
    if score >= 70:
        return "direct_support"
    if score >= 40:
        return "indirect_support"
    if score > 0:
        return "background_context"
    return "insufficient_evidence"


def _evidence_strength(evidence_type: str, score: int) -> str:
    if evidence_type == "official_reference":
        return "weak"
    if evidence_type == "direct_support" or score >= 70:
        return "strong"
    if evidence_type == "indirect_support" or score >= 40:
        return "medium"
    if evidence_type in {"background_context", "official_reference"} or score > 0:
        return "weak"
    return "none"


def _supports_claim(evidence_type: str) -> str:
    if evidence_type in {"direct_support", "indirect_support"}:
        return "supports"
    if evidence_type == "insufficient_evidence":
        return "not_enough_info"
    return "unclear"


def _confidence(score: int, evidence_type: str) -> str:
    if evidence_type == "official_reference":
        return "low"
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _make_snippet(
    *,
    claim_index: int,
    source: dict,
    evidence_text: str,
    evidence_type: str,
    relevance_score: int,
    extraction_method: str,
    match_reason: str = "",
) -> dict:
    evidence_text = sanitize_text(evidence_text)
    evidence_type = sanitize_text(evidence_type)
    strength = _evidence_strength(evidence_type, relevance_score)
    return {
        "evidence_id": _evidence_id(
            str(claim_index),
            source.get("source_id") or "",
            evidence_text[:120],
            evidence_type,
        ),
        "claim_index": claim_index,
        "source_id": source.get("source_id") or "",
        "source_title": sanitize_text(source.get("title") or ""),
        "source_url": source.get("url") or "",
        "publisher": sanitize_text(source.get("publisher") or ""),
        "evidence_text": evidence_text,
        "evidence_type": evidence_type,
        "evidence_strength": strength,
        "relevance_score": relevance_score,
        "supports_claim": _supports_claim(evidence_type),
        "extraction_method": extraction_method,
        "extraction_confidence": _confidence(relevance_score, evidence_type),
        "match_reason": match_reason or strength,
        "extracted_at": _now_iso(),
    }


def _source_metadata_text(source: dict) -> str:
    return _normalize(
        " ".join(
            [
                source.get("title") or "",
                source.get("publisher") or "",
                source.get("query_used") or "",
                source.get("purpose") or "",
                source.get("retrieval_method") or "",
                source.get("url") or "",
            ]
        )
    )


def _source_metadata_snippet(claim_index: int, claim: dict, source: dict) -> dict | None:
    score, reason = _score_text_against_claim(claim, _source_metadata_text(source))
    if score < 20:
        return None

    source_type = source.get("source_type") or ""
    is_official = source_type in {"official_government", "public_institution"}
    evidence_type = "official_reference" if is_official else "background_context"
    evidence_text = (
        "공식기관 후보는 확인되었지만 실제 문서 본문은 아직 수집되지 않았습니다."
        if is_official
        else "뉴스/검색 후보의 제목, 출처, 검색어가 주장과 일부 겹쳐 약한 근거로 표시합니다."
    )
    method = (
        "official_candidate_metadata_overlap_without_body"
        if is_official
        else "news_fallback_metadata_overlap"
    )
    if is_official:
        reason = f"official body not fetched; {reason}"
    else:
        reason = f"weak news fallback evidence; {reason}"

    return _make_snippet(
        claim_index=claim_index,
        source=source,
        evidence_text=evidence_text,
        evidence_type=evidence_type,
        relevance_score=score,
        extraction_method=method,
        match_reason=reason,
    )


def extract_evidence_snippets(
    *,
    normalized_claims: list[dict],
    source_candidates: list[dict],
    article_body: str = "",
    max_snippets_per_claim: int = 3,
) -> dict:
    sentences = _split_sentences(article_body)
    evidence_snippets = []
    claim_evidence_map = {}

    news_sources = [
        source
        for source in (source_candidates or [])
        if source.get("raw_text_available") and source.get("purpose") == "news_context"
    ]
    fallback_news_source = news_sources[0] if news_sources else {
        "source_id": "current_article_body",
        "title": "Current article body",
        "url": "",
        "publisher": "",
        "source_type": "established_news",
    }

    for index, claim in enumerate(normalized_claims or []):
        claim_snippet_ids = []
        scored_sentences = sorted(
            (
                (*_score_text_against_claim(claim, sentence), sentence)
                for sentence in sentences
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = [
            (score, reason, sentence)
            for score, reason, sentence in scored_sentences
            if score >= 25
        ][:max_snippets_per_claim]

        for score, reason, sentence in selected:
            evidence_type = _evidence_type(score, fallback_news_source.get("source_type") or "")
            snippet = _make_snippet(
                claim_index=index,
                source=fallback_news_source,
                evidence_text=sentence,
                evidence_type=evidence_type,
                relevance_score=score,
                extraction_method="article_body_sentence_overlap",
                match_reason=reason,
            )
            evidence_snippets.append(snippet)
            claim_snippet_ids.append(snippet["evidence_id"])

        metadata_sources = [
            source
            for source in (source_candidates or [])
            if source.get("claim_index") == index and not source.get("raw_text_available")
        ]
        metadata_snippets = []
        for source in metadata_sources:
            snippet = _source_metadata_snippet(index, claim, source)
            if snippet:
                metadata_snippets.append(snippet)

        metadata_snippets.sort(key=lambda item: item.get("relevance_score", 0), reverse=True)
        for snippet in metadata_snippets[:2]:
            evidence_snippets.append(snippet)
            claim_snippet_ids.append(snippet["evidence_id"])

        if not claim_snippet_ids:
            reason = "no matched text"
            if source_candidates:
                reason = "query overlap too low"
            snippet = _make_snippet(
                claim_index=index,
                source=fallback_news_source,
                evidence_text="해당 주장과 직접 연결할 수 있는 근거 문장을 찾지 못했습니다.",
                evidence_type="insufficient_evidence",
                relevance_score=0,
                extraction_method="no_relevant_sentence_found",
                match_reason=reason,
            )
            evidence_snippets.append(snippet)
            claim_snippet_ids.append(snippet["evidence_id"])

        claim_evidence_map[str(index)] = claim_snippet_ids

    insufficient_count = sum(
        1 for snippet in evidence_snippets if snippet.get("evidence_type") == "insufficient_evidence"
    )
    strength_summary = _strength_summary(evidence_snippets)
    print(f"[EvidenceExtractionAgent] extracted {len(evidence_snippets)} evidence snippets")
    print(f"[EvidenceExtractionAgent] mapped evidence to {len(claim_evidence_map)} claims")
    print(f"[EvidenceExtractionAgent] insufficient evidence count: {insufficient_count}")
    print(
        "[EvidenceExtractionAgent] strength "
        f"strong={strength_summary['strong']} "
        f"medium={strength_summary['medium']} "
        f"weak={strength_summary['weak']}"
    )
    return {
        "evidence_snippets": evidence_snippets,
        "claim_evidence_map": claim_evidence_map,
    }


def _strength_summary(evidence_snippets: list[dict]) -> dict:
    snippets = evidence_snippets or []
    return {
        "strong": sum(1 for item in snippets if item.get("evidence_strength") == "strong"),
        "medium": sum(1 for item in snippets if item.get("evidence_strength") == "medium"),
        "weak": sum(1 for item in snippets if item.get("evidence_strength") == "weak"),
        "none": sum(1 for item in snippets if item.get("evidence_strength") in {"none", None}),
    }


def summarize_evidence_snippets(evidence_snippets: list[dict]) -> dict:
    snippets = evidence_snippets or []
    zero_reasons = [
        item.get("match_reason") or item.get("extraction_method") or "unknown"
        for item in snippets
        if item.get("evidence_strength") in {"none", None}
        or item.get("evidence_type") == "insufficient_evidence"
    ]
    if not snippets:
        zero_reasons = ["no matched text"]
    return {
        "evidence_snippet_count": len(snippets),
        "direct_support_count": sum(1 for item in snippets if item.get("evidence_type") == "direct_support"),
        "official_reference_count": sum(1 for item in snippets if item.get("evidence_type") == "official_reference"),
        "insufficient_evidence_count": sum(1 for item in snippets if item.get("evidence_type") == "insufficient_evidence"),
        "evidence_strength_summary": _strength_summary(snippets),
        "evidence_zero_reasons": list(dict.fromkeys(zero_reasons))[:5],
    }
