import argparse
import time

from database import init_db, save_analysis_result
from main import analyze_pipeline
from structured_logging import get_logger


# M14.0-print-b (2026-05-26): module logger for the scheduler CLI.
# scheduler.py is operator-run only (render.yaml runs uvicorn /
# worker.py, never scheduler). The structured_logging handler emits
# to stderr — operators still see all messages during interactive
# `--once` / `--loop` runs in the default LOG_FORMAT=text mode.
log = get_logger(__name__)


DEFAULT_QUERIES = [
    "전세대출",
    "금리",
    "부동산 정책",
    "청년 대출",
    "중소기업 금융지원",
]


def _iter_api_results(report: dict):
    for item in report.get("news_results", []):
        api_result = item.get("api_result") or {}
        if api_result:
            yield api_result


def run_once():
    init_db()
    # M14.0-print-b (2026-05-26): print → log.info conversion. All
    # status lines preserved verbatim with structured extras for
    # JSON-log queryability.
    log.info("[Scheduler] Starting run...")

    for query in DEFAULT_QUERIES:
        log.info(f"[Scheduler] Query: {query}", extra={"query": query})
        try:
            report = analyze_pipeline(query=query, max_news=1)
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
