import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import MAX_ARTICLE_CHARS, MAX_NEWS_RESULTS, MAX_POLICY_SENTENCES, QUERY
from news_collector import search_google_news_rss_with_meta, resolve_google_news_url
from article_extractor import fetch_article_body
from claim_extractor import extract_verifiable_claims
from claim_normalizer import normalize_claims
from rule_engine import extract_policy_claim_sentences
from ai_reasoner import run_ai_reasoning
from memory_store import (
    load_policy_memory,
    save_policy_memory,
    make_article_id,
    summarize_all_memory,
    move_existing_articles_to_better_topics,
    update_memory_with_result,
)
from official_source_search import (
    generate_official_source_candidates,
    print_official_source_candidates,
)
from source_retrieval_agent import build_source_retrieval_context
from source_reliability_agent import evaluate_source_candidates
from evidence_extraction_agent import extract_evidence_snippets
from official_crawler import fetch_official_evidence, print_official_evidence_results
from evidence_comparator import (
    compare_news_with_official_evidence,
    print_evidence_comparison,
)
from policy_confidence import calculate_policy_confidence, print_policy_confidence
from policy_impact import analyze_policy_impact, print_policy_impact
from policy_decision import make_final_decision, print_final_decision
from topic_classifier import classify_policy_topic
from timeline import print_timeline_summary
from verification_card import build_verification_card, print_verification_card


REPORTS_DIR = Path("reports")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_report_path(run_started_at: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    started = datetime.fromisoformat(run_started_at)
    filename = started.strftime("policy_analysis_%Y%m%d_%H%M%S.json")
    return REPORTS_DIR / filename


def build_topics_summary(memory: dict) -> dict:
    summary = {}

    for topic, data in memory.get("topics", {}).items():
        summary[topic] = {
            "event_count": len(data.get("events", [])),
            "latest_stage": data.get("latest_stage"),
            "latest_probability": data.get("latest_probability"),
            "latest_market_impact": data.get("latest_market_impact"),
            "latest_signal_change": data.get("latest_signal_change"),
            "timeline": data.get("timeline", {}),
        }

    return summary


def save_run_report(report: dict, run_started_at: str) -> Path:
    report_path = build_report_path(run_started_at)

    with open(report_path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=True, indent=2)

    return report_path


def print_rule_based_results(policy_claims: list[dict]):
    print("\n----- Rule-based policy sentences -----")

    if not policy_claims:
        print("No important policy sentences found.")
        return

    for item in policy_claims:
        print(f"- {item['sentence']}")
        print(f"  score: {item['score']}")
        print(f"  authority: {item['authority_label']}")
        print(f"  strength: {item['strength_label']}")
        print(f"  execution: {item['execution_label']}")
        print(f"  reasons: {', '.join(item['reasons'])}")


def print_ai_results(ai_result: dict):
    print("\n----- AI reasoning result -----")

    if not ai_result.get("ai_available"):
        print("AI reasoning unavailable")
        print("reason:", ai_result.get("error"))
        print(ai_result.get("fallback_message"))
        return

    print("summary:", ai_result.get("one_line_summary"))
    print("policy signal:", ai_result.get("policy_signal_detected"))
    print("main issue:", ai_result.get("main_policy_issue"))
    print("execution probability:", str(ai_result.get("execution_probability")) + "%")
    print("execution stage:", ai_result.get("execution_stage"))
    print("market impact:", ai_result.get("market_impact_level"))
    print("signal change:", ai_result.get("signal_change"))
    print("official source needed:", ai_result.get("official_source_needed"))
    print("official evidence found:", ai_result.get("official_evidence_found"))
    print("official evidence summary:", ai_result.get("official_evidence_summary"))
    print("official comparison status:", ai_result.get("official_comparison_status"))
    print("official support score:", ai_result.get("official_support_score"))
    print("official verification note:", ai_result.get("official_verification_note"))

    print("\nrecommended official sources:")
    for source in ai_result.get("recommended_official_sources", []):
        if isinstance(source, dict):
            print(
                "-",
                source.get("source_name"),
                "|",
                source.get("source_type"),
                "|",
                source.get("search_url") or source.get("official_search_url"),
            )
        else:
            print("-", source)

    print("\nmemory comparison:")
    print(ai_result.get("memory_comparison"))

    print("\naffected groups:")
    for group in ai_result.get("affected_groups", []):
        print("-", group)

    print("\nwhy it matters:")
    print(ai_result.get("why_it_matters"))

    print("\nevidence sentences:")
    for sentence in ai_result.get("evidence_sentences", []):
        print("-", sentence)

    print("\nrisk factors:")
    for risk in ai_result.get("risk_factors", []):
        print("-", risk)

    print("\nfinal judgment:")
    print(ai_result.get("final_judgment"))


def analyze_pipeline(query: str = QUERY, max_news: int = MAX_NEWS_RESULTS) -> dict:
    run_started_at = utc_now_iso()
    report_items = []
    saved_event_count = 0
    duplicate_count = 0

    memory = load_policy_memory()

    move_existing_articles_to_better_topics(memory)
    save_policy_memory(memory)

    news_collection = search_google_news_rss_with_meta(query, max_results=max_news)
    news_results = news_collection.get("results", [])
    news_collection_debug = news_collection.get("debug", {})

    if not news_results:
        print("No news found in the recent window.")
        run_finished_at = utc_now_iso()
        report = {
            "run_started_at": run_started_at,
            "run_finished_at": run_finished_at,
            "query": query,
            "total_news_count": 0,
            "saved_event_count": 0,
            "duplicate_count": 0,
            "news_collection_debug": news_collection_debug,
            "topics_summary": build_topics_summary(memory),
            "news_results": [],
        }
        report_path = save_run_report(report, run_started_at)
        print("\nSaved run report:", report_path)
        report["report_path"] = str(report_path)
        return report

    for i, news in enumerate(news_results, start=1):
        print(f"\n========== News {i} ==========")
        print("title:", news["title"])
        print("published:", news["published"])
        print("Google News link:", news["google_link"])
        print("summary:", news["summary"])

        print("\n----- Resolve original URL -----")
        original_url = resolve_google_news_url(news["google_link"])
        print("original URL:", original_url)

        article_id = make_article_id(news["title"], original_url)
        existing_ids = {article.get("article_id") for article in memory.get("articles", [])}
        duplicate = article_id in existing_ids

        print("\n----- Fetch article body -----")
        article_body = fetch_article_body(original_url, max_chars=MAX_ARTICLE_CHARS)
        print(article_body[:1000])

        claims = extract_verifiable_claims(
            article_body=article_body,
            title=news.get("title") or "",
            summary=news.get("summary") or "",
            max_claims=3,
        )
        normalized_claims = normalize_claims(claims)

        policy_claims = extract_policy_claim_sentences(
            article_body,
            max_sentences=MAX_POLICY_SENTENCES,
        )

        print_rule_based_results(policy_claims)

        memory_context = summarize_all_memory(memory)
        core_policy_issue = (
            policy_claims[0]["sentence"]
            if policy_claims
            else news.get("summary") or news.get("title") or ""
        )
        preliminary_topic = classify_policy_topic(
            news_title=news["title"],
            news_summary=news["summary"],
            article_body=article_body,
            ai_result={
                "main_policy_issue": core_policy_issue,
                "one_line_summary": news["summary"],
            },
        )
        official_source_candidates = generate_official_source_candidates(
            news_title=news["title"],
            core_policy_issue=core_policy_issue,
            topic=preliminary_topic,
        )
        print_official_source_candidates(official_source_candidates)

        source_retrieval = build_source_retrieval_context(
            normalized_claims=normalized_claims,
            news=news,
            original_url=original_url,
            original_query=query,
            article_body=article_body,
            official_source_candidates=official_source_candidates,
        )
        source_queries = source_retrieval.get("source_queries", [])
        source_candidates = evaluate_source_candidates(
            source_retrieval.get("source_candidates", [])
        )
        evidence_extraction = extract_evidence_snippets(
            normalized_claims=normalized_claims,
            source_candidates=source_candidates,
            article_body=article_body,
        )
        evidence_snippets = evidence_extraction.get("evidence_snippets", [])
        claim_evidence_map = evidence_extraction.get("claim_evidence_map", {})

        official_evidence_results = fetch_official_evidence(
            official_source_candidates,
            max_candidates=3,
            news_context={
                "title": news["title"],
                "summary": news["summary"],
                "article_body": article_body,
                "topic": preliminary_topic,
                "policy_claims": policy_claims,
            },
        )
        print_official_evidence_results(official_evidence_results)

        evidence_comparison = compare_news_with_official_evidence(
            news_title=news["title"],
            news_summary=news["summary"],
            article_body=article_body,
            policy_claims=policy_claims,
            official_evidence_results=official_evidence_results,
        )
        print_evidence_comparison(evidence_comparison)

        policy_confidence = calculate_policy_confidence(
            news_title=news["title"],
            news_summary=news["summary"],
            article_body=article_body,
            policy_claims=policy_claims,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
        )
        print_policy_confidence(policy_confidence)

        policy_impact = analyze_policy_impact(
            news_title=news["title"],
            news_summary=news["summary"],
            article_body=article_body,
            policy_claims=policy_claims,
        )
        print_policy_impact(policy_impact)

        final_decision = make_final_decision(
            policy_confidence=policy_confidence,
            policy_impact=policy_impact,
        )
        print_final_decision(final_decision)

        verification_card = build_verification_card(
            news=news,
            original_url=original_url,
            policy_claims=policy_claims,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
            policy_confidence=policy_confidence,
            article_body=article_body,
            claims=claims,
            normalized_claims=normalized_claims,
            source_queries=source_queries,
            source_candidates=source_candidates,
            evidence_snippets=evidence_snippets,
            claim_evidence_map=claim_evidence_map,
        )
        print_verification_card(verification_card)

        ai_result = run_ai_reasoning(
            news_title=news["title"],
            news_summary=news["summary"],
            article_body=article_body,
            policy_claims=policy_claims,
            memory_context=memory_context,
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
        )

        print_ai_results(ai_result)

        topic = preliminary_topic
        saved_to_memory = False

        if ai_result.get("ai_available"):
            topic = classify_policy_topic(
                news_title=news["title"],
                news_summary=news["summary"],
                article_body=article_body,
                ai_result=ai_result,
            )

            print("\n----- Topic classification -----")
            print("topic:", topic)

            memory = update_memory_with_result(
                memory=memory,
                topic=topic,
                article_id=article_id,
                news=news,
                original_url=original_url,
                ai_result=ai_result,
                policy_claims=policy_claims,
            )

            save_policy_memory(memory)
            saved_to_memory = not duplicate

            if saved_to_memory:
                saved_event_count += 1

        if duplicate:
            duplicate_count += 1

        report_items.append(
            {
                "title": news.get("title"),
                "published": news.get("published"),
                "original_url": original_url,
                "summary": news.get("summary"),
                "topic": topic,
                "claims": claims,
                "normalized_claims": normalized_claims,
                "source_queries": source_queries,
                "source_candidates": source_candidates,
                "evidence_snippets": evidence_snippets,
                "claim_evidence_map": claim_evidence_map,
                "policy_claims": policy_claims,
                "official_source_candidates": official_source_candidates,
                "official_evidence_results": official_evidence_results,
                "evidence_comparison": evidence_comparison,
                "policy_confidence": policy_confidence,
                "policy_impact": policy_impact,
                "final_decision": final_decision,
                "verification_card": verification_card,
                "ai_result": ai_result,
                "saved_to_memory": saved_to_memory,
                "duplicate": duplicate,
                "api_result": {
                    "title": news.get("title"),
                    "original_url": original_url,
                    "topic": topic,
                    "claims": claims,
                    "normalized_claims": normalized_claims,
                    "source_queries": source_queries,
                    "source_candidates": source_candidates,
                    "evidence_snippets": evidence_snippets,
                    "claim_evidence_map": claim_evidence_map,
                    "policy_sentences": policy_claims,
                    "official_sources": official_source_candidates,
                    "evidence_comparison": evidence_comparison,
                    "policy_confidence": policy_confidence,
                    "policy_impact": policy_impact,
                    "final_decision": final_decision,
                    "verification_card": verification_card,
                    "claim_text": verification_card.get("claim_text"),
                    "verdict_label": verification_card.get("verdict_label"),
                    "verdict_confidence": verification_card.get("verdict_confidence"),
                    "evidence_sources": verification_card.get("evidence_sources"),
                    "source_reliability_score": verification_card.get("source_reliability_score"),
                    "source_reliability_reason": verification_card.get("source_reliability_reason"),
                    "evidence_summary": verification_card.get("evidence_summary"),
                    "missing_context": verification_card.get("missing_context"),
                    "last_checked_at": verification_card.get("last_checked_at"),
                    "review_status": verification_card.get("review_status"),
                },
            }
        )

        if not ai_result.get("ai_available"):
            print("\n----- Topic classification -----")
            print("topic:", topic)

        print("\n" + "=" * 80)

    print_timeline_summary(memory)

    run_finished_at = utc_now_iso()
    report = {
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "query": query,
        "total_news_count": len(report_items),
        "saved_event_count": saved_event_count,
        "duplicate_count": duplicate_count,
        "news_collection_debug": news_collection_debug,
        "topics_summary": build_topics_summary(memory),
        "news_results": report_items,
    }
    report_path = save_run_report(report, run_started_at)
    print("\nSaved run report:", report_path)
    report["report_path"] = str(report_path)
    return report


def main():
    analyze_pipeline()


if __name__ == "__main__":
    main()
