"""B2B-READINESS-AUDIT — READ-ONLY pre-outreach verification (pin-OUT, reusable).

Verifies, before every outreach wave, that everything a B2B prospect could
click or scrutinise matches live truth TODAY:

  C1  the flagship's live numbers on GET /api/claim/<flagship>, reported for
      the operator to confirm the outreach copy against (OBSERVED, not
      compared — see C1-OBSERVE-NOT-COMPARE below)
  C2  cross-surface agreement of the flagship cluster's outlet number
  C3  link integrity (weekly archive links, member /history rows, the four
      public pages + honesty strings, found:false posture)
  C4  corpus invariants (truth_claim / operator_review_required /
      verdict_label legality) over the FULL corpus
  C5  pipeline freshness (daily adds, latest weekly report + brainmap
      snapshot, recent null rates)
  C6  recent-row quality (sentence-join rate, boilerplate at the
      PROMOTION layer vs raw stored)

SAFETY
------
* DB: SELECT only. The engine is opened with
  default_transaction_read_only=on so a stray write would ERROR, and
  USE_POSTGRES_WRITE is never read or set. DATABASE_URL comes from the
  environment or from the repo-root .env (parsed locally, never printed).
* HTTP: sequential GETs against the live site, hard-capped at
  MAX_LIVE_REQUESTS (58 < the 60 budget) with a polite delay.
* No product code imported except the pure normalizer-free constants
  mirrored below (mirrors are marked with their source lines).

Usage:
    python scripts/b2b_readiness_audit.py               # full audit
    python scripts/b2b_readiness_audit.py --selftest    # offline parse checks
    python scripts/b2b_readiness_audit.py --base http://127.0.0.1:8000

Exit codes: 0 = audit ran (verdict in output) / selftest passed;
            1 = selftest failed; 2 = usage / cannot even start.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BASE = "https://tickedin.org"
# AUDIT-HARDENING: cap covers ~55 first attempts + retry headroom (each
# failed request may retry twice with backoff). Still sequential + delayed —
# a fully healthy run uses the same ~55 as before; retries spend budget only
# when something already failed.
MAX_LIVE_REQUESTS = 90
REQUEST_DELAY_S = 0.25          # sequential + polite, never hammering

FLAGSHIP_LINEAGE = "48e3baa51df2"
FLAGSHIP_REPRESENTATIVE = 8523

# AUDIT-HARDENING C7 — matcher-consistency baseline. The period-mismatch
# predicate (ported into api_server.py during CLAIM-GRAPHS; reused here, never
# a third copy) is EXPECTED to be non-zero over stored genuine-flagged rows:
# suppression happens at display, stored values are deliberately untouched.
# What this check catches is GROWTH — a new wrong-period attachment displayed
# as confirmed would be the fourth instance of the defect fixed three times.
# Baseline measured 2026-07-27: exactly these three cards (MATCHER-GUARD).
MATCHER_MISMATCH_KNOWN_IDS = frozenset({7871, 9534, 13977})
MATCHER_MISMATCH_BASELINE = 3
# C1-OBSERVE-NOT-COMPARE (2026-08-04) — the four EMAIL_* constants that used to
# live here (78 / 156 / 2026-06-30 / 2026-07-19) were typed by hand from an
# outreach draft and went stale the moment the cluster grew. On 08-04 the live
# claim page and the outreach copy BOTH said 157 / 07-20 and only this file
# still said 156 / 07-19 — so the audit printed "UPDATE EMAIL COPY" about a
# correct email. An instrument that tells you to break a working artefact is
# worse than no instrument.
#
# Retyping them would only reschedule the same failure, so the comparison is
# GONE rather than corrected. Nothing in this repo carries the outreach copy:
# the emails live on the operator's desktop, and b2b_briefings/ holds per-
# customer weekly briefings from a different window (the flagship appears there
# with outlet_count=20 for 2026-06-15..07-14), so it is not the same number and
# cannot serve as the source. A file added here purely to hold these four values
# would be one more hand-maintained constant wearing a different hat.
#
# C1 therefore OBSERVES: it prints the live four values and asks the operator —
# the only party who can read the email — to confirm the copy matches. It never
# asserts what the email says, so it can no longer be wrong about it.

# AUDIT-SPINE-BASELINE C5 — the spine-artifact expectation is DERIVED from the
# clock, never hardcoded. Two pinned dates went stale the moment the spine ran
# again: the check reported "expect 2026-07-14 / 2026-07-20" against NEWER
# observed artifacts and still called them "an older batch", so it warned on
# every run. A gate that always warns trains the operator to skim past it —
# the same defect already fixed for the C4 null-verdict baseline.
#
# The rule follows the schedule and the two producing scripts, not a guess:
#   * the spine's cron time is READ FROM render.yaml at runtime (below);
#   * snapshot_brainmap_growth.py:298 sets snapshot_date = the run's UTC DATE,
#     so a Sunday run stamps that Sunday;
#   * generate_weekly_report.py:313 defaults week_start = today - 6 days (the
#     spine forwards no --week-start on the unattended run), so a Sunday run
#     stamps the preceding MONDAY.
# Verified against the first true automated run: generated_at 2026-07-26 19:20
# UTC (Sun) produced snapshot_date=2026-07-26 and week_start=2026-07-20 (Mon).
#
# GRACE: the expectation only comes due SPINE_GRACE_HOURS after the scheduled
# time, so an audit run while the chain is still executing (it takes ~20 min)
# cannot manufacture a false WARN.
#
# SPINE-AFTER-COLLECTION: the hour used to be typed here as 19. That is the
# same defect C1 above was repaired for — a hand-copied mirror of a value that
# lives somewhere else, which goes stale silently the moment the real one
# moves. render.yaml IS in this repo and IS the thing that provisions the cron,
# so the schedule is parsed from it. The parse is deliberately strict: only a
# plain 5-field expression with numeric minute/hour/day-of-week is accepted,
# and ANY other shape (ranges, lists, steps, a missing block) falls back to the
# constants below AND says so in the C5 row, so a divergence shows up in the
# output instead of being silently assumed.
SPINE_GRACE_HOURS = 6
# C5-COLLAPSE-NOT-BAND (2026-08-04) — DAILY_ADD_BAND = (90, 160) is GONE.
# It was a hand-typed snapshot of the 07-18..07-26 collection regime, and the
# regime moved (post-07-25 dedup shift: complete days now run 51-88), so C5
# warned on every run for over a week while saying nothing. Measured over all
# 96 complete days: 8-46 (manual era) / 229-2440 (backfill spikes) / 109-175
# (early cron) / 51-88 (current). There IS no stable band — dedup rejects more
# as the corpus grows, so the count is a function of corpus size, not a
# constant, and any typed band is pre-stale.
#
# The question the operator needs answered is not "is today inside a band" but
# "is collection still working". That is answered without a band:
#   * a complete day at ZERO is a FAIL (unchanged — the collapse this check
#     exists to catch);
#   * a complete day that COLLAPSED below a fixed fraction of the trailing
#     window's median WARNs. The median is computed from the live data at run
#     time, so the expectation moves when the truth moves; only the RATIO is
#     typed, and a ratio is a structural rule (like SPINE_GRACE_HOURS), not a
#     measurement wearing a constant's clothes. 1/4 is far below every
#     legitimate regime shift observed (largest real drop: 140 -> 51 ≈ 0.36 of
#     the then-median) while far above a trickle (a broken pipeline limping at
#     a handful of rows against a ~75 median reads ~0.05).
#   * everything else PASSes, and the row REPORTS the judged counts plainly —
#     the C1 lesson — so the operator sees the level without a verdict on it.
DAILY_ADD_COLLAPSE_RATIO = 0.25   # judged day < ratio * trailing median -> WARN
DAILY_ADD_TRAILING_DAYS = 14      # median window, ending before the judged days


def daily_adds_status(adds, trailing_counts):
    """(status, note) for C5 daily adds. PURE — takes the judged complete days
    ({iso_day: count}, oldest first) and the trailing window's counts (absent
    days already materialised as 0 by the caller), so every branch is
    selftestable without a DB.

    FAIL: any judged complete day at zero — collection stopped; never softened.
    WARN: any judged day below DAILY_ADD_COLLAPSE_RATIO * trailing median —
          the trend BROKE (a trickle from a half-dead pipeline), as opposed to
          drifting, which is the corpus growing and is not a defect.
    PASS: otherwise. The counts themselves are always in the row; the level
          carries no verdict (C1-OBSERVE-NOT-COMPARE).
    A young corpus with an empty trailing window cannot be trend-judged: the
    zero-check still stands, and the note says the trend check did not run
    rather than silently passing it."""
    zero_days = [d for d, v in adds.items() if v == 0]
    if zero_days:
        return "FAIL", ("ZERO adds on complete day(s) %s — collection stopped"
                        % ", ".join(zero_days))
    if not trailing_counts:
        return "PASS", ("trend not judged — no trailing window yet "
                        "(zero-day check still active)")
    ordered = sorted(trailing_counts)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2.0)
    floor = DAILY_ADD_COLLAPSE_RATIO * median
    collapsed = [d for d, v in adds.items() if v < floor]
    basis = ("trailing %d-day median=%s, collapse floor=%.1f (x%.2f)"
             % (len(trailing_counts), median, floor, DAILY_ADD_COLLAPSE_RATIO))
    if median == 0:
        # A dead trailing window makes the floor vacuous (anything >= 0
        # passes). Say so: the judged days being non-zero is then the ONLY
        # thing standing between this row and a lie.
        return "PASS", ("trend not judgeable — trailing median is 0 "
                        "(window itself collected nothing); zero-day check "
                        "still active")
    if collapsed:
        return "WARN", ("COLLAPSED below trend: %s — %s"
                        % (", ".join("%s=%d" % (d, adds[d]) for d in collapsed),
                           basis))
    return "PASS", basis

# Fallback ONLY — used when render.yaml cannot be parsed. Kept at the value
# that shipped before this change so a fallback is visibly the old assumption.
SPINE_FALLBACK = (6, 19, 0)     # (python weekday Mon=0..Sun=6, hour, minute)


def parse_cron_schedule(expr):
    """('30 22 * * 0') -> (python_weekday, hour, minute), or None when the
    expression is anything this parser cannot resolve exactly. Cron numbers
    day-of-week 0=Sunday; Python's weekday() is Mon=0..Sun=6."""
    parts = (expr or "").strip().strip('"\'').split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*":
        return None
    if not (minute.isdigit() and hour.isdigit() and dow.isdigit()):
        return None
    minute, hour, dow = int(minute), int(hour), int(dow)
    if not (0 <= minute < 60 and 0 <= hour < 24 and 0 <= dow <= 7):
        return None
    return ((dow % 7) + 6) % 7, hour, minute


def read_spine_schedule(path=None):
    """(weekday, hour, minute, source) for the weekly-spine cron.

    Reads the `schedule:` of the render.yaml block whose `name:` is
    weekly-spine. No YAML dependency: the file is walked block by block, a
    block being delimited by a `- type:` line. Returns the SPINE_FALLBACK with
    a source string naming the reason whenever the block, the key, or the
    expression cannot be resolved — never a guess, and never silent."""
    path = path or (ROOT / "render.yaml")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return SPINE_FALLBACK + ("FALLBACK: render.yaml unreadable (%s)"
                                 % type(exc).__name__,)
    name, schedule = None, None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- type:"):
            if name == "weekly-spine" and schedule:
                break
            name, schedule = None, None
        if stripped.startswith("name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("schedule:"):
            schedule = stripped.split(":", 1)[1].strip()
    if name != "weekly-spine" or not schedule:
        return SPINE_FALLBACK + ("FALLBACK: no weekly-spine schedule in render.yaml",)
    parsed = parse_cron_schedule(schedule)
    if parsed is None:
        return SPINE_FALLBACK + ("FALLBACK: unparseable cron %r" % schedule,)
    return parsed + ("render.yaml %s" % schedule,)


SPINE_WEEKDAY_UTC, SPINE_HOUR_UTC, SPINE_MINUTE_UTC, SPINE_SCHEDULE_SOURCE = (
    read_spine_schedule())


def last_expected_spine_run(now_utc, weekday=None, hour=None, minute=None):
    """The most recent scheduled spine run that is already DUE (i.e. at least
    SPINE_GRACE_HOURS in the past). Pure — takes the clock as an argument so the
    staleness cases are selftestable, and takes the schedule so a case can pin
    one explicitly instead of moving whenever render.yaml does."""
    weekday = SPINE_WEEKDAY_UTC if weekday is None else weekday
    hour = SPINE_HOUR_UTC if hour is None else hour
    minute = SPINE_MINUTE_UTC if minute is None else minute
    due = now_utc - timedelta(hours=SPINE_GRACE_HOURS)
    anchor = due.replace(hour=hour, minute=minute, second=0, microsecond=0)
    anchor -= timedelta(days=(anchor.weekday() - weekday) % 7)
    if anchor > due:
        anchor -= timedelta(days=7)
    return anchor


def expected_spine_artifacts(now_utc, weekday=None, hour=None, minute=None):
    """(expected_week_start, expected_snapshot_date) as ISO date strings for
    the last DUE spine run — the OLDEST artifacts a healthy spine may hold."""
    run_date = last_expected_spine_run(now_utc, weekday, hour, minute).date()
    return (run_date - timedelta(days=6)).isoformat(), run_date.isoformat()


def spine_artifact_status(wk_latest, snap_latest, now_utc):
    """(status, detail) for the C5 spine-artifact check.

    NOT weakened — the direction that matters is preserved exactly: an artifact
    OLDER than the last due run still WARNs, because that is the failure this
    check exists to catch (the spine did not run, or failed). What changes is
    that an artifact at or NEWER than the expectation now PASSes, since a
    manual rebuild legitimately runs ahead of the cron. A MISSING artifact is
    a WARN, never a pass."""
    exp_wk, exp_snap = expected_spine_artifacts(now_utc)
    behind = []
    for label, observed, expected in (("weekly", wk_latest, exp_wk),
                                      ("snapshot", snap_latest, exp_snap)):
        if observed is None or str(observed) == "":
            behind.append("%s=missing" % label)
        elif str(observed) < expected:
            # Whole cycles missed, so the message says HOW stale, not just that
            # it is: 6 days behind still means one missed Sunday.
            days = (date.fromisoformat(expected)
                    - date.fromisoformat(str(observed))).days
            behind.append("%s=%s is %d cycle(s) behind %s"
                          % (label, observed, -(-days // 7), expected))
    # SPINE-AFTER-COLLECTION: the assumed schedule is STATED, and names where it
    # came from. If render.yaml ever stops being readable the row says
    # "FALLBACK: …" instead of quietly checking against a time that no longer
    # happens — the failure this check used to be capable of.
    detail = ("weekly=%s (expect >=%s) snapshot=%s (expect >=%s) [schedule "
              "%02d:%02d UTC weekday=%d, +%dh grace, from %s]"
              % (wk_latest, exp_wk, snap_latest, exp_snap,
                 SPINE_HOUR_UTC, SPINE_MINUTE_UTC, SPINE_WEEKDAY_UTC,
                 SPINE_GRACE_HOURS, SPINE_SCHEDULE_SOURCE))
    if behind:
        # em-dash, not a pipe: the observed cell must not split the table.
        return "WARN", detail + " — STALE: " + "; ".join(behind)
    return "PASS", detail

# AUDIT-BASELINES C4 — null verdict_label baseline, measured 2026-07-27.
# These are ids 1-11, ALL created 2026-04-30 (first deploy day), before the
# verdict_label column existed (added 2026-05-02 as an additive ALTER). Zero
# new occurrences in the weeks since, the card renders its safe neutral
# fallback, and they sit far below any recent feed — a fossil, not a live
# defect. Recorded as an ID SET, not just a count, so the check reports
# CHANGE instead of restating a known state on every run:
#   * a RISE names the new ids (a live row losing its label),
#   * a DROP is surfaced too — rows do not un-null themselves, so a
#     disappearance means something WROTE to them.
# The rows themselves are never touched: this records a state, it does not
# repair one, and it silences nothing that changes.
KNOWN_NULL_VERDICT_IDS = frozenset(range(1, 12))
KNOWN_NULL_VERDICTS = len(KNOWN_NULL_VERDICT_IDS)

# AUDIT-BASELINES C5 — the memory-outage day. 2026-07-25 recorded 73 adds
# (below the band) during an OOM outage. It is RECORDED, never exempted: the
# day still counts as out-of-band and still WARNs; the annotation only tells
# the operator which known event the flagged day corresponds to, and the day
# ages out of the 3-day window on its own.
KNOWN_LOW_ADD_DAYS = {"2026-07-25": "memory outage (OOM); 73 adds recorded"}

# Honesty strings the pages MUST show (source: prompt + web/*.html copy).
S_NOT_VERIFICATION = "검증이 아닙니다"
S_COLLECTED_BASIS = "수집 기사 기준"
S_NEAR_ANCHOR_LEGEND = "첫 보도를 제외"
S_BOILER_FRAGMENT = "무단 전재 및 재배포 금지"

# Legal verdict-label closed set — MIRROR of honesty_guard.LEGAL_VERDICT_LABELS
# (honesty_guard.py:37-47). Kept inline so the audit needs no project imports.
LEGAL_VERDICT_LABELS = frozenset({
    "",
    "draft_disputed",
    "draft_high_risk_review",
    "draft_needs_review",
    "draft_needs_official_confirmation",
    "draft_needs_context",
    "draft_verified",
    "draft_likely_true",
    "draft_unverified",
})

# MIRROR of web/index.html CLAIM_FURNITURE_MARKERS + claimIsBoilerplateFurniture
# (index.html:12793-12819): marker spans removed; furniture iff <15 Hangul left.
CLAIM_FURNITURE_MARKERS = [
    re.compile(r"무단\s*전재"),
    re.compile(r"재배포"),
    re.compile(r"AI\s*학습\s*및\s*활용\s*금지"),
    re.compile(r"송고"),
    re.compile(r"저작권자"),
    re.compile(r"제보는\s*카카오톡"),
    re.compile(r"기사문의\s*및\s*제보"),
    re.compile(r"재판매\s*및\s*DB\s*금지"),
]
# Broader copyright-furniture markers (scripts/boilerplate_claim_probe.py
# SEED_MARKERS, copyright family only) — used as the INDEPENDENT post-gate
# detector so the gate mirror can't trivially certify itself.
BROAD_BOILER_MARKERS = [
    re.compile(r"무단\s*전재"),
    re.compile(r"재배포"),
    re.compile(r"저작권"),
    re.compile(r"[ⓒ©]|copyright", re.IGNORECASE),
    re.compile(r"AI\s*학습"),
    re.compile(r"송고\b|\d\s*송고|송고\s*$"),
    re.compile(r"재판매\s*및\s*DB\s*금지"),
]
HANGUL_RX = re.compile(r"[가-힣]")
# Sentence-join defect: Hangul + terminal punctuation + Hangul with NO space.
SENTENCE_JOIN_RX = re.compile(r"[가-힣][.!?][가-힣]")


def furniture_gate(text: str) -> bool:
    """Mirror of claimIsBoilerplateFurniture: True = rejected as furniture."""
    value = str(text or "")
    if not value:
        return False
    matched = False
    for rx in CLAIM_FURNITURE_MARKERS:
        replaced = rx.sub(" ", value)
        if replaced != value:
            matched = True
            value = replaced
    if not matched:
        return False
    return len(HANGUL_RX.findall(value)) < 15


def broad_boiler(text: str) -> bool:
    """Independent detector: copyright markers present AND furniture dominates
    (<15 Hangul after removing broad marker spans)."""
    value = str(text or "")
    if not value:
        return False
    matched = False
    for rx in BROAD_BOILER_MARKERS:
        replaced = rx.sub(" ", value)
        if replaced != value:
            matched = True
            value = replaced
    if not matched:
        return False
    return len(HANGUL_RX.findall(value)) < 15


def claim_pool(normalized_raw, claims_raw) -> list:
    """The frontend promotion pool, SAME order (index.html:12933-12944):
    normalized_claims[].claim_text first, then claims[]."""
    def loads(raw):
        if raw in (None, ""):
            return None
        if isinstance(raw, (list, dict)):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return None
    pool = []
    normalized = loads(normalized_raw)
    if isinstance(normalized, list):
        for entry in normalized:
            if isinstance(entry, dict):
                pool.append(str(entry.get("claim_text") or ""))
            elif isinstance(entry, str):
                pool.append(entry)
    claims = loads(claims_raw)
    if isinstance(claims, list):
        pool.extend(str(c or "") for c in claims if isinstance(c, str))
    return [t.strip() for t in pool if str(t or "").strip()]


def promoted_pick(claim_text, normalized_raw, claims_raw, title) -> str:
    """Mirror of the card's primary-claim selection ORDER for the furniture
    question only (buildReviewerSafeClaim funnels every candidate through
    sanitizeClaimText, whose FIRST gate is the furniture gate): stored
    claim_text if it survives, else first surviving pool entry, else the
    title fallback."""
    if str(claim_text or "").strip() and not furniture_gate(claim_text):
        return str(claim_text).strip()
    for candidate in claim_pool(normalized_raw, claims_raw):
        if not furniture_gate(candidate):
            return candidate
    return str(title or "").strip()


def parse_env_file(path: Path) -> dict:
    """Minimal KEY=VALUE .env parser (no export, strips optional quotes)."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        values[key.strip()] = raw
    return values


def resolve_weekly_links(weeks_payloads: dict, timeline_fetch) -> dict:
    """Pure link-resolution over archived weekly reports, exactly as
    weekly.html does (weekly.html:221-277): stored lineage_id wins; else
    representative_analysis_id -> timeline_fetch(rid) -> found+lineage_id.
    timeline_fetch(rid) returns a payload dict or None. Returns
    {week_start: (resolved, total, [misses])}."""
    out = {}
    for week_start, payload in weeks_payloads.items():
        entries = (payload or {}).get("top") or []
        resolved, misses = 0, []
        for entry in entries:
            lid = entry.get("lineage_id")
            if isinstance(lid, str) and lid.strip():
                resolved += 1
                continue
            rid = entry.get("representative_analysis_id")
            data = timeline_fetch(rid) if rid is not None else None
            if (isinstance(data, dict) and data.get("found") is True
                    and str(data.get("lineage_id") or "").strip()):
                resolved += 1
            else:
                misses.append("rank%s(rep=%s)" % (entry.get("rank"), rid))
        out[week_start] = (resolved, len(entries), misses)
    return out


def scan_forbidden_score_keys(payload, allowed=("policy_confidence_score",)):
    """Recursively collect key paths containing 'score' (any aggregate or
    combined score field would read as a claim verdict). Member-row
    policy_confidence_score is the ONE allowed score field."""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if "score" in str(key).lower() and key not in allowed:
                    hits.append(path + "." + str(key))
                walk(value, path + "." + str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, "%s[%d]" % (path, i))

    walk(payload, "$")
    return hits


# ---------------------------------------------------------------------------
# Live HTTP (budgeted, sequential)
# ---------------------------------------------------------------------------
class Budget:
    def __init__(self, cap):
        self.cap = cap
        self.used = 0


# AUDIT-HARDENING: paths that stayed unreachable after retries. A non-empty
# list becomes the NET reachability FAIL row — a network failure is VISIBLE
# and blocking, never a silent pass, and never a crash (this run died twice
# on prod transients before this hardening).
NETWORK_FAILURES: list = []
RETRY_BACKOFF_S = (1.0, 2.5)  # short, courteous; sequential requests


def http_get(base, path, budget, as_json=True, timeout=30):
    """One budgeted GET with retry/backoff. Returns (status, payload).

    Distinguishes "the check failed" from "we could not look": transient
    failures (DNS/timeout/5xx/non-JSON body on a 200) are retried with
    backoff; after the last retry the path is recorded in NETWORK_FAILURES
    and (None, None) is returned — every consumer's isinstance guard then
    takes its negative branch, and the NET row at the end blocks the verdict.
    A non-JSON 4xx body returns (status, None) without retry (a real answer,
    just not JSON). Never raises; never hands a str to a dict consumer (the
    crash class this removes)."""
    url = base.rstrip("/") + path
    last_err = "unknown"
    for attempt in range(1 + len(RETRY_BACKOFF_S)):
        if budget.used >= budget.cap:
            NETWORK_FAILURES.append("%s (budget exhausted at %d)" % (path, budget.cap))
            return (None, None)
        budget.used += 1
        if attempt:
            time.sleep(RETRY_BACKOFF_S[attempt - 1])
        time.sleep(REQUEST_DELAY_S)
        req = urllib.request.Request(url, headers={
            "User-Agent": "b2b-readiness-audit/1.0 (read-only preflight)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace") if err.fp else ""
            status = err.code
            if status >= 500:  # transient server side — retry
                last_err = "http %s" % status
                continue
        except Exception as err:  # DNS/timeout/reset — retry
            last_err = str(err)[:120]
            continue
        if as_json:
            try:
                return (status, json.loads(body))
            except ValueError:
                if status == 200:  # 200 with an HTML error page — transient
                    last_err = "non-JSON body (http %s)" % status
                    continue
                return (status, None)
        return (status, body)
    NETWORK_FAILURES.append("%s (%s)" % (path, last_err))
    return (None, None)


# ---------------------------------------------------------------------------
# Read-only DB engine (no USE_POSTGRES_WRITE, forced read-only transactions)
# ---------------------------------------------------------------------------
def build_readonly_engine():
    import os
    import sqlalchemy as sa

    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        url = parse_env_file(ROOT / ".env").get("DATABASE_URL", "").strip()
    if not url:
        return None, "DATABASE_URL not found (env or .env)"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        try:
            import psycopg  # noqa: F401
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        except ImportError:
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    try:
        engine = sa.create_engine(
            url, pool_pre_ping=True,
            connect_args={
                "connect_timeout": 20,
                # Any write statement ERRORS instead of executing.
                "options": "-c default_transaction_read_only=on "
                           "-c statement_timeout=120000",
            })
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return engine, None
    except Exception as err:
        return None, "DB connect failed: %s" % str(err)[:160]


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# REVIEWER-INTO-AUDIT — the semantic reviewer as an ADVISORY notice.
#
# WHY IT IS NOT A ROW IN `rep`: run_audit's exit code is computed from
# rep.rows ONLY (rep.worst(), plus the `fails`/`warns` list comprehensions over
# rep.rows). The reviewer is a non-deterministic LLM layer whose own docs say
# it is flag-and-hold BY CONSTRUCTION — it never auto-passes and never
# auto-fails, because run-to-run disagreement is expected and Korean quotation
# cannot be mechanically separated from assertion. A layer like that must never
# decide whether nine cold emails go out. So its result is collected in a
# SEPARATE list that rep never sees: there is no status string it could emit,
# and no future edit to the status-ranking dict, that can reach the exit code.
# The send decision stays with the deterministic rows.
#
# INVOKED AS A SUBPROCESS, exactly like CHECK 8 invokes the render scanner, so
# scripts/showcase_reviewer_card_probe.py needs no callable entry point and is
# not modified — its prompt and its three permitted questions are untouched.
#
# Probe exit codes (read from its own main()): 0 = ran, nothing held; 3 = notes
# held for a human read; 2 = vacuous drift detector (its self-check failed).
# A missing key/DB makes it print a sentinel and return 0, so returncode alone
# would read as "clean" — the stdout sentinel is what distinguishes that.
REVIEWER_CMD = [sys.executable, "-X", "utf8",
                str(ROOT / "scripts" / "showcase_reviewer_card_probe.py")]
REVIEWER_UNAVAILABLE_MARKS = ("ANTHROPIC_API_KEY not set", "DATABASE_URL not set")


def reviewer_advisory_row(returncode, output):
    """Pure classifier: (probe exit code, probe stdout) -> advisory row.

    Returns (label, status, observed, note). NEVER raises, and the caller puts
    the result in advisory_rows, never in rep.rows.
    """
    text = output or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    unavailable = [m for m in REVIEWER_UNAVAILABLE_MARKS if m in text]
    if unavailable:
        return ("R1 semantic reviewer", "UNAVAILABLE",
                "did not run: %s" % unavailable[0],
                "no semantic read was taken this run — the deterministic rows "
                "above are unaffected and still decide the send")
    if returncode == 2:
        return ("R1 semantic reviewer", "UNAVAILABLE",
                "drift detector self-check failed (exit 2) — no verdicts taken",
                "the reviewer refused to spend calls on a detector it cannot "
                "trust; deterministic rows above are unaffected")
    if returncode == 3:
        held = [ln for ln in lines if ln.startswith("HOLD FOR HUMAN READ")]
        drift = [ln for ln in lines if ln.startswith("TRUTH-DRIFT:")]
        return ("R1 semantic reviewer", "HOLD",
                ((held[0] if held else "notes held") + " | "
                 + (drift[0] if drift else ""))[:260],
                "a person must read the held notes before sending; this does "
                "NOT block the audit and does not decide the send")
    if returncode == 0:
        drift = [ln for ln in lines if ln.startswith("TRUTH-DRIFT:")]
        return ("R1 semantic reviewer", "NO HOLD",
                (drift[0] if drift else "ran, no notes held"),
                "nothing held for a human read this run; the reviewer never "
                "auto-passes the send either")
    return ("R1 semantic reviewer", "UNAVAILABLE",
            "probe exited %s — %s" % (returncode,
                                      (lines[-1][:120] if lines else "no output")),
            "no semantic read was taken this run — deterministic rows above "
            "are unaffected")


# ---------------------------------------------------------------------------
# C8-WARN-VOCABULARY — split the render scan's warn lines into findings and
# self-disclosures, using a vocabulary the SCAN ITSELF prints.
# ---------------------------------------------------------------------------
RENDER_WARN_PREFIX = "RENDER-SCAN WARN:"
# The scanner prints one measurement line per class per window, e.g.
#   RATE [mod14] bullet_char: 650/1053 = 61.7% (baseline 60.7% +5.0pp)
# ahead of its warns. That is its complete measured vocabulary at runtime.
_RENDER_RATE_RX = re.compile(r"^RATE \[([^\]]+)\]\s*([^:]+):")


def classify_render_warns(scan_output):
    """(defect_signals, disclosures) from a render-scan stdout+stderr blob.

    DERIVED, NOT TYPED. The scan's WARN-level *finding* channel is the
    baseline-comparison channel — "CEILING RISE [<window>] <class>: X% >
    baseline Y% + Zpp". Every class and window it can name is announced first in
    its own ``RATE [<window>] <class>:`` lines, so this reads that vocabulary out
    of the run itself: a warn line that carries a measured window tag or names a
    measured class is a finding. Adding a ceiling class to
    card_render_baselines.json extends this classifier automatically — there is
    no list in this file to forget to update, which is the whole point.

    Everything else the scan warns about is a statement ABOUT ITSELF, not about
    a reader's card: which adapters it covered, which it skipped and why, which
    reads are deliberate older-schema support, a recorded coupling note. Those
    are disclosures. They are returned (never dropped) so the caller can show
    the count, but they are not defect signals and must not hold a send.

    Pure and total: no I/O, never raises, empty input -> ([], [])."""
    text = scan_output or ""
    windows, classes = set(), set()
    for line in text.splitlines():
        match = _RENDER_RATE_RX.match(line)
        if match:
            windows.add(match.group(1).strip())
            classes.add(match.group(2).strip())
    signals, disclosures = [], []
    for line in text.splitlines():
        if not line.startswith(RENDER_WARN_PREFIX):
            continue
        body = line[len(RENDER_WARN_PREFIX):].strip()
        measured = any("[%s]" % w in body for w in windows) or any(
            re.search(r"(?<![0-9A-Za-z_])%s(?![0-9A-Za-z_])" % re.escape(c), body)
            for c in classes)
        (signals if measured else disclosures).append(body)
    return signals, disclosures


def render_disclosure_labels(disclosures):
    """Distinct gate labels of the disclosure lines, in first-seen order, taken
    from the lines themselves (text before the first ':') so a NEW kind of
    disclosure shows up in the C8 row on the first run that emits it instead of
    hiding inside a bare count."""
    labels = []
    for body in disclosures or []:
        label = body.split(":", 1)[0].strip()[:48] or "(unlabelled)"
        if label not in labels:
            labels.append(label)
    return labels


class Report:
    def __init__(self):
        self.rows = []

    def add(self, check, status, observed, impact):
        self.rows.append((check, status, observed, impact))

    def worst(self):
        # ERROR (a check crashed / could not run) blocks like FAIL — an
        # unverified gate must never read as passable.
        order = {"FAIL": 2, "ERROR": 2, "WARN": 1, "PASS": 0, "SKIP": 0,
                 "INFO": 0}
        return max((order.get(s, 0) for _, s, _, _ in self.rows), default=0)


def run_audit(base: str, with_reviewer: bool = False) -> int:
    rep = Report()
    budget = Budget(MAX_LIVE_REQUESTS)
    p = print
    # REVIEWER-INTO-AUDIT: advisory notices live here, NOT in rep.rows, so they
    # cannot reach rep.worst() / fails / warns and therefore cannot change the
    # exit code. Default empty — the reviewer is opt-in (--with-reviewer).
    advisory_rows = []

    # ---------------- CHECK 1 — the email's numbers -----------------------
    status, claim = http_get(base, "/api/claim/" + FLAGSHIP_LINEAGE, budget)
    claim_ok = status == 200 and isinstance(claim, dict) and claim.get("found") is True
    if not claim_ok:
        rep.add("C1 email numbers", "FAIL",
                "claim payload http=%s found=%s" % (status, getattr(claim, "get", lambda *_: "?")("found") if isinstance(claim, dict) else "?"),
                "email's flagship link renders '집계를 찾을 수 없' — dead pitch")
        claim = {}
    cluster = (claim.get("cluster") or {}) if isinstance(claim, dict) else {}
    outlets = cluster.get("outlet_count")
    members = cluster.get("member_count")
    earliest = str(cluster.get("earliest_member_published_at") or "")[:10]
    latest = str(cluster.get("latest_member_published_at") or "")[:10]
    timeline = (claim.get("timeline") or {}) if isinstance(claim, dict) else {}
    daily = timeline.get("daily") or []
    daily_sum = sum(int(e.get("count") or 0) for e in daily if isinstance(e, dict))
    dated = timeline.get("dated_members")
    undated = timeline.get("undated_members")
    member_rows = (claim.get("members") or []) if isinstance(claim, dict) else []
    null_verdict = sum(1 for m in member_rows if not str(m.get("verdict_label") or "").strip())
    null_conf = sum(1 for m in member_rows if m.get("policy_confidence_score") is None)
    score_leaks = [h for h in scan_forbidden_score_keys(claim)
                   if not re.search(r"\$\.members\[\d+\]\.policy_confidence_score$", h)]

    if claim_ok:
        # C1-OBSERVE-NOT-COMPARE: report live truth, claim nothing about the
        # email. INFO is weight 0 in Report.worst() and is not collected into
        # `warns`, so this row cannot move the verdict or the exit code — it is
        # an instruction to the operator, not a finding about the product.
        rep.add("C1 email numbers", "INFO",
                "LIVE NOW: outlets=%s members=%s earliest=%s latest=%s "
                "— CONFIRM the outreach copy cites these four values before "
                "sending (this audit cannot read the email; it is not in the "
                "repo)" % (outlets, members, earliest, latest),
                "an email citing older numbers than the page it links to")
        tl_ok = daily_sum == members
        tl_labelled = (daily_sum == dated and (dated + (undated or 0)) == members)
        rep.add("C1 timeline sum",
                "PASS" if tl_ok else ("WARN" if tl_labelled else "FAIL"),
                "sum(daily)=%s member_count=%s dated=%s undated=%s"
                % (daily_sum, members, dated, undated),
                "timeline curve contradicts the headline count on the claim page")
        rep.add("C1 no aggregate score", "PASS" if not score_leaks else "FAIL",
                "forbidden score keys: %s" % (score_leaks or "none"),
                "a combined number reads as a truth verdict — honesty breach")
        rep.add("C1 member fields", "PASS" if (null_verdict == 0 and null_conf == 0) else "FAIL",
                "rows=%d null_verdict=%d null_conf=%d capped=%s"
                % (len(member_rows), null_verdict, null_conf, claim.get("member_rows_capped")),
                "blank verdict/score cells in the member table")

    # ---------------- CHECK 2 — cross-surface agreement -------------------
    _, spread = http_get(base, "/api/spread/%d" % FLAGSHIP_REPRESENTATIVE, budget)
    spread_outlets = None
    if isinstance(spread, dict):
        spread_outlets = (spread.get("cluster") or {}).get("outlet_count") \
            if isinstance(spread.get("cluster"), dict) else spread.get("outlet_count")
    _, sizes = http_get(base, "/api/cluster-sizes?ids=%d" % FLAGSHIP_REPRESENTATIVE, budget)
    sizes_outlets = (sizes.get("sizes") or {}).get(str(FLAGSHIP_REPRESENTATIVE)) \
        if isinstance(sizes, dict) else None
    _, trending = http_get(base, "/api/trending", budget)
    trend_entry = None
    for entry in (trending.get("trending") or []) if isinstance(trending, dict) else []:
        if entry.get("cluster_lineage_id") == FLAGSHIP_LINEAGE:
            trend_entry = entry
            break
    _, weekly_latest = http_get(base, "/api/weekly-report", budget)
    weekly_payload = weekly_latest.get("report") if isinstance(weekly_latest, dict) and isinstance(weekly_latest.get("report"), dict) else weekly_latest
    weekly_entry = None
    if isinstance(weekly_payload, dict):
        for entry in weekly_payload.get("top") or []:
            # Archived weekly rows can predate CLAIM-LINK (stored lineage_id
            # null, resolved client-side) — fall back to the representative id.
            if (entry.get("lineage_id") == FLAGSHIP_LINEAGE
                    or entry.get("representative_analysis_id") == FLAGSHIP_REPRESENTATIVE):
                weekly_entry = entry
                break

    same_def = {"claim(all-time,graph)": outlets,
                "spread(graph)": spread_outlets,
                "cluster-sizes(graph)": sizes_outlets}
    same_vals = {v for v in same_def.values() if v is not None}
    labelled = {
        "trending(snapshot)": (trend_entry or {}).get("current_outlet_count"),
        "trending growth": (trend_entry or {}).get("growth"),
        "weekly(at-generation)": (weekly_entry or {}).get("outlet_count"),
        "weekly window_members": (weekly_entry or {}).get("window_member_count"),
    }
    rep.add("C2 cross-surface", "PASS" if len(same_vals) == 1 else "FAIL",
            "; ".join("%s=%s" % kv for kv in {**same_def, **labelled}.items()),
            "two pages show different outlet counts for the same claim")

    # ---------------- CHECK 3 — link integrity ----------------------------
    _, weeks_resp = http_get(base, "/api/weekly-report-weeks", budget)
    weeks = [w.get("week_start") for w in (weeks_resp.get("weeks") or [])] \
        if isinstance(weeks_resp, dict) else []
    weeks_payloads = {}
    for week in weeks:
        _, wp = http_get(base, "/api/weekly-report/" + str(week), budget)
        weeks_payloads[week] = wp.get("report") if isinstance(wp, dict) and isinstance(wp.get("report"), dict) else wp

    def timeline_fetch(rid):
        try:
            _, data = http_get(base, "/api/topic-timeline/%s" % rid, budget)
        except RuntimeError:
            return None
        return data if isinstance(data, dict) else None

    resolution = resolve_weekly_links(weeks_payloads, timeline_fetch)
    total_entries = sum(t for _, t, _ in resolution.values())
    total_resolved = sum(r for r, _, _ in resolution.values())
    all_misses = [m for _, _, ms in resolution.values() for m in ms]
    # AUDIT-HARDENING: 0 verified entries is a vacuous pass (typically an
    # upstream fetch failure) — never PASS on nothing.
    wl_status = ("WARN" if total_entries == 0
                 else ("PASS" if not all_misses else "WARN"))
    rep.add("C3 weekly links", wl_status,
            "%d/%d resolved over %d weeks%s"
            % (total_resolved, total_entries, len(resolution),
               ("; misses: " + ", ".join(all_misses)) if all_misses
               else ("" if total_entries else "; NOTHING VERIFIED")),
            "an entry renders with no claim link (safe, but a dead end)")

    sample_ids = []
    if member_rows:
        n = min(10, len(member_rows))
        step = max(1, (len(member_rows) - 1) // max(1, n - 1)) if n > 1 else 1
        seen = set()
        for i in range(0, len(member_rows), step):
            rid = member_rows[i].get("analysis_id")
            if rid is not None and rid not in seen:
                sample_ids.append(rid)
                seen.add(rid)
            if len(sample_ids) == n:
                break
    hist_ok, hist_bad = 0, []
    for rid in sample_ids:
        st, data = http_get(base, "/history/%s" % rid, budget)
        result = data.get("result") if isinstance(data, dict) else None
        if st == 200 and isinstance(result, dict) and result.get("title"):
            hist_ok += 1
        else:
            hist_bad.append("%s(http=%s)" % (rid, st))
    # AUDIT-HARDENING: an empty sample is a vacuous pass — never PASS on nothing.
    mr_status = ("WARN" if not sample_ids
                 else ("PASS" if not hist_bad else "FAIL"))
    rep.add("C3 member rows", mr_status,
            "%d/%d renderable%s" % (hist_ok, len(sample_ids),
                                    ("; bad: " + ", ".join(hist_bad)) if hist_bad
                                    else ("" if sample_ids else "; NOTHING VERIFIED")),
            "clicking a member article 404s from the claim page")

    pages = [
        ("/", [S_NOT_VERIFICATION], []),
        ("/web/weekly.html", [S_NOT_VERIFICATION, S_NEAR_ANCHOR_LEGEND], []),
        ("/web/claim.html?id=" + FLAGSHIP_LINEAGE,
         [S_NOT_VERIFICATION, S_COLLECTED_BASIS], []),
        ("/web/brainmap.html", [S_NOT_VERIFICATION], []),
    ]
    page_problems = []
    for path, must, must_not in pages:
        st, html = http_get(base, path, budget, as_json=False)
        if st != 200 or not isinstance(html, str):
            page_problems.append("%s http=%s" % (path, st))
            continue
        for s in must:
            if s not in html:
                page_problems.append("%s missing '%s'" % (path, s))
        for s in must_not:
            if s in html:
                page_problems.append("%s contains '%s'" % (path, s))
    # Home cards are client-rendered: audit the feed the cards are built from
    # and mirror the promotion gate — the boilerplate fragment must not be
    # able to reach a card's primary-claim slot.
    st, feed = http_get(base, "/history?limit=30", budget)
    feed_rows = (feed.get("results") or []) if isinstance(feed, dict) else []
    feed_boiler = 0
    for row in feed_rows:
        pick = promoted_pick(row.get("claim_text"), row.get("normalized_claims"),
                             row.get("claims"), row.get("title"))
        if broad_boiler(pick) or S_BOILER_FRAGMENT in pick:
            feed_boiler += 1
    if st != 200 or not feed_rows:
        page_problems.append("/history feed http=%s rows=%d" % (st, len(feed_rows)))
    elif feed_boiler:
        page_problems.append("boilerplate reaches %d home card(s)" % feed_boiler)
    rep.add("C3 pages+honesty", "PASS" if not page_problems else "FAIL",
            "4 pages + 30-card feed; " + ("; ".join(page_problems) or "all strings present, 0 boilerplate cards"),
            "missing disclaimer / copyright furniture shown as a claim")

    st, ghost = http_get(base, "/api/claim/deadbeef0000", budget)
    ghost_ok = st == 200 and isinstance(ghost, dict) and ghost.get("found") is False
    rep.add("C3 not-found posture", "PASS" if ghost_ok else "FAIL",
            "http=%s payload=%s" % (st, json.dumps(ghost, ensure_ascii=False)[:60]),
            "a fabricated page for a nonexistent claim id")

    # ---------------- CHECKS 4-6 — database -------------------------------
    engine, db_err = build_readonly_engine()
    if engine is None:
        for cid in ("C4 invariants", "C5 freshness", "C6 recent quality",
                    "C7 matcher-consistency", "C7 leak-scan",
                    "C8 render-scan"):
            rep.add(cid, "FAIL", db_err, "unverified corpus before outreach")
    else:
        import sqlalchemy as sa
        with engine.connect() as conn:
            def q(sql, **params):
                return conn.execute(sa.text(sql), params).fetchall()

            # C4 — invariants over EVERY table carrying the columns.
            tc_tables = [r[0] for r in q(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'truth_claim' AND table_schema = 'public'")]
            tc_bad = {t: q('SELECT COUNT(*) FROM "%s" WHERE truth_claim <> 0' % t)[0][0]
                      for t in tc_tables}
            orr_tables = [r[0] for r in q(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'operator_review_required' "
                "AND table_schema = 'public'")]
            orr_bad = {t: q('SELECT COUNT(*) FROM "%s" '
                            'WHERE operator_review_required <> 1' % t)[0][0]
                       for t in orr_tables}
            tc_total = sum(tc_bad.values())
            orr_total = sum(orr_bad.values())
            rep.add("C4 truth_claim", "PASS" if tc_total == 0 else "FAIL",
                    "%d violations over %d tables %s" % (tc_total, len(tc_bad),
                    {t: n for t, n in tc_bad.items() if n} or ""),
                    "a stored row asserts truth — core honesty invariant broken")
            rep.add("C4 operator_review", "PASS" if orr_total == 0 else "FAIL",
                    "%d violations over %d tables %s" % (orr_total, len(orr_bad),
                    {t: n for t, n in orr_bad.items() if n} or ""),
                    "evidence row marked as not needing human review")

            labels = q("SELECT COALESCE(verdict_label,'<null>') AS v, COUNT(*) "
                       "FROM analysis_results GROUP BY 1 ORDER BY 2 DESC")
            illegal = [(v, n) for v, n in labels
                       if v not in LEGAL_VERDICT_LABELS and v != "<null>"]
            # AUDIT-BASELINES: compare the null set against the recorded
            # baseline IDS (not just the count) — a same-size set with
            # different members is still a change, and a count alone could
            # not name what moved.
            null_ids = {int(r[0]) for r in q(
                "SELECT id FROM analysis_results "
                "WHERE verdict_label IS NULL OR verdict_label = ''")}
            nullish = len(null_ids)
            new_nulls = sorted(null_ids - KNOWN_NULL_VERDICT_IDS)
            gone_nulls = sorted(KNOWN_NULL_VERDICT_IDS - null_ids)
            if illegal:
                label_status = "FAIL"
            elif new_nulls or gone_nulls:
                label_status = "WARN"
            else:
                label_status = "PASS"
            baseline_note = ("at baseline %d (ids 1-11, 2026-04-30 pre-column "
                             "fossils)" % KNOWN_NULL_VERDICTS)
            if new_nulls:
                baseline_note = ("ROSE to %d — NEW null ids=%s"
                                 % (nullish, new_nulls))
            if gone_nulls:
                baseline_note = (("%s; " % baseline_note if new_nulls else "")
                                 + "FELL to %d — baseline ids no longer null=%s "
                                   "(rows do not un-null themselves: something "
                                   "wrote to them)" % (nullish, gone_nulls))
            rep.add("C4 verdict_label", label_status,
                    "distinct=%s illegal=%s null/empty=%d %s"
                    % ([(v, n) for v, n in labels], illegal or "none",
                       nullish, baseline_note),
                    "a label above draft_* reads as a confirmed verdict")
            rep.add("C4 fabricated-inst", "SKIP",
                    "no anomaly-scan script in scripts/; 7/21 full-corpus scan "
                    "measured Category A = 0 — not reimplemented ad hoc",
                    "-")

            # C5 — freshness.
            # AUDIT-BASELINES C5 — judge COMPLETE UTC days only; the current UTC
            # day is partial BY DEFINITION and is reported, never judged.
            # C5-COLLAPSE-NOT-BAND: the judged days are checked for STOPPED
            # (zero -> FAIL) and for a BROKEN trend (below a fraction of the
            # trailing window's median -> WARN). The expectation is computed
            # from the live data each run — nothing here goes stale when the
            # collection regime moves, which the typed 90-160 band did within
            # nine days of being written.
            today = datetime.now(timezone.utc).date()
            window_days = 3 + DAILY_ADD_TRAILING_DAYS
            day_rows = dict(q(
                "SELECT substr(created_at,1,10) AS d, COUNT(*) "
                "FROM analysis_results WHERE created_at >= :cut "
                "GROUP BY 1 ORDER BY 1",
                cut=str(today - timedelta(days=window_days + 1))))
            complete_days = [str(today - timedelta(days=i)) for i in (3, 2, 1)]
            adds = {d: int(day_rows.get(d, 0)) for d in complete_days}
            # Trailing window ends where the judged days begin. Absent days are
            # 0 — a day the pipeline wrote nothing is a zero-yield day, not a
            # missing datum.
            trailing = [int(day_rows.get(str(today - timedelta(days=i)), 0))
                        for i in range(4, 4 + DAILY_ADD_TRAILING_DAYS)]
            fresh_status, fresh_note = daily_adds_status(adds, trailing)
            # A recorded outage day is ANNOTATED, never exempted — it keeps its
            # WARN and ages out of the window on its own. (Zero days get no
            # annotation: zero is a FAIL about TODAY's collection, and a
            # "known" tag next to it would read as an excuse.)
            for d, reason in KNOWN_LOW_ADD_DAYS.items():
                if d in fresh_note and adds.get(d, -1) > 0:
                    fresh_note += " [known: %s]" % reason
            max_id = q("SELECT MAX(id) FROM analysis_results")[0][0]
            rep.add("C5 daily adds", fresh_status,
                    "max_id=%s judged(complete UTC days)=%s — %s | "
                    "%s=%s partial — reported, not judged"
                    % (max_id, adds, fresh_note,
                       today, day_rows.get(str(today), 0)),
                    "stale data — 'today's briefing' built from old rows")

            wk = q("SELECT week_start FROM weekly_reports ORDER BY id DESC LIMIT 1")
            wk_latest = wk[0][0] if wk else None
            snap = q("SELECT MAX(snapshot_date) FROM brainmap_snapshots")[0][0]
            # AUDIT-SPINE-BASELINE: expectation derived from the clock, and the
            # comparison is >= not ==, so only artifacts OLDER than the last due
            # Sunday 19:00 UTC run warn. A manual rebuild running ahead is normal.
            arts_status, arts_detail = spine_artifact_status(
                wk_latest, snap, datetime.now(timezone.utc))
            rep.add("C5 spine artifacts", arts_status, arts_detail,
                    "weekly page / brainmap serving an older batch than expected")

            cut7 = str(today - timedelta(days=7))
            n7, pub_null, dom_null = q(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN domain IS NULL OR domain='' THEN 1 ELSE 0 END) "
                "FROM analysis_results WHERE created_at >= :cut", cut=cut7)[0]
            pub_rate = 100.0 * (pub_null or 0) / n7 if n7 else 0.0
            dom_rate = 100.0 * (dom_null or 0) / n7 if n7 else 0.0
            rep.add("C5 7d null rates",
                    "WARN" if (pub_rate > 3 or dom_rate > 3) else "PASS",
                    "rows=%s published_at_null=%.1f%% domain_null=%.1f%% (warn>3%%)"
                    % (n7, pub_rate, dom_rate),
                    "undated/uncategorised rows degrade briefing filters")

            # C6 — recent-row quality.
            recent = q("SELECT claim_text, normalized_claims, claims, title "
                       "FROM analysis_results WHERE created_at >= :cut", cut=cut7)
            joins = sum(1 for ct, _, _, _ in recent
                        if SENTENCE_JOIN_RX.search(str(ct or "")))
            raw_boiler = sum(1 for ct, _, _, _ in recent
                             if broad_boiler(ct) or furniture_gate(ct))
            promo_boiler = sum(
                1 for ct, nc, cl, ti in recent
                if broad_boiler(promoted_pick(ct, nc, cl, ti)))
            n = len(recent) or 1
            join_rate = 100.0 * joins / n
            promo_rate = 100.0 * promo_boiler / n
            rep.add("C6 sentence-join", "WARN" if join_rate > 3 else "PASS",
                    "%d/%d = %.1f%% (known ~1%%, warn>3%%)" % (joins, len(recent), join_rate),
                    "claims render with sentences jammed together")
            rep.add("C6 boilerplate", "FAIL" if promo_boiler else "PASS",
                    "promotion-layer=%d/%d (%.2f%%, must be ~0); raw stored "
                    "claim_text=%d (reported only)" % (promo_boiler, len(recent),
                                                       promo_rate, raw_boiler),
                    "copyright furniture shown as an article's core claim")
        # ---- CHECK 7 — matcher consistency + official-leak scan ------------
        # (AUDIT-HARDENING; both crash-guarded so a failure here still lets
        # the rest of the audit report.)
        import subprocess
        import sys as _sys
        import tempfile

        rows7 = []
        # SUPPRESSION-UNIFY: the Python predicate's id set, for the parity
        # cross-check against the JS chain (None = not computed -> ERROR row,
        # never a silent skip).
        mismatch_ids = None
        try:
            with engine.connect() as conn:
                rows7 = conn.execute(sa.text(
                    "SELECT id, content_nature, source_candidates, "
                    "normalized_claims, claims, source_reliability_summary, "
                    "debug_summary, evidence_summary, source_reliability_reason "
                    "FROM analysis_results WHERE source_reliability_summary "
                    "LIKE '%\"has_genuine_official_support\": true%'")).fetchall()
        except Exception as err:
            rep.add("C7 matcher-consistency", "ERROR",
                    "genuine-row fetch crashed: %s" % str(err)[:120],
                    "wrong-period matches could grow unseen")
            # The leak scan shares this input — it must FAIL VISIBLY too,
            # never silently disappear from the table.
            rep.add("C7 leak-scan", "ERROR",
                    "input fetch crashed (see matcher-consistency) — scan "
                    "did not run", "the leak scan silently skipped")
            rep.add("C7 predicate-parity", "ERROR",
                    "input fetch crashed — parity not compared",
                    "screen and counts could disagree about official support")

        if not rows7:
            pass  # ERROR rows above already reported; nothing to scan
        else:
            # Reuse THE ported predicate (api_server.py, CLAIM-GRAPHS) — never
            # a third copy. Importing api_server builds the FastAPI app object
            # in memory; it starts no server, opens no DB connection and makes
            # no network call at import (the offline test suite imports it the
            # same way).
            try:
                if str(ROOT) not in _sys.path:
                    _sys.path.insert(0, str(ROOT))
                from api_server import _official_periodic_edition_mismatch
                mismatch_ids = sorted(
                    r[0] for r in rows7
                    if _official_periodic_edition_mismatch(r[2], r[3], r[4]))
                grown = sorted(set(mismatch_ids) - MATCHER_MISMATCH_KNOWN_IDS)
                mm_status = "WARN" if grown else "PASS"
                rep.add("C7 matcher-consistency", mm_status,
                        "genuine-flagged rows=%d period-mismatch=%d (baseline %d: %s)%s"
                        % (len(rows7), len(mismatch_ids),
                           MATCHER_MISMATCH_BASELINE,
                           sorted(MATCHER_MISMATCH_KNOWN_IDS),
                           ("; NEW ids=%s — GROWTH, investigate before send" % grown)
                           if grown else ""),
                        "a new wrong-period doc displayed as confirmed — the "
                        "defect fixed three times, recurring")
            except Exception as err:
                rep.add("C7 matcher-consistency", "ERROR",
                        "predicate run crashed: %s" % str(err)[:120],
                        "wrong-period matches could grow unseen")

            # Leak scan: the five official-assertion surfaces are frontend
            # display logic, so the scan EXECUTES the real main.js chain in
            # Node (scripts/official_leak_scan.js — committed, no scratch
            # harness). The audit feeds it the genuine-flagged rows and FAILS
            # if it cannot run: the gate is not passable while skipping it.
            try:
                dump = {str(r[0]): {
                    "content_nature": r[1], "source_candidates": r[2],
                    "normalized_claims": r[3], "claims": r[4],
                    "source_reliability_summary": r[5], "debug_summary": r[6],
                    "evidence_summary": r[7], "source_reliability_reason": r[8],
                } for r in rows7}
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".json", delete=False,
                        encoding="utf-8") as tf:
                    json.dump(dump, tf, ensure_ascii=False)
                    scan_input = tf.name
                proc = subprocess.run(
                    ["node", str(ROOT / "scripts" / "official_leak_scan.js"),
                     scan_input],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=180)
                tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
                if proc.returncode == 0:
                    rep.add("C7 leak-scan", "PASS", tail[0],
                            "an official-evidence assertion leaks on a "
                            "suppressed card (4th recurrence)")
                else:
                    rep.add("C7 leak-scan", "FAIL",
                            ((proc.stdout or "") + (proc.stderr or ""))
                            .strip().replace("\n", " / ")[-220:],
                            "an official-evidence assertion leaks on a "
                            "suppressed card (4th recurrence)")
                # SUPPRESSION-UNIFY: parity cross-check. The scan emits the id
                # set the JS predicate chain flagged over the SAME rows the
                # Python predicate just evaluated (mismatch_ids above). Any
                # difference means the screen and the counts disagree about
                # whether we assert official support — FAIL, never WARN.
                # Additional assertion only: the baseline growth check above
                # is untouched.
                js_ids = None
                for line in (proc.stdout or "").splitlines():
                    if line.startswith("JS_SUPPRESSED_IDS="):
                        try:
                            js_ids = sorted(json.loads(line.split("=", 1)[1]))
                        except ValueError:
                            js_ids = None
                if js_ids is None or mismatch_ids is None:
                    rep.add("C7 predicate-parity", "ERROR",
                            "id sets unavailable (js=%s py=%s) — parity not "
                            "compared; rerun"
                            % ("missing" if js_ids is None else "ok",
                               "missing" if mismatch_ids is None else "ok"),
                            "screen and counts could disagree about official "
                            "support")
                else:
                    only_js = sorted(set(js_ids) - set(mismatch_ids))
                    only_py = sorted(set(mismatch_ids) - set(js_ids))
                    if only_js or only_py:
                        rep.add("C7 predicate-parity", "FAIL",
                                "JS and Python predicates DISAGREE: "
                                "only_js=%s only_py=%s (js=%s py=%s) — one "
                                "implementation was changed without the other"
                                % (only_js, only_py, js_ids, mismatch_ids),
                                "the screen suppresses a match the counts "
                                "still include, or the reverse — the defect "
                                "class that shipped three times")
                    else:
                        rep.add("C7 predicate-parity", "PASS",
                                "JS set == Python set over %d rows: %s"
                                % (len(rows7), js_ids),
                                "screen and counts could disagree about "
                                "official support")
            except FileNotFoundError:
                rep.add("C7 leak-scan", "FAIL",
                        "node not found — the gate REQUIRES the scan: install "
                        "node or run `node scripts/official_leak_scan.js "
                        "<dump.json>` alongside and re-audit",
                        "the leak scan silently skipped")
                rep.add("C7 predicate-parity", "ERROR",
                        "scan did not run (node missing) — parity not compared",
                        "screen and counts could disagree about official support")
            except Exception as err:
                rep.add("C7 leak-scan", "ERROR",
                        "scan invocation crashed: %s" % str(err)[:120],
                        "the leak scan silently skipped")
                rep.add("C7 predicate-parity", "ERROR",
                        "scan invocation crashed — parity not compared",
                        "screen and counts could disagree about official support")

        # ---- CHECK 8 — card-render scan (CARD-RENDER-AUDIT) ----------------
        # Executes the REAL main.js render chain (scripts/card_render_audit.js
        # — committed sibling of the leak scan; baselines in
        # scripts/card_render_baselines.json) over the deterministic mod-14
        # sample + the latest-500 window and measures reader-visible defect
        # classes. ZERO classes (fixed leaks: English sentences, raw enums,
        # literal \uXXXX, mixed-scale, HTML-as-text) FAIL on ANY occurrence;
        # CEILING classes (bullet furniture, hero-restates-title, …) WARN
        # only on growth past their recorded baselines. An inability to run
        # is an ERROR — the gate is not passable while skipping it. Adds
        # ~35s (a ~13s dump of ~1,480 rows + a ~22s Node render pass).
        RENDER_COLS = ("title", "claim_text", "content_nature", "claims",
                       "normalized_claims", "evidence_snippets",
                       "evidence_sources", "source_candidates",
                       "source_reliability_summary",
                       "source_reliability_reason", "evidence_summary",
                       "debug_summary", "evidence_extraction_summary",
                       "contradiction_summary", "contradiction_checks",
                       "missing_context", "verdict_label",
                       "policy_alert_level")
        try:
            with engine.connect() as conn:
                max_id8 = conn.execute(sa.text(
                    "SELECT MAX(id) FROM analysis_results")).scalar()
                rows8 = conn.execute(sa.text(
                    "SELECT id, %s FROM analysis_results "
                    "WHERE MOD(id, 14) = 0 OR id > :cut ORDER BY id"
                    % ", ".join(RENDER_COLS)),
                    {"cut": (max_id8 or 0) - 500}).fetchall()
            dump8 = {"_meta": {"max_id": max_id8},
                     "rows": {str(r[0]): {
                         c: (None if v is None else str(v))
                         for c, v in zip(RENDER_COLS, r[1:])}
                         for r in rows8}}
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".json", delete=False,
                    encoding="utf-8") as tf8:
                json.dump(dump8, tf8, ensure_ascii=False)
                render_input = tf8.name
            proc8 = subprocess.run(
                ["node", str(ROOT / "scripts" / "card_render_audit.js"),
                 render_input],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300)
            out8 = (proc8.stdout or "") + (proc8.stderr or "")
            tail8 = [ln for ln in out8.splitlines()
                     if ln.startswith("RENDER SCAN")][-1:] or ["(no summary)"]
            # C8-WARN-VOCABULARY: a healthy scan emits ~10 warn lines that are
            # coverage/success disclosures (adapters covered, adapters skipped,
            # deliberate compat reads, a recorded note). Treating any warn line
            # as a defect made C8 structurally unable to PASS, so the row said
            # WARN on every clean run — a gate that always warns is one the
            # operator learns to skim. Only the measured (baseline-comparison)
            # channel is a defect signal; see classify_render_warns.
            signals8, disclosures8 = classify_render_warns(out8)
            disclosed8 = ("; %d coverage disclosure(s): %s"
                          % (len(disclosures8),
                             ", ".join(render_disclosure_labels(disclosures8)))
                          ) if disclosures8 else ""
            if proc8.returncode != 0:
                rep.add("C8 render-scan", "FAIL",
                        out8.strip().replace("\n", " / ")[-260:],
                        "machine text or a broken sentence reaches a "
                        "reader's card — the classes we only ever caught "
                        "by eye")
            elif signals8:
                rep.add("C8 render-scan", "WARN",
                        (tail8[0] + " / " + " / ".join(signals8))[:260],
                        "a display artefact is GROWING past its recorded "
                        "baseline")
            else:
                rep.add("C8 render-scan", "PASS",
                        (tail8[0] + disclosed8)[:260],
                        "machine text or a broken sentence reaches a "
                        "reader's card — the classes we only ever caught "
                        "by eye")
        except FileNotFoundError:
            rep.add("C8 render-scan", "FAIL",
                    "node not found — the gate REQUIRES the render scan: "
                    "install node or run `node scripts/card_render_audit.js "
                    "<dump.json>` alongside and re-audit",
                    "the render scan silently skipped")
        except Exception as err:
            rep.add("C8 render-scan", "ERROR",
                    "render-scan invocation crashed: %s" % str(err)[:140],
                    "the render scan silently skipped")
        engine.dispose()

    # ---------------- R1 — semantic reviewer (ADVISORY, opt-in) ------------
    # Every failure mode lands in advisory_rows; nothing here can raise into
    # the audit, and nothing here touches rep. If this whole block vanished the
    # verdict would be byte-identical.
    if with_reviewer:
        try:
            import subprocess  # local, mirroring CHECK 7/8's own local import
            procR = subprocess.run(REVIEWER_CMD, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=600)
            advisory_rows.append(reviewer_advisory_row(
                procR.returncode, (procR.stdout or "") + (procR.stderr or "")))
        except subprocess.TimeoutExpired:
            advisory_rows.append(("R1 semantic reviewer", "UNAVAILABLE",
                                  "probe exceeded its 600s timeout",
                                  "no semantic read was taken this run — "
                                  "deterministic rows above are unaffected"))
        except Exception as err:  # noqa: BLE001 — must never reach the audit
            advisory_rows.append(("R1 semantic reviewer", "UNAVAILABLE",
                                  "probe invocation crashed: %s" % str(err)[:140],
                                  "no semantic read was taken this run — "
                                  "deterministic rows above are unaffected"))

    # ---------------- output ----------------------------------------------
    if NETWORK_FAILURES:
        rep.add("NET reachability", "FAIL",
                "%d request(s) unreachable after retries: %s"
                % (len(NETWORK_FAILURES),
                   "; ".join(NETWORK_FAILURES[:6])
                   + ("; …" if len(NETWORK_FAILURES) > 6 else "")),
                "checks above may show failures caused by the OUTAGE, not the "
                "product — fix connectivity and rerun; never send on this run")
    p("")
    p("| check | status | observed | if it failed, a customer sees |")
    p("|---|---|---|---|")
    for check, statx, observed, impact in rep.rows:
        p("| %s | %s | %s | %s |" % (check, statx, observed.replace("|", "/"),
                                     impact))
    p("")
    # REVIEWER-INTO-AUDIT: printed BELOW the table and outside it, so it reads
    # as a notice a person acts on rather than a row in the verdict. Its status
    # words are deliberately NOT PASS/FAIL/WARN.
    if with_reviewer:
        p("ADVISORY (not part of the verdict — the rows above decide the send):")
        for label, statx, observed, note in advisory_rows:
            p("  [%s] %s — %s" % (statx, label, observed))
            p("      %s" % note)
        p("")
    else:
        p("ADVISORY: semantic reviewer not run (opt-in). Add --with-reviewer "
          "to include it; it spends ~$0.20 of Anthropic balance per run and "
          "can only ever print a HOLD notice, never change this verdict.")
        p("")
    p("live requests used: %d / %d" % (budget.used, budget.cap))
    worst = rep.worst()
    fails = [c for c, s, _, _ in rep.rows if s in ("FAIL", "ERROR")]
    warns = [c for c, s, _, _ in rep.rows if s == "WARN"]
    if worst == 2:
        p("VERDICT: fix %s first" % ", ".join(fails))
        # AUDIT-HARDENING: the gate's exit code now carries the verdict so a
        # scripted preflight cannot overlook a FAIL/ERROR table.
        return 1
    if warns:
        p("VERDICT: safe to send on 8/3 AFTER addressing warns: %s" % ", ".join(warns))
    else:
        p("VERDICT: safe to send on 8/3")
    return 0


# ---------------------------------------------------------------------------
# Selftest — offline, no network, no DB
# ---------------------------------------------------------------------------
def selftest() -> int:
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append("%s: got %r want %r" % (name, got, want))

    boiler_a = ("제보는 카카오톡 okjebo <저작권자(c) 연합뉴스, 무단 전재-재배포, "
                "AI 학습 및 활용 금지> 2026년06월20일 08시00분 송고")
    boiler_b = "무단 전재-재배포, AI 학습 및 활용 금지> 2026년06월30일 17시11분 송고"
    real_e = ("국토부는 노선버스에 지급 중인 유류세연동보조금과 유가연동보조금을 "
              "모두 지원하되 지급 단가는 노선버스의 50% 수준으로 적용한다.")
    real_g = "정부는 AI 학습 데이터의 무단 전재를 금지하는 저작권법 개정안을 발표했다."
    check("gate-boiler-a", furniture_gate(boiler_a), True)
    check("gate-boiler-b", furniture_gate(boiler_b), True)
    check("gate-real-e", furniture_gate(real_e), False)
    check("gate-real-g-quotes-markers", furniture_gate(real_g), False)
    check("broad-boiler-b", broad_boiler(boiler_b), True)
    check("broad-real-g", broad_boiler(real_g), False)

    check("join-hit", bool(SENTENCE_JOIN_RX.search("정책이다.다음 문장")), True)
    check("join-decimal", bool(SENTENCE_JOIN_RX.search("금리 1.5% 인상")), False)
    check("join-spaced", bool(SENTENCE_JOIN_RX.search("정책이다. 다음 문장")), False)

    pool = claim_pool(json.dumps([{"claim_text": "정상 주장"}]), json.dumps([boiler_b]))
    check("pool", pool, ["정상 주장", boiler_b])
    check("promoted-skips-boiler",
          promoted_pick(boiler_b, None, json.dumps(["실제 정책 주장이 여기에 있다"]), "제목"),
          "실제 정책 주장이 여기에 있다")
    check("promoted-title-fallback", promoted_pick(boiler_b, None, None, "제목"), "제목")

    env = parse_env_file(Path(__file__))  # non-env file -> harmless keys only
    check("env-parse-type", isinstance(env, dict), True)

    # AUDIT-SPINE-BASELINE — the derived spine expectation, pinned by CASE
    # rather than by date, so this can never go stale the way the two
    # hardcoded constants it replaced did.
    # SPINE-AFTER-COLLECTION: these four cases pin the PRE-MOVE schedule
    # explicitly (Sun 19:00) so their expected values keep meaning exactly what
    # they meant when they were written — they assert the arithmetic, not
    # whatever render.yaml happens to say today.
    OLD_SPINE = (6, 19, 0)
    tue = datetime(2026, 7, 28, 19, 0, tzinfo=timezone.utc)      # Tue
    check("spine-anchor-tue", last_expected_spine_run(tue, *OLD_SPINE).isoformat(),
          "2026-07-26T19:00:00+00:00")                            # prev Sunday
    check("spine-expected-tue", expected_spine_artifacts(tue, *OLD_SPINE),
          ("2026-07-20", "2026-07-26"))                           # Mon, Sun
    # Grace: at Sunday 19:30 UTC the chain is still running, so the run that
    # started 30 min ago is NOT yet due — the previous Sunday still governs.
    sun_during = datetime(2026, 7, 26, 19, 30, tzinfo=timezone.utc)
    check("spine-grace-during-run", expected_spine_artifacts(sun_during, *OLD_SPINE),
          ("2026-07-13", "2026-07-19"))
    # ...and once the grace has elapsed, that Sunday becomes the expectation.
    sun_after = datetime(2026, 7, 27, 2, 0, tzinfo=timezone.utc)
    check("spine-grace-elapsed", expected_spine_artifacts(sun_after, *OLD_SPINE),
          ("2026-07-20", "2026-07-26"))

    # The cron parser: only exactly-resolvable expressions are accepted.
    check("cron-parse-new", parse_cron_schedule("30 22 * * 0"), (6, 22, 30))
    check("cron-parse-old", parse_cron_schedule("0 19 * * 0"), (6, 19, 0))
    check("cron-parse-monday", parse_cron_schedule("0 4 * * 1"), (0, 4, 0))
    for bad in ("*/30 22 * * 0", "30 22 * * 0-3", "30 22 * * SUN",
                "30 22 1 * 0", "30 22 * * ", ""):
        check("cron-reject %r" % bad, parse_cron_schedule(bad), None)
    # The schedule actually shipped in render.yaml is readable and is the one
    # this audit assumes. A fallback would be named in the source string.
    wd, hh, mm, src = read_spine_schedule()
    check("spine-schedule-from-render-yaml", (wd, hh, mm), (6, 22, 30))
    check("spine-schedule-source-not-fallback", src.startswith("render.yaml"), True)
    # Post-move arithmetic: 22:30 + 6h grace comes due 04:30 Monday UTC.
    NEW_SPINE = (6, 22, 30)
    sun_mid_run = datetime(2026, 8, 2, 22, 45, tzinfo=timezone.utc)
    check("spine-new-during-run", expected_spine_artifacts(sun_mid_run, *NEW_SPINE),
          ("2026-07-20", "2026-07-26"))
    mon_after_grace = datetime(2026, 8, 3, 4, 45, tzinfo=timezone.utc)
    check("spine-new-grace-elapsed",
          expected_spine_artifacts(mon_after_grace, *NEW_SPINE),
          ("2026-07-27", "2026-08-02"))
    # The move must NOT change which DATES are expected — only when they fall
    # due — so a Monday-afternoon audit sees the same expectation either way.
    mon_pm = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    check("spine-move-changes-no-expected-dates",
          expected_spine_artifacts(mon_pm, *OLD_SPINE),
          expected_spine_artifacts(mon_pm, *NEW_SPINE))

    check("spine-current-passes",
          spine_artifact_status("2026-07-20", "2026-07-26", tue)[0], "PASS")
    check("spine-newer-manual-rebuild-passes",
          spine_artifact_status("2026-07-27", "2026-08-02", tue)[0], "PASS")
    one_stale = spine_artifact_status("2026-07-13", "2026-07-19", tue)
    check("spine-one-cycle-stale-warns", one_stale[0], "WARN")
    check("spine-one-cycle-stale-names-cycles",
          "weekly=2026-07-13 is 1 cycle(s) behind 2026-07-20" in one_stale[1], True)
    many_stale = spine_artifact_status("2026-06-29", "2026-07-05", tue)
    check("spine-several-cycles-stale-warns", many_stale[0], "WARN")
    check("spine-several-cycles-stale-counts",
          "snapshot=2026-07-05 is 3 cycle(s) behind 2026-07-26" in many_stale[1], True)
    # A partial miss (6 days, not a full 7) is still one missed Sunday.
    check("spine-partial-cycle-counts-as-one",
          "snapshot=2026-07-20 is 1 cycle(s) behind 2026-07-26"
          in spine_artifact_status("2026-07-20", "2026-07-20", tue)[1], True)
    check("spine-missing-artifact-warns",
          spine_artifact_status(None, "2026-07-26", tue)[0], "WARN")
    check("spine-missing-artifact-named",
          "weekly=missing" in spine_artifact_status(None, "2026-07-26", tue)[1], True)

    weeks = {"2026-07-14": {"top": [
        {"rank": 1, "lineage_id": "abc123", "representative_analysis_id": 1},
        {"rank": 2, "lineage_id": None, "representative_analysis_id": 42},
        {"rank": 3, "lineage_id": "", "representative_analysis_id": 43},
    ]}}
    fetched = []

    def fake_fetch(rid):
        fetched.append(rid)
        return {"found": True, "lineage_id": "def456"} if rid == 42 else {"found": False}

    res = resolve_weekly_links(weeks, fake_fetch)
    check("resolve-counts", res["2026-07-14"][:2], (2, 3))
    check("resolve-misses", res["2026-07-14"][2], ["rank3(rep=43)"])
    check("resolve-fetch-only-missing", fetched, [42, 43])

    payload = {"cluster": {"outlet_count": 78},
               "members": [{"policy_confidence_score": 55}],
               "bad": {"avg_score": 1}}
    hits = scan_forbidden_score_keys(payload)
    check("score-scan", hits, ["$.bad.avg_score"])

    # C8-WARN-VOCABULARY — both directions, on the scan's REAL line shapes.
    # The RATE lines are the only place the class/window vocabulary is typed,
    # and they come from the scan, exactly as at runtime.
    scan_healthy = "\n".join([
        "RATE [mod14] bullet_char: 650/1053 = 61.7% (baseline 60.7% +5.0pp)",
        "RATE [mod14] cand_tail: p90=6 p99=13 max=21 (baseline p90=6 p99=12)",
        "RATE [latest500] sentence_join: 3/500 = 0.6% (baseline 0.7% +1.0pp)",
        "RENDER-SCAN WARN: TRENDING NOTE: renderTrendingTop5 still numbers rows"
        " before filtering, so any null representative reaching it leaves a hole",
        "RENDER-SCAN WARN: ADAPTER-FIELD-CONTRACT covered: topicCardFromResult"
        " (1 holder site(s), 6 field read(s) verified against its return literal)",
        "RENDER-SCAN WARN: ADAPTER-FIELD-CONTRACT compat read: "
        "buildSlimHistoryRecord does not produce row.title, but the read falls"
        " back to a produced key or a literal",
        "RENDER-SCAN WARN: ADAPTER-FIELD-CONTRACT skipped: buildReviewQueueItem"
        " — return spreads `existingItem`, a runtime value read back from storage",
        "RENDER SCAN PASSED WITH WARNS: mod14=1053 latest500=500 rows, warns=4",
    ])
    healthy_signals, healthy_disclosures = classify_render_warns(scan_healthy)
    check("c8-healthy-no-signals", healthy_signals, [])
    check("c8-healthy-discloses-all-four", len(healthy_disclosures), 4)
    check("c8-disclosure-labels",
          render_disclosure_labels(healthy_disclosures),
          ["TRENDING NOTE", "ADAPTER-FIELD-CONTRACT covered",
           "ADAPTER-FIELD-CONTRACT compat read",
           "ADAPTER-FIELD-CONTRACT skipped"])
    # A genuine defect signal on the same output must still be a signal — both
    # the named-class shape and the cand_tail shape, which names no class and is
    # caught by its window tag alone.
    scan_defect = scan_healthy + "\n" + "\n".join([
        "RENDER-SCAN WARN: CEILING RISE [mod14] bullet_char: 71.2% > baseline"
        " 60.7% + 5.0pp (e.g. ids 14,28,42) — the artefact is GROWING",
        "RENDER-SCAN WARN: CEILING RISE [latest500] candidate-count tail: p90=19"
        " p99=44 vs baseline p90=6 p99=12 — cards are accumulating even more"
        " unrelated documents",
    ])
    defect_signals, defect_disclosures = classify_render_warns(scan_defect)
    check("c8-defect-both-signals", len(defect_signals), 2)
    check("c8-defect-names-class",
          defect_signals[0].startswith("CEILING RISE [mod14] bullet_char"), True)
    check("c8-defect-cand-tail-by-window",
          "candidate-count tail" in defect_signals[1], True)
    check("c8-defect-disclosures-unchanged", len(defect_disclosures), 4)
    check("c8-empty-input", classify_render_warns(""), ([], []))

    # C5-COLLAPSE-NOT-BAND — every branch of daily_adds_status, on real-shaped
    # numbers. The one that matters most: the ACTUAL regime shift (median-140
    # window falling to 51-adds days) must stay QUIET, while a trickle fails.
    tr_old_regime = [109, 120, 130, 145, 175, 141, 134, 73, 140, 82, 80, 88, 76, 80]
    check("c5-real-shift-quiet",
          daily_adds_status({"a": 51, "b": 74, "c": 75}, tr_old_regime)[0], "PASS")
    check("c5-zero-fails",
          daily_adds_status({"a": 51, "b": 0, "c": 75}, tr_old_regime)[0], "FAIL")
    check("c5-zero-names-day",
          "b" in daily_adds_status({"a": 51, "b": 0, "c": 75}, tr_old_regime)[1],
          True)
    collapse = daily_adds_status({"a": 80, "b": 76, "c": 5}, tr_old_regime)
    check("c5-trickle-warns", collapse[0], "WARN")
    check("c5-trickle-names-day-and-basis",
          "c=5" in collapse[1] and "median=" in collapse[1], True)
    check("c5-young-corpus-passes-with-note",
          daily_adds_status({"a": 10}, [])[0], "PASS")
    check("c5-dead-trailing-not-judgeable",
          "not judgeable" in daily_adds_status({"a": 10}, [0] * 14)[1], True)
    check("c5-zero-beats-dead-trailing",
          daily_adds_status({"a": 0}, [0] * 14)[0], "FAIL")

    if failures:
        print("SELFTEST FAIL (%d):" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("SELFTEST PASS (all parsing logic checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="B2B readiness audit (read-only)")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--selftest", action="store_true")
    # REVIEWER-INTO-AUDIT: OPT-IN, deliberately. The reviewer spends ~$0.20 of
    # a low Anthropic balance per run, and an audit the operator hesitates to
    # run is worse than one that skips this row by default. The deterministic
    # pre-send check stays free, offline-of-Anthropic, and instant.
    parser.add_argument("--with-reviewer", action="store_true",
                        help="also run the semantic reviewer probe and print "
                             "its result as an ADVISORY notice (~$0.20). It "
                             "can never change this audit's exit code.")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    return run_audit(args.base, with_reviewer=args.with_reviewer)


if __name__ == "__main__":
    sys.exit(main())
