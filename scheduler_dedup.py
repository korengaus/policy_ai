"""M38 — scheduler pre-analysis URL dedup gate (pin-OUT helper).

Purpose
-------
The pre-verdict LLM judge binding is live in production, so every
``analyze_pipeline`` run spends a real Claude judge call. The scheduler's
existing dedup is POST-spend (``save_analysis_result`` →
``result_exists_by_url`` runs only AFTER the judge already fired). This helper
adds a PRE-analysis gate: resolve the top article URL a ``max_news=1`` run
WOULD analyze for a topic (news collection + URL decode only — NEVER the judge
or full pipeline) and, if that URL already exists in Postgres, tell the
scheduler to skip the topic entirely so no judge call is ever made.

Why a separate module
---------------------
``scheduler.py`` is pin-IN (``tests/test_log_level_reclassification.py``
MIGRATED_FILES) — its ``log.*`` call count is part of the
EXPECTED_TOTAL_LOG_CALLS=331 / EXPECTED_TOTAL_LOG_ERRORS=16 pins. This module
is NOT in MIGRATED_FILES, so the pre-skip / fail-open log lines live here and
do not touch the pins. ``scheduler.py`` only gains a function CALL (no log).

Contract
--------
``should_skip_topic(query)`` is judge-free and NEVER raises:
  * resolves the topic's top article URL via the SAME path the pipeline uses
    (so the checked URL matches the ``original_url`` the pipeline would save);
  * returns True (and logs a clear pre-skip line) ONLY when a URL resolves AND
    ``result_exists_by_url`` confirms it is already stored;
  * returns False on no-news / unresolved URL (fall through to normal analysis
    — do NOT skip on a dedup basis when there's nothing to dedup);
  * FAIL-OPEN: on any error (news fetch, URL decode, or the existence check),
    logs an error and returns False — we prefer paying for one analysis over
    silently dropping a topic.

Unconditional skip: no date-window / re-analysis-after-N-days logic
(deliberate future extension). The post-save dedup in ``scheduler.py`` remains
as a backstop.
"""

from __future__ import annotations

from database import result_exists_by_url
from news_collector import resolve_google_news_url, search_google_news_rss_with_meta
from structured_logging import get_logger


log = get_logger(__name__)


def _resolve_top_article_url(query: str) -> str:
    """Return the original (decoded) URL of the top article a ``max_news=1``
    run would analyze for ``query``, or "" when none resolves.

    News collection + Google-News URL decode ONLY — both are cached and
    judge-free. The decoded URL matches the ``original_url`` the pipeline
    derives at ``main.py:642`` (``resolve_google_news_url(news["google_link"])``)
    and saves, so the existence check is apples-to-apples. May raise; the
    public entry point wraps this fail-open.
    """
    collection = search_google_news_rss_with_meta(query, max_results=1)
    results = (collection or {}).get("results") or []
    if not results:
        return ""
    google_link = (results[0] or {}).get("google_link") or ""
    if not google_link:
        return ""
    return resolve_google_news_url(google_link) or ""


def should_skip_topic(query: str) -> bool:
    """True iff the top article ``query`` would analyze already exists in
    storage (so the scheduler can skip the topic BEFORE any judge spend).

    Judge-free and NEVER raises. Fail-open: returns False on no-news,
    unresolved URL, or ANY error.
    """
    try:
        original_url = _resolve_top_article_url(query)
        if not original_url:
            return False
        if result_exists_by_url(original_url):
            log.info(
                f"[Scheduler] Pre-skip: already analyzed top article for query: {query}",
                extra={"query": query, "original_url": original_url[:500]},
            )
            return True
        return False
    except Exception as error:
        # Fail-open: prefer analyzing one topic over silently dropping it.
        log.error(
            f"[Scheduler] Pre-skip check failed for query {query}: {error}",
            extra={
                "query": query,
                "exception_type": type(error).__name__,
                "exception_message": str(error)[:500],
            },
        )
        return False
