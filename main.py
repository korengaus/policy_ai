import json
import os
import sys
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import config
from config import (
    AI_MODEL,
    MAX_ARTICLE_CHARS,
    MAX_NEWS_RESULTS,
    MAX_POLICY_SENTENCES,
    QUERY,
    describe_ai_config,
)
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
from official_source_body import enrich_official_source_candidates_with_bodies
from official_evidence_resolution import (
    extract_primary_document_match,
    _is_strong_primary_document_match,
    resolve_official_evidence,
)
from evidence_extraction_agent import extract_evidence_snippets
from contradiction_agent import run_contradiction_checks
from bias_framing_agent import analyze_bias_framing
from semantic_evidence_agent import compute_semantic_evidence_summary
from official_crawler import fetch_official_evidence, print_official_evidence_results
from evidence_comparator import (
    compare_news_with_official_evidence,
    print_evidence_comparison,
)
from policy_confidence import calculate_policy_confidence, print_policy_confidence
from policy_impact import analyze_policy_impact, print_policy_impact
from policy_decision import (
    action_recommendation_for,
    decision_summary_for,
    make_final_decision,
    print_final_decision,
)
from policy_scoring import calibrate_final_decision
from topic_classifier import classify_policy_topic
from timeline import print_timeline_summary
from verification_card import build_verification_card, print_verification_card
from pipeline_debug import build_pipeline_debug_summary
from text_utils import sanitize_data, sanitize_text

import llm_judge

from structured_logging import get_logger

log = get_logger(__name__)


REPORTS_DIR = Path("reports")
ANALYSIS_CACHE_PATH = Path(".cache") / "analysis_result_cache.json"
ANALYSIS_CACHE_TTL_SECONDS = 30 * 60
ANALYSIS_CACHE_VERSION = "official_source_retrieval_v4_claim_specific"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_cache_text(value: str) -> str:
    return re.sub(r"\s+", " ", sanitize_text(value or "").strip().lower())


def _news_identity(news: dict, original_index: int) -> dict:
    return {
        "title": _normalize_cache_text(news.get("title") or ""),
        "source": _normalize_cache_text(news.get("source") or news.get("publisher") or ""),
        "url": news.get("original_url") or news.get("link") or news.get("google_link") or "",
        "published": news.get("published") or news.get("published_at") or "",
    }


def build_analysis_cache_key(query: str, max_news: int, news_results: list[dict]) -> str:
    identities = [_news_identity(news, index) for index, news in enumerate(news_results or [])]
    identities.sort(
        key=lambda item: (
            item["title"],
            item["source"],
            item["url"],
            item["published"],
        )
    )
    payload = {
        "version": ANALYSIS_CACHE_VERSION,
        "query": _normalize_cache_text(query),
        "max_news": int(max_news or 0),
        "news": identities,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _load_analysis_cache() -> dict:
    try:
        if ANALYSIS_CACHE_PATH.exists():
            return json.loads(ANALYSIS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as error:
        log.error(f"[AnalysisCache] read failed: {error}")
    return {}


def _save_analysis_cache(cache: dict) -> None:
    try:
        ANALYSIS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ANALYSIS_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        log.error(f"[AnalysisCache] write failed: {error}")


def _analysis_cache_fresh(entry: dict) -> bool:
    try:
        cached_at = datetime.fromisoformat(entry.get("cached_at") or "")
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        return age <= ANALYSIS_CACHE_TTL_SECONDS
    except Exception:
        return False


def _apply_analysis_cache_debug(
    report_items: list[dict],
    *,
    analysis_cache_hit: bool,
    analysis_cache_key: str,
    news_collection_debug: dict,
) -> list[dict]:
    updated_items = []
    for item in report_items or []:
        cloned = dict(item or {})
        debug = dict(cloned.get("debug_summary") or {})
        debug.update(
            {
                "analysis_cache_hit": analysis_cache_hit,
                "analysis_cache_key": analysis_cache_key,
                "analysis_cache_ttl_seconds": ANALYSIS_CACHE_TTL_SECONDS,
                "analysis_cache_version": ANALYSIS_CACHE_VERSION,
                "news_cache_hit": bool(news_collection_debug.get("news_cache_hit")),
                "news_cache_key": news_collection_debug.get("news_cache_key"),
                "news_cache_ttl_seconds": news_collection_debug.get("news_cache_ttl_seconds"),
                "news_collection_mode": news_collection_debug.get("news_collection_mode"),
                "collection_source": news_collection_debug.get("collection_source"),
            }
        )
        cloned["debug_summary"] = debug
        verification_card = dict(cloned.get("verification_card") or {})
        verification_card["debug_summary"] = debug
        cloned["verification_card"] = verification_card
        api_result = dict(cloned.get("api_result") or {})
        api_result["debug_summary"] = debug
        api_result["verification_card"] = verification_card
        api_result["news_collection_debug"] = news_collection_debug
        cloned["api_result"] = api_result
        cloned["news_collection_debug"] = news_collection_debug
        updated_items.append(cloned)
    return sanitize_data(updated_items)


def _get_cached_analysis_report(
    *,
    query: str,
    run_started_at: str,
    news_collection_debug: dict,
    topics_summary: dict,
    analysis_cache_key: str,
) -> dict | None:
    cache = _load_analysis_cache()
    entry = cache.get(analysis_cache_key)
    if not entry or not _analysis_cache_fresh(entry):
        return None

    log.info(f"[AnalysisCache] Cache hit: key={analysis_cache_key}")
    report_items = _apply_analysis_cache_debug(
        entry.get("news_results") or [],
        analysis_cache_hit=True,
        analysis_cache_key=analysis_cache_key,
        news_collection_debug=news_collection_debug,
    )
    run_finished_at = utc_now_iso()
    report = sanitize_data(
        {
            "run_started_at": run_started_at,
            "run_finished_at": run_finished_at,
            "query": query,
            "total_news_count": len(report_items),
            "saved_event_count": 0,
            "duplicate_count": 0,
            "news_collection_debug": {
                **(news_collection_debug or {}),
                "analysis_cache_hit": True,
                "analysis_cache_key": analysis_cache_key,
                "analysis_cache_ttl_seconds": ANALYSIS_CACHE_TTL_SECONDS,
                "analysis_cache_version": ANALYSIS_CACHE_VERSION,
            },
            "topics_summary": topics_summary,
            "ai_status_summary": _summarize_ai_status_from_items(report_items),
            "news_results": report_items,
        }
    )
    report_path = save_run_report(report, run_started_at)
    log.info(f'\nSaved cached run report: {report_path}')
    report["report_path"] = str(report_path)
    return report


def _store_analysis_report(
    *,
    analysis_cache_key: str,
    query: str,
    max_news: int,
    news_results: list[dict],
    report_items: list[dict],
) -> None:
    cache = _load_analysis_cache()
    cache[analysis_cache_key] = {
        "cached_at": utc_now_iso(),
        "version": ANALYSIS_CACHE_VERSION,
        "query": _normalize_cache_text(query),
        "max_news": int(max_news or 0),
        "news_identities": [_news_identity(news, index) for index, news in enumerate(news_results or [])],
        "news_results": sanitize_data(report_items),
    }
    _save_analysis_cache(cache)
    log.info(f"[AnalysisCache] Cache stored: key={analysis_cache_key} ttl={ANALYSIS_CACHE_TTL_SECONDS}s")


def build_report_path(run_started_at: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    started = datetime.fromisoformat(run_started_at)
    filename = started.strftime("policy_analysis_%Y%m%d_%H%M%S.json")
    return REPORTS_DIR / filename


def _summarize_ai_status_from_items(report_items: list[dict]) -> dict:
    config_snapshot = describe_ai_config()
    base = {
        "ai_status": "unavailable",
        "ai_status_reason": "no_results",
        "ai_model": config_snapshot.get("ai_model"),
        "ai_available": False,
        "ai_api_key_present": config_snapshot.get("ai_api_key_present", False),
    }
    if not report_items:
        if config_snapshot.get("ai_api_key_present"):
            base["ai_status_reason"] = "no_news_collected"
        else:
            base["ai_status_reason"] = "missing_api_key"
        return base

    for item in report_items:
        api_result = (item or {}).get("api_result") or {}
        ai_status = api_result.get("ai_status")
        if not ai_status:
            continue
        return {
            "ai_status": ai_status,
            "ai_status_reason": api_result.get("ai_status_reason", "unknown"),
            "ai_model": api_result.get("ai_model") or config_snapshot.get("ai_model"),
            "ai_available": bool(api_result.get("ai_available")),
            "ai_api_key_present": config_snapshot.get("ai_api_key_present", False),
        }

    return base


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
    log.info("\n----- Rule-based policy sentences -----")

    if not policy_claims:
        log.info("No important policy sentences found.")
        return

    for item in policy_claims:
        log.info(f"- {item['sentence']}")
        log.info(f"  score: {item['score']}")
        log.info(f"  authority: {item['authority_label']}")
        log.info(f"  strength: {item['strength_label']}")
        log.info(f"  execution: {item['execution_label']}")
        log.info(f"  reasons: {', '.join(item['reasons'])}")


def print_ai_results(ai_result: dict):
    log.info("\n----- AI reasoning result -----")

    if not ai_result.get("ai_available"):
        log.info("AI reasoning unavailable")
        log.info(f"reason: {ai_result.get('error')}")
        log.info(ai_result.get("fallback_message"))
        return

    log.info(f"summary: {ai_result.get('one_line_summary')}")
    log.info(f"policy signal: {ai_result.get('policy_signal_detected')}")
    log.info(f"main issue: {ai_result.get('main_policy_issue')}")
    log.info(f"execution probability: {str(ai_result.get('execution_probability')) + '%'}")
    log.info(f"execution stage: {ai_result.get('execution_stage')}")
    log.info(f"market impact: {ai_result.get('market_impact_level')}")
    log.info(f"signal change: {ai_result.get('signal_change')}")
    log.info(f"official source needed: {ai_result.get('official_source_needed')}")
    log.info(f"official evidence found: {ai_result.get('official_evidence_found')}")
    log.info(f"official evidence summary: {ai_result.get('official_evidence_summary')}")
    log.info(f"official comparison status: {ai_result.get('official_comparison_status')}")
    log.info(f"official support score: {ai_result.get('official_support_score')}")
    log.info(f"official verification note: {ai_result.get('official_verification_note')}")

    log.info("\nrecommended official sources:")
    for source in ai_result.get("recommended_official_sources", []):
        if isinstance(source, dict):
            log.info(f"- {source.get('source_name')} | {source.get('source_type')} | {source.get('search_url') or source.get('official_search_url')}")
        else:
            log.info(f'- {source}')

    log.info("\nmemory comparison:")
    log.info(ai_result.get("memory_comparison"))

    log.info("\naffected groups:")
    for group in ai_result.get("affected_groups", []):
        log.info(f'- {group}')

    log.info("\nwhy it matters:")
    log.info(ai_result.get("why_it_matters"))

    log.info("\nevidence sentences:")
    for sentence in ai_result.get("evidence_sentences", []):
        log.info(f'- {sentence}')

    log.info("\nrisk factors:")
    for risk in ai_result.get("risk_factors", []):
        log.info(f'- {risk}')

    log.info("\nfinal judgment:")
    log.info(ai_result.get("final_judgment"))


# M11.0d-3a: heuristic normalization from P3's draft-disposition label
# vocabulary to P1/P2's alert-tier vocabulary. The mapping mirrors
# Section C of docs/VERDICT_PRODUCER_DISAGREEMENT_MAP.md and the
# `_p3_implied_alert_tier` helper used by
# tests/test_verdict_producer_disagreement_diagnostic.py. This is
# an OBSERVABILITY-ONLY mapping: production verdict logic never
# consumes it; only the debug_summary disagreement_signal does.
_P3_TO_ALERT_TIER: dict[str, str] = {
    "draft_verified": "HIGH",
    "draft_likely_true": "MEDIUM",
    "draft_disputed": "WATCH",
    "draft_high_risk_review": "WATCH",
    "draft_needs_review": "WATCH",
    "draft_needs_official_confirmation": "WATCH",
    "draft_needs_context": "WATCH",
    "draft_unverified": "LOW",
}


# M13.1b — alert-tier downgrade map. Conservative one-tier drop applied
# when the LLM judge returns ``action="downgrade"`` against a
# rule-based P2 alert level. The judge can never raise the tier; LOW
# is the floor.
_ALERT_TIER_DOWNGRADE = {"HIGH": "WATCH", "WATCH": "LOW", "LOW": "LOW"}


def _apply_judge_to_final_decision(
    verdict, final_decision: dict, debug_summary: dict,
) -> bool:
    """Apply an LLM judge verdict to ``final_decision`` under strict
    invariants. Returns True iff the verdict materially changed any
    field. NEVER raises.

    Invariants enforced here (defence-in-depth, on top of
    ``llm_judge.validate_judge_response_json``):

    * ``confirm`` — no change.
    * ``flag_for_review`` — sets ``final_decision[\"llm_judge_flagged_for_review\"]``
      to True; never touches ``policy_alert_level`` or any other field.
    * ``downgrade`` — drops ``policy_alert_level`` by exactly one tier
      via ``_ALERT_TIER_DOWNGRADE``. The judge's proposed
      ``new_label`` (verdict_label rank) is validated by the schema
      validator; the application site only uses it to confirm the
      downgrade intent — verdict_label itself is NEVER modified here
      (verification_card[\"verdict_label\"] is byte-identical pre/post).
    * Never modifies ``operator_review_required`` (ALWAYS True
      elsewhere) or ``truth_claim`` (ALWAYS False elsewhere).
    * Never modifies ``action_recommendation`` / ``decision_summary``
      (prose realignment already ran).
    """
    if verdict is None:
        return False
    action = getattr(verdict, "action", None)
    if action == "confirm":
        return False
    if action == "flag_for_review":
        final_decision["llm_judge_flagged_for_review"] = True
        return True
    if action == "downgrade":
        current_tier = final_decision.get("policy_alert_level")
        new_tier = _ALERT_TIER_DOWNGRADE.get(current_tier)
        if new_tier is None or new_tier == current_tier:
            # Unknown / already-LOW tier: no-op rather than guess.
            return False
        final_decision["policy_alert_level"] = new_tier
        return True
    return False


def _apply_prejudge_to_final_decision(
    verdict,
    final_decision: dict,
    debug_summary: dict,
    *,
    primary_document_match: dict | None,
) -> tuple[bool, str | None]:
    """M22-3a — GUARDED, downgrade-only binding of the PRE-verdict judge.

    Thin guard wrapper around :func:`_apply_judge_to_final_decision`: it
    decides WHETHER the judge's verdict is allowed to bind, then delegates
    the actual mutation VERBATIM to that function so the tier-drop map
    (``_ALERT_TIER_DOWNGRADE``) and the ``flag_for_review`` path are reused
    unchanged. NEVER raises (the delegate never raises).

    Returns ``(applied, override_reason)``:

    * ``applied`` — True iff this call materially changed ``final_decision``.
    * ``override_reason`` — non-None ONLY when a ``downgrade`` was REFUSED by
      a guard, so the prejudge debug payload can record why the downgrade did
      not bind. None otherwise (including for confirm / flag_for_review).

    Guards apply to ``downgrade`` ONLY:

    (b) ``_is_strong_primary_document_match(primary_document_match)`` — a
        genuine strong Lane-B primary-document body match (stable marker +
        official_body_match + strong classification + score>=75). The
        deterministic verdict then outweighs the judge: the downgrade is
        refused (``override_reason="strong_primary_document"``). This is the
        load-bearing guard.
    (a) ``verdict.fell_back`` — the judge fell back to safe-confirm (no real
        LLM judgment). Structurally impossible to pair with a ``downgrade``
        (safe-confirm hardcodes ``action="confirm"``), enforced as
        defense-in-depth (``override_reason="judge_fallback"``).

    ``flag_for_review`` delegates straight through (sets the human-review flag
    only; NO strong-evidence guard — a flag never changes the verdict).
    ``confirm`` is a no-op. ``verdict_label`` is NEVER touched (neither here
    nor in the delegate).
    """
    if verdict is None:
        return False, None
    action = getattr(verdict, "action", None)
    if action == "downgrade":
        # Guard (b) — strong primary-document evidence overrides the judge.
        if _is_strong_primary_document_match(primary_document_match):
            return False, "strong_primary_document"
        # Guard (a) — refuse a fallback "downgrade" (defense-in-depth).
        if getattr(verdict, "fell_back", False):
            return False, "judge_fallback"
        applied = _apply_judge_to_final_decision(
            verdict, final_decision, debug_summary,
        )
        return bool(applied), None
    # flag_for_review / confirm — delegate verbatim, no strong-evidence guard.
    applied = _apply_judge_to_final_decision(
        verdict, final_decision, debug_summary,
    )
    return bool(applied), None


def _build_disagreement_signal(
    *,
    p1_alert_level_raw: str | None,
    p2_alert_level: str | None,
    p3_verdict_label: str | None,
) -> dict:
    """Build the M11.0d-3a disagreement_signal dict.

    Records the three producer labels + a heuristic P3-to-alert-tier
    normalization + an ``agreed`` boolean + a human-readable
    description. Pure function; no I/O; no exceptions raised on
    missing inputs (uses ``"unknown"`` for any None).

    The structure is consumed by debug_summary["disagreement_signal"]
    and by the ``log.info("verdict.disagreement_signal", extra=...)``
    emission in main.analyze_pipeline. It is NOT exposed at the top
    of final_decision or verification_card.
    """
    p1 = p1_alert_level_raw or "unknown"
    p2 = p2_alert_level or "unknown"
    p3 = p3_verdict_label or "unknown"
    p3_tier = _P3_TO_ALERT_TIER.get(p3, "UNKNOWN")
    agreed = (p1 == p2 == p3_tier) and p1 != "unknown" and p3_tier != "UNKNOWN"
    if agreed:
        description = f"P1=P2=P3={p1} (all agree)"
    else:
        parts = [f"P1={p1}", f"P2={p2}", f"P3={p3}({p3_tier})"]
        disagreeing = []
        if p1 != p2:
            disagreeing.append("P1≠P2")
        if p1 != p3_tier:
            disagreeing.append("P1≠P3")
        if p2 != p3_tier:
            disagreeing.append("P2≠P3")
        description = " ".join(parts) + " — " + ", ".join(disagreeing)
    return {
        "p1_label": p1,
        "p2_label": p2,
        "p3_label": p3,
        "p3_implied_tier": p3_tier,
        "agreed": agreed,
        "disagreement_description": description,
    }


# =========================================================================
# M15.0d — Per-news-item parallel processing helpers.
#
# The per-news-item loop body splits cleanly into two phases at the LLM
# call boundary:
#
#   Phase A — verdict computation (URL resolve → article fetch →
#             official source → evidence extraction → contradiction →
#             bias framing → policy confidence/impact → P1 → P3 → P4
#             rewrite → P2 calibration → disagreement_signal). Pure
#             function of (news, memory_snapshot, query, ...). Safe
#             to parallelize across news items.
#
#   Phase B — LLM call (ai_reasoner.run_ai_reasoning), AI-driven topic
#             classification, duplicate detection (against latest
#             memory), memory mutation, and report assembly. Must run
#             SEQUENTIALLY in submission order to (a) respect OpenAI
#             rate limits, (b) preserve memory mutation determinism,
#             and (c) keep `report_items` in input order.
#
# The split preserves every M11.0d invariant by construction: verdict
# logic (P1/P2/P3/disagreement_signal) runs identically inside Phase A.
# M11.0d-1 snapshot pin (42 synthetic rows + 3 named fixtures) is the
# strongest safety signal — it must pass byte-identical after this
# refactor.
#
# Concurrency limit: MAX_PARALLEL_NEWS_ITEMS env var, default 3.
# Setting to 1 produces byte-identical behaviour to pre-M15.0d
# sequential execution (safe rollback path).
# =========================================================================


def _max_parallel_news_items() -> int:
    """Return the per-pipeline parallel-worker limit. Defaults to 3
    when the env var is unset / invalid. Caller must clamp to
    ``len(news_results)`` so we never spawn more workers than items."""
    raw = os.environ.get("MAX_PARALLEL_NEWS_ITEMS", "").strip()
    if not raw:
        return 3
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return max(1, value)


def _process_news_item_phase_a(
    news: dict,
    *,
    index: int,
    total: int,
    memory_snapshot: dict,
    query: str,
    news_collection_debug: dict,
    analysis_cache_key: str,
) -> dict:
    """Run the verdict-computation half of the per-news-item pipeline.

    Pure function — reads ``memory_snapshot`` but never mutates it
    (or anything else outside its own return value). Safe to call
    concurrently from multiple threads.

    Returns a dict carrying every per-item value Phase B + the
    final report assembly need.
    """
    log.info(f"\n========== News {index} ==========")
    log.info(f"title: {news['title']}")
    log.info(f"published: {news['published']}")
    log.info(f"Google News link: {news['google_link']}")
    log.info(f"summary: {news['summary']}")

    log.info("\n----- Resolve original URL -----")
    original_url = resolve_google_news_url(news["google_link"])
    log.info(f'original URL: {original_url}')

    article_id = make_article_id(news["title"], original_url)

    log.info("\n----- Fetch article body -----")
    article_body = sanitize_text(fetch_article_body(original_url, max_chars=MAX_ARTICLE_CHARS))
    log.info(article_body[:1000])

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

    memory_context = summarize_all_memory(memory_snapshot)
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

    official_evidence_results = fetch_official_evidence(
        official_source_candidates,
        max_candidates=5,
        news_context={
            "title": news["title"],
            "summary": news["summary"],
            "article_body": article_body,
            "topic": preliminary_topic,
            "policy_claims": policy_claims,
        },
    )
    print_official_evidence_results(official_evidence_results)

    source_retrieval = build_source_retrieval_context(
        normalized_claims=normalized_claims,
        news=news,
        original_url=original_url,
        original_query=query,
        article_body=article_body,
        official_source_candidates=official_source_candidates,
    )
    source_queries = source_retrieval.get("source_queries", [])
    source_candidates, official_body_debug = enrich_official_source_candidates_with_bodies(
        source_retrieval.get("source_candidates", []),
        official_evidence_results,
        normalized_claims,
    )
    # M21 Phase 2b: inject Policy Briefing press releases as primary-document
    # official candidates (Option A). Gated by POLICY_BRIEFING_ENABLED (default
    # false): when off, no provider is constructed, zero network happens, and
    # source_candidates / debug_summary stay byte-identical to pre-M21. The
    # releases carry their body already (raw_text_available=True), so they skip
    # the crawl-based enrich above and flow straight into resolve_official_evidence,
    # which computes official_body_match (the M19-3 guard is the only path to the
    # reliability uplift — we never set official_body_match here). All provider
    # logging lives in providers/policy_briefing.py (pin-OUT); the only
    # observability in this pinned file is the in-memory counter below.
    policy_briefing_count = None
    if config.policy_briefing_enabled():
        from providers.policy_briefing import fetch_and_build_policy_briefing_candidates

        policy_briefing_candidates, policy_briefing_count = (
            fetch_and_build_policy_briefing_candidates(normalized_claims)
        )
        if policy_briefing_candidates:
            source_candidates = source_candidates + policy_briefing_candidates
    # M23: second primary-document source — National Law (법제처). Same Option-A
    # injection as Policy Briefing, gated by NATIONAL_LAW_ENABLED (default
    # false): when off, no provider is constructed, zero network happens, and
    # source_candidates / debug_summary stay byte-identical. Law candidates carry
    # their body (raw_text_available=True) and the stable marker national_law_mst,
    # flowing through the SAME resolve→evaluate→extract→Lane-B path as PB. All
    # provider logging lives in providers/national_law.py (pin-OUT); the only
    # observability here is the in-memory counter below (no log.* in this pinned
    # file).
    national_law_count = None
    if config.national_law_enabled():
        from providers.national_law import fetch_and_build_national_law_candidates

        national_law_candidates, national_law_count = (
            fetch_and_build_national_law_candidates(normalized_claims)
        )
        if national_law_candidates:
            source_candidates = source_candidates + national_law_candidates
    source_candidates, official_resolution_debug = resolve_official_evidence(
        source_candidates,
        normalized_claims,
    )
    source_candidates = evaluate_source_candidates(source_candidates)
    # M22-1 — Lane A↔B join: extract a GENUINE strong Policy-Briefing (Lane B)
    # official body match from source_candidates so the Lane-A-only verdict
    # producers below (compare_news_with_official_evidence, calculate_policy_confidence)
    # can raise the verdict deterministically and conservatively. Returns None
    # when no such match exists (incl. POLICY_BRIEFING_ENABLED=false, since no
    # policy_briefing_api candidate is ever injected) → both producers behave
    # byte-identically to pre-M22-1. Reads resolve-computed fields only (M19-3).
    primary_document_match = extract_primary_document_match(source_candidates)
    evidence_extraction = extract_evidence_snippets(
        normalized_claims=normalized_claims,
        source_candidates=source_candidates,
        article_body=article_body,
    )
    evidence_snippets = evidence_extraction.get("evidence_snippets", [])
    claim_evidence_map = evidence_extraction.get("claim_evidence_map", {})
    contradiction_result = run_contradiction_checks(
        normalized_claims=normalized_claims,
        evidence_snippets=evidence_snippets,
        claim_evidence_map=claim_evidence_map,
        source_queries=source_queries,
    )
    contradiction_checks = contradiction_result.get("contradiction_checks", [])
    contradiction_summary = contradiction_result.get("contradiction_summary", {})
    bias_framing_result = analyze_bias_framing(
        normalized_claims=normalized_claims,
        news_title=news.get("title") or "",
        news_summary=news.get("summary") or "",
        article_body=article_body,
        source_candidates=source_candidates,
        evidence_snippets=evidence_snippets,
        claim_evidence_map=claim_evidence_map,
        contradiction_checks=contradiction_checks,
    )
    bias_framing_analysis = bias_framing_result.get("bias_framing_analysis", [])
    bias_framing_summary = bias_framing_result.get("bias_framing_summary", {})

    # Phase 2 M5: optional semantic evidence matching. Strictly additive —
    # the summary is computed read-only from existing inputs and attached
    # to debug_summary below. It never feeds policy_confidence, verdict
    # labels, or final_decision. When SEMANTIC_MATCHING_ENABLED is false
    # (default) this short-circuits to an "unavailable" summary with no
    # external calls.
    semantic_evidence_summary = compute_semantic_evidence_summary(
        normalized_claims=normalized_claims,
        claim_text=(news.get("title") or "") + " " + (news.get("summary") or ""),
        source_candidates=source_candidates,
        evidence_snippets=evidence_snippets,
    )

    evidence_comparison = compare_news_with_official_evidence(
        news_title=news["title"],
        news_summary=news["summary"],
        article_body=article_body,
        policy_claims=policy_claims,
        official_evidence_results=official_evidence_results,
        primary_document_match=primary_document_match,
    )
    print_evidence_comparison(evidence_comparison)

    policy_confidence = calculate_policy_confidence(
        news_title=news["title"],
        news_summary=news["summary"],
        article_body=article_body,
        policy_claims=policy_claims,
        official_evidence_results=official_evidence_results,
        evidence_comparison=evidence_comparison,
        primary_document_match=primary_document_match,
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
    # M11.0d-3a: capture P1's pure output BEFORE the mid-pipeline
    # official_mismatch rewrite or P2's calibrator overwrite. Used
    # downstream to populate debug_summary["disagreement_signal"].
    p1_alert_level_raw = final_decision.get("policy_alert_level")

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
        contradiction_checks=contradiction_checks,
        contradiction_summary=contradiction_summary,
        bias_framing_analysis=bias_framing_analysis,
        bias_framing_summary=bias_framing_summary,
    )
    debug_summary = build_pipeline_debug_summary(
        news=news,
        original_url=original_url,
        claims=claims,
        normalized_claims=normalized_claims,
        source_candidates=source_candidates,
        official_source_candidates=official_source_candidates,
        evidence_snippets=evidence_snippets,
        contradiction_checks=contradiction_checks,
        bias_framing_analysis=bias_framing_analysis,
        verification_card=verification_card,
    )
    debug_summary["news_cache_hit"] = bool(news_collection_debug.get("news_cache_hit"))
    debug_summary["news_cache_key"] = news_collection_debug.get("news_cache_key")
    debug_summary["news_cache_ttl_seconds"] = news_collection_debug.get("news_cache_ttl_seconds")
    debug_summary["news_collection_mode"] = news_collection_debug.get("news_collection_mode")
    debug_summary["collection_source"] = news_collection_debug.get("collection_source")
    debug_summary["analysis_cache_hit"] = False
    debug_summary["analysis_cache_key"] = analysis_cache_key
    debug_summary["analysis_cache_ttl_seconds"] = ANALYSIS_CACHE_TTL_SECONDS
    debug_summary.update(official_body_debug or {})
    debug_summary.update(official_resolution_debug or {})
    # M21 Phase 2b: in-branch-only debug key. policy_briefing_count is None on
    # the disabled path, so this key is NOT added there — the disabled
    # debug_summary stays byte-identical to pre-M21 (mirrors naver_api_count).
    if policy_briefing_count is not None:
        debug_summary["policy_briefing_count"] = policy_briefing_count
    # M23: in-branch-only debug key. national_law_count is None on the disabled
    # path, so this key is NOT added there — disabled debug_summary stays
    # byte-identical (mirrors policy_briefing_count).
    if national_law_count is not None:
        debug_summary["national_law_count"] = national_law_count
    # FRESHNESS Phase 2: surface the article publish date + collection source so
    # the frontend can derive a CONSERVATIVE "freshly-broken" label (distinct
    # from "old-unmatched"). Additive, byte-identical convention (mirrors
    # policy_briefing_count): the two keys are added ONLY when a REAL parseable
    # date exists AND the source is trusted ({google_rss, naver_api}). HTML
    # fallback sources (naver_fallback/daum_fallback) synthesize published=NOW,
    # so they are excluded here at the source — a synthetic date can never reach
    # the client. When excluded, NO key is added, so existing date-less/fallback
    # rows stay byte-identical. Pure data key (no log call site); touches no
    # verdict field.
    _article_published = news.get("published_at") or news.get("published")
    if _article_published and news.get("source") in {"google_rss", "naver_api"}:
        debug_summary["article_published_at"] = _article_published
        debug_summary["article_source"] = news.get("source")
    debug_summary["semantic_evidence_summary"] = semantic_evidence_summary

    if verification_card.get("official_mismatch"):
        policy_confidence = dict(policy_confidence)
        policy_confidence["policy_confidence_score"] = min(
            int(policy_confidence.get("policy_confidence_score") or 0),
            20,
        )
        policy_confidence["verification_strength"] = "none"
        policy_confidence["confidence_evidence_source"] = None
        policy_confidence["confidence_evidence_title"] = None
        policy_confidence["confidence_evidence_url"] = None
        policy_confidence["confidence_evidence_grade"] = None
        mismatch_reasons = verification_card.get("official_mismatch_reasons") or []
        policy_confidence["confidence_reasons"] = [
            "no usable official document",
            "official source topic mismatch",
            *mismatch_reasons[:2],
        ]

        final_decision = dict(final_decision)
        final_decision["policy_alert_level"] = (
            "WATCH"
            if policy_impact.get("impact_level") == "high"
            else final_decision.get("policy_alert_level", "WATCH")
        )
        final_decision["action_recommendation"] = "추가 공식 출처 확인 필요"
        final_decision["decision_summary"] = (
            "공식 상세 근거가 부족하거나 뉴스 핵심 주제와 불일치하여 추가 공식 출처 확인이 필요합니다."
        )
        decision_reasons = list(final_decision.get("decision_reasons") or [])
        for reason in ["no usable official evidence", "official source topic mismatch"]:
            if reason not in decision_reasons:
                decision_reasons.append(reason)
        final_decision["decision_reasons"] = decision_reasons
        verification_card["verdict_confidence"] = policy_confidence["policy_confidence_score"]

    # M11.0d-3b (NARROW Strategy A): codification point. P2 is the
    # authoritative producer of policy_alert_level. P1's value
    # captured above as p1_alert_level_raw survives via the
    # disagreement_signal.
    final_decision, debug_summary = calibrate_final_decision(
        final_decision=final_decision,
        policy_confidence=policy_confidence,
        policy_impact=policy_impact,
        verification_card=verification_card,
        source_candidates=source_candidates,
        evidence_snippets=evidence_snippets,
        debug_summary=debug_summary,
    )

    # M11.0d-3b-2 (Strategy A FULL): realign Korean prose to P2's
    # authoritative policy_alert_level. P1 emits prose branched on
    # its own label, so when P1≠P2 (~30% of analyses) the user
    # sees prose describing one tier next to an alert card for
    # another. We re-derive decision_summary + action_recommendation
    # from P2's label using the public prose helpers exposed by
    # policy_decision. Gated on `not official_mismatch` so the
    # conservative override applied at L735-749 (which hard-sets
    # both prose fields to the "추가 공식 출처 확인 필요" pair)
    # survives untouched. market_signal and decision_reasons are
    # label-independent and stay as P1+P2 left them.
    if not verification_card.get("official_mismatch"):
        aligned_alert_level = final_decision.get("policy_alert_level")
        aligned_market_signals = final_decision.get("market_signal") or []
        final_decision["action_recommendation"] = action_recommendation_for(
            aligned_alert_level,
            aligned_market_signals,
            policy_confidence,
            policy_impact,
        )
        final_decision["decision_summary"] = decision_summary_for(
            aligned_alert_level,
            aligned_market_signals,
            policy_confidence,
            policy_impact,
        )

    print_final_decision(final_decision)

    # M22-3a — snapshot hoist. Capture the deterministic pre-judge P2 alert
    # level HERE, BEFORE any judge (record-only or binding) can mutate it.
    # Formerly captured just above the post-verdict block; hoisted up so the
    # M22-3a guarded prejudge-binding path (below) downgrades policy_alert_level
    # only AFTER this snapshot is taken. disagreement_signal reads this snapshot
    # (not the live final_decision), so it stays a pure function of the
    # deterministic P1/P2/P3 producers and remains byte-identical. The value is
    # identical to the former capture point: nothing between here and the
    # post-verdict block mutates policy_alert_level on the flag-off path.
    p2_alert_pre_judge = final_decision.get("policy_alert_level")

    # M22-3a — is the guarded downgrade-binding prejudge path active? Requires
    # BOTH the M22-2 record-only flag AND the new M22-3a binding flag. Computed
    # once so the prejudge block (which APPLIES the guarded downgrade) and the
    # post-verdict block (which is SKIPPED for mutual exclusion) read a single
    # consistent value. Default off → False → behavior unchanged.
    prejudge_binding_active = (
        llm_judge.llm_judge_prejudge_enabled()
        and llm_judge.llm_judge_prejudge_binding_enabled()
    )

    # M22-2 / M22-3a — PRE-verdict LLM judge. A SEPARATE, INDEPENDENT
    # invocation from the post-verdict binding block below. It runs HERE,
    # after the verdict is fully locked (P2 calibrate_final_decision above
    # is authoritative for policy_alert_level; verdict_label was set by
    # build_verification_card earlier), after the prose realignment, and
    # AFTER the p2_alert_pre_judge snapshot (hoisted above) — so even when it
    # binds it cannot perturb disagreement_signal. It writes
    # debug_summary["llm_judge_prejudge"].
    #
    # M22-2 (record-only): gated by LLM_JUDGE_PREJUDGE_ENABLED alone — builds
    # the verdict, records the payload with applied=False, mutates NOTHING.
    # M22-3a (guarded binding): when prejudge_binding_active (BOTH prejudge
    # flags on, default off), it additionally calls
    # _apply_prejudge_to_final_decision, which may downgrade policy_alert_level
    # by ONE tier (never raise, never touch verdict_label) subject to the two
    # guards. With the binding flag off this is byte-identical to M22-2.
    #
    # NO log.* is emitted here: main.py is a pin-IN file for the M14.4
    # log-count test, so this path must not add a log call. The judge's own
    # logging lives in llm_judge.py (pin-OUT); M22-3 observability lives ONLY
    # as structured fields (applied / override_reason) in the payload below.
    # On any failure the payload degrades to None (no log), exactly mirroring
    # the post-verdict block's None default.
    prejudge_debug_payload = None
    if llm_judge.llm_judge_prejudge_enabled():
        try:
            prejudge_input = llm_judge.JudgeInput(
                current_label=verification_card.get("verdict_label") or "",
                policy_confidence_score=policy_confidence.get(
                    "policy_confidence_score"
                ),
                verification_strength=policy_confidence.get(
                    "verification_strength"
                ),
                claim_text=(news.get("title") or "")[:1000],
                official_sources_count=len(source_candidates or []),
                evidence_summary=verification_card.get("evidence_summary"),
                contradiction_summary=contradiction_summary,
                bias_framing_summary=bias_framing_summary,
            )
            prejudge_verdict = llm_judge.run_judge(
                prejudge_input, model=AI_MODEL,
            )
            prejudge_debug_payload = llm_judge.judge_verdict_to_dict(
                prejudge_verdict
            )
            if prejudge_binding_active:
                # M22-3a — guarded downgrade-only binding. Runs AFTER the
                # p2_alert_pre_judge snapshot (hoisted above), so a downgrade
                # here never changes disagreement_signal. The wrapper only
                # ever lowers policy_alert_level by one tier via the delegated
                # _apply_judge_to_final_decision, or sets the human-review
                # flag; it never touches verdict_label.
                applied, override_reason = _apply_prejudge_to_final_decision(
                    prejudge_verdict,
                    final_decision,
                    debug_summary,
                    primary_document_match=primary_document_match,
                )
                prejudge_debug_payload["applied"] = bool(applied)
                if override_reason is not None:
                    prejudge_debug_payload["override_reason"] = override_reason
            else:
                # Record-only (M22-2): NEVER applied. Pinned False so the
                # payload is unambiguous about zero verdict influence.
                prejudge_debug_payload["applied"] = False
        except Exception:  # noqa: BLE001 — prejudge judge must never break pipeline
            prejudge_debug_payload = None
    debug_summary["llm_judge_prejudge"] = prejudge_debug_payload

    # M13.1b — LLM judge invocation. The judge can confirm, downgrade
    # the policy_alert_level by one tier, or flag for human review.
    # It CANNOT raise the alert level, modify verdict_label, change
    # operator_review_required (ALWAYS True), or change truth_claim
    # (ALWAYS False). The invariants are enforced both by
    # ``llm_judge.validate_judge_response_json`` (schema layer) and by
    # ``_apply_judge_to_final_decision`` (application layer).
    #
    # disagreement_signal MUST see the PRE-judge p2 alert so the
    # P1/P2/P3 signal stays a function of the deterministic producers
    # only. Otherwise judge downgrades would silently rewrite the
    # signal and break the 6 M11.0d-1 snapshot fixtures. The snapshot
    # (p2_alert_pre_judge) is captured above, hoisted in M22-3a to precede
    # the prejudge binding block.
    #
    # M22-3a mutual exclusion: when the guarded prejudge-binding path is
    # active it ALSO downgrades policy_alert_level. To avoid a double
    # downgrade, this post-verdict block is SKIPPED while prejudge-binding is
    # active (`and not prejudge_binding_active`). When prejudge-binding is OFF
    # (default), this condition reduces to `llm_judge.llm_judge_enabled()` —
    # byte-identical to HEAD.
    judge_debug_payload = None
    if llm_judge.llm_judge_enabled() and not prejudge_binding_active:
        try:
            judge_input = llm_judge.JudgeInput(
                current_label=verification_card.get("verdict_label") or "",
                policy_confidence_score=policy_confidence.get(
                    "policy_confidence_score"
                ),
                verification_strength=policy_confidence.get(
                    "verification_strength"
                ),
                claim_text=(news.get("title") or "")[:1000],
                official_sources_count=len(source_candidates or []),
                evidence_summary=verification_card.get("evidence_summary"),
                contradiction_summary=contradiction_summary,
                bias_framing_summary=bias_framing_summary,
            )
            judge_verdict = llm_judge.run_judge(judge_input, model=AI_MODEL)
            applied = _apply_judge_to_final_decision(
                judge_verdict, final_decision, debug_summary,
            )
            judge_debug_payload = llm_judge.judge_verdict_to_dict(judge_verdict)
            judge_debug_payload["applied"] = bool(applied)
        except Exception as exc:  # noqa: BLE001 — judge must never break pipeline
            log.warning(
                "llm_judge.failed",
                # M13.1c-hotfix-1: capture the truncated exception
                # message alongside the type so operators can pinpoint
                # KeyError keys / SDK error texts without enabling
                # debug logging. 200-char cap mirrors the conservative
                # truncation used by pipeline_worker.save_failed.
                extra={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:200],
                },
            )
    debug_summary["llm_judge"] = judge_debug_payload

    # M11.0d-3a (Strategy C): record the three producer labels.
    # p2_alert_pre_judge (captured above) preserves the deterministic
    # P2 value even if M13.1b downgraded the live final_decision.
    disagreement_signal = _build_disagreement_signal(
        p1_alert_level_raw=p1_alert_level_raw,
        p2_alert_level=p2_alert_pre_judge,
        p3_verdict_label=verification_card.get("verdict_label"),
    )
    debug_summary["disagreement_signal"] = disagreement_signal
    log.info(
        "verdict.disagreement_signal",
        extra={
            "p1_label": disagreement_signal["p1_label"],
            "p2_label": disagreement_signal["p2_label"],
            "p3_label": disagreement_signal["p3_label"],
            "p3_implied_tier": disagreement_signal["p3_implied_tier"],
            "agreed": disagreement_signal["agreed"],
            "disagreement_description": disagreement_signal["disagreement_description"],
        },
    )
    verification_card["debug_summary"] = debug_summary
    verification_card = sanitize_data(verification_card)
    evidence_quality_summary = verification_card.get("evidence_quality_summary") or {}
    claim_evidence_quality_summary = (
        verification_card.get("claim_evidence_quality_summary") or []
    )
    print_verification_card(verification_card)

    return {
        "index": index,
        "total": total,
        "news": news,
        "original_url": original_url,
        "article_id": article_id,
        "article_body": article_body,
        "claims": claims,
        "normalized_claims": normalized_claims,
        "policy_claims": policy_claims,
        "memory_context": memory_context,
        "preliminary_topic": preliminary_topic,
        "official_source_candidates": official_source_candidates,
        "official_evidence_results": official_evidence_results,
        "source_queries": source_queries,
        "source_candidates": source_candidates,
        "evidence_snippets": evidence_snippets,
        "claim_evidence_map": claim_evidence_map,
        "contradiction_checks": contradiction_checks,
        "contradiction_summary": contradiction_summary,
        "bias_framing_analysis": bias_framing_analysis,
        "bias_framing_summary": bias_framing_summary,
        "evidence_comparison": evidence_comparison,
        "policy_confidence": policy_confidence,
        "policy_impact": policy_impact,
        "final_decision": final_decision,
        "verification_card": verification_card,
        "debug_summary": debug_summary,
        "evidence_quality_summary": evidence_quality_summary,
        "claim_evidence_quality_summary": claim_evidence_quality_summary,
        "news_collection_debug": news_collection_debug,
    }


def _run_ai_reasoning_for_phase_a(phase_a: dict) -> dict:
    """M26.3: the Phase-B ai_reasoner network call as a pure function of
    ``phase_a`` (every argument is frozen at Phase A time — including
    ``memory_context``, the Phase-A memory snapshot — so this is independent
    of any Phase-B memory mutation and of other items). Used BOTH inline
    (concurrency gate off) and in the concurrent fan-out (gate on), so the
    call is identical in both paths. ``run_ai_reasoning`` never raises.
    """
    news = phase_a["news"]
    return run_ai_reasoning(
        news_title=news["title"],
        news_summary=news["summary"],
        article_body=phase_a["article_body"],
        policy_claims=phase_a["policy_claims"],
        memory_context=phase_a["memory_context"],
        official_source_candidates=phase_a["official_source_candidates"],
        official_evidence_results=phase_a["official_evidence_results"],
        evidence_comparison=phase_a["evidence_comparison"],
    )


def _apply_news_item_phase_b(
    phase_a: dict, memory: dict, ai_result: dict | None = None,
) -> dict:
    """Sequential half of the per-news-item pipeline: LLM call,
    AI-driven topic, duplicate detection (against the LATEST
    memory), memory mutation, and report-item assembly. Mutates
    ``memory`` in-place — caller must ensure this runs serially in
    submission order.

    M26.3: ``ai_result`` may be a precomputed reasoning result from the
    concurrent fan-out. When None (gate off / default), ``run_ai_reasoning``
    is called inline exactly as pre-M26.3 — byte-identical. Either way the
    result is the same pure function of ``phase_a``; everything order-dependent
    below (dedup, memory mutation/save, counters) stays serial in submission
    order.

    Returns a dict with:
      * ``report_item``  — the per-news dict appended to report_items
      * ``saved_to_memory`` — bool, whether this item added to memory
      * ``duplicate``    — bool, whether this item was a known dup
    """
    news = phase_a["news"]
    original_url = phase_a["original_url"]
    article_id = phase_a["article_id"]
    preliminary_topic = phase_a["preliminary_topic"]

    # Re-compute duplicate against the LATEST memory state (which
    # includes any items added by earlier Phase B iterations in this
    # same run). Preserves exact byte-identical behaviour to the
    # pre-M15.0d sequential loop.
    existing_ids = {article.get("article_id") for article in memory.get("articles", [])}
    duplicate = article_id in existing_ids

    # M26.3: use the precomputed fan-out result when supplied; otherwise call
    # inline exactly as pre-M26.3 (gate-off path, byte-identical). The call is
    # the same pure function of phase_a in both paths.
    if ai_result is None:
        ai_result = _run_ai_reasoning_for_phase_a(phase_a)
    print_ai_results(ai_result)

    topic = preliminary_topic
    saved_to_memory = False

    if ai_result.get("ai_available"):
        topic = classify_policy_topic(
            news_title=news["title"],
            news_summary=news["summary"],
            article_body=phase_a["article_body"],
            ai_result=ai_result,
        )

        log.info("\n----- Topic classification -----")
        log.info(f'topic: {topic}')

        update_memory_with_result(
            memory=memory,
            topic=topic,
            article_id=article_id,
            news=news,
            original_url=original_url,
            ai_result=ai_result,
            policy_claims=phase_a["policy_claims"],
        )

        save_policy_memory(memory)
        saved_to_memory = not duplicate

    if not ai_result.get("ai_available"):
        log.info("\n----- Topic classification -----")
        log.info(f'topic: {topic}')

    log.info("\n" + "=" * 80)

    report_item = sanitize_data({
        "title": news.get("title"),
        "published": news.get("published"),
        "original_url": original_url,
        "summary": news.get("summary"),
        "topic": topic,
        "claims": phase_a["claims"],
        "normalized_claims": phase_a["normalized_claims"],
        "source_queries": phase_a["source_queries"],
        "source_candidates": phase_a["source_candidates"],
        "evidence_snippets": phase_a["evidence_snippets"],
        "claim_evidence_map": phase_a["claim_evidence_map"],
        "claim_evidence_quality_summary": phase_a["claim_evidence_quality_summary"],
        "evidence_quality_summary": phase_a["evidence_quality_summary"],
        "contradiction_checks": phase_a["contradiction_checks"],
        "contradiction_summary": phase_a["contradiction_summary"],
        "bias_framing_analysis": phase_a["bias_framing_analysis"],
        "bias_framing_summary": phase_a["bias_framing_summary"],
        "policy_claims": phase_a["policy_claims"],
        "official_source_candidates": phase_a["official_source_candidates"],
        "official_evidence_results": phase_a["official_evidence_results"],
        "evidence_comparison": phase_a["evidence_comparison"],
        "policy_confidence": phase_a["policy_confidence"],
        "policy_impact": phase_a["policy_impact"],
        "final_decision": phase_a["final_decision"],
        "verification_card": phase_a["verification_card"],
        "debug_summary": phase_a["debug_summary"],
        "news_collection_debug": phase_a["news_collection_debug"],
        "ai_result": ai_result,
        "saved_to_memory": saved_to_memory,
        "duplicate": duplicate,
        "api_result": {
            "title": news.get("title"),
            "original_url": original_url,
            "topic": topic,
            "claims": phase_a["claims"],
            "normalized_claims": phase_a["normalized_claims"],
            "source_queries": phase_a["source_queries"],
            "source_candidates": phase_a["source_candidates"],
            "evidence_snippets": phase_a["evidence_snippets"],
            "claim_evidence_map": phase_a["claim_evidence_map"],
            "claim_evidence_quality_summary": phase_a["claim_evidence_quality_summary"],
            "evidence_quality_summary": phase_a["evidence_quality_summary"],
            "contradiction_checks": phase_a["contradiction_checks"],
            "contradiction_summary": phase_a["contradiction_summary"],
            "bias_framing_analysis": phase_a["bias_framing_analysis"],
            "bias_framing_summary": phase_a["bias_framing_summary"],
            "policy_sentences": phase_a["policy_claims"],
            "official_sources": phase_a["official_source_candidates"],
            "evidence_comparison": phase_a["evidence_comparison"],
            "policy_confidence": phase_a["policy_confidence"],
            "policy_impact": phase_a["policy_impact"],
            "final_decision": phase_a["final_decision"],
            "verification_card": phase_a["verification_card"],
            "debug_summary": phase_a["debug_summary"],
            "news_collection_debug": phase_a["news_collection_debug"],
            "claim_text": phase_a["verification_card"].get("claim_text"),
            "verdict_label": phase_a["verification_card"].get("verdict_label"),
            "verdict_confidence": phase_a["verification_card"].get("verdict_confidence"),
            "evidence_sources": phase_a["verification_card"].get("evidence_sources"),
            "source_reliability_score": phase_a["verification_card"].get("source_reliability_score"),
            "source_reliability_reason": phase_a["verification_card"].get("source_reliability_reason"),
            "evidence_summary": phase_a["verification_card"].get("evidence_summary"),
            "missing_context": phase_a["verification_card"].get("missing_context"),
            "last_checked_at": phase_a["verification_card"].get("last_checked_at"),
            "review_status": phase_a["verification_card"].get("review_status"),
            "ai_status": ai_result.get("ai_status", "unavailable"),
            "ai_status_reason": ai_result.get("ai_status_reason", "unknown"),
            "ai_model": ai_result.get("ai_model"),
            "ai_available": bool(ai_result.get("ai_available")),
        },
    })

    return {
        "report_item": report_item,
        "saved_to_memory": saved_to_memory,
        "duplicate": duplicate,
    }


def analyze_pipeline(
    query: str = QUERY,
    max_news: int = MAX_NEWS_RESULTS,
    *,
    progress_callback: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    run_started_at = utc_now_iso()
    report_items = []
    saved_event_count = 0
    duplicate_count = 0

    memory = load_policy_memory()

    move_existing_articles_to_better_topics(memory)
    save_policy_memory(memory)

    news_collection = search_google_news_rss_with_meta(query, max_results=max_news)
    news_results = sanitize_data(news_collection.get("results", []))
    news_collection_debug = sanitize_data(news_collection.get("debug", {}))
    analysis_cache_key = build_analysis_cache_key(query, max_news, news_results)
    cached_report = _get_cached_analysis_report(
        query=query,
        run_started_at=run_started_at,
        news_collection_debug=news_collection_debug,
        topics_summary=build_topics_summary(memory),
        analysis_cache_key=analysis_cache_key,
    )
    if cached_report is not None:
        return cached_report

    if not news_results:
        log.info("No news found in the recent window.")
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
            "ai_status_summary": _summarize_ai_status_from_items([]),
            "news_results": [],
        }
        report_path = save_run_report(report, run_started_at)
        log.info(f'\nSaved run report: {report_path}')
        report["report_path"] = str(report_path)
        return report

    # M15.0d — Phase A (parallel) + Phase B (sequential) split.
    # See _process_news_item_phase_a / _apply_news_item_phase_b above
    # and Section A-I of the M15.0d Phase 1 diagnosis for the safety
    # rationale. With MAX_PARALLEL_NEWS_ITEMS=1 (env override), the
    # path is byte-identical to the pre-M15.0d sequential loop.
    total_items = len(news_results)
    max_parallel = min(_max_parallel_news_items(), total_items)
    log.info(
        "M15.0d parallel phase start: total=%d workers=%d",
        total_items, max_parallel,
    )
    if progress_callback is not None:
        try:
            progress_callback("news_item_parallel_started", {
                "total": total_items, "workers": max_parallel,
            })
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    phase_a_results: list = [None] * total_items
    if max_parallel <= 1 or total_items <= 1:
        # Sequential path — byte-identical to pre-M15.0d.
        for i, news in enumerate(news_results):
            try:
                phase_a_results[i] = _process_news_item_phase_a(
                    news,
                    index=i + 1,
                    total=total_items,
                    memory_snapshot=memory,
                    query=query,
                    news_collection_debug=news_collection_debug,
                    analysis_cache_key=analysis_cache_key,
                )
            except Exception:
                log.exception(
                    "M15.0d Phase A failed for news index %d (sequential)", i + 1,
                )
            if progress_callback is not None:
                try:
                    progress_callback("news_item_completed", {
                        "index": i + 1, "total": total_items,
                    })
                except Exception:  # noqa: BLE001
                    pass
    else:
        # Parallel path — ThreadPoolExecutor over Phase A. I/O-bound
        # work (HTTP fetches) releases the GIL; verdict computation
        # is per-item pure-function work that does not touch shared
        # mutable state (memory is read-only here; mutations happen
        # in Phase B below).
        with ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="m15-0d-phase-a",
        ) as executor:
            future_to_index = {
                executor.submit(
                    _process_news_item_phase_a,
                    news,
                    index=i + 1,
                    total=total_items,
                    memory_snapshot=memory,
                    query=query,
                    news_collection_debug=news_collection_debug,
                    analysis_cache_key=analysis_cache_key,
                ): i
                for i, news in enumerate(news_results)
            }
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                try:
                    phase_a_results[idx] = future.result()
                except Exception:
                    log.exception(
                        "M15.0d Phase A failed for news index %d (parallel)", idx + 1,
                    )
                log.info(
                    "M15.0d Phase A item complete: index=%d total=%d",
                    idx + 1, total_items,
                )
                if progress_callback is not None:
                    try:
                        progress_callback("news_item_completed", {
                            "index": idx + 1, "total": total_items,
                        })
                    except Exception:  # noqa: BLE001
                        pass

    # M15-dedup-1 Part A — post-resolve URL dedup. Google News RSS
    # commonly returns multiple ``<item>`` entries with different
    # ``google_link`` GUIDs that ``resolve_google_news_url`` (called
    # inside Phase A) decodes to the same upstream ``original_url``
    # (different syndications of the same article). Without this
    # pass the pipeline would process the same article twice in
    # Phase B (LLM call + memory mutation + report assembly), then
    # api_server would emit two identical cards.
    #
    # M15-dedup-2 — title-based dedup as a SECOND layer. Operator
    # observed two cards with identical titles but different upstream
    # URLs on Render ("청년 버팀목 전세대출 2년 새 반토막 ...
    # - 아시아투데이" syndicated by two different publishers; URLs
    # differ → M15-dedup-1 doesn't catch them, but the user sees
    # what look like duplicate cards). Title normalization is
    # ``title.strip().lower()`` (no fuzzy matching — too risky for
    # Korean).
    #
    # Key choices:
    #   * Dedup key is ``original_url`` (post-decode) — title is too
    #     coarse for the primary key, ``google_link`` doesn't match
    #     cross-syndication. Title is the second layer applied only
    #     when the URL check passed.
    #   * Items whose ``original_url`` equals their ``google_link``
    #     are treated as UNIQUE — that equality marks a gnewsdecoder
    #     failure (see ``news_collector.resolve_google_news_url``
    #     fallback path). Collapsing decode failures together would
    #     drop distinct articles. Title dedup is ALSO skipped for
    #     decode-failure items (we cannot reliably distinguish two
    #     genuinely-different articles from two failed-decode dupes
    #     when the only signal is a Google redirect URL).
    #   * Items missing ``original_url`` (rare; would mean Phase A
    #     returned a malformed dict) are also treated as unique.
    #   * Empty titles are NOT used as a collision key (avoids
    #     collapsing items that lost their title to upstream
    #     metadata problems).
    seen_urls: set = set()
    seen_titles: set = set()
    deduped_phase_a_results: list = []
    for phase_a in phase_a_results:
        if phase_a is None:
            deduped_phase_a_results.append(phase_a)
            continue
        url = phase_a.get("original_url") or ""
        google_link = (phase_a.get("news") or {}).get("google_link") or ""
        title_raw = (phase_a.get("news") or {}).get("title") or ""
        if not url or url == google_link:
            # No decoded URL (or decode failure) — preserve as unique.
            # Title dedup also skipped here.
            deduped_phase_a_results.append(phase_a)
            continue
        if url in seen_urls:
            log.info(
                "M15-dedup-1: skipping duplicate news item",
                extra={
                    "duplicate_url": url[:500],
                    "duplicate_title": title_raw[:200],
                },
            )
            continue
        normalized_title = title_raw.strip().lower()
        if normalized_title and normalized_title in seen_titles:
            log.info(
                "M15-dedup-2: skipping duplicate title",
                extra={
                    "duplicate_title": title_raw[:200],
                    "duplicate_url": url[:500],
                },
            )
            continue
        seen_urls.add(url)
        if normalized_title:
            seen_titles.add(normalized_title)
        deduped_phase_a_results.append(phase_a)
    phase_a_results = deduped_phase_a_results

    # M26.3 — optional concurrent fan-out of the Phase-B ai_reasoner network
    # calls. Each run_ai_reasoning is a pure function of phase_a (inputs frozen
    # at Phase A time — see _run_ai_reasoning_for_phase_a), so running them
    # concurrently yields the SAME per-item ai_result as sequential. ONLY the
    # network call fans out; the order-dependent fold-back below (dedup, memory
    # mutation/save, counters, topic) stays serial in original submission
    # order. Network-bound, NOT CPU/Chromium — LESSON 1 (1-CPU Playwright
    # parallelism) does not apply; the pool is bounded regardless. Gated off by
    # default (AI_REASONER_CONCURRENCY_ENABLED): when off, ai_results stays all
    # None and the loop calls run_ai_reasoning inline exactly as pre-M26.3
    # (byte-identical). NO log.* is emitted here (main.py pin 331/16).
    ai_results: list = [None] * len(phase_a_results)
    concurrency_enabled = config.ai_reasoner_concurrency_enabled()
    if concurrency_enabled:
        fanout_indices = [
            i for i, phase_a in enumerate(phase_a_results) if phase_a is not None
        ]
        if fanout_indices:
            max_workers = max(
                1, min(config.ai_reasoner_max_concurrency(), len(fanout_indices))
            )
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="m26-3-ai-reasoner",
            ) as executor:
                future_to_index = {
                    executor.submit(
                        _run_ai_reasoning_for_phase_a, phase_a_results[i]
                    ): i
                    for i in fanout_indices
                }
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    try:
                        ai_results[idx] = future.result()
                    except Exception:  # noqa: BLE001
                        # run_ai_reasoning never raises (catches all -> error
                        # dict), so this is contractually unreachable. Handle
                        # silently into an error-shaped dict — NO log.* so the
                        # main.py pin (331/16) stays unchanged. The fold-back
                        # treats ai_available=False like a failed reasoning.
                        ai_results[idx] = {
                            "ai_available": False,
                            "ai_status": "error",
                            "ai_status_reason": "fanout_exception",
                            "ai_model": AI_MODEL,
                        }

    # Phase B — sequential, in original submission order. LLM call +
    # memory mutation + report assembly. Order-deterministic by
    # construction. With concurrency on, the (precomputed) ai_result is
    # folded in here in order; with it off, ai_results[i] is None and
    # _apply_news_item_phase_b calls run_ai_reasoning inline as before.
    for i, phase_a in enumerate(phase_a_results):
        if phase_a is None:
            # Phase A failed for this index — skip Phase B (preserves
            # the existing "exceptions swallowed at outer level"
            # contract; the operator sees the failure in logs).
            continue
        # Gate off -> call exactly as pre-M26.3 (2 args, byte-identical). Gate
        # on -> fold in the precomputed fan-out result for this index.
        if concurrency_enabled:
            phase_b = _apply_news_item_phase_b(phase_a, memory, ai_result=ai_results[i])
        else:
            phase_b = _apply_news_item_phase_b(phase_a, memory)
        report_items.append(phase_b["report_item"])
        if phase_b["saved_to_memory"]:
            saved_event_count += 1
        if phase_b["duplicate"]:
            duplicate_count += 1


    print_timeline_summary(memory)

    run_finished_at = utc_now_iso()
    report = sanitize_data({
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "query": query,
        "total_news_count": len(report_items),
        "saved_event_count": saved_event_count,
        "duplicate_count": duplicate_count,
        "news_collection_debug": news_collection_debug,
        "topics_summary": build_topics_summary(memory),
        "ai_status_summary": _summarize_ai_status_from_items(report_items),
        "news_results": report_items,
    })
    # M27 — surface the per-news-item primary-document provider counters
    # (policy_briefing_count / national_law_count) at the top level. PURE
    # read-only aggregation of values already produced per item
    # (main.py:888-894); observability only — touches no verdict/scoring field
    # and emits no log. Mirrors the existing "is not None" convention: when
    # both providers are off (production default) no item carries the counts,
    # so the key is NOT added and the report stays byte-identical to HEAD.
    policy_briefing_total = 0
    national_law_total = 0
    saw_primary_document_count = False
    for item in report_items:
        item_debug = item.get("debug_summary") or {}
        policy_briefing_value = item_debug.get("policy_briefing_count")
        if policy_briefing_value is not None:
            policy_briefing_total += policy_briefing_value
            saw_primary_document_count = True
        national_law_value = item_debug.get("national_law_count")
        if national_law_value is not None:
            national_law_total += national_law_value
            saw_primary_document_count = True
    if saw_primary_document_count:
        report["primary_document_counts"] = {
            "policy_briefing": policy_briefing_total,
            "national_law": national_law_total,
        }
    _store_analysis_report(
        analysis_cache_key=analysis_cache_key,
        query=query,
        max_news=max_news,
        news_results=news_results,
        report_items=report_items,
    )
    report_path = save_run_report(report, run_started_at)
    log.info(f'\nSaved run report: {report_path}')
    report["report_path"] = str(report_path)
    return report


def main():
    analyze_pipeline()


if __name__ == "__main__":
    main()
