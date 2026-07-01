"""OBIT-LEAK Phase 1 — READ-ONLY probe: are obituaries leaking via the PRIMARY
collector path (the same wiring gap the opinion leak had)?

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE /
ALTER. Touches no production code, no verdict logic, no pins. Mirrors the structure
of scripts/column_leak_probe.py and reads the ACTUAL filter surface from
news_collector (OBITUARY_MARKERS + _reject_title_reason) rather than a stale copy.

WHY
---
The COLUMN-LEAK fix proved _reject_title_reason was wired ONLY into the fallback
scrapers (via _accept_fallback_candidate) and never into the primary naver_api /
google_rss selection. The OBITUARY reject (M43-A, OBITUARY_MARKERS) lives in the
SAME _reject_title_reason and returns "obituary_or_funeral_notice" — so it has the
IDENTICAL wiring gap. The COLUMN-LEAK fix added a primary-path filter that drops
ONLY items whose reason == "opinion_or_column"; an obituary returns a DIFFERENT
reason, so the primary-path filter KEEPS it. => obituaries still leak on the primary
path. Obituaries are MORE sensitive than opinion (a real person's death), so we
measure the leak before proposing any fix.

BOUNDARY NOTE (important)
-------------------------
The recency split uses OBIT_FILTER_SHIP_DATE = "2026-06-06" — the M43 obituary
filter's OWN ship date (git commit 1ced230 "M43: block obituary titles …"), NOT the
2026-06-26 opinion COLUMN-FILTER date. 2026-06-06 is the correct "the obituary
filter already existed, so a leak after this is a real primary-path bypass"
boundary. (The opinion probe used 2026-06-26 because that is when ITS filter
shipped.) A row dated on/after 2026-06-06 that is an obituary is a genuine bypass.

WHAT IT MEASURES
----------------
  1. INVENTORY   — flag every row whose title looks like an obituary, via the ACTUAL
     OBITUARY_MARKERS (별세/부고/빈소/발인/영결식/장례식장/故) PLUS a small ADVISORY set of
     death-adjacent terms NOT in the marker set (사망/숨진/…) that a human should
     eyeball. Records id, created_at, era, matched token, MARKER vs ADVISORY,
     collection_source, title.
  2. REJECT-REPLAY — re-run _reject_title_reason (pure over the title) and bucket:
       OBIT-B1      = reason == "obituary_or_funeral_notice"  -> the filter WOULD
                      catch it, yet it is stored -> PRIMARY-PATH BYPASS (wiring gap).
       OBIT-B2      = reason is None (death-adjacent term NOT in the marker set)
                      -> coverage gap; needs a human eyeball.
       OBIT-B_OTHER = rejected for a different reason (opinion/too-short/…).
  3. COLLECTION-SOURCE CONFIRM — for OBIT-B1, cross-tab collection_source. If the
     leaked obituaries are naver_api / google_rss (not *_fallback), that is the
     data-side confirmation of the primary-path wiring gap.
  4. SENSITIVITY READ — for EACH post-ship-date flagged row, print the FULL title so
     the operator can judge (a) genuine obituary (block), (b) POLICY article that
     merely contains a death word (e.g. 산재 사망 예방 대책 / 고독사 대책 — ON-TOPIC,
     must NOT block — the obituary analog of FACTUAL-KEEP), or (c) borderline. The
     probe does NOT auto-decide.
  5. FAITHFULNESS + SUMMARY.

FAITHFULNESS / LIMITS
---------------------
  * REJECT-REPLAY is DEFINITIVE: _reject_title_reason is a pure title function,
    replayed over the real stored title with the IMPORTED (not copied) marker set.
    OBIT-B1 vs B2 is a hard fact about the current filter.
  * The MARKER-vs-ADVISORY split shows coverage: markers are what the filter checks;
    advisory tokens are death-adjacent words the filter does NOT check (candidate
    coverage — for a LATER decision, NOT proposed here).
  * collection_source = winning SEARCH ENGINE; naver_api/google_rss on an OBIT-B1
    row is the bypass evidence (the reject only runs on *_fallback scrapers).

SAFETY
------
  SELECT-only. Mirrors scripts/column_leak_probe.py: postgres_storage.get_engine(),
  engine.connect() (never begin()), no commit. ASCII-guarded prints so a Korean /
  mojibake title can never crash the Worker Shell.

Usage (real run happens in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/obit_leak_probe.py
    PYTHONPATH=. python scripts/obit_leak_probe.py --selftest   # offline, no DB

Requires for a real run: USE_POSTGRES_WRITE=true, DATABASE_URL=postgresql+psycopg://…

Exit codes: 0 = summary printed / engine unavailable / selftest passed; 1 = selftest
failed; 2 = CLI usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# Make the project root importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Render Worker Shell is UTF-8; reconfigure defensively with errors="replace" so an
# odd byte can never raise (mirrors scripts/column_leak_probe.py).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# SCAN WINDOW — top-of-file constant. Default None = the WHOLE corpus. Set to an
# int N to scan only the most recent N rows by id (id DESC).
# ---------------------------------------------------------------------------
SCAN_LAST_N_ROWS = None

# OBITUARY-filter ship date (M43, git commit 1ced230, 2026-06-06) — the pre/post
# recency boundary. A flagged row on/after this date is a genuine primary-path
# bypass (the obituary filter already existed). Compared as a 'YYYY-MM-DD' string
# prefix of created_at (lexicographic == chronological).
OBIT_FILTER_SHIP_DATE = "2026-06-06"


# ---------------------------------------------------------------------------
# Import the ACTUAL filter surface from news_collector — NOT a hard-coded copy.
# ---------------------------------------------------------------------------
from news_collector import (  # noqa: E402  (after sys.path / stdout setup)
    OBITUARY_MARKERS,
    _reject_title_reason,
    _normalize_spaces,
)

# The exact reason string the obituary branch returns (news_collector.py L484).
OBIT_REASON = "obituary_or_funeral_notice"

# ADVISORY death-adjacent tokens NOT in OBITUARY_MARKERS. These are DEATH SIGNALS
# the live filter does NOT check — a probe-side heuristic to surface possible
# coverage gaps for human review. Deliberately includes 사망 (which also appears in
# ON-TOPIC policy: 산재 사망 예방 / 사망사고 — those are the (b) case the operator must
# keep, NOT block). NOT proposed for production here.
ADVISORY_DEATH_TOKENS = (
    "사망", "숨진", "숨져", "타계", "영면", "서거", "순직", "유명을 달리",
)


def p(line: str = "") -> None:
    """ASCII-guarded print — prints the UTF-8 line directly; on any encode error
    falls back to a backslash-escaped ASCII rendering so the shell never chokes."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _date10(created_at) -> str:
    """'YYYY-MM-DD' from a str or datetime created_at; '(unknown)' if unusable."""
    if created_at is None:
        return "(unknown)"
    if isinstance(created_at, str):
        s = created_at.strip()
        return s[:10] if len(s) >= 10 else "(unknown)"
    try:
        return created_at.isoformat()[:10]
    except Exception:  # noqa: BLE001
        s = str(created_at)
        return s[:10] if len(s) >= 10 else "(unknown)"


def filter_era(created_at) -> str:
    """'pre' / 'post' / 'unknown' relative to OBIT_FILTER_SHIP_DATE."""
    day = _date10(created_at)
    if day == "(unknown)":
        return "unknown"
    return "post" if day >= OBIT_FILTER_SHIP_DATE else "pre"


def _collection_source(debug_summary_text) -> str:
    """Extract debug_summary.collection_source (winning SEARCH ENGINE, not entry
    path). '(unknown)' on NULL / non-str / parse failure / missing key."""
    if not debug_summary_text or not isinstance(debug_summary_text, str):
        return "(unknown)"
    try:
        parsed = json.loads(debug_summary_text)
    except Exception:  # noqa: BLE001 — malformed legacy JSON must not crash
        return "(unknown)"
    if not isinstance(parsed, dict):
        return "(unknown)"
    value = parsed.get("collection_source")
    if value is None:
        return "(unknown)"
    text = str(value).strip()
    return text or "(unknown)"


def classify_title(title: str) -> dict:
    """Pure obituary classification over the title. Returns:
        markers  : list of OBITUARY_MARKERS present (substring on normalized title)
        advisory : list of ADVISORY_DEATH_TOKENS present (non-marker death words)
        reason   : news_collector._reject_title_reason(title) verbatim
    """
    normalized = _normalize_spaces(title or "")
    markers = [m for m in OBITUARY_MARKERS if m in normalized]
    advisory = [a for a in ADVISORY_DEATH_TOKENS if a in normalized]
    reason = _reject_title_reason(title or "")
    return {"markers": markers, "advisory": advisory, "reason": reason}


def is_flagged(info: dict) -> bool:
    """A row looks obituary-ish iff a marker OR an advisory death token is present."""
    return bool(info["markers"] or info["advisory"])


def matched_via(info: dict) -> str:
    """'MARKER' if a real OBITUARY_MARKER matched (the filter checks these); else
    'ADVISORY' (flagged only by a death-adjacent word the filter does NOT check)."""
    return "MARKER" if info["markers"] else "ADVISORY"


def matched_token(info: dict) -> str:
    return "+".join(info["markers"]) if info["markers"] else "+".join(info["advisory"])


def bucket_of(info: dict) -> str:
    """OBIT-B1 = filter would catch (obituary reason); OBIT-B2 = filter passes (None);
    OBIT-B_OTHER = rejected for an unrelated reason (opinion/too-short/…)."""
    reason = info["reason"]
    if reason == OBIT_REASON:
        return "OBIT-B1"
    if reason is None:
        return "OBIT-B2"
    return "OBIT-B_OTHER"


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST — no DB. Validates the replay + the (b) policy-keep surfacing.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== OBIT-LEAK PROBE — OFFLINE SELF-TEST (no DB) ===")
    p(f"OBITUARY_MARKERS ({len(OBITUARY_MARKERS)}): {', '.join(OBITUARY_MARKERS)}")
    p(f"ADVISORY_DEATH_TOKENS ({len(ADVISORY_DEATH_TOKENS)}): "
      f"{', '.join(ADVISORY_DEATH_TOKENS)}")
    p(f"OBIT_FILTER_SHIP_DATE: {OBIT_FILTER_SHIP_DATE}")
    p("")
    failures = 0

    # (id, title, expect_reason(None means 'is None'), expect_bucket, expect_via, note)
    cases = [
        ("김대중 전 대통령 별세, 정부 국장 논의 빈소 마련",
         OBIT_REASON, "OBIT-B1", "MARKER",
         "별세/빈소 markers -> filter would catch -> primary bypass"),
        ("[부고] 홍길동 전 장관 모친상 발인 안내",
         OBIT_REASON, "OBIT-B1", "MARKER",
         "부고/발인 markers -> OBIT-B1"),
        ("정부, 청년 주거지원 3년 성과 발표 및 종합대책 확정",
         None, None, None,
         "clean policy title -> reason None, NOT flagged"),
        ("산재 사망 예방 대책, 국회 본회의 통과",
         None, "OBIT-B2", "ADVISORY",
         "(b) POLICY article w/ 사망 -> reason None, flagged ADVISORY -> operator KEEPS"),
    ]
    for title, exp_reason, exp_bucket, exp_via, note in cases:
        info = classify_title(title)
        reason_ok = (info["reason"] == exp_reason)
        if exp_bucket is None:
            # clean policy: expect NOT flagged
            flagged = is_flagged(info)
            ok = reason_ok and not flagged
            p(f"[{'PASS' if ok else 'FAIL'}] reason={info['reason']!r} flagged={flagged} "
              f"| {title[:45]}")
        else:
            flagged = is_flagged(info)
            bucket = bucket_of(info)
            via = matched_via(info)
            ok = reason_ok and flagged and bucket == exp_bucket and via == exp_via
            p(f"[{'PASS' if ok else 'FAIL'}] reason={info['reason']!r} bucket={bucket} "
              f"via={via} | {title[:45]}")
        if not ok:
            failures += 1
        p(f"        expect: reason={exp_reason!r} bucket={exp_bucket} via={exp_via}  ({note})")

    p("")
    if failures:
        p(f"SELF-TEST FAILED: {failures} case(s) mismatched.")
        return 1
    p("SELF-TEST PASSED: obituary markers -> OBIT-B1; clean policy -> None; "
      "산재 사망 policy -> OBIT-B2/ADVISORY (surfaced for the operator, NOT auto-blocked).")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obit_leak_probe",
        description="READ-ONLY probe: are obituaries leaking via the primary collector path.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Run the offline logic self-test (no DB) and exit.",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.selftest:
        return run_selftest()

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()
    p("=== OBIT-LEAK Phase 1 PROBE (READ-ONLY) ===")
    p(f"local: {now_local.isoformat(timespec='seconds')}")
    p(f"UTC:   {now_utc.isoformat(timespec='seconds')}")
    p(f"scan window: {'WHOLE CORPUS' if SCAN_LAST_N_ROWS is None else f'last {SCAN_LAST_N_ROWS} rows by id'}")
    p(f"OBIT_FILTER_SHIP_DATE (pre/post boundary): {OBIT_FILTER_SHIP_DATE} "
      f"(M43 obituary filter; distinct from the 2026-06-26 opinion COLUMN-FILTER)")
    p("")
    p(f"OBITUARY_MARKERS ({len(OBITUARY_MARKERS)}): {', '.join(OBITUARY_MARKERS)}")
    p(f"obituary reason string: {OBIT_REASON!r}")
    p(f"ADVISORY_DEATH_TOKENS (non-marker, probe-side): {', '.join(ADVISORY_DEATH_TOKENS)}")

    # Import postgres_storage AFTER argparse so --selftest / --help never require the
    # DB dependency (mirrors scripts/observe_daily.py + column_leak_probe.py).
    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("\nEngine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check that needs no DB.)")
        return 0

    sql = "SELECT id, created_at, title, debug_summary FROM analysis_results"
    if SCAN_LAST_N_ROWS is not None:
        sql += " ORDER BY id DESC LIMIT :lim"

    with engine.connect() as conn:
        stmt = sa.text(sql)
        if SCAN_LAST_N_ROWS is not None:
            stmt = stmt.bindparams(lim=int(SCAN_LAST_N_ROWS))
        rows = conn.execute(stmt).all()

    scanned = 0
    flagged_rows = []
    for r in rows:
        m = r._mapping
        scanned += 1
        title = m["title"] or ""
        info = classify_title(title)
        if not is_flagged(info):
            continue
        flagged_rows.append({
            "id": m["id"],
            "created_at": m["created_at"],
            "title": title,
            "info": info,
            "src": _collection_source(m["debug_summary"]),
            "era": filter_era(m["created_at"]),
        })

    # ---- SECTION 1: INVENTORY --------------------------------------------------
    p("")
    p("=== SECTION 1 — INVENTORY (flagged rows) ===")
    p("id | created_at | era | via | token | bucket | reject | src | title")
    if not flagged_rows:
        p("(no flagged rows)")
    for fr in flagged_rows:
        info = fr["info"]
        p(f"{fr['id']} | {str(fr['created_at'])[:19]} | {fr['era']} | {matched_via(info)} | "
          f"{matched_token(info)} | {bucket_of(info)} | {info['reason']!r} | "
          f"{fr['src']} | {str(fr['title'])[:70]}")

    # ---- SECTION 2: REJECT-REPLAY BUCKETS + date-split -------------------------
    buckets = {"OBIT-B1": [], "OBIT-B2": [], "OBIT-B_OTHER": []}
    for fr in flagged_rows:
        buckets[bucket_of(fr["info"])].append(fr)

    def _split(rows):
        return (
            [fr for fr in rows if fr["era"] == "pre"],
            [fr for fr in rows if fr["era"] == "post"],
            [fr for fr in rows if fr["era"] == "unknown"],
        )

    p("")
    p("=== SECTION 2 — REJECT-REPLAY BUCKETS (date-split around "
      f"{OBIT_FILTER_SHIP_DATE}) ===")
    for name, desc in (
        ("OBIT-B1", "filter WOULD catch (reason==obituary), yet stored -> PRIMARY BYPASS"),
        ("OBIT-B2", "filter PASSES (reason==None) -> coverage gap (death-adjacent, non-marker)"),
        ("OBIT-B_OTHER", "rejected for a DIFFERENT reason (opinion/too-short/…)"),
    ):
        pre, post, unk = _split(buckets[name])
        p(f"{name}: {desc}")
        p(f"    total={len(buckets[name])}  pre={len(pre)}  post={len(post)}"
          + (f"  unknown={len(unk)}" if unk else "")
          + f"  post_ids={[fr['id'] for fr in post][:20]}")

    # Full per-row list for POST rows (the ones that matter now).
    post_rows = [fr for fr in flagged_rows if fr["era"] == "post"]
    p("")
    p(f"--- POST-{OBIT_FILTER_SHIP_DATE} flagged rows (full list) ---")
    p("id | date | via | token | bucket | src | title")
    if not post_rows:
        p("(none)")
    for fr in post_rows:
        info = fr["info"]
        p(f"{fr['id']} | {_date10(fr['created_at'])} | {matched_via(info)} | "
          f"{matched_token(info)} | {bucket_of(info)} | {fr['src']} | {str(fr['title'])[:70]}")

    # ---- SECTION 3: COLLECTION-SOURCE CONFIRM (OBIT-B1) ------------------------
    p("")
    p("=== SECTION 3 — COLLECTION-SOURCE CONFIRM (OBIT-B1 = primary-bypass candidates) ===")
    b1 = buckets["OBIT-B1"]
    src_tab = {}
    for fr in b1:
        src_tab[fr["src"]] = src_tab.get(fr["src"], 0) + 1
    p(f"OBIT-B1 total = {len(b1)}")
    p("    by collection_source: "
      + (", ".join(f"{k}={v}" for k, v in sorted(src_tab.items(), key=lambda kv: (-kv[1], kv[0]))) or "(none)"))
    p("    A *_fallback source means the reject DID run (fallback scrapers call it).")
    p("    naver_api / google_rss / forced_* means the reject NEVER ran on that row")
    p("    -> data-side confirmation of the same primary-path wiring gap as the opinion")
    p("    leak. (reject-replay definitive; the fallback-vs-primary source is the")
    p("    bypass evidence.)")

    # ---- SECTION 4: SENSITIVITY READ (post rows, full titles) ------------------
    p("")
    p("=== SECTION 4 — SENSITIVITY READ (post rows; HUMAN JUDGMENT, no auto-decide) ===")
    p("Obituaries are sensitive. For EACH post-boundary flagged row, judge:")
    p("  (a) GENUINE OBITUARY  -> should never be a policy card (block).")
    p("  (b) POLICY w/ a death word (e.g. 산재 사망 예방 / 고독사 대책) -> ON-TOPIC, KEEP.")
    p("  (c) BORDERLINE        -> operator decides.")
    p("The (b) case is the obituary analog of the opinion FACTUAL-KEEP guardrail: a")
    p("policy piece that merely mentions death must NOT be blocked. The probe only")
    p("surfaces; it does not decide.")
    p("")
    if not post_rows:
        p("(no post-boundary flagged rows to review)")
    for fr in post_rows:
        info = fr["info"]
        p(f"  id={fr['id']} [{bucket_of(info)}/{matched_via(info)}] token={matched_token(info)} "
          f"src={fr['src']} date={_date10(fr['created_at'])}")
        p(f"    TITLE: {fr['title']}")

    # ---- SECTION 5: FAITHFULNESS + SUMMARY -------------------------------------
    p("")
    p("=== SECTION 5 — FAITHFULNESS NOTE ===")
    p("* REJECT-REPLAY is DEFINITIVE: _reject_title_reason is a pure title function,")
    p("  replayed over the real stored title with the IMPORTED OBITUARY_MARKERS.")
    p("  OBIT-B1 vs OBIT-B2 is a hard fact about the current filter.")
    p("* MARKER vs ADVISORY = coverage: markers are what the filter checks; advisory")
    p("  death tokens (사망/숨진/…) are NOT checked -> OBIT-B2 candidates for a LATER")
    p("  decision (NOT proposed here). 사망 also appears in ON-TOPIC policy (산재 사망 /")
    p("  사망사고) -> Section 4 (b): those must NOT be blocked.")
    p("* collection_source = winning SEARCH ENGINE; naver_api/google_rss on an OBIT-B1")
    p("  row is the primary-path bypass evidence.")

    b1_pre, b1_post, _ = _split(buckets["OBIT-B1"])
    b2_pre, b2_post, _ = _split(buckets["OBIT-B2"])
    advisory_tokens = {}
    for fr in flagged_rows:
        for tok in fr["info"]["advisory"]:
            advisory_tokens[tok] = advisory_tokens.get(tok, 0) + 1

    p("")
    p("=== SUMMARY ===")
    p(f"rows scanned:            {scanned}")
    p(f"flagged (obituary-like): {len(flagged_rows)}")
    p(f"OBIT-B1 (filter-would-catch-but-stored = BYPASS): total={len(buckets['OBIT-B1'])} "
      f"pre={len(b1_pre)} post={len(b1_post)}")
    p(f"OBIT-B2 (coverage gap, filter passes):            total={len(buckets['OBIT-B2'])} "
      f"pre={len(b2_pre)} post={len(b2_post)}")
    p(f"OBIT-B_OTHER (unrelated reject reason):           total={len(buckets['OBIT-B_OTHER'])}")
    p("")
    p(f"RECENT OBITUARY LEAK (post-{OBIT_FILTER_SHIP_DATE}) = OBIT-B1_post ({len(b1_post)}) "
      f"+ any OBIT-B2_post ({len(b2_post)}) the operator CONFIRMS as a true obituary "
      "(Section 4). B2 rows that are POLICY-with-a-death-word must NOT be counted.")
    p(f"distinct ADVISORY (non-marker) death tokens found: {len(advisory_tokens)}  "
      + (", ".join(f"{k}(x{v})" for k, v in sorted(advisory_tokens.items(), key=lambda kv: (-kv[1], kv[0]))) or "(none)"))
    p("  (listed for a LATER coverage decision — NOT proposed for the marker set now.)")

    p("\n[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
