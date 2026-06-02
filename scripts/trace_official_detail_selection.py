#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M19-source-reliability-3b — Instrumented live trace of official detail-URL selection.

READ-ONLY DIAGNOSTIC. Does NOT edit any production source module. It imports the
existing official-crawler path and records, per official candidate, the full
candidate-link set, the selection ordering, the selected URL, and the fetched
document's relevance/grade — so we can decide whether the dominant failure is:

  PRESENT_BUT_MISRANKED  -> a query-relevant detail link existed but lost selection
  ABSENT                 -> no query-relevant detail link was on the page at all
  FETCHED_THEN_REJECTED  -> the selected link was on-topic but the gate rejected it
  SEARCH_FAILED          -> no candidate links / search endpoint error
  NETWORK_FAIL           -> connection reset / 404 / 406 / timeout

Scope guard: ONE query, the official-evidence portion only (max 5 official
sources, mirroring main.py:622-633). No PG writes. No embeddings (the traced
path stops before evidence extraction / semantic matching). No commit.

Usage (on the Worker Shell, or any box with live .go.kr access):
    python scripts/trace_official_detail_selection.py
    python scripts/trace_official_detail_selection.py "전세대출 규제"

A JSON trace is written under the gitignored reports/ directory and a human
summary is printed to stdout.

This script makes real HTTP requests to *.go.kr / *.or.kr for the single chosen
query. That is the measurement. It does not loop over news items.
"""
from __future__ import annotations

import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlparse

# Ensure the repo root (parent of scripts/) is importable when this file is run
# directly as `python scripts/trace_official_detail_selection.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# --- production modules: IMPORT ONLY, never mutated on disk -------------------
import official_crawler
from official_crawler import (
    fetch_best_official_document,
    _candidate_selection_key,
)
from official_source_search import generate_official_source_candidates
from topic_classifier import classify_policy_topic
from official_relevance import extract_query_terms


# ---------------------------------------------------------------------------
# Chosen query.
#
# Default: "전세사기" (jeonse / lease fraud). Rationale:
#   * It is a concrete, high-salience policy entity with heavy official press
#     coverage across 국토교통부 (MOLIT), 금융위 (FSC), 주택도시보증공사 (HUG),
#     and 경찰청 — there was a 2023-24 특별법/대책 with many press releases.
#   * Because relevant official detail pages almost certainly EXIST, an ABSENT
#     result would point squarely at search-targeting (not a coverage gap),
#     maximising the power of this single run to distinguish MISRANKED vs ABSENT.
# Override by passing a query as argv[1].
# ---------------------------------------------------------------------------
DEFAULT_QUERY = "전세사기"

# Heuristic human-relevance probe for the chosen topic. Used ONLY to tag each
# candidate link as topically relevant or not — it does NOT influence the real
# crawler path. extract_query_terms() keeps compound tokens (e.g. "전세사기")
# whole, which would under-count a press release titled "전세 피해 지원"; this
# expanded set is the honest probe a human would apply when eyeballing links.
# Labelled clearly as a heuristic in the output.
TOPIC_PROBE_KEYWORDS = {
    "전세사기": ["전세사기", "전세", "사기", "피해", "피해자", "보증", "임차", "임대차", "보증금", "깡통"],
    "전세대출 규제": ["전세대출", "전세", "대출", "규제", "보증", "DSR", "한도", "임차"],
}


def _domain(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _probe_terms(query: str) -> list[str]:
    if query in TOPIC_PROBE_KEYWORDS:
        return TOPIC_PROBE_KEYWORDS[query]
    # Fall back to the crawler's own query-term extractor for an unknown query.
    return extract_query_terms(query)


def _overlap(text: str, terms: list[str]) -> list[str]:
    text = text or ""
    return [t for t in terms if t and t in text]


# ---------------------------------------------------------------------------
# Read-only in-process wrappers.
#
# These wrap function REFERENCES inside the already-imported official_crawler
# module object (in memory only — the .py file on disk is untouched). Each
# wrapper RECORDS inputs/outputs and then returns the ORIGINAL result verbatim,
# so the real crawler path is byte-identical to production. The wrappers also
# attempt a *broad* re-extraction (max_links bumped to 25) to reveal whether a
# query-relevant link exists BEYOND the production top-5 cap — this is the only
# behaviour beyond pure observation, and it is captured into a side channel,
# never returned into the live path.
# ---------------------------------------------------------------------------
_CAPTURE: dict = {
    "extract_candidate_links": OrderedDict(),   # search_url -> {...}
    "rendered": OrderedDict(),                   # url -> {...}
}

_ORIG_EXTRACT = official_crawler._extract_candidate_links
_ORIG_RENDERED = getattr(official_crawler, "extract_rendered_links", None)


def _wrapped_extract_candidate_links(*, search_html, search_url, source_name, query):
    # 1) The REAL call — result returned to the live path unchanged.
    real_links, real_parser = _ORIG_EXTRACT(
        search_html=search_html,
        search_url=search_url,
        source_name=source_name,
        query=query,
    )

    # 2) Broad re-extraction for diagnostics (HTTP-path parsers only; no network
    #    — we reuse the already-fetched search_html). Defensive: must never
    #    break the live path.
    broad = []
    broad_parser = None
    try:
        from official_site_parsers import extract_links_for_site
        try:
            site_links = extract_links_for_site(
                search_html=search_html,
                base_url=search_url,
                source_name=source_name,
                query=query,
                max_links=25,
            )
        except Exception:
            site_links = []
        if site_links:
            broad, broad_parser = site_links, "site_specific(max=25)"
        else:
            broad = official_crawler.extract_official_result_links(
                search_html, search_url, max_links=25
            )
            broad_parser = "generic_fallback(max=25)"
    except Exception as exc:  # pragma: no cover - diagnostic only
        broad, broad_parser = [], f"broad_extract_error:{type(exc).__name__}"

    _CAPTURE["extract_candidate_links"].setdefault(search_url, {
        "source_name": source_name,
        "query": query,
        "real_parser": real_parser,
        "real_links": [
            {"url": c.get("url"), "text": c.get("text"), "score": c.get("score")}
            for c in (real_links or [])
        ],
        "broad_parser": broad_parser,
        "broad_links": [
            {"url": c.get("url"), "text": c.get("text"), "score": c.get("score")}
            for c in (broad or [])
        ],
    })
    return real_links, real_parser


def _wrapped_rendered(url, *args, **kwargs):
    rendered = _ORIG_RENDERED(url, *args, **kwargs)
    try:
        rl = rendered.get("rendered_links") or []
        _CAPTURE["rendered"].setdefault(url, {
            "rendered_used": rendered.get("rendered_used"),
            "rendered_parser_used": rendered.get("rendered_parser_used"),
            "rendered_links_count": rendered.get("rendered_links_count"),
            "raw_links_count": rendered.get("raw_links_count"),
            "rendered_error": rendered.get("rendered_error"),
            "rendered_title": rendered.get("rendered_title"),
            "rendered_links": [
                {"url": c.get("url"), "text": c.get("text"), "score": c.get("score")}
                for c in rl
            ],
        })
    except Exception:
        pass
    return rendered


def _install_wrappers():
    official_crawler._extract_candidate_links = _wrapped_extract_candidate_links
    if _ORIG_RENDERED is not None:
        official_crawler.extract_rendered_links = _wrapped_rendered


def _restore_wrappers():
    official_crawler._extract_candidate_links = _ORIG_EXTRACT
    if _ORIG_RENDERED is not None:
        official_crawler.extract_rendered_links = _ORIG_RENDERED


# ---------------------------------------------------------------------------
# Per-candidate verdict tagging.
# ---------------------------------------------------------------------------
def _classify_verdict(result: dict, candidate_links: list[dict], selected_url: str,
                      probe_terms: list[str]) -> tuple[str, str]:
    error = str(result.get("error") or "")
    lowered = error.lower()

    network_markers = (
        "connectionreset", "connection aborted", "max retries", "newconnectionerror",
        "timeout", "timed out", "406", "404", "not acceptable", "not found",
        "remotedisconnected", "connection refused",
    )
    if any(m in lowered for m in network_markers):
        return "NETWORK_FAIL", error[:200]

    if not candidate_links:
        return "SEARCH_FAILED", error[:200] or "no candidate links extracted from search page"
    if "no official document candidate links" in lowered \
       or "no valid detail document" in lowered \
       or "no official search url" in lowered \
       or "fss search returned error" in lowered:
        return "SEARCH_FAILED", error[:200]

    # Relevance of each candidate link (by link text vs the topic probe).
    def rel(link):
        return len(_overlap(link.get("text") or "", probe_terms))

    relevant = [c for c in candidate_links if rel(c) >= 1]
    max_rel = max((rel(c) for c in candidate_links), default=0)
    selected = next((c for c in candidate_links if c.get("url") == selected_url), None)
    selected_rel = rel(selected) if selected else 0
    usable = bool(result.get("usable"))

    if max_rel == 0:
        return "ABSENT", "no candidate link text overlaps the topic probe terms"

    if selected is not None and selected_rel >= max_rel and selected_rel >= 1:
        # The most on-topic link WAS selected.
        if usable:
            return "OK_USABLE", "on-topic link selected and passed the gate"
        return ("FETCHED_THEN_REJECTED",
                f"on-topic link selected (overlap={selected_rel}) but rejected: "
                f"grade={result.get('evidence_grade')} "
                f"relevance={result.get('document_relevance_score')} "
                f"exclude={result.get('should_exclude_from_verification')}")

    # A more on-topic link existed than the one selected.
    return ("PRESENT_BUT_MISRANKED",
            f"selected overlap={selected_rel} but a candidate with overlap={max_rel} "
            f"existed and was not selected ({len(relevant)} relevant link(s) in set)")


def trace_query(query: str) -> dict:
    topic = classify_policy_topic(
        news_title=query,
        news_summary=query,
        article_body="",
        ai_result={"main_policy_issue": query, "one_line_summary": query},
    )
    candidates = generate_official_source_candidates(
        news_title=query,
        core_policy_issue=query,
        topic=topic,
        max_candidates=5,
    )
    probe_terms = _probe_terms(query)

    news_context = {
        "title": query,
        "summary": query,
        "article_body": "",
        "topic": topic,
        "policy_claims": [],
    }

    trace = {
        "query": query,
        "classified_topic": topic,
        "probe_terms": probe_terms,
        "probe_terms_note": (
            "Heuristic human-relevance probe for tagging links only; does NOT "
            "influence the live crawler path. extract_query_terms keeps compound "
            "tokens whole, so this expanded set avoids under-counting."
        ),
        "official_source_candidates": [
            {
                "source_name": c.get("source_name"),
                "source_type": c.get("source_type"),
                "search_query": c.get("search_query"),
                "search_query_variants": c.get("search_query_variants"),
                "official_search_url": c.get("official_search_url"),
            }
            for c in candidates
        ],
        "candidates": [],
        "run_started_at": datetime.now(timezone.utc).isoformat(),
    }

    _install_wrappers()
    try:
        for cand in candidates:
            _CAPTURE["extract_candidate_links"].clear()
            _CAPTURE["rendered"].clear()
            try:
                result = fetch_best_official_document(cand, news_context=news_context)
            except Exception as exc:  # isolate; record as a failure row
                result = {
                    "source_name": cand.get("source_name"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "candidate_links": [],
                }

            candidate_links = result.get("candidate_links") or []
            selected_url = result.get("selected_document_url")

            # Annotate each candidate link with selection key + topic overlap.
            annotated = []
            for link in candidate_links:
                try:
                    sel_key = list(_candidate_selection_key(link))
                except Exception:
                    sel_key = None
                annotated.append({
                    "url": link.get("url"),
                    "text": link.get("text"),
                    "link_score": link.get("link_score") if link.get("link_score") is not None else link.get("score"),
                    "is_detail_page": link.get("is_detail_page"),
                    "id_detected": link.get("id_detected"),
                    "url_depth_score": link.get("url_depth_score"),
                    "relevance_score": link.get("relevance_score"),
                    "relevance_level": link.get("relevance_level"),
                    "selection_key": sel_key,
                    "topic_overlap_terms": _overlap(link.get("text") or "", probe_terms),
                    "is_selected": link.get("url") == selected_url,
                })
            # Order as the crawler's first-stage selection would (descending key).
            annotated_sorted = sorted(
                annotated,
                key=lambda a: (a["selection_key"] is not None, a["selection_key"] or []),
                reverse=True,
            )

            verdict, verdict_reason = _classify_verdict(
                result, candidate_links, selected_url, probe_terms,
            )

            broad_capture = list(_CAPTURE["extract_candidate_links"].values())
            rendered_capture = list(_CAPTURE["rendered"].values())

            trace["candidates"].append({
                "source_name": result.get("source_name") or cand.get("source_name"),
                "source_type": cand.get("source_type"),
                "base_domain": _domain(cand.get("official_search_url") or ""),
                "site_key": result.get("site_key"),
                "search_query_used": result.get("search_query_used") or cand.get("search_query"),
                "search_attempt_urls": [
                    a.get("url") for a in (result.get("search_attempt_results") or [])
                ] or [cand.get("official_search_url")],
                "parser_used": result.get("parser_used"),
                "browser_fallback_used": result.get("browser_fallback_used"),
                "rendered_error": result.get("rendered_error"),
                "candidate_links_count": len(candidate_links),
                "candidate_links_ordered_by_selection_key": annotated_sorted,
                "selected_document_url": selected_url,
                "winner": {
                    "document_title": result.get("document_title"),
                    "document_text_length": result.get("document_text_length"),
                    "document_relevance_score": result.get("document_relevance_score"),
                    "matched_query_terms": result.get("matched_query_terms"),
                    "matched_concepts": result.get("matched_concepts"),
                    "evidence_grade": result.get("evidence_grade"),
                    "document_type": result.get("document_type"),
                    "should_exclude_from_verification": result.get("should_exclude_from_verification"),
                    "usable": result.get("usable"),
                    "weakly_usable": result.get("weakly_usable"),
                    "classification_reasons": result.get("classification_reasons"),
                    "topic_overlap_in_title": _overlap(result.get("document_title") or "", probe_terms),
                },
                "error": result.get("error"),
                "broad_pool_capture": broad_capture,     # max=25 re-extraction (HTTP parsers)
                "rendered_pool_capture": rendered_capture,
                "verdict": verdict,
                "verdict_reason": verdict_reason,
            })
    finally:
        _restore_wrappers()

    trace["run_finished_at"] = datetime.now(timezone.utc).isoformat()

    # Headline tally.
    tally: "OrderedDict[str, int]" = OrderedDict()
    for c in trace["candidates"]:
        tally[c["verdict"]] = tally.get(c["verdict"], 0) + 1
    trace["verdict_tally"] = tally
    return trace


def _print_summary(trace: dict) -> None:
    print("=" * 78)
    print(f"OFFICIAL DETAIL-URL SELECTION TRACE — query: {trace['query']!r}")
    print(f"classified_topic: {trace['classified_topic']}")
    print(f"probe_terms (heuristic): {trace['probe_terms']}")
    print("=" * 78)
    for c in trace["candidates"]:
        print(f"\n### {c['source_name']}  [{c.get('site_key')}]  -> {c['verdict']}")
        print(f"    reason: {c['verdict_reason']}")
        print(f"    search_query_used: {c.get('search_query_used')!r}")
        print(f"    search_attempt_urls: {c.get('search_attempt_urls')}")
        print(f"    parser_used: {c.get('parser_used')}  browser_fallback: {c.get('browser_fallback_used')}  rendered_error: {c.get('rendered_error')}")
        print(f"    candidate_links_count: {c['candidate_links_count']}")
        for i, link in enumerate(c["candidate_links_ordered_by_selection_key"]):
            mark = " <== SELECTED" if link["is_selected"] else ""
            print(f"      [{i}] overlap={link['topic_overlap_terms']} detail={link['is_detail_page']} "
                  f"id={link['id_detected']} link_score={link['link_score']} rel={link['relevance_score']}{mark}")
            print(f"          text: {(link['text'] or '')[:90]!r}")
            print(f"          url : {link['url']}")
        w = c["winner"]
        print(f"    selected_url: {c['selected_document_url']}")
        print(f"    winner: title={ (w.get('document_title') or '')[:80]!r}")
        print(f"            relevance={w.get('document_relevance_score')} grade={w.get('evidence_grade')} "
              f"type={w.get('document_type')} usable={w.get('usable')} exclude={w.get('should_exclude_from_verification')}")
        print(f"            matched_query_terms={w.get('matched_query_terms')} title_overlap={w.get('topic_overlap_in_title')}")
        # Broad pool: did a relevant link exist beyond the live top-5 cap?
        for cap in c.get("broad_pool_capture", []):
            rel_broad = [b for b in cap.get("broad_links", []) if any(t in (b.get("text") or "") for t in trace["probe_terms"])]
            print(f"    broad_pool[{cap.get('broad_parser')}] total={len(cap.get('broad_links', []))} "
                  f"topic_relevant={len(rel_broad)}")
            for b in rel_broad[:5]:
                print(f"        + overlap text: {(b.get('text') or '')[:80]!r}  {b.get('url')}")
    print("\n" + "=" * 78)
    print("VERDICT TALLY:", dict(trace["verdict_tally"]))
    print("=" * 78)


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    trace = trace_query(query)
    _print_summary(trace)

    # Anchor output to <repo_root>/reports so the trace always lands in the
    # gitignored directory regardless of the shell's working directory.
    reports_dir = os.path.join(_REPO_ROOT, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_q = "".join(ch for ch in query if ch.isalnum())[:24] or "query"
    out_path = os.path.join(reports_dir, f"trace_official_detail_selection_{safe_q}_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(trace, fh, ensure_ascii=False, indent=2)
    print(f"\nJSON trace written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
