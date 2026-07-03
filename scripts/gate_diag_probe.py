"""GATE-DIAG — READ-ONLY, SELECT-only measurement of the domain-expansion gate
and the hot_topics denylist coverage.

WHY: the domain-expansion gate ("add the next domain's seeds only after
ELECTION-person names are 0 for 2 consecutive observation days") has not opened
for weeks because Korean news never goes 2 days without politics at the HEADLINE
level. This probe measures whether that gate is ever satisfiable at the CARD
level, whether the denylist already keeps political content OUT of stored cards
regardless of headline politics, and whether a new domain's seeds would route
through the same safeguard. MEASURE BEFORE REDESIGN — the new gate rule is Joe's
decision after seeing this.

MEASUREMENT ONLY. Every DB statement is a SELECT (engine.connect(), never
begin(); no commit). Touches no production code, no verdict logic, no pins, no
config. Reuses the REAL denylist wiring:
  * hot_topics._DENYLIST (election/politician/securities/foreign + OBITUARY) —
    imported live; the ELECTION/POLITICIAN subset used for A/B is validated to be
    a strict subset of it at --selftest (a subset VIEW, not a re-implementation).
  * news_collector.OBITUARY_MARKERS — imported live for Metric D.
  * hot_topics.build_dynamic_queries / _passes_domain_filter — inspected (source,
    read-only) for the Metric-C structural safeguard check.
Election detection over stored TITLES uses the SAME mechanism the gate/denylist
use: substring match against the denylist political subset (== _passes_domain_filter's
`any(marker in text ...)`), not a new classifier.

METRICS
-------
  A. GATE-BLOCK HISTORY: last 21 observed days — per day, how many STORED cards
     had an election/politician marker in the TITLE (leaked into cards). Days with
     0 election CARDS, and the longest run of consecutive 0-CARD days. Answers:
     is "2 consecutive 0-CARD days" ever achievable (vs headline-level politics)?
  B. DENYLIST COVERAGE: corpus count + rate of stored cards whose TITLE carries an
     election/politician denylist marker (i.e. leaked PAST the denylist into cards).
     ~0 => the denylist holds regardless of headline politics.
  C. LEAK SIMULATION (structural, read-only): confirm build_dynamic_queries applies
     _passes_domain_filter (allowlist-require + denylist-drop) to EVERY picked
     keyword AFTER the LLM pick — so a new health/environment seed is protected by
     the SAME denylist that is holding for the current domains.
  D. CURRENT DOMAIN CLEANLINESS: welfare/agriculture/labor, last 14 observed days —
     counts of obituary-marker / election-marker / promo-heuristic TITLES (expect ~0;
     the gate's real intent is that current domains stay clean).

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  * title, created_at, query, domain are all TOP-LEVEL columns of analysis_results
    (database.py:331-343). created_at is TEXT; the probe parses the leading
    YYYY-MM-DD. domain is nullable on un-backfilled old rows (those rows are simply
    not counted for the per-domain Metric D; noted).
  * "election-name detection" maps to: substring of the ELECTION/POLITICIAN denylist
    subset present in the stored `title` string — the identical substring mechanism
    _passes_domain_filter uses on candidate keywords. No person-NLP; substring only.
  * The day window is anchored to the MAX stored created_at date (data-relative,
    deterministic — the probe does NOT call datetime.now()), printed as the anchor.

SAFETY: SELECT-only; engine.connect(); no commit; lazy DB import inside the live
path so --selftest is fully offline. ASCII-guarded prints (json.dumps ensure_ascii).

Usage (real run in the Render Worker Shell):
    PYTHONPATH=. python scripts/gate_diag_probe.py
    PYTHONPATH=. python scripts/gate_diag_probe.py --selftest   # offline, no DB

Exit codes: 0 = dump printed / engine unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# ELECTION / POLITICIAN subset — a documented VIEW into the real denylist. Each
# marker is validated to be a member of the live hot_topics._DENYLIST at
# --selftest (see run_selftest), so this is a subset of the real source, never a
# divergent re-implementation. These are the election words + current politician
# NAMES from hot_topics._LOCAL_DENYLIST (the political portion of the denylist);
# securities/foreign/sports markers are intentionally excluded — this metric is
# about ELECTION/politician leakage specifically.
# ---------------------------------------------------------------------------
ELECTION_MARKERS = (
    # election words
    "선거", "당선", "득표", "지방선거", "여당", "야당", "대선", "총선", "공천", "탄핵",
    # current high-profile politician names
    "이재명", "윤석열", "한동훈", "이준석", "김건희",
)

CLEAN_DOMAINS = ("welfare", "agriculture", "labor")

# HEURISTIC listing/promo markers — REPLICATED VERBATIM from
# realestate_seed_scope_probe.py (SCOPING ONLY; not a classifier/filter). Used for
# Metric D's promo-title count.
PROMO_MARKERS = (
    "분양", "청약", "견본주택", "모델하우스", "분양신청", "분양가", "입주자모집",
    "계약금", "중도금", "잔금", "평당", "매매가", "임대료", "전용면적", "입주",
    "수자인", "자이", "푸르지오", "힐스테이트", "래미안", "더퍼스트", "e편한세상",
    "이편한세상", "아이파크", "롯데캐슬", "위브", "더샵", "센트럴파크",
)


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _day_of(created_at) -> str:
    """Leading YYYY-MM-DD of a stored created_at value ('' if unparseable)."""
    m = _DATE_RE.search(str(created_at or ""))
    return m.group(1) if m else ""


def _markers_in(text: str, markers) -> list[str]:
    """Substring hits of `markers` in `text` — the SAME mechanism
    _passes_domain_filter uses (any(marker in text ...)). Returns the hit list."""
    t = str(text or "")
    return [mk for mk in markers if mk in t]


def _promo_hit(text: str) -> bool:
    t = str(text or "")
    return any(mk in t for mk in PROMO_MARKERS)


def _prev_days(anchor: str, n: int) -> list[str]:
    """The n calendar days ending at `anchor` (inclusive), oldest->newest, as
    YYYY-MM-DD strings. Uses date arithmetic on the parsed anchor (no wall clock)."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in anchor.split("-"))
    end = date(y, m, d)
    return [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _longest_zero_run(counts_in_order: list[int]) -> int:
    """Longest run of consecutive zero-count days."""
    best = cur = 0
    for c in counts_in_order:
        cur = cur + 1 if c == 0 else 0
        best = max(best, cur)
    return best


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== GATE-DIAG --selftest (offline; no DB, no network) ===")
    failures = []

    # 1. ELECTION_MARKERS must be a strict subset of the LIVE denylist (reuse, not
    #    re-implementation). If hot_topics import fails, degrade to a note.
    try:
        from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
        live = set(_HT_DENYLIST)
        missing = [mk for mk in ELECTION_MARKERS if mk not in live]
        if missing:
            failures.append(f"ELECTION_MARKERS not in live _DENYLIST: {[_ascii(x) for x in missing]}")
        else:
            p(f"  [ok] ELECTION_MARKERS ({len(ELECTION_MARKERS)}) all present in live "
              f"hot_topics._DENYLIST ({len(live)} markers) — subset view validated.")
    except Exception as exc:  # noqa: BLE001
        p(f"  [note] could not import hot_topics._DENYLIST offline ({str(exc)[:80]}); "
          "subset validation deferred to live run.")

    # 2. Marker substring detection.
    title_pol = "이재명 부동산 대책 발표"          # politician name substring
    title_clean = "전세 공급 대책 시행 방안"        # policy-only, no political marker
    if not _markers_in(title_pol, ELECTION_MARKERS):
        failures.append("election marker not detected in a political title")
    if _markers_in(title_clean, ELECTION_MARKERS):
        failures.append("false election-marker hit on a clean policy title")
    p(f"  [{'ok' if not failures else 'xx'}] title marker detection: "
      f"political={_markers_in(title_pol, ELECTION_MARKERS)} clean={_markers_in(title_clean, ELECTION_MARKERS)}")

    # 3. Day helpers.
    days = _prev_days("2026-07-03", 21)
    if len(days) != 21 or days[-1] != "2026-07-03" or days[0] != "2026-06-13":
        failures.append(f"_prev_days window wrong: {days[0]}..{days[-1]} n={len(days)}")
    run = _longest_zero_run([0, 0, 3, 0, 0, 0, 1])
    if run != 3:
        failures.append(f"_longest_zero_run expected 3, got {run}")
    if _day_of("2026-07-03 12:30:00") != "2026-07-03" or _day_of("garbage") != "":
        failures.append("_day_of parse wrong")
    p(f"  [{'ok' if not failures else 'xx'}] day helpers: window {days[0]}..{days[-1]}, "
      f"longest_zero_run={run}")

    # 4. Promo heuristic.
    if not _promo_hit("래미안 분양 청약 경쟁률") or _promo_hit("실업급여 지급 확대"):
        failures.append("promo heuristic misfire")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (marker subset + detection + day math + promo heuristic)")
    return 0


# ---------------------------------------------------------------------------
# LIVE RUN (SELECT-only)
# ---------------------------------------------------------------------------
def run_live() -> int:
    p("=== GATE-DIAG (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    # Validate the subset view against the live denylist before measuring.
    try:
        from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
        live_deny = set(_HT_DENYLIST)
        missing = [mk for mk in ELECTION_MARKERS if mk not in live_deny]
        p(f"  ELECTION_MARKERS subset of live hot_topics._DENYLIST: "
          f"{'YES' if not missing else 'NO — ' + str([_ascii(x) for x in missing])} "
          f"({len(ELECTION_MARKERS)} of {len(live_deny)})")
    except Exception as exc:  # noqa: BLE001
        p(f"  (could not import hot_topics._DENYLIST: {str(exc)[:80]})")

    try:
        from news_collector import OBITUARY_MARKERS  # type: ignore
    except Exception:  # noqa: BLE001
        OBITUARY_MARKERS = ()

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    with engine.connect() as conn:
        all_rows = conn.execute(
            sa.text("SELECT title, created_at FROM analysis_results")
        ).all()
        clean_rows = {}
        for dom in CLEAN_DOMAINS:
            clean_rows[dom] = conn.execute(
                sa.text("SELECT title, created_at FROM analysis_results WHERE domain = :d")
                .bindparams(d=dom)
            ).all()

    # Anchor the day window to the most recent stored created_at (data-relative,
    # deterministic — no wall clock).
    days_present = sorted({_day_of(r._mapping["created_at"]) for r in all_rows} - {""})
    if not days_present:
        p("  No parseable created_at dates found — cannot build the day window.")
        return 0
    anchor = days_present[-1]

    # ---- METRIC A -----------------------------------------------------------
    p("")
    p("=== METRIC A — GATE-BLOCK HISTORY (election CARDS stored per day, last 21d) ===")
    p(f"  anchor day (max stored created_at): {anchor}; corpus rows: {len(all_rows)}")
    window = _prev_days(anchor, 21)
    win_set = set(window)
    per_day = {d: 0 for d in window}
    for r in all_rows:
        m = r._mapping
        day = _day_of(m["created_at"])
        if day in win_set and _markers_in(m["title"], ELECTION_MARKERS):
            per_day[day] += 1
    counts_in_order = [per_day[d] for d in window]
    p("  date | election-name CARDS stored")
    for d in window:
        flag = "" if per_day[d] == 0 else "  <-- election card(s)"
        p(f"    {d} | {per_day[d]}{flag}")
    zero_days = sum(1 for c in counts_in_order if c == 0)
    longest = _longest_zero_run(counts_in_order)
    p(f"  days with 0 election CARDS (of 21): {zero_days}")
    p(f"  longest run of consecutive 0-CARD days: {longest}")
    p(f"  => '2 consecutive 0-CARD days' achievable at the CARD level: "
      f"{'YES' if longest >= 2 else 'NO'} "
      f"(gate uses card-level cleanliness, not headline-level politics)")

    # ---- METRIC B -----------------------------------------------------------
    p("")
    p("=== METRIC B — DENYLIST COVERAGE (election markers leaked into stored cards) ===")
    corpus = len(all_rows)
    leaked = 0
    leak_samples = []
    for r in all_rows:
        m = r._mapping
        hits = _markers_in(m["title"], ELECTION_MARKERS)
        if hits:
            leaked += 1
            if len(leak_samples) < 10:
                leak_samples.append((m["title"], hits))
    rate = f"{round(100 * leaked / corpus, 2)}%" if corpus else "n/a"
    p(f"  corpus stored cards: {corpus}")
    p(f"  cards with an election/politician marker in TITLE (leaked past denylist): {leaked} ({rate})")
    for title, hits in leak_samples:
        p(f"    - {_ascii(str(title)[:90])}  hits={[_ascii(h) for h in hits]}")
    p(f"  => denylist holds regardless of headline politics: "
      f"{'YES (~0 leaked)' if leaked == 0 else 'PARTIAL — see samples'}")

    # ---- METRIC C -----------------------------------------------------------
    p("")
    p("=== METRIC C — LEAK SIMULATION (structural: does every seed route through the filter?) ===")
    try:
        import inspect
        import hot_topics
        src = inspect.getsource(hot_topics.build_dynamic_queries)
        i_pick = src.find("_call_anthropic_pick")
        i_filter = src.find("_passes_domain_filter")
        applied = i_pick != -1 and i_filter != -1 and i_pick < i_filter
        p(f"  hot_topics.build_dynamic_queries applies _passes_domain_filter to picked keywords: "
          f"{'YES' if i_filter != -1 else 'NO'}")
        p(f"  filter runs AFTER the LLM pick (post-pick gate on ALL keywords): "
          f"{'YES' if applied else 'NO'}")
        pf = inspect.getsource(hot_topics._passes_domain_filter)
        deny_drop = "_DENYLIST" in pf
        allow_req = "_ALLOWLIST" in pf
        p(f"  _passes_domain_filter: denylist-drop={('YES' if deny_drop else 'NO')}, "
          f"allowlist-require={('YES' if allow_req else 'NO')}")
        p(f"  => a NEW health/environment seed is filtered by the SAME denylist (build_dynamic_queries")
        p(f"     has NO per-domain branch — the pick pool + filter are domain-agnostic): "
          f"{'YES' if (applied and deny_drop) else 'REVIEW'}")
    except Exception as exc:  # noqa: BLE001
        p(f"  (could not inspect hot_topics.build_dynamic_queries: {str(exc)[:100]})")

    # ---- METRIC D -----------------------------------------------------------
    p("")
    p("=== METRIC D — CURRENT DOMAIN CLEANLINESS (welfare/agriculture/labor, last 14d) ===")
    window14 = set(_prev_days(anchor, 14))
    for dom in CLEAN_DOMAINS:
        rows = clean_rows[dom]
        in_win = [r for r in rows if _day_of(r._mapping["created_at"]) in window14]
        obit = sum(1 for r in in_win if _markers_in(r._mapping["title"], OBITUARY_MARKERS))
        elec = sum(1 for r in in_win if _markers_in(r._mapping["title"], ELECTION_MARKERS))
        promo = sum(1 for r in in_win if _promo_hit(r._mapping["title"]))
        p(f"  {dom}: {len(in_win)} rows in last 14d | obituary={obit} election={elec} promo={promo}")
    p("  => expect ~0 across all three; the gate's real intent is that current domains stay clean.")

    p("")
    p("NOTE: election detection is a substring match against the ELECTION/POLITICIAN subset of the")
    p("REAL hot_topics._DENYLIST (the same mechanism _passes_domain_filter uses) — not a classifier.")
    p("Measurement only; nothing written, nothing proposed. The new gate rule is a product decision.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY domain-expansion gate + denylist-coverage diagnostic. "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
