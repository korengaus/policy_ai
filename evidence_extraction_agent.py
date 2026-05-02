from datetime import datetime, timezone
import hashlib
import re


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    normalized = _normalize(text)
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?。])\s+|(?<=다)\s+|(?<=요)\s+", normalized)
    sentences = []
    for part in parts:
        sentence = part.strip(" -•·\t\r\n")
        if 25 <= len(sentence) <= 350:
            sentences.append(sentence)
    return sentences


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[가-힣A-Za-z0-9.%]+", text or "")
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


def _score_sentence(claim: dict, sentence: str) -> int:
    claim_terms = _claim_keywords(claim)
    sentence_terms = _tokens(sentence)
    if not claim_terms or not sentence_terms:
        return 0

    overlap = claim_terms & sentence_terms
    score = min(70, len(overlap) * 12)

    for field in ["actor", "action", "target", "object"]:
        value = claim.get(field) or ""
        if value and value != "unknown" and value in sentence:
            score += 10

    quantity = claim.get("quantity") or ""
    if quantity:
        quantity_tokens = _tokens(quantity)
        if quantity_tokens & sentence_terms:
            score += 15

    return min(score, 100)


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
) -> dict:
    return {
        "evidence_id": _evidence_id(
            str(claim_index),
            source.get("source_id") or "",
            evidence_text[:120],
            evidence_type,
        ),
        "claim_index": claim_index,
        "source_id": source.get("source_id") or "",
        "source_title": source.get("title") or "",
        "source_url": source.get("url") or "",
        "publisher": source.get("publisher") or "",
        "evidence_text": evidence_text,
        "evidence_type": evidence_type,
        "relevance_score": relevance_score,
        "supports_claim": _supports_claim(evidence_type),
        "extraction_method": extraction_method,
        "extraction_confidence": _confidence(relevance_score, evidence_type),
        "extracted_at": _now_iso(),
    }


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
                (_score_sentence(claim, sentence), sentence)
                for sentence in sentences
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = [
            (score, sentence)
            for score, sentence in scored_sentences
            if score >= 25
        ][:max_snippets_per_claim]

        for score, sentence in selected:
            evidence_type = _evidence_type(score, fallback_news_source.get("source_type") or "")
            snippet = _make_snippet(
                claim_index=index,
                source=fallback_news_source,
                evidence_text=sentence,
                evidence_type=evidence_type,
                relevance_score=score,
                extraction_method="article_body_sentence_overlap",
            )
            evidence_snippets.append(snippet)
            claim_snippet_ids.append(snippet["evidence_id"])

        official_sources = [
            source
            for source in (source_candidates or [])
            if source.get("claim_index") == index
            and source.get("source_type") in {"official_government", "public_institution"}
            and not source.get("raw_text_available")
        ][:2]
        for source in official_sources:
            snippet = _make_snippet(
                claim_index=index,
                source=source,
                evidence_text="공식기관 후보는 확인되었지만 실제 문서 본문은 아직 수집되지 않았습니다.",
                evidence_type="official_reference",
                relevance_score=int(source.get("reliability_score") or 0),
                extraction_method="official_candidate_without_body",
            )
            evidence_snippets.append(snippet)
            claim_snippet_ids.append(snippet["evidence_id"])

        if not claim_snippet_ids:
            snippet = _make_snippet(
                claim_index=index,
                source=fallback_news_source,
                evidence_text="해당 주장과 직접 연결할 수 있는 근거 문장을 찾지 못했습니다.",
                evidence_type="insufficient_evidence",
                relevance_score=0,
                extraction_method="no_relevant_sentence_found",
            )
            evidence_snippets.append(snippet)
            claim_snippet_ids.append(snippet["evidence_id"])

        claim_evidence_map[str(index)] = claim_snippet_ids

    insufficient_count = sum(
        1 for snippet in evidence_snippets if snippet.get("evidence_type") == "insufficient_evidence"
    )
    print(f"[EvidenceExtractionAgent] extracted {len(evidence_snippets)} evidence snippets")
    print(f"[EvidenceExtractionAgent] mapped evidence to {len(claim_evidence_map)} claims")
    print(f"[EvidenceExtractionAgent] insufficient evidence count: {insufficient_count}")
    return {
        "evidence_snippets": evidence_snippets,
        "claim_evidence_map": claim_evidence_map,
    }


def summarize_evidence_snippets(evidence_snippets: list[dict]) -> dict:
    snippets = evidence_snippets or []
    return {
        "evidence_snippet_count": len(snippets),
        "direct_support_count": sum(1 for item in snippets if item.get("evidence_type") == "direct_support"),
        "official_reference_count": sum(1 for item in snippets if item.get("evidence_type") == "official_reference"),
        "insufficient_evidence_count": sum(1 for item in snippets if item.get("evidence_type") == "insufficient_evidence"),
    }
