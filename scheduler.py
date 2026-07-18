import argparse
import time

from database import save_analysis_result
from hot_topics import build_query_list
from main import analyze_pipeline
from scheduler_dedup import should_skip_topic
from structured_logging import get_logger


# M14.0-print-b (2026-05-26): module logger for the scheduler CLI.
# scheduler.py is operator-run only (render.yaml runs uvicorn /
# worker.py, never scheduler). The structured_logging handler emits
# to stderr — operators still see all messages during interactive
# `--once` / `--loop` runs in the default LOG_FORMAT=text mode.
log = get_logger(__name__)


DEFAULT_QUERIES = [
    "주택담보대출 규제",
    "스트레스 DSR 가계부채",
    "전세 공급 대책",
    "청년 정책 지원",
    "양도세 세제 개편",
    "소상공인 지원",
    "복지 예산",
    # WELFARE-SEED — welfare-domain expansion (program-level; one domain at a time)
    "기초생활보장",
    "노인 돌봄",
    "장애인 지원",
    "아동수당",
    # AGRI-LABOR-SEED — agriculture + labor expansion (program-level; tragedy/
    # dispute-prone terms excluded, mirroring WELFARE-SEED)
    "귀농 귀촌 지원",
    "농산물 가격 안정",
    "스마트팜 농업",
    "농가 소득 지원",
    "최저임금",
    "청년 일자리",
    "고용 안정 지원",
    "근로자 권익",
    # ENV-SEED — environment-domain expansion (program-level; one domain at a time,
    # mirroring WELFARE-SEED / AGRI-LABOR-SEED). Health held for later (higher noise).
    "탄소중립 온실가스 감축",
    "배출권거래제",
    "재생에너지 태양광 풍력 정책",
    "미세먼지 대기질 대책",
    "기후위기 대응 녹색금융",
    # EDU-SEED — education-domain expansion (COLLECTION-SEEDS). The education
    # label joined domain_classifier on 2026-07-14 (DOMAIN-LABEL 2a) with no
    # dedicated seeds — "a category is an empty container; search queries fill
    # it." Terms mirror the classifier's own education tokens (교육/대입/학교/
    # 교육청/교육부) so collected articles classify INTO education, and follow
    # the program-level hygiene of the blocks above (no tragedy/dispute terms).
    "교육개혁",
    "대입 제도 개편",
    "사교육 대책",
    "교권 보호",
    "늘봄학교",
    "교육 예산",
    # HEALTH-SEED — health-domain expansion. This REVERSES the deliberate
    # "Health held for later (higher noise)" deferral recorded on ENV-SEED
    # above: health sat at 1.3% of recent intake with zero seeds, and the
    # noise concern is now mitigated by program-level terms mirroring the
    # classifier's health tokens (의료/건강/병원) rather than incident terms.
    "의료개혁",
    "건강보험",
    "의대 정원",
    "필수의료",
    "공공의료",
    "비대면진료",
    # STAT-SEED — statistics-domain expansion (classifier tokens: 통계청 지표/
    # 물가지수/고용률). NOTE: the agency formerly 통계청 was renamed
    # 국가데이터처 (National Data Agency) on 2025-10-01, but 통계청 remains in
    # colloquial use and regional-office names — BOTH are included deliberately
    # for coverage; this is not a duplicate to clean up.
    "국가데이터처",
    "통계청",
    "소비자물가지수",
    "고용동향",
    "인구동향",
    "가계동향조사",
]


def _iter_api_results(report: dict):
    for item in report.get("news_results", []):
        api_result = item.get("api_result") or {}
        if api_result:
            yield api_result


def run_once():
    # M12.0e-6b-3: SQLite init removed; the scheduler writes via the PG
    # mirror (save_analysis_result), and ensure_schema creates the PG
    # schema lazily on first engine use.
    # M14.0-print-b (2026-05-26): print → log.info conversion. All
    # status lines preserved verbatim with structured extras for
    # JSON-log queryability.
    log.info("[Scheduler] Starting run...")

    # HOTTOPIC Phase 2 — the fixed DEFAULT_QUERIES seed list (42 seeds across 10
    # domains as of COLLECTION-SEEDS; the old "fixed 7" wording predated four
    # expansion blocks) + (flag-gated) dynamic AI hot-topic keywords.
    # build_query_list returns exactly DEFAULT_QUERIES when HOT_TOPIC_ENABLED is
    # off (byte-identical), and appends filtered dynamic keywords when on. All
    # selector logic + logging live in the pin-OUT hot_topics module, so this
    # file gains only the call (no new log.* line; pins 331/16 unchanged).
    for query in build_query_list(DEFAULT_QUERIES):
        log.info(f"[Scheduler] Query: {query}", extra={"query": query})
        # M38 — pre-analysis dedup gate. Skip the topic BEFORE analyze_pipeline
        # (and therefore before any judge spend) when its top article is already
        # stored. Judge-free, fail-open; its logging lives in the pin-OUT
        # scheduler_dedup module so this file's log.* count is unchanged.
        if should_skip_topic(query):
            continue
        try:
            # COLLECTION-SEEDS: max_news 1 -> 3 (collection WIDTH only — every
            # article still runs the unchanged pipeline). Known interaction,
            # deliberately left alone: should_skip_topic above checks only the
            # TOP article's URL, so a topic whose #1 result is stored skips
            # entirely even if results #2-3 are new — effective yield < 3x.
            report = analyze_pipeline(query=query, max_news=3)
            results = list(_iter_api_results(report))
            if not results:
                log.info(
                    f"[Scheduler] No results for query: {query}",
                    extra={"query": query},
                )
                continue

            for result in results:
                save_status = save_analysis_result(result, query=query)
                title = result.get("title") or "(untitled)"
                if save_status.get("duplicate"):
                    log.info(
                        f"[Scheduler] Duplicate skipped: {title}",
                        extra={"query": query, "title": title[:200]},
                    )
                elif save_status.get("saved"):
                    log.info(
                        f"[Scheduler] Saved: {title}",
                        extra={"query": query, "title": title[:200]},
                    )
                else:
                    log.info(
                        f"[Scheduler] Not saved: {title}",
                        extra={"query": query, "title": title[:200]},
                    )
        except Exception as error:
            # M14.0-print-b: print → log.error. Inside the broad
            # `except Exception as error:` block — satisfies the
            # M14.4 NoFalsePositiveErrorsPin via inside-except path,
            # so no keyword needed in the literal text.
            log.error(
                f"[Scheduler] Error for query {query}: {error}",
                extra={
                    "query": query,
                    "exception_type": type(error).__name__,
                    "exception_message": str(error)[:500],
                },
            )

    log.info("[Scheduler] Run complete.")


def run_loop(interval_minutes=60):
    interval_seconds = max(1, int(interval_minutes)) * 60
    # M14.0-print-b (2026-05-26): print → log.info conversion.
    log.info(
        f"[Scheduler] Loop started. interval_minutes={interval_minutes}",
        extra={"interval_minutes": interval_minutes},
    )
    log.info("[Scheduler] Press Ctrl+C to stop.")

    try:
        while True:
            run_once()
            log.info(
                f"[Scheduler] Sleeping for {interval_minutes} minutes...",
                extra={"interval_minutes": interval_minutes},
            )
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log.info("[Scheduler] Stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="Run scheduled Policy AI analysis.")
    parser.add_argument("--once", action="store_true", help="Run scheduled analysis once.")
    parser.add_argument("--loop", action="store_true", help="Run scheduled analysis repeatedly.")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in minutes.")
    args = parser.parse_args()

    if args.loop:
        run_loop(interval_minutes=args.interval)
        return

    run_once()


if __name__ == "__main__":
    main()
