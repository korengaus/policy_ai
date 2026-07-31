#!/usr/bin/env python3
"""DAILY-GRAPH-ALERT — pin-OUT wrapper for the daily-graph cron.

WHY THIS FILE EXISTS: the daily-graph cron command was one long line chaining
both scripts with grep assertions. Render's dashboard refuses to save a command
that long ("Failed to fetch (api.render.com)"; a short ``echo test`` saves
fine, and the Korean title was ruled out by retrying in English), so the whole
command moves here and the dashboard field becomes:

    python scripts/daily_graph_alert.py

WHAT IT RUNS, in order, the second GATED on the first:

    scripts/embed_backfill.py          must print "=== SUMMARY ==="
    scripts/build_brainmap_graph.py    must print "wrote 1 brainmap_graph row"

THE SILENT-SUCCESS TRAP (the reason the old command carried greps): BOTH
scripts print a guidance line and ``return 0`` when DATABASE_URL is unset, and
build_brainmap_graph does the same when USE_POSTGRES_WRITE != "true"
(embed_backfill.py:349/353, build_brainmap_graph.py:1164/1169). A bare chain
would hand Render rc=0 for a run that did NOTHING. The greps are preserved here
as REQUIRED_MARKER substring assertions on each child's captured output; a
missing marker is a FAILURE (exit 3) even when the child returned 0. The
embed marker is the exact literal "=== SUMMARY ===", so the dry-run heading
"=== SUMMARY (DRY-RUN — no API call, no write) ===" does NOT satisfy it.

DESIGN RULES:
  * WRAPPER ONLY. Neither child is modified, neither is imported; both are
    subprocess-run with their production argv. No new stored field — the only
    write in the whole run is embed_backfill's existing embedding_cache save
    plus build_brainmap_graph's existing single brainmap_graph row. No verdict,
    no threshold, no re-analysis. truth_claim False.
  * Output is STREAMED line-by-line as it arrives (children run with -u), so
    the Render log shows exactly what it shows today. Lines are additionally
    accumulated in memory for the assertions above — printing and capturing,
    not capturing instead of printing.
  * EXIT NON-ZERO on any failure (child rc passthrough, or 3 for a failed
    marker assertion) so Render's own failure notification still fires.
  * Notifier is weekly_spine.notify by IMPORT — it carries the RFC 2047 header
    encoding that fixed the Korean-title bug. NO second notifier exists here.
    A raising notifier is swallowed and can never change the exit code.
  * NO snapshot step and NO weekly generator. Those stay weekly on purpose:
    /api/trending diffs the two most recent snapshot batches and the hero
    eyebrow reads "주간 스냅샷 기준", so a daily snapshot would make that label
    false.
  * pin-OUT: zero logger call sites (print only) — the 331/16 log pins cannot
    move.

USAGE
    python scripts/daily_graph_alert.py             # the cron command
    python scripts/daily_graph_alert.py --selftest  # pure; no DB, no network

DASHBOARD-PROVISIONED, like daily-collection: set the command above in the
Render dashboard. NTFY_TOPIC must be on THAT service for the notification to
be sent; unset, weekly_spine.notify prints the banner instead and the run is
otherwise identical.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# weekly_spine (same directory, pin-OUT) owns the ntfy plumbing.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Exit code for "child returned 0 but its output proves it did nothing".
EXIT_ASSERT = 3

# The two steps, in order, each with the grep assertion the old one-line cron
# command carried. `argv` is the production invocation — no extra flags, so
# embed_backfill uses its default batch size and unlimited --max-rows, and
# build_brainmap_graph does a real (non-dry-run, anchored) build.
STEPS = (
    {"name": "embed_backfill",
     "argv": ["scripts/embed_backfill.py"],
     "marker": "=== SUMMARY ==="},
    {"name": "build_brainmap_graph",
     "argv": ["scripts/build_brainmap_graph.py"],
     "marker": "wrote 1 brainmap_graph row"},
)

# Operator-facing numbers, lifted from the captured output. Every one of these
# is optional: a missing line degrades that field to 미상 and never fails the
# run — the assertions above are what decide success.
RE_EMBEDDED = re.compile(r"^\s*newly embedded\s*:\s*(\d+)\s*$", re.M)
RE_MISSING = re.compile(r"^\[brainmap\] corpus .*\bmissing=(\d+)", re.M)
RE_ANCHOR = re.compile(r"^\[brainmap\] layout: (.+)$", re.M)
RE_LINEAGE = re.compile(
    r"^\[brainmap\] lineage: carried=(\d+) minted=(\d+) merged_away=(\d+)", re.M)


def run_child(argv):
    """Run one child with output STREAMED and captured. Returns (rc, text).

    ``-u`` keeps the child unbuffered so the Render log fills in real time
    rather than in one burst at exit. A child killed by signal N maps to 128+N
    so Render still sees non-zero. A child that cannot start is rc=1.
    """
    command = [sys.executable, "-u"] + list(argv)
    try:
        proc = subprocess.Popen(
            command, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as exc:
        print("[graph-alert] could not start %s: %s"
              % (argv[0], type(exc).__name__))
        return 1, ""
    chunks = []
    if proc.stdout is not None:
        for line in proc.stdout:
            chunks.append(line)
            print(line, end="", flush=True)
    rc = proc.wait()
    return (rc if rc >= 0 else 128 + abs(rc)), "".join(chunks)


def parse_embedded(text):
    match = RE_EMBEDDED.search(text)
    return int(match.group(1)) if match else None


def parse_missing(text):
    match = RE_MISSING.search(text)
    return int(match.group(1)) if match else None


def parse_anchor(text):
    matches = RE_ANCHOR.findall(text)
    # "layout-3d:" lines do not match; the 2D layout line is the anchoring
    # statement. Last one wins if a build ever prints more than one.
    return matches[-1].strip() if matches else None


def parse_lineage(text):
    match = RE_LINEAGE.search(text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def build_notification(results, failure, minutes, steps=STEPS):
    """(title, message, priority) for one run. Pure — selftestable."""
    combined = "\n".join(text for _, _, text in results)
    embedded = parse_embedded(combined)
    missing = parse_missing(combined)
    anchor = parse_anchor(combined)
    lineage = parse_lineage(combined)

    ran = ["%s rc=%d" % (name, rc) for name, rc, _ in results]
    done = {name for name, _, _ in results}
    skipped = [s["name"] for s in steps if s["name"] not in done]
    if skipped:
        ran.append("%s 미실행" % ", ".join(skipped))

    lines = [
        "신규 임베딩 %s · 벡터 missing %s"
        % (("%d건" % embedded) if embedded is not None else "미상",
           ("%d" % missing) if missing is not None else "미상"),
        "앵커링: %s" % (anchor or "미상"),
        "계보: %s" % (("carried=%d minted=%d merged_away=%d" % lineage)
                      if lineage else "미상"),
        "%s · %.0f분" % (" · ".join(ran), minutes),
    ]

    if failure is None:
        title = "일일 브레인맵 완료 — 신규 임베딩 %s" % (
            ("%d건" % embedded) if embedded is not None else "미상")
        return title, "\n".join(lines), "default"

    name, kind, marker, rc = failure
    if kind == "rc":
        title = "일일 브레인맵 실패 — %s rc=%d" % (name, rc)
        head = "%s 가 rc=%d 로 종료 — 이후 단계 중단" % (name, rc)
    else:
        title = "일일 브레인맵 실패 — %s 검증 실패" % name
        head = ("%s 는 rc=0 이지만 '%s' 출력이 없음 — DATABASE_URL/"
                "USE_POSTGRES_WRITE 미설정으로 아무것도 하지 않은 '조용한 성공'"
                "일 수 있음" % (name, marker))
    return title, "\n".join([head] + lines), "high"


def run(child_runner, notifier, steps=STEPS):
    """Orchestrate one wrapped run; returns the exit code.

    Each step must return 0 AND print its marker before the next one starts.
    """
    started = time.time()
    results = []
    failure = None
    for step in steps:
        rc, text = child_runner(step["argv"])
        results.append((step["name"], rc, text))
        if rc != 0:
            failure = (step["name"], "rc", step["marker"], rc)
            break
        if step["marker"] not in text:
            print("[graph-alert] ASSERTION FAILED: %s returned 0 without "
                  "printing %r — treating as failure."
                  % (step["name"], step["marker"]))
            failure = (step["name"], "marker", step["marker"], EXIT_ASSERT)
            break

    minutes = (time.time() - started) / 60.0
    title, message, priority = build_notification(
        results, failure, minutes, steps=steps)

    # Belt over weekly_spine.notify's own braces: even a RAISING notifier must
    # never change the run's exit code.
    try:
        notifier(title, message, priority=priority)
    except Exception as exc:
        print("[graph-alert] notify raised %s — ignored" % type(exc).__name__)
    return 0 if failure is None else failure[3]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()

    import weekly_spine

    return run(run_child, weekly_spine.notify)


# ---------------------------------------------------------------------------
# Selftest — stubbed children + stubbed notifier, plus ONE real call into
# weekly_spine.notify with the ntfy env cleared (prints, never sends).
# No DB, no network, no embedding, no graph build.
# ---------------------------------------------------------------------------

SAMPLE_EMBED = """\
EMBED-BACKFILL — title+claim_text into embedding_cache
[embed-backfill] page 1: 200 rows seen (running total 200)  embedded=12 cache_hit=188  ~$0.0002

=== SUMMARY ===
  rows seen                 : 200
  newly embedded            : 12
  cache hits (skipped, $0)  : 188
  embed failures (None)     : 0
"""

SAMPLE_GRAPH = """\
BUILD-BRAINMAP-GRAPH — k=8 sim>=0.80 embed=title+claim (openai/text-embedding-3-small)
[brainmap] corpus rows=2410 unique texts=2380 vectors resolved=2377 missing=3
[brainmap] layout: anchored to previous build (known=820 new=14)
[brainmap] layout-3d: fresh (no previous graph to anchor to)
[brainmap] nodes=2377 edges=5120 clusters=61 singletons=900 largest=48
[brainmap] lineage: carried=61 minted=4 merged_away=2
[brainmap] wrote 1 brainmap_graph row (generated_at=2026-07-31T21:00:00Z)
"""


def selftest() -> int:
    import os

    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    sent = []

    def notifier(title, message, priority="default"):
        sent.append((title, message, priority))
        return True

    def scripted(*pairs):
        """child_runner returning (rc, text) per call, in order."""
        state = {"i": 0}

        def runner(argv):
            i = state["i"]
            state["i"] += 1
            return pairs[min(i, len(pairs) - 1)]
        return runner, state

    # 1. Happy path: both markers present, one notification, exit 0.
    sent.clear()
    runner, state = scripted((0, SAMPLE_EMBED), (0, SAMPLE_GRAPH))
    rc = run(runner, notifier)
    check("1 rc 0", rc == 0)
    check("1 both steps ran", state["i"] == 2)
    check("1 one notification", len(sent) == 1)
    check("1 title", sent[0][0] == "일일 브레인맵 완료 — 신규 임베딩 12건")
    check("1 priority", sent[0][2] == "default")
    check("1 embedded+missing", "신규 임베딩 12건 · 벡터 missing 3" in sent[0][1])
    check("1 anchor line",
          "앵커링: anchored to previous build (known=820 new=14)" in sent[0][1])
    check("1 lineage line",
          "계보: carried=61 minted=4 merged_away=2" in sent[0][1])
    check("1 rc summary",
          "embed_backfill rc=0 · build_brainmap_graph rc=0" in sent[0][1])

    # 2. SILENT SUCCESS — embed returns 0 with the DATABASE_URL guidance line
    # and no SUMMARY. The graph step must NOT run and the exit must be non-zero.
    sent.clear()
    silent = ("DATABASE_URL not set — run in the Render Worker Shell (or "
              "locally with $env:DATABASE_URL pointed at the external DB).\n")
    runner, state = scripted((0, silent), (0, SAMPLE_GRAPH))
    rc = run(runner, notifier)
    check("2 non-zero exit", rc == EXIT_ASSERT and rc != 0)
    check("2 graph step gated off", state["i"] == 1)
    check("2 title", sent[0][0] == "일일 브레인맵 실패 — embed_backfill 검증 실패")
    check("2 priority high", sent[0][2] == "high")
    check("2 marker named", "'=== SUMMARY ==='" in sent[0][1])
    check("2 silent-success named", "조용한 성공" in sent[0][1])
    check("2 skipped step named", "build_brainmap_graph 미실행" in sent[0][1])

    # 2b. A DRY-RUN summary heading must NOT satisfy the assertion.
    sent.clear()
    dry = SAMPLE_EMBED.replace(
        "=== SUMMARY ===", "=== SUMMARY (DRY-RUN — no API call, no write) ===")
    runner, state = scripted((0, dry), (0, SAMPLE_GRAPH))
    rc = run(runner, notifier)
    check("2b dry-run rejected", rc == EXIT_ASSERT and state["i"] == 1)

    # 3. Graph returns 0 without writing its row (USE_POSTGRES_WRITE unset).
    sent.clear()
    refused = ("[brainmap] corpus rows=2410 unique texts=2380 vectors "
               "resolved=2377 missing=3\n"
               "USE_POSTGRES_WRITE is not 'true' — refusing to write.\n")
    runner, _ = scripted((0, SAMPLE_EMBED), (0, refused))
    rc = run(runner, notifier)
    check("3 non-zero exit", rc == EXIT_ASSERT)
    check("3 title", sent[0][0]
          == "일일 브레인맵 실패 — build_brainmap_graph 검증 실패")
    check("3 marker named", "'wrote 1 brainmap_graph row'" in sent[0][1])
    check("3 embed number still reported", "신규 임베딩 12건" in sent[0][1])

    # 4. Child rc passthrough, both positions, with the later step gated off.
    sent.clear()
    runner, state = scripted((1, "boom\n"), (0, SAMPLE_GRAPH))
    rc = run(runner, notifier)
    check("4 embed rc passthrough", rc == 1 and state["i"] == 1)
    check("4 title", sent[0][0] == "일일 브레인맵 실패 — embed_backfill rc=1")
    sent.clear()
    runner, _ = scripted((0, SAMPLE_EMBED), (2, "boom\n"))
    rc = run(runner, notifier)
    check("4b graph rc passthrough", rc == 2)
    check("4b priority high", sent[0][2] == "high")

    # 5. A raising notifier changes nothing — success stays 0, failure stays
    # non-zero. Alerting may never mask, or invent, a failure.
    def raising(title, message, priority="default"):
        raise RuntimeError("ntfy down")

    runner, _ = scripted((0, SAMPLE_EMBED), (0, SAMPLE_GRAPH))
    check("5 success survives raising notifier", run(runner, raising) == 0)
    runner, _ = scripted((0, "nothing\n"), (0, SAMPLE_GRAPH))
    check("5b failure survives raising notifier",
          run(runner, raising) == EXIT_ASSERT)

    # 6. Parsers: present, absent, and not-fooled-by-the-3d-line.
    check("6 embedded", parse_embedded(SAMPLE_EMBED) == 12)
    check("6 embedded absent", parse_embedded("nothing here") is None)
    check("6 missing", parse_missing(SAMPLE_GRAPH) == 3)
    check("6 anchor 2d only",
          parse_anchor(SAMPLE_GRAPH) == "anchored to previous build (known=820 new=14)")
    check("6 lineage", parse_lineage(SAMPLE_GRAPH) == (61, 4, 2))
    check("6 lineage absent", parse_lineage(SAMPLE_EMBED) is None)
    check("6 fresh anchor",
          parse_anchor("[brainmap] layout: fresh (no previous graph to anchor"
                       " to)\n") == "fresh (no previous graph to anchor to)")

    # 7. REAL streaming through run_child: a stub child printing to stdout AND
    # stderr, exiting non-zero. Proves output is captured for the assertions
    # AND that the exit code survives.
    stub = ("import sys;"
            "print('=== SUMMARY ===');"
            "print('to stderr', file=sys.stderr);"
            "sys.exit(5)")
    rc, text = run_child(["-c", stub])
    check("7 rc captured", rc == 5)
    check("7 stdout captured", "=== SUMMARY ===" in text)
    check("7 stderr merged in", "to stderr" in text)
    rc, text = run_child(["-c", "raise SystemExit(0)"])
    check("7b clean child", rc == 0 and text == "")

    # 8. NOTIFY PATH, FOR REAL — weekly_spine.notify is called with NTFY_URL /
    # NTFY_TOPIC cleared, so it takes its documented print-instead-of-send
    # branch: no socket is opened, and the return is False. This exercises the
    # exact function production calls (no fake), which is the only part of the
    # notification that cannot be checked on a box without NTFY_TOPIC.
    import weekly_spine

    saved = {key: os.environ.pop(key, None) for key in ("NTFY_URL", "NTFY_TOPIC")}
    try:
        title, message, priority = build_notification(
            [("embed_backfill", 0, SAMPLE_EMBED),
             ("build_brainmap_graph", 0, SAMPLE_GRAPH)], None, 3.0)
        print("--- weekly_spine.notify (env cleared -> prints, never sends) ---")
        result = weekly_spine.notify(title, message, priority=priority)
        print("--- end ---")
        check("8 notify took the print branch", result is False)
        check("8 header encoder accepts the Korean title",
              weekly_spine._header_title(title).startswith("=?UTF-8?B?"))
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value

    if failures:
        print("SELFTEST FAILED: " + ", ".join(failures))
        return 1
    print("SELFTEST PASSED (12 cases, 38 assertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
