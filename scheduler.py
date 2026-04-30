import argparse
import time

from database import init_db, save_analysis_result
from main import analyze_pipeline


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
    print("[Scheduler] Starting run...")

    for query in DEFAULT_QUERIES:
        print(f"[Scheduler] Query: {query}")
        try:
            report = analyze_pipeline(query=query, max_news=1)
            results = list(_iter_api_results(report))
            if not results:
                print(f"[Scheduler] No results for query: {query}")
                continue

            for result in results:
                save_status = save_analysis_result(result, query=query)
                title = result.get("title") or "(untitled)"
                if save_status.get("duplicate"):
                    print(f"[Scheduler] Duplicate skipped: {title}")
                elif save_status.get("saved"):
                    print(f"[Scheduler] Saved: {title}")
                else:
                    print(f"[Scheduler] Not saved: {title}")
        except Exception as error:
            print(f"[Scheduler] Error for query {query}: {error}")

    print("[Scheduler] Run complete.")


def run_loop(interval_minutes=60):
    interval_seconds = max(1, int(interval_minutes)) * 60
    print(f"[Scheduler] Loop started. interval_minutes={interval_minutes}")
    print("[Scheduler] Press Ctrl+C to stop.")

    try:
        while True:
            run_once()
            print(f"[Scheduler] Sleeping for {interval_minutes} minutes...")
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("[Scheduler] Stopped by user.")


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
