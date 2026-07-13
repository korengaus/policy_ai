# SPINE-A1a — weekly automation spine: run the track-record chain unattended
# in HARD order embed -> build -> snapshot -> report -> prediction-log
# (steps 2..6; NO ingest/backfill step 1). STOP-ON-FAILURE: a non-zero exit or exception in any step
# aborts the rest, because every later step reads what an earlier step wrote
# (snapshot & report SELECT the NEWEST brainmap_graph, so they MUST run after
# build). A DB-size precheck skips the whole run (fail-SAFE) when Postgres is
# near full — better to skip than fill the disk mid-run (the 7/7 DB-full
# lesson). ntfy hooks announce overall success / any-step failure.
#
# RESTART-SAFE: the spine holds NO state. Every child is idempotent —
# embed_backfill skips cache-hits, build_brainmap_graph INSERTs a fresh row
# (old rows are free history), snapshot dedups on (snapshot_date, graph_ref),
# generate_weekly_report skips an existing week_start — so a mid-run Worker
# restart is recovered by simply rerunning the spine. No step double-writes
# destructively on rerun.
#
# ORCHESTRATION ONLY — raises NO verdict. It shells out to four verdict-free
# scripts/*; truth_claim / verdict_label / policy_alert_level are never
# touched here. pin-OUT (scripts/*, no log-site edits) — 331/16 unaffected.
#
# USAGE (operator / future Render Cron — DATABASE_URL at the external
# Postgres, USE_POSTGRES_WRITE=true for a REAL run):
#   python scripts/weekly_spine.py --selftest              # pure offline, no DB
#   python scripts/weekly_spine.py --dry-run --mode weekly # no writes; reports DB size
#   python scripts/weekly_spine.py --mode weekly           # REAL chain (needs USE_POSTGRES_WRITE=true)
#   python scripts/weekly_spine.py --mode weekly --week-start 2026-07-06 --week-end 2026-07-12
#
# ENV (all optional, safe fallbacks):
#   DB_PLAN_SIZE_BYTES     Render Postgres plan size in bytes (precheck cap base).
#   DB_SIZE_SKIP_FRACTION  Skip when size/plan >= this fraction (default 0.90).
#   NTFY_URL               Full ntfy endpoint to POST to (highest priority).
#   NTFY_TOPIC             ntfy.sh topic name (POSTs to https://ntfy.sh/<topic>).
#                          If neither NTFY_* is set, notifications PRINT instead.
#
# SAFETY: no requirements.txt / render.yaml change (numpy + Render Cron are
# A1b). stdlib + psycopg only (the same driver the children use). Never
# prints DATABASE_URL or any API key; never hardcodes an ntfy topic; never
# crashes when an env var is unset; fail-CLOSED (the children refuse to write
# without USE_POSTGRES_WRITE=true) and fail-SAFE (skip when DB near full).

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunables (top-of-file, commented).
# ---------------------------------------------------------------------------
# DB-size precheck cap base. Render Postgres plan size in BYTES. There is NO
# balance/quota API, so the spine compares pg_database_size against this. The
# default below is a PLACEHOLDER (10 GiB) — set DB_PLAN_SIZE_BYTES to the REAL
# plan size on the Worker/Cron (A1b) so the guard is neither useless (cap too
# high -> never skips -> disk fills) nor trigger-happy (cap too low -> always
# skips -> chain never runs). When the default is in use the precheck says so.
DEFAULT_DB_PLAN_SIZE_BYTES = 10 * 1024 ** 3
# Skip the run when used fraction >= this. Fail-SAFE margin below 100%.
DEFAULT_DB_SIZE_SKIP_FRACTION = 0.90

# The five children, in the ONE legal order. embed/build take only the mode
# flags; report also forwards the optional window flags. NO step 1 (ingest/
# backfill). prediction_log_weekly (B4 Phase 2b) is deliberately LAST: it
# consumes the snapshot batches + graph, and a track-record logging failure
# must never abort the already-completed user-facing report.
_CHILDREN = ("embed_backfill.py", "build_brainmap_graph.py",
             "snapshot_brainmap_growth.py", "generate_weekly_report.py",
             "prediction_log_weekly.py")


def normalize_db_url(raw_url):
    """The children's exact idiom: SQLAlchemy-style -> libpq DSN."""
    return (raw_url.replace("postgresql+psycopg://", "postgresql://")
                   .replace("postgresql+psycopg2://", "postgresql://"))


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable — no DB, no subprocess, no network).
# ---------------------------------------------------------------------------
def should_skip_for_size(size_bytes, plan_size_bytes, fraction):
    """Fail-SAFE precheck decision. True => DB near full, SKIP the run.

    Defensive: a non-positive/unknown plan size can NEVER force a skip
    (returns False) — we don't abort a run on a bad cap; we only abort when
    we positively know the DB is near a real, positive plan size."""
    if not plan_size_bytes or plan_size_bytes <= 0:
        return False
    if size_bytes is None or size_bytes < 0:
        return False
    return (size_bytes / plan_size_bytes) >= fraction


def _human_bytes(n):
    if n is None:
        return "unknown"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return "%.1f %s" % (value, unit)
        value /= 1024.0


def build_child_argv(script_name, dry_run, selftest, week_start=None,
                     week_end=None, top_n=None):
    """The EXACT flags each child accepts (confirmed in Phase 1). --selftest
    and --dry-run are mutually exclusive at the spine level; only the report
    child takes the window/top-n pass-throughs."""
    argv = [sys.executable, str(_SCRIPTS_DIR / script_name)]
    if selftest:
        argv.append("--selftest")
        return argv
    if dry_run:
        argv.append("--dry-run")
    if script_name == "generate_weekly_report.py":
        if week_start:
            argv += ["--week-start", week_start]
        if week_end:
            argv += ["--week-end", week_end]
        if top_n is not None:
            argv += ["--top-n", str(top_n)]
    return argv


def summarize_results(results):
    """One line per step for the success notify. `results` is the list of
    per-step dicts from run_chain. Uses the child's last non-empty stdout
    line as its 'step-done marker' — robust without brittle per-child
    parsing."""
    lines = []
    for r in results:
        marker = (r.get("tail") or "").strip() or "(no output)"
        lines.append("%d. %s: rc=%d (%.1fs) — %s"
                     % (r["step"], r["label"], r["rc"], r["seconds"],
                        marker[:160]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ntfy — env-driven, PRINT fallback, never crashes, never hardcodes a topic.
# ---------------------------------------------------------------------------
def _ntfy_endpoint():
    url = (os.environ.get("NTFY_URL") or "").strip()
    if url:
        return url
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if topic:
        return "https://ntfy.sh/%s" % topic
    return None


def notify(title, message, priority="default"):
    """Send an ntfy notification if NTFY_URL / NTFY_TOPIC is set, else PRINT.
    Best-effort: any send failure degrades to a printed warning — a
    notification problem must NEVER change the run's exit code."""
    endpoint = _ntfy_endpoint()
    banner = "[notify] %s\n%s" % (title, message)
    if not endpoint:
        print(banner)
        print("[notify] (NTFY_URL/NTFY_TOPIC unset — printed above instead of sent)")
        return False
    try:
        req = urllib.request.Request(
            endpoint,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("[notify] sent to %s: %s" % (endpoint, title))
        return True
    except Exception as exc:  # noqa: BLE001 — notify must never crash the run
        print(banner)
        print("[notify] send failed (%s) — printed above instead."
              % type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# DB-size precheck.
# ---------------------------------------------------------------------------
def read_db_size_bytes(db_url):
    """SELECT pg_database_size(current_database()). Returns int bytes, or
    None if the DB can't be reached (caller decides — a read failure never
    forces a skip; the children fail-close on their own)."""
    import psycopg  # lazy — importing this module must not connect
    try:
        with psycopg.connect(normalize_db_url(db_url), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database())")
                row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception as exc:  # noqa: BLE001
        print("[precheck] could not read DB size (%s) — continuing; the "
              "children fail-close on their own env guards." % type(exc).__name__)
        return None


def db_precheck(dry_run):
    """Returns (skip: bool, reason: str). SKIP only in a REAL run when the DB
    is positively near a real plan size. In --dry-run: report, never skip."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[precheck] DATABASE_URL unset — skipping size precheck "
              "(the children will fail-close).")
        return False, "DATABASE_URL unset"

    plan_env = (os.environ.get("DB_PLAN_SIZE_BYTES") or "").strip()
    try:
        plan_size = int(plan_env) if plan_env else DEFAULT_DB_PLAN_SIZE_BYTES
    except ValueError:
        plan_size = DEFAULT_DB_PLAN_SIZE_BYTES
    using_default = not plan_env
    try:
        fraction = float((os.environ.get("DB_SIZE_SKIP_FRACTION") or "").strip()
                         or DEFAULT_DB_SIZE_SKIP_FRACTION)
    except ValueError:
        fraction = DEFAULT_DB_SIZE_SKIP_FRACTION

    size = read_db_size_bytes(db_url)
    pct = ("%.1f%%" % (100.0 * size / plan_size)
           if size is not None and plan_size else "unknown")
    print("[precheck] db_size=%s plan=%s (%s of plan) skip_at>=%.0f%%%s"
          % (_human_bytes(size), _human_bytes(plan_size), pct, fraction * 100,
             "  [plan=DEFAULT — set DB_PLAN_SIZE_BYTES]" if using_default else ""))

    if should_skip_for_size(size, plan_size, fraction):
        reason = ("DB near full: %s / %s (>= %.0f%%)"
                  % (_human_bytes(size), _human_bytes(plan_size), fraction * 100))
        if dry_run:
            print("[precheck] DRY-RUN — would SKIP (%s), but continuing to "
                  "report only." % reason)
            return False, reason
        return True, reason
    return False, "db size ok"


# ---------------------------------------------------------------------------
# Chain runner (child invocation injectable for the offline selftest).
# ---------------------------------------------------------------------------
def _subprocess_runner(argv):
    """Default child runner: run the child, echo its output live-ish, and
    return (rc, last_nonempty_stdout_line). Captures so the notify summary
    can carry a per-step marker; also prints so the operator sees it."""
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n")
    tail = ""
    for line in reversed((proc.stdout or "").splitlines()):
        if line.strip():
            tail = line.strip()
            break
    return proc.returncode, tail


def run_chain(steps, runner):
    """Run the ordered steps STOP-ON-FAILURE. `steps` is a list of
    (step_no, label, argv). Returns (ok, results, failed_step_or_None).
    A non-zero rc OR a runner exception aborts the remaining steps."""
    results = []
    for step_no, label, argv in steps:
        print("\n=== [spine] step %d/%d START — %s ==="
              % (step_no, len(steps), label))
        start = time.time()
        try:
            rc, tail = runner(argv)
        except Exception as exc:  # noqa: BLE001 — a crashing child aborts the chain
            seconds = time.time() - start
            results.append({"step": step_no, "label": label, "rc": 1,
                            "seconds": seconds,
                            "tail": "runner raised %s: %s"
                                    % (type(exc).__name__, exc)})
            print("=== [spine] step %d %s CRASHED after %.1fs: %s ==="
                  % (step_no, label, seconds, exc))
            return False, results, {"step": step_no, "label": label}
        seconds = time.time() - start
        results.append({"step": step_no, "label": label, "rc": rc,
                        "seconds": seconds, "tail": tail})
        print("=== [spine] step %d %s DONE rc=%d (%.1fs) ==="
              % (step_no, label, rc, seconds))
        if rc != 0:
            return False, results, {"step": step_no, "label": label}
    return True, results, None


def _plan_steps(dry_run, selftest, week_start, week_end, top_n):
    labels = ("embed_backfill", "build_brainmap_graph",
              "snapshot_brainmap_growth", "generate_weekly_report",
              "prediction_log_weekly")
    steps = []
    for i, (script_name, label) in enumerate(zip(_CHILDREN, labels), start=1):
        argv = build_child_argv(script_name, dry_run, selftest,
                                week_start, week_end, top_n)
        steps.append((i, label, argv))
    return steps


def run_weekly(dry_run, week_start=None, week_end=None, top_n=None):
    """The --mode weekly orchestration: precheck -> chain -> notify. Returns
    a process exit code (0 ok, non-zero on skip/failure)."""
    mode_tag = "DRY-RUN" if dry_run else "REAL"
    print("WEEKLY-SPINE — mode=weekly (%s): embed -> build -> snapshot -> "
          "report -> prediction-log" % mode_tag)

    skip, reason = db_precheck(dry_run)
    if skip:
        notify("weekly-spine SKIPPED",
               "DB-size precheck skipped the run.\n%s" % reason,
               priority="high")
        print("[spine] SKIPPED by precheck: %s" % reason)
        return 3  # distinct exit code: skipped (not a chain failure)

    steps = _plan_steps(dry_run, selftest=False, week_start=week_start,
                        week_end=week_end, top_n=top_n)
    ok, results, failed = run_chain(steps, _subprocess_runner)
    summary = summarize_results(results)

    print("\n===== [spine] OVERALL %s =====" % ("PASS" if ok else "FAIL"))
    print(summary)
    if ok:
        notify("weekly-spine OK (%s)" % mode_tag,
               "All %d steps passed.\n%s" % (len(results), summary))
        return 0
    notify("weekly-spine FAILED (%s)" % mode_tag,
           "Step %d (%s) failed — chain aborted.\n%s"
           % (failed["step"], failed["label"], summary),
           priority="high")
    return 1


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — two parts, both pure-offline (no DB, no network):
#   (A) the spine's OWN logic via a FAKE child runner (stop-on-failure,
#       notify wiring, precheck math, exit codes, simulated child failure);
#   (B) delegate each real child's --selftest via subprocess (children's
#       selftests are offline by construction).
# ---------------------------------------------------------------------------
def _selftest_logic():
    print("=== WEEKLY-SPINE --selftest part A (spine logic; fake runner) ===")
    failures = []

    def check(name, ok):
        print("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    # (a) precheck math: skip only above the fraction, with a positive plan.
    check("should_skip True at 95% of a real plan",
          should_skip_for_size(95, 100, 0.90) is True)
    check("should_skip False at 80%",
          should_skip_for_size(80, 100, 0.90) is False)
    check("unknown/zero plan never forces a skip",
          should_skip_for_size(999, 0, 0.90) is False
          and should_skip_for_size(None, 100, 0.90) is False)

    # (b) argv construction: selftest short-circuits; only report gets window.
    embed_argv = build_child_argv("embed_backfill.py", False, True)
    check("selftest argv passes --selftest, no window flags",
          embed_argv[-1] == "--selftest")
    rep_argv = build_child_argv("generate_weekly_report.py", True, False,
                                week_start="2026-07-06", top_n=5)
    check("dry-run report argv carries --dry-run + window/top-n",
          "--dry-run" in rep_argv and "--week-start" in rep_argv
          and "2026-07-06" in rep_argv and "--top-n" in rep_argv
          and "5" in rep_argv)
    embed_dry = build_child_argv("embed_backfill.py", True, False,
                                 week_start="x")
    check("non-report child ignores window flags",
          "--week-start" not in embed_dry and "--dry-run" in embed_dry)

    # (c) stop-on-failure: a failing step 2 aborts 3 & 4; success runs all.
    fake_steps = [(1, "s1", ["a"]), (2, "s2", ["b"]),
                  (3, "s3", ["c"]), (4, "s4", ["d"])]

    def ok_runner(argv):
        return 0, "done %s" % argv[0]

    def fail_at_2(argv):
        return (0 if argv[0] != "b" else 7), "ran %s" % argv[0]

    ok_all, res_all, failed_all = run_chain(fake_steps, ok_runner)
    check("all-pass chain runs 4 steps, ok, no failed step",
          ok_all and len(res_all) == 4 and failed_all is None)
    ok2, res2, failed2 = run_chain(fake_steps, fail_at_2)
    check("failure at step 2 aborts (only 2 steps ran) and names it",
          (not ok2) and len(res2) == 2 and failed2["step"] == 2)

    # (d) a crashing child is caught, aborts, and is reported (not re-raised).
    def crash_runner(argv):
        raise RuntimeError("boom")

    okc, resc, failedc = run_chain([(1, "s1", ["a"])], crash_runner)
    check("crashing child caught -> chain fails, step named",
          (not okc) and failedc["step"] == 1 and "boom" in resc[0]["tail"])

    # (e) notify never crashes and falls back to print when unset. Force the
    #     env clear for this assertion so a locally-set NTFY_* can't sway it.
    saved = {k: os.environ.pop(k, None) for k in ("NTFY_URL", "NTFY_TOPIC")}
    try:
        sent = notify("t", "m")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    check("notify returns False (printed) when NTFY_* unset, no crash",
          sent is False)

    # (f) summary renders one line per step with the tail marker.
    summary = summarize_results(res_all)
    check("summary has one line per step",
          summary.count("\n") == 3 and "1. s1" in summary)

    print("[selftest A] %s"
          % ("PASS" if not failures else "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def _selftest_children():
    print("\n=== WEEKLY-SPINE --selftest part B (delegate children --selftest) ===")
    failures = []
    for script_name in _CHILDREN:
        argv = build_child_argv(script_name, False, True)
        print("\n--- %s --selftest ---" % script_name)
        rc, tail = _subprocess_runner(argv)
        if rc != 0:
            failures.append("%s (rc=%d)" % (script_name, rc))
    print("\n[selftest B] %s"
          % ("PASS" if not failures else "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def run_selftest():
    a = _selftest_logic()
    b = _selftest_children()
    ok = (a == 0 and b == 0)
    print("\nSELFTEST: %s (spine-logic %s, children %s)"
          % ("PASS" if ok else "FAIL",
             "PASS" if a == 0 else "FAIL", "PASS" if b == 0 else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="weekly_spine",
        description="Weekly automation spine: run embed -> build -> snapshot "
                    "-> report -> prediction-log in hard order, "
                    "stop-on-failure, with a DB-size precheck and ntfy "
                    "success/failure hooks. Orchestration only — raises no "
                    "verdict.",
    )
    parser.add_argument("--mode", choices=["weekly"], default="weekly",
                        help="Chain to run. Only 'weekly' (steps 2..5, NO "
                             "ingest/backfill) exists today.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Delegate each child's --dry-run (no writes); "
                             "the precheck reports size but never skips.")
    parser.add_argument("--selftest", action="store_true",
                        help="Pure-offline logic check + delegate each child's "
                             "--selftest. No DB, no network.")
    # Trivially-forwarded pass-throughs — the report child is the only one that
    # accepts a window; unset => it defaults to the trailing 7 days.
    parser.add_argument("--week-start", default=None,
                        help="YYYY-MM-DD forwarded to generate_weekly_report.")
    parser.add_argument("--week-end", default=None,
                        help="YYYY-MM-DD forwarded to generate_weekly_report.")
    parser.add_argument("--top-n", type=int, default=None,
                        help="Forwarded to generate_weekly_report (--top-n).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    return run_weekly(args.dry_run, week_start=args.week_start,
                      week_end=args.week_end, top_n=args.top_n)


if __name__ == "__main__":
    sys.exit(main())
