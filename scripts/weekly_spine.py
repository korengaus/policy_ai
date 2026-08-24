# SPINE-A1a — weekly automation spine: run the track-record chain unattended
# in HARD order embed -> build -> snapshot -> report -> prediction-log
# -> topic-alerts (steps 2..7; NO ingest/backfill step 1). STOP-ON-FAILURE: a non-zero exit or exception in any step
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
import base64
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

# STREAM-GUARD: stdout has been guarded here since the spine was written;
# stderr is now guarded the same way. Both matter — a cp949 console cannot
# encode U+2014 and this file carries em-dashes in 9 print sites, and an
# exception out of print() inside a cron chain can take the run with it.
# Unchanged in behaviour for stdout; nothing about any message changes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
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

# The six children, in the ONE legal order. embed/build take only the mode
# flags; report also forwards the optional window flags. NO step 1 (ingest/
# backfill). prediction_log_weekly (B4 Phase 2b) and queue_topic_alerts
# (TOPIC-ALERT 2b) are deliberately LAST, in that order: both consume the
# fresh snapshot batches + graph, and a track-record-logging or alert
# failure must never abort the already-completed user-facing report —
# the same positional failure-isolation contract (STOP-ON-FAILURE means a
# last-step failure can no longer touch the earlier steps' output).
# queue_topic_alerts exits 0 on <2 snapshot batches, so it is safe here
# from the very first run.
_CHILDREN = ("embed_backfill.py", "build_brainmap_graph.py",
             "snapshot_brainmap_growth.py", "generate_weekly_report.py",
             "prediction_log_weekly.py", "queue_topic_alerts.py")


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


# SPINE-SUMMARY-LINEAGE (109-APPLY): the summary used to carry ONLY each
# child's last stdout line, and the builder's last line is the row-written
# confirmation — so the lineage numbers (carried/minted/merged_away, printed
# one line earlier) never reached the notification, and the operator read
# them from the daily-graph cron's no-op rebuild instead. These prefixes are
# the EXACT literals the builder prints (build_brainmap_graph.py:1262); a
# matching line is appended to that step's marker. Selection is by prefix on
# the full captured stdout, not by position, so reordering the builder's
# output cannot drop it silently.
KEY_LINE_PREFIXES = ("[brainmap] lineage:",)
# The one step expected to print a lineage line; its ABSENCE is reported,
# never silently dropped — a summary that quietly loses a line again is the
# defect this fixes.
_LINEAGE_STEP_LABEL = "build_brainmap_graph"
_LINEAGE_MISSING_NOTE = "계보 라인 없음 — 빌더 출력 형식 변경 여부 확인 필요"


def _tail_with_key_lines(stdout):
    """Step marker: the last non-empty stdout line, plus any KEY_LINE_PREFIXES
    line printed earlier (latest occurrence, ' · '-joined). Pure — selftested."""
    lines = [line.strip() for line in (stdout or "").splitlines()
             if line.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    for prefix in KEY_LINE_PREFIXES:
        if tail.startswith(prefix):
            continue  # the key line IS the last line — never duplicate it
        for line in reversed(lines[:-1]):
            if line.startswith(prefix):
                tail = "%s · %s" % (tail, line)
                break
    return tail


def summarize_results(results):
    """One line per step for the success notify. `results` is the list of
    per-step dicts from run_chain. Uses the child's last non-empty stdout
    line as its 'step-done marker' (plus any KEY_LINE_PREFIXES line the
    runner surfaced) — robust without brittle per-child parsing. For the
    build step specifically, a SUCCESSFUL run whose marker carries no
    lineage line gets an explicit says-so note instead of silence."""
    lines = []
    for r in results:
        marker = (r.get("tail") or "").strip() or "(no output)"
        if (r.get("label") == _LINEAGE_STEP_LABEL and r.get("rc") == 0
                and KEY_LINE_PREFIXES[0] not in marker):
            marker = "%s · %s" % (marker, _LINEAGE_MISSING_NOTE)
        lines.append("%d. %s: rc=%d (%.1fs) — %s"
                     % (r["step"], r["label"], r["rc"], r["seconds"],
                        marker[:200]))
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


def _header_title(title):
    """ALERT-FIX: HTTP headers are latin-1 ONLY, so a Korean Title header made
    urllib raise UnicodeEncodeError and the send failed every time — the daily
    collection alert (title "일일 수집 완료 — 신규 N건") therefore NEVER reached
    the operator, while the weekly spine's ASCII titles sent fine. Its absence
    was being read as "the container died".

    Fix: RFC 2047 encoded-word (=?UTF-8?B?<base64>?=), which ntfy decodes back
    to the original text. Chosen over ntfy's JSON-body publishing because that
    form POSTs the TOPIC in the body to the server ROOT, so it would have to
    re-derive a base URL from NTFY_URL (which points at the topic, and may be a
    self-hosted path) — a bigger, riskier change to a path the weekly spine
    depends on. This keeps the endpoint, method and body byte-identical.

    ASCII titles are returned UNCHANGED, so every request the weekly spine
    makes today is byte-for-byte what it was before this fix."""
    try:
        title.encode("ascii")
        return title
    except UnicodeEncodeError:
        # One single encoded-word (never folded): a header must stay on one
        # line, so this deliberately does not use email.header's line wrapping.
        blob = base64.b64encode(title.encode("utf-8")).decode("ascii")
        return "=?UTF-8?B?%s?=" % blob


# NOTIFY-RETRY (2026-08-04) — one attempt used to be the whole delivery
# guarantee. On 08-02 the daily-graph run finished correctly (both children
# rc=0, vectors missing 0, merged_away 0) and the operator got nothing: the log
# ended at "[notify] send failed (URLError)". The same topic delivered the
# collection alert that morning and delivery resumed by itself the next day, so
# ntfy was fine — a single packet was lost and nothing tried again. A channel
# whose silence carries no information is not a monitoring channel, and this is
# the device built to catch silent failure failing silently.
#
# BOUNDED, deliberately: 3 attempts, 10s per request (the timeout was already
# explicit — it is preserved, not introduced), waits of 1s and 2s between them.
# WORST CASE 3x10 + 1 + 2 = 33s, i.e. +23s over the old single attempt, and only
# when every attempt fails. A successful first attempt is byte-identical to
# before: one request, one log line, no sleep.
NOTIFY_ATTEMPTS = 3
NOTIFY_TIMEOUT_S = 10
NOTIFY_BACKOFF_S = (1.0, 2.0)   # between attempts; len == NOTIFY_ATTEMPTS - 1


def _notify_error_label(exc):
    """'HTTPError 503' when the failure carries a status, else 'URLError'.
    A code-less failure prints exactly what it printed before this change, so
    the 08-02 log line ("send failed (URLError)") still reads the same."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return "%s %d" % (type(exc).__name__, code)
    return type(exc).__name__


def _notify_retryable(exc):
    """True when a second attempt could plausibly succeed.

    urllib DOES surface enough to tell these apart: a non-2xx response raises
    HTTPError (a URLError subclass) carrying ``.code``, while a transport
    problem — DNS, connect refused, reset, read timeout — raises a bare
    URLError/OSError with no code at all. So a 4xx is a rejection that will be
    rejected identically the next two times and is NOT retried; a 5xx and every
    code-less transport failure are."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 400 <= code < 500:
        return False
    return True


def notify(title, message, priority="default"):
    """Send an ntfy notification if NTFY_URL / NTFY_TOPIC is set, else PRINT.
    Best-effort: any send failure degrades to a printed warning — a
    notification problem must NEVER change the run's exit code.

    Retries per NOTIFY-RETRY above. The three log states the 08-02 diagnosis
    depended on are unchanged in kind — unset config, send failed, sent — and
    the failure line now states how many attempts were made."""
    endpoint = _ntfy_endpoint()
    banner = "[notify] %s\n%s" % (title, message)
    if not endpoint:
        print(banner)
        print("[notify] (NTFY_URL/NTFY_TOPIC unset — printed above instead of sent)")
        return False

    last_exc = None
    attempts = 0
    for attempt in range(1, NOTIFY_ATTEMPTS + 1):
        attempts = attempt
        try:
            req = urllib.request.Request(
                endpoint,
                data=message.encode("utf-8"),
                headers={"Title": _header_title(title), "Priority": priority},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT_S).read()
            if attempt == 1:
                print("[notify] sent to %s: %s" % (endpoint, title))
            else:
                print("[notify] sent to %s: %s (attempt %d/%d)"
                      % (endpoint, title, attempt, NOTIFY_ATTEMPTS))
            return True
        except Exception as exc:  # noqa: BLE001 — notify must never crash the run
            last_exc = exc
            if attempt >= NOTIFY_ATTEMPTS or not _notify_retryable(exc):
                break
            wait = NOTIFY_BACKOFF_S[attempt - 1]
            print("[notify] attempt %d/%d failed (%s) — retrying in %.0fs"
                  % (attempt, NOTIFY_ATTEMPTS, _notify_error_label(exc), wait))
            time.sleep(wait)

    detail = _notify_error_label(last_exc)
    if not _notify_retryable(last_exc):
        detail += ", not retried — a 4xx is a rejection, not a hiccup"
    print(banner)
    print("[notify] send failed after %d attempt(s) (%s) — printed above "
          "instead." % (attempts, detail))
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
    return (rc, marker) where marker is the last non-empty stdout line plus
    any KEY_LINE_PREFIXES line (the builder's lineage numbers). Captures so
    the notify summary can carry a per-step marker; also prints so the
    operator sees it."""
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n")
    # SPINE-SUMMARY-LINEAGE: last line plus any key line (lineage) — see
    # _tail_with_key_lines.
    return proc.returncode, _tail_with_key_lines(proc.stdout)


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
              "prediction_log_weekly", "queue_topic_alerts")
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
          "report -> prediction-log -> topic-alerts" % mode_tag)

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

    # (g) SPINE-SUMMARY-LINEAGE: the marker keeps the last line AND surfaces
    # the lineage line printed earlier; no lineage line -> just the last line;
    # a lineage line that IS the last line is not duplicated.
    builder_out = ("[brainmap] nodes=16032 edges=6595 clusters=1223\n"
                   "[brainmap] lineage: carried=1213 minted=10 merged_away=2\n"
                   "[brainmap] wrote 1 brainmap_graph row (generated_at=X)\n")
    tail_g = _tail_with_key_lines(builder_out)
    check("key-line marker = last line + lineage line",
          tail_g == "[brainmap] wrote 1 brainmap_graph row (generated_at=X)"
                    " · [brainmap] lineage: carried=1213 minted=10 "
                    "merged_away=2")
    check("no key line -> plain last line",
          _tail_with_key_lines("a\nb\n") == "b")
    check("key line as last line is not duplicated",
          _tail_with_key_lines("x\n[brainmap] lineage: carried=1 minted=0 "
                               "merged_away=0\n")
          == "[brainmap] lineage: carried=1 minted=0 merged_away=0")
    check("empty output -> empty marker", _tail_with_key_lines("") == "")

    # (h) a SUCCESSFUL build step whose marker lost the lineage line says so
    # in the summary — visible absence, never a silent drop. Other steps and
    # failed builds are not annotated.
    with_lineage = summarize_results([
        {"step": 2, "label": "build_brainmap_graph", "rc": 0, "seconds": 1.0,
         "tail": tail_g}])
    check("build summary line carries the lineage numbers",
          "carried=1213 minted=10 merged_away=2" in with_lineage
          and _LINEAGE_MISSING_NOTE not in with_lineage)
    without = summarize_results([
        {"step": 2, "label": "build_brainmap_graph", "rc": 0, "seconds": 1.0,
         "tail": "[brainmap] wrote 1 brainmap_graph row (generated_at=X)"}])
    check("missing lineage line is reported, not dropped",
          _LINEAGE_MISSING_NOTE in without)
    other = summarize_results([
        {"step": 1, "label": "embed_backfill", "rc": 0, "seconds": 1.0,
         "tail": "=== SUMMARY ==="},
        {"step": 2, "label": "build_brainmap_graph", "rc": 5, "seconds": 1.0,
         "tail": "boom"}])
    check("non-build and failed steps get no lineage note",
          _LINEAGE_MISSING_NOTE not in other)

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
                    "-> report -> prediction-log -> topic-alerts in hard order, "
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
