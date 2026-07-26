#!/usr/bin/env python3
"""COLLECTION-ALERT — pin-OUT wrapper for the daily-collection cron.

Wraps ``python scheduler.py --once`` so the run ends with ONE ntfy
notification carrying the number of rows it added:

    success:  일일 수집 완료 — 신규 N건
    failure:  일일 수집 실패 — rc={rc}, 신규 N건   (priority high)

A zero-row success arrives as 신규 0건 ON PURPOSE — a job that "succeeds"
while collecting nothing is the failure mode this alert exists to catch.

DESIGN RULES (COLLECTION-ALERT Phase 1/2):
  * scheduler.py is pin-IN and is NOT touched; this wrapper subprocess-runs
    it unchanged. New file is pin-OUT: the 331/16 log pins cannot move.
  * COUNT = MAX(id) delta, NOT a created_at comparison: created_at is TEXT,
    so a format/timezone mismatch would return 0 every day — a standing
    false "신규 0건" alarm that trains the operator to ignore the alert.
    The id delta is immune to timestamp formatting. (Known, accepted
    approximations: a concurrent writer's rows land in the delta, and a
    rolled-back insert can widen it by consuming sequence ids — both rare
    and both err on the visible side, never toward silent 0.)
  * The child's stdout/stderr are INHERITED, not captured — Render logs look
    exactly as they do today.
  * The wrapper EXITS WITH THE CHILD'S EXIT CODE so Render still marks a
    failed run failed. Nothing in the alerting layer may mask a failure —
    and nothing in it may block collection: a failed baseline query still
    runs the child and reports 신규 건수 미상.
  * Notifier is weekly_spine.notify by IMPORT (best-effort: unreachable
    ntfy / unset env prints and continues); a raising notifier is swallowed.
  * SELECT only (MAX(id) twice); no writes, no schema change, no secrets
    printed. Reads DATABASE_URL directly — it does NOT depend on
    USE_POSTGRES_WRITE (that flag gates the app's dual-write engine, not
    this read).

USAGE
    python scripts/daily_collection_alert.py             # the cron command
    python scripts/daily_collection_alert.py --selftest  # pure, no DB/network
    python scripts/daily_collection_alert.py --check-db  # SELECT MAX(id) only

The daily-collection cron is DASHBOARD-provisioned: point its command at
this script in the Render dashboard (render.yaml is documentation only) and
add NTFY_TOPIC to that service's env for the notification to be sent.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# weekly_spine (same directory, pin-OUT) owns the ntfy plumbing.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def read_max_id():
    """SELECT MAX(id) FROM analysis_results, or None on ANY failure.
    Failure is printed (type only — never the URL) and never raises."""
    import os

    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("[collection-alert] DATABASE_URL unset — count unavailable")
        return None
    try:
        import psycopg
        import weekly_spine

        # normalize_db_url strips a SQLAlchemy dialect suffix
        # ("postgresql+psycopg://" -> "postgresql://") so raw psycopg accepts
        # the same DATABASE_URL the app's engine uses.
        url = weekly_spine.normalize_db_url(url)
        with psycopg.connect(url, connect_timeout=15) as conn:
            row = conn.execute(
                "SELECT MAX(id) FROM analysis_results").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:
        print("[collection-alert] baseline query failed: %s"
              % type(exc).__name__)
        return None


def read_briefing_status(min_id):
    """SILENT-FAILURE-FLAG: (error_rows, keyed_rows) among rows with
    id > min_id, read from debug_summary.policy_briefing_status. Rows WITHOUT
    the key (old rows / disabled lane) are UNKNOWN and excluded from both
    numbers — absence is never counted as failure or success. None on ANY
    failure (best-effort, mirrors read_max_id; never raises)."""
    import os

    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url or min_id is None:
        return None
    try:
        import psycopg
        import weekly_spine

        url = weekly_spine.normalize_db_url(url)
        with psycopg.connect(url, connect_timeout=15) as conn:
            row = conn.execute(
                "SELECT "
                "COUNT(*) FILTER (WHERE debug_summary LIKE "
                "'%\"policy_briefing_status\": \"error\"%'), "
                "COUNT(*) FILTER (WHERE debug_summary LIKE "
                "'%\"policy_briefing_status\"%') "
                "FROM analysis_results WHERE id > %s", (min_id,)).fetchone()
        return (int(row[0] or 0), int(row[1] or 0))
    except Exception as exc:
        print("[collection-alert] briefing-status query failed: %s"
              % type(exc).__name__)
        return None


def run_child():
    """Run the collection unchanged, output inherited. Returns the exit code
    (a child killed by signal N maps to 128+N so Render sees non-zero)."""
    try:
        rc = subprocess.call(
            [sys.executable, "scheduler.py", "--once"], cwd=str(REPO_ROOT))
    except Exception as exc:
        print("[collection-alert] could not start scheduler: %s"
              % type(exc).__name__)
        return 1
    return rc if rc >= 0 else 128 + abs(rc)


def run(child_runner, max_id_reader, notifier, briefing_reader=None):
    """Orchestrate one wrapped run. Pure enough to selftest with fakes.
    Returns the exit code to pass through (ALWAYS the child's).
    briefing_reader (SILENT-FAILURE-FLAG, optional): min_id -> (error_rows,
    keyed_rows) or None; when MOST keyed new rows report a failed
    policy-briefing lookup, the ONE existing notification carries it —
    no second alerting path, exit code untouched."""
    started = time.time()
    before = max_id_reader()
    rc = child_runner()
    after = max_id_reader()

    if before is not None and after is not None:
        delta_text = "신규 %d건" % max(0, after - before)
    else:
        delta_text = "신규 건수 미상 (카운트 조회 실패)"
    minutes = (time.time() - started) / 60.0

    if rc == 0:
        title = "일일 수집 완료 — %s" % delta_text
        message = "%s · %.0f분 · scheduler.py --once rc=0" % (delta_text, minutes)
        priority = "default"
    else:
        title = "일일 수집 실패 — rc=%d, %s" % (rc, delta_text)
        message = "%s · %.0f분 · scheduler.py --once rc=%d" % (delta_text, minutes, rc)
        priority = "high"

    # SILENT-FAILURE-FLAG: the 07-14/15 briefing outage was invisible for 11
    # days because failures stored as plain zeros. If most keyed new rows say
    # "error", say so loudly in the SAME notification. Best-effort: a failed
    # or absent reader changes nothing; rows without the key are unknown and
    # already excluded by the reader.
    if briefing_reader is not None:
        try:
            briefing = briefing_reader(before)
        except Exception as exc:
            print("[collection-alert] briefing reader raised %s — ignored"
                  % type(exc).__name__)
            briefing = None
        if briefing:
            error_rows, keyed_rows = briefing
            if keyed_rows > 0 and error_rows * 2 >= keyed_rows:
                title += " · 정책브리핑 조회 장애 의심"
                message += (" · 정책브리핑 조회 실패 %d/%d건 — 0건이 '없음'이"
                            " 아니라 '못 봄'일 수 있음" % (error_rows, keyed_rows))
                priority = "high"

    # Belt over weekly_spine.notify's own braces: even a RAISING notifier
    # must never change the run's exit code.
    try:
        notifier(title, message, priority=priority)
    except Exception as exc:
        print("[collection-alert] notify raised %s — ignored"
              % type(exc).__name__)
    return rc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--check-db" in argv:
        value = read_max_id()
        print("[collection-alert] MAX(id) = %r" % value)
        return 0 if value is not None else 1

    import weekly_spine

    return run(run_child, read_max_id, weekly_spine.notify,
               briefing_reader=read_briefing_status)


# ---------------------------------------------------------------------------
# Selftest — stubbed child + stubbed DB + stubbed notifier. No network, no
# DB, no collection run.
# ---------------------------------------------------------------------------

def selftest() -> int:
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    def fake_reader(values):
        state = {"i": 0}

        def reader():
            i = state["i"]
            state["i"] += 1
            return values[min(i, len(values) - 1)]
        return reader

    sent = []

    def notifier(title, message, priority="default"):
        sent.append((title, message, priority))
        return True

    # 1. success, positive delta.
    sent.clear()
    rc = run(lambda: 0, fake_reader([100, 103]), notifier)
    check("1 rc passthrough 0", rc == 0)
    check("1 title", sent[0][0] == "일일 수집 완료 — 신규 3건")
    check("1 priority", sent[0][2] == "default")

    # 2. success, ZERO delta — must arrive as 신규 0건, never suppressed.
    sent.clear()
    rc = run(lambda: 0, fake_reader([100, 100]), notifier)
    check("2 rc 0", rc == 0)
    check("2 zero visible", sent and "신규 0건" in sent[0][0])

    # 3. child failure rc=7 — exit code passes through, priority high.
    sent.clear()
    rc = run(lambda: 7, fake_reader([100, 101]), notifier)
    check("3 rc passthrough 7", rc == 7)
    check("3 title", sent[0][0] == "일일 수집 실패 — rc=7, 신규 1건")
    check("3 priority high", sent[0][2] == "high")

    # 4. notifier raising must not change the exit code.
    def raising_notifier(title, message, priority="default"):
        raise RuntimeError("ntfy down")

    rc = run(lambda: 0, fake_reader([100, 102]), raising_notifier)
    check("4 notify exception swallowed", rc == 0)

    # 5. failed baseline query: collection still runs, count reported 미상.
    sent.clear()
    ran = {"child": False}

    def child():
        ran["child"] = True
        return 0

    rc = run(child, fake_reader([None, None]), notifier)
    check("5 child still ran", ran["child"])
    check("5 rc 0", rc == 0)
    check("5 count unknown", sent and "신규 건수 미상" in sent[0][0])

    # 5b. baseline ok but post-run read fails -> also 미상 (no fake 0).
    sent.clear()
    run(lambda: 0, fake_reader([100, None]), notifier)
    check("5b count unknown", sent and "신규 건수 미상" in sent[0][0])

    # 6. SILENT-FAILURE-FLAG: majority-failed briefing lookup surfaces in the
    # ONE notification (title + message + high priority), rc untouched.
    sent.clear()
    rc = run(lambda: 0, fake_reader([100, 110]), notifier,
             briefing_reader=lambda min_id: (8, 10))
    check("6 rc 0", rc == 0)
    check("6 title flag", sent and "정책브리핑 조회 장애 의심" in sent[0][0])
    check("6 message detail", sent and "정책브리핑 조회 실패 8/10건" in sent[0][1])
    check("6 priority high", sent and sent[0][2] == "high")

    # 6b. minority failures / no keyed rows / reader failure -> unchanged.
    sent.clear()
    run(lambda: 0, fake_reader([100, 110]), notifier,
        briefing_reader=lambda min_id: (1, 10))
    check("6b minority silent", sent and "장애 의심" not in sent[0][0])
    sent.clear()
    run(lambda: 0, fake_reader([100, 110]), notifier,
        briefing_reader=lambda min_id: (0, 0))
    check("6b zero-keyed silent", sent and "장애 의심" not in sent[0][0])
    sent.clear()
    run(lambda: 0, fake_reader([100, 110]), notifier,
        briefing_reader=lambda min_id: None)
    check("6b reader-none silent", sent and "장애 의심" not in sent[0][0])

    def raising_briefing(min_id):
        raise RuntimeError("db down")

    sent.clear()
    rc = run(lambda: 0, fake_reader([100, 110]), notifier,
             briefing_reader=raising_briefing)
    check("6c raising reader swallowed", rc == 0 and sent
          and "장애 의심" not in sent[0][0])

    if failures:
        print("SELFTEST FAILED: " + ", ".join(failures))
        return 1
    print("SELFTEST PASSED (11 cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
