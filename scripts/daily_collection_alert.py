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

import os
import subprocess
import sys
import threading
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


# ---------------------------------------------------------------------------
# OOM-INSTRUMENT — peak-memory sampling (stdlib only; NO new dependency).
#
# WHY: the cron died with "Out of memory (used over 2Gi)" on 07-25/26 and
# Render exposes no memory graph for cron services. The OOM-FIX cache bound
# removed ~205MB of transient peak — ~10% of the ceiling — so a large
# unmeasured baseline is still in play. This makes the run report its own
# high-water mark in the ONE notification that already exists.
#
# HOW, in preference order (first that works wins):
#   1. cgroup v2 ``memory.peak`` / v1 ``memory.max_usage_in_bytes`` — a
#      KERNEL-maintained high-water mark for the whole container. This is
#      exactly the number Render's OOM killer trips on, it needs no sampling
#      (so it cannot miss a spike between samples), and it includes every
#      descendant — Chromium included — by construction.
#   2. cgroup v2 ``memory.current``, sampled.
#   3. /proc RSS summed over the child AND its descendants, sampled.
# Nothing available (Windows dev boxes, a locked-down kernel) -> None, and
# the notification degrades silently to today's text.
#
# THERMOMETER, NOT A VALVE: the sampler is a daemon thread, every read is
# wrapped, and a failure yields None. It cannot block, slow, or fail the
# collection; the child is still Popen'd with output inherited and its exit
# code still propagates untouched.
# ---------------------------------------------------------------------------

# 15s = 4 samples/minute, ~280 samples over a 70-minute run. Each sample is
# one small file read (cgroup) or a /proc walk of a handful of pids; cost is
# microseconds of a daemon thread that is asleep >99.99% of the time.
MEM_SAMPLE_INTERVAL_SECONDS = 15.0

_CGROUP_PEAK_FILES = (
    "/sys/fs/cgroup/memory.peak",                        # cgroup v2
    "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",   # cgroup v1
)
_CGROUP_CURRENT_FILES = (
    "/sys/fs/cgroup/memory.current",                     # cgroup v2
    "/sys/fs/cgroup/memory/memory.usage_in_bytes",       # cgroup v1
)


def _read_int_file(path):
    """First integer in `path`, or None on ANY failure. Never raises."""
    try:
        with open(path, "r") as handle:
            return int(handle.read().split()[0])
    except Exception:
        return None


def _cgroup_bytes(candidates):
    for path in candidates:
        value = _read_int_file(path)
        if value is not None and value > 0:
            return value
    return None


def _proc_descendants(pid, proc_root="/proc"):
    """`pid` plus every descendant, read from /proc/*/stat PPid links.

    Walks the whole table once and builds the parent->children map, which
    works on kernels without CONFIG_PROC_CHILDREN. Returns [] on failure.
    """
    parents = {}
    try:
        for entry in os.listdir(proc_root):
            if not entry.isdigit():
                continue
            try:
                with open(os.path.join(proc_root, entry, "stat"), "r") as handle:
                    data = handle.read()
                # comm (field 2) may contain spaces/parens -> split after ')'.
                fields = data[data.rindex(")") + 1:].split()
                parents[int(entry)] = int(fields[1])  # field 4 = PPid
            except Exception:
                continue
    except Exception:
        return []

    tree = [int(pid)]
    seen = {int(pid)}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent in seen and child not in seen:
                seen.add(child)
                tree.append(child)
                changed = True
    return tree


try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except Exception:
    _PAGE_SIZE = 4096


def _proc_tree_rss_bytes(pid, proc_root="/proc"):
    """Summed RSS of `pid` + descendants, or None if nothing was readable.

    NOTE: summing RSS double-counts pages shared between processes (Chromium
    shares a great deal), so this OVER-estimates. It is the last resort
    precisely for that reason — the cgroup readers above are exact.
    """
    total = 0
    found = False
    for target in _proc_descendants(pid, proc_root=proc_root):
        try:
            # statm field 2 (index 1) is RSS in PAGES.
            with open(os.path.join(proc_root, str(target), "statm"), "r") as handle:
                pages = int(handle.read().split()[1])
        except Exception:
            continue
        total += pages * _PAGE_SIZE
        found = True
    return total if found else None


def sample_memory_bytes(pid, proc_root="/proc"):
    """One sample of whole-container memory, or None if unmeasurable."""
    value = _cgroup_bytes(_CGROUP_CURRENT_FILES)
    if value is not None:
        return value
    return _proc_tree_rss_bytes(pid, proc_root=proc_root)


class _PeakSampler(threading.Thread):
    """Daemon thread recording the high-water mark while the child runs."""

    def __init__(self, pid, interval=MEM_SAMPLE_INTERVAL_SECONDS,
                 sampler=sample_memory_bytes):
        super().__init__(daemon=True, name="oom-instrument")
        self._pid = pid
        self._interval = interval
        self._sampler = sampler
        self._stop = threading.Event()
        self.peak = None

    def _record(self):
        try:
            value = self._sampler(self._pid)
        except Exception:
            return
        if value is not None and (self.peak is None or value > self.peak):
            self.peak = value

    def run(self):
        # A sampler that raises on EVERY call must still not spin or escape:
        # _record swallows, and the loop is interval-paced regardless.
        self._record()
        while not self._stop.wait(self._interval):
            self._record()

    def stop(self):
        self._stop.set()
        # Bounded join only — a wedged sampler can never hold up the run
        # (daemon=True already guarantees it cannot hold up interpreter exit).
        try:
            self.join(timeout=2.0)
        except Exception:
            pass
        # The kernel's own high-water mark beats anything sampling saw: it
        # cannot miss a spike that happened between two samples.
        try:
            kernel_peak = _cgroup_bytes(_CGROUP_PEAK_FILES)
        except Exception:
            kernel_peak = None
        if kernel_peak is not None and (self.peak is None or kernel_peak > self.peak):
            self.peak = kernel_peak
        return self.peak


def format_bytes(value):
    """1234567890 -> '1.15GiB'. None -> None (caller omits the field)."""
    if value is None:
        return None
    try:
        size = float(value)
    except Exception:
        return None
    for unit in ("B", "KiB", "MiB"):
        if size < 1024.0:
            return "%.0f%s" % (size, unit)
        size /= 1024.0
    return "%.2fGiB" % size


def run_child(command=None, sampler_factory=None):
    """Run the collection unchanged, output inherited. Returns
    ``(exit_code, peak_bytes_or_None)`` — a child killed by signal N maps to
    128+N so Render sees non-zero.

    ``command`` / ``sampler_factory`` exist ONLY so --selftest can drive this
    exact path with a stub child; production calls it with no arguments.

    OOM-INSTRUMENT: subprocess.call is Popen(...).wait() with the pid thrown
    away; we keep the pid to sample it and are otherwise identical — same
    argv, same cwd, same inherited stdio, same return-code mapping.
    """
    sampler = None
    command = list(command or [sys.executable, "scheduler.py", "--once"])
    try:
        proc = subprocess.Popen(command, cwd=str(REPO_ROOT))
    except Exception as exc:
        print("[collection-alert] could not start scheduler: %s"
              % type(exc).__name__)
        return 1, None
    try:
        factory = sampler_factory or _PeakSampler
        sampler = factory(proc.pid)
        sampler.start()
    except Exception as exc:
        # Measurement is optional; collection is not.
        print("[collection-alert] memory sampler unavailable: %s"
              % type(exc).__name__)
        sampler = None
    rc = proc.wait()
    peak = None
    if sampler is not None:
        try:
            peak = sampler.stop()
        except Exception as exc:
            print("[collection-alert] memory sampler stop raised %s — ignored"
                  % type(exc).__name__)
    return (rc if rc >= 0 else 128 + abs(rc)), peak


def run(child_runner, max_id_reader, notifier, briefing_reader=None):
    """Orchestrate one wrapped run. Pure enough to selftest with fakes.
    Returns the exit code to pass through (ALWAYS the child's).
    briefing_reader (SILENT-FAILURE-FLAG, optional): min_id -> (error_rows,
    keyed_rows) or None; when MOST keyed new rows report a failed
    policy-briefing lookup, the ONE existing notification carries it —
    no second alerting path, exit code untouched."""
    started = time.time()
    before = max_id_reader()
    child_result = child_runner()
    # OOM-INSTRUMENT: run_child now returns (rc, peak_bytes). A plain int is
    # still accepted so every existing caller/fake keeps working and an
    # unmeasured run is indistinguishable from today's behaviour.
    if isinstance(child_result, tuple):
        rc, peak_bytes = child_result
    else:
        rc, peak_bytes = child_result, None
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

    # OOM-INSTRUMENT: the peak rides in the ONE existing message, next to the
    # row count. Unmeasurable (no cgroup/proc, sampler failure) -> the field
    # is simply absent and the text is byte-identical to today's.
    peak_text = format_bytes(peak_bytes)
    if peak_text:
        message += " · 최대 메모리 %s" % peak_text

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

    # ----------------------------------------------------------------
    # OOM-INSTRUMENT cases.
    # ----------------------------------------------------------------

    # 7. A normal run reports the peak alongside the row count.
    sent.clear()
    rc = run(lambda: (0, 1503238553), fake_reader([100, 103]), notifier)
    check("7 rc 0", rc == 0)
    check("7 peak in message", sent and "최대 메모리 1.40GiB" in sent[0][1])
    check("7 count still there", sent and "신규 3건" in sent[0][1])

    # 8. Sampling unavailable -> silent degrade to today's exact text.
    sent.clear()
    run(lambda: (0, None), fake_reader([100, 103]), notifier)
    check("8 no memory field", sent and "최대 메모리" not in sent[0][1])
    check("8 text unchanged", sent
          and sent[0][1] == "신규 3건 · 0분 · scheduler.py --once rc=0")

    # 8b. A bare int (pre-instrument contract) still works.
    sent.clear()
    rc = run(lambda: 0, fake_reader([100, 103]), notifier)
    check("8b int contract", rc == 0 and "최대 메모리" not in sent[0][1])

    # 9. Child FAILS but the peak is still reported — the OOM case: rc!=0 is
    # exactly when the number matters most.
    sent.clear()
    rc = run(lambda: (9, 2147483648), fake_reader([100, 101]), notifier)
    check("9 rc passthrough", rc == 9)
    check("9 priority high", sent and sent[0][2] == "high")
    check("9 peak reported on failure", sent and "최대 메모리 2.00GiB" in sent[0][1])

    # 10. A sampler that RAISES on every call must not affect the exit code
    # and must not escape — the thermometer never becomes a valve.
    def exploding_sampler(pid):
        raise RuntimeError("no /proc here")

    sampler = _PeakSampler(os.getpid(), interval=0.01,
                           sampler=exploding_sampler)
    sampler.start()
    time.sleep(0.05)
    check("10 raising sampler -> None", sampler.stop() is None
          or isinstance(sampler.peak, int))
    check("10 thread died cleanly", not sampler.is_alive())

    # 10b. Constructor/thread failure inside run_child degrades, keeps rc.
    class BrokenSampler:
        def __init__(self, pid):
            raise OSError("cannot start thread")

    rc, peak = run_child(
        command=[sys.executable, "-c", "raise SystemExit(3)"],
        sampler_factory=BrokenSampler)
    check("10b rc survives broken sampler", rc == 3)
    check("10b peak None", peak is None)

    # 11. Descendants are INCLUDED: a synthetic /proc with python -> scheduler
    # -> chromium proves the walk follows PPid links, not just the direct
    # child. Measuring only the parent would report 10 pages of a 130-page
    # tree — a reassuring wrong number.
    import shutil
    import tempfile

    proc_root = tempfile.mkdtemp()
    try:
        # comm values that BREAK naive str.split() parsing: real /proc comm
        # is arbitrary text in parens (Chromium renderers look like this).
        comms = {100: "python3", 200: "python3",
                 300: "chrome (renderer) x", 400: "unrelated"}
        for pid, ppid, rss_pages in ((100, 1, 10), (200, 100, 20),
                                     (300, 200, 100), (400, 1, 999)):
            d = os.path.join(proc_root, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "stat"), "w") as fh:
                fh.write("%d (%s) S %d 1 1 0 -1 0 0 0\n"
                         % (pid, comms[pid], ppid))
            with open(os.path.join(d, "statm"), "w") as fh:
                fh.write("%d %d 0 0 0 0 0\n" % (rss_pages * 2, rss_pages))

        tree = _proc_descendants(100, proc_root=proc_root)
        check("11 grandchild found", set(tree) == {100, 200, 300})
        check("11 unrelated pid excluded", 400 not in tree)
        total = _proc_tree_rss_bytes(100, proc_root=proc_root)
        check("11 tree rss summed", total == 130 * _PAGE_SIZE)
        check("11 parent alone would undercount",
              _proc_tree_rss_bytes(300, proc_root=proc_root) == 100 * _PAGE_SIZE)
        check("11 missing proc -> None",
              _proc_tree_rss_bytes(100, proc_root=os.path.join(
                  proc_root, "nope")) is None)
    finally:
        shutil.rmtree(proc_root, ignore_errors=True)

    # 12. REAL stub child allocating a KNOWN amount, driven through the exact
    # production path (run_child -> Popen -> _PeakSampler). Confirms the
    # number means something rather than merely existing.
    alloc_mb = 180
    stub = (
        "import time;"
        "buf = bytearray(%d*1024*1024);"
        "buf[::4096] = b'x'*len(buf[::4096]);"
        "time.sleep(1.2)" % alloc_mb
    )
    started_at = time.time()
    rc, peak = run_child(command=[sys.executable, "-c", stub])
    elapsed = time.time() - started_at
    check("12 stub child rc 0", rc == 0)
    check("12 sampler did not delay the child", elapsed < 8.0)
    if sample_memory_bytes(os.getpid()) is None:
        # No cgroup and no /proc (Windows dev box): the documented silent
        # degrade. Nothing to range-check, and that is a PASS.
        print("[selftest] sampling unavailable on %s — degrade path exercised"
              % sys.platform)
        check("12 unavailable -> None", peak is None)
    else:
        low, high = alloc_mb * 1024 * 1024, 8 * 1024 * 1024 * 1024
        print("[selftest] stub allocated %dMiB, sampler peaked at %s"
              % (alloc_mb, format_bytes(peak)))
        check("12 peak measured", peak is not None)
        check("12 peak in range", peak is not None and low <= peak <= high)

    # 12b. PEAK semantics: the reported number must be the high-water mark,
    # not the first or last sample. A run that transiently touches 1.9GiB and
    # settles back at 300MiB must report 1.9GiB — that transient IS the OOM.
    ramp = [100 * 1024 ** 2, 1900 * 1024 ** 2, 300 * 1024 ** 2]
    state = {"i": 0}

    def ramping_sampler(pid):
        value = ramp[min(state["i"], len(ramp) - 1)]
        state["i"] += 1
        return value

    ramp_sampler = _PeakSampler(os.getpid(), interval=0.01,
                                sampler=ramping_sampler)
    ramp_sampler.start()
    time.sleep(0.2)
    ramp_sampler._stop.set()
    ramp_sampler.join(timeout=2.0)
    check("12b peak is the maximum", ramp_sampler.peak == 1900 * 1024 ** 2)
    check("12b not the last sample", ramp_sampler.peak != 300 * 1024 ** 2)

    # 12c. cgroup readers: real file IO, known value, and a bad path is None.
    cg_dir = tempfile.mkdtemp()
    try:
        good = os.path.join(cg_dir, "memory.current")
        with open(good, "w") as fh:
            fh.write("1610612736\n")
        check("12c cgroup value read", _cgroup_bytes([good]) == 1610612736)
        check("12c missing file -> None",
              _cgroup_bytes([os.path.join(cg_dir, "nope")]) is None)
        junk = os.path.join(cg_dir, "junk")
        with open(junk, "w") as fh:
            fh.write("max\n")
        check("12c junk -> None", _cgroup_bytes([junk]) is None)
        check("12c first readable wins",
              _cgroup_bytes([junk, good]) == 1610612736)
    finally:
        shutil.rmtree(cg_dir, ignore_errors=True)

    # 13. format_bytes never raises and omits the field on junk.
    check("13 none", format_bytes(None) is None)
    check("13 junk", format_bytes("abc") is None)
    check("13 mib", format_bytes(5 * 1024 * 1024) == "5MiB")
    check("13 gib", format_bytes(2 * 1024 ** 3) == "2.00GiB")

    if failures:
        print("SELFTEST FAILED: " + ", ".join(failures))
        return 1
    print("SELFTEST PASSED (22 cases, 54 assertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
