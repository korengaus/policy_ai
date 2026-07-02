"""ENGINE-HEALTH-PROBE — READ-ONLY, SELECT-only measurement of the Fable Day1-2
top engine-health findings (checks A-F) against stored production rows.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE /
ALTER / commit. Touches no production code, no verdict logic, no pins. Mirrors the
structure + safety guards of scripts/obit_leak_probe.py and scripts/column_leak_probe.py
and REUSES the authoritative predicates from news_collector (imports the real
_reject_title_reason, OPINION_MARKERS, _has_opinion_bracket, OBITUARY_MARKERS,
_normalize_spaces) rather than re-implementing reject/marker logic.

WHY
---
The Day1-2 review surfaced candidate defects as MECHANISMS, not confirmed bugs. Our
rule is MEASURE BEFORE SURGERY (OBIT-LEAK had a real structural gap but 0 actual leak
-> not fixed). This probe COUNTS how often each top finding actually occurs in stored
rows so real can be told from phantom before any fix is designed. Nothing is fixed
here.

CHECKS
------
  A. [HIGH#1a resolve downgrade] rows with >=1 source_candidate where
     official_body_match == False AND official_body_match_score >= 62 (the enrich
     whole-body matcher scored >=62 but the FINAL match flag is False -> _resolve_source
     downgraded it).
  B. [HIGH#1b zero-sentence body] candidates with official_matched_sentences empty/None
     AND official_body_length > 0 (a body existed but no sentence matched); sub-split:
     how many of those also had official_body_match_score >= 62.
  C. [HIGH#2 low-confidence + supportive label] rows where policy_confidence_score
     (0-100) <= 20 AND verdict_label in the SUPPORTIVE tiers. Split ==20 (clamp) vs
     <20 (floor). Up to 5 examples.
  D. [HIGH#3 force-path bypass — NEW EVIDENCE vs COLUMN-LEAK] rows whose stored
     collection_source is in the FALLBACK set; for each, replay the REAL
     _reject_title_reason(title) and count non-None (would have been rejected),
     broken down by reason. Up to 5 example titles.
  E. [obit-precedence interaction — NEW EVIDENCE] among PRIMARY-source rows, titles
     where the REAL reject returns "obituary_or_funeral_notice" AND the title ALSO
     carries an OPINION marker / opinion-bracket (an opinion column leaking via the
     obit branch, since the obit reason is checked before the opinion reason). Up to 5.
  F. [MED summary over-trust] rows where source_reliability_summary
     .official_direct_match_classification == "strong_official_direct_support" AND
     has_genuine_official_support is not True.

FIELD-NAME NOTES (confirmed by grep before writing)
---------------------------------------------------
  * TOP-LEVEL columns (analysis_results): id, created_at, title,
    policy_confidence_score (Integer 0-100), verdict_label (Text), source_candidates
    (JSON TEXT list), source_reliability_summary (JSON TEXT dict), debug_summary
    (JSON TEXT dict).
  * collection_source lives INSIDE debug_summary JSON (NOT a column) — same as
    obit_leak_probe / column_leak_probe.
  * has_genuine_official_support lives INSIDE source_reliability_summary JSON
    (verification_card.py:727).
  * SURPRISE: the item-level `forced_fallback` flag (news_collector.py:741) is NOT
    persisted anywhere (no column, absent from debug_summary). So check D uses
    debug_summary.collection_source in the FALLBACK set as the faithful stored proxy
    for "row came through the fallback/force lanes", then relies on the DEFINITIVE
    reject-replay over the stored title. mode==forced_google_rss is surfaced as a
    related addendum (its collection_source is the PRIMARY value 'google_rss').

SUPPORTIVE TIERS
----------------
  _verdict_label (verification_card.py) emits these affirmative-support labels:
  draft_verified (L548, L559) and draft_likely_true (L561). Those are the two that
  ASSERT the news is verified / likely-true and are therefore contradictory next to a
  clamped confidence <=20. All other labels (draft_unverified / draft_disputed /
  draft_needs_official_confirmation / draft_needs_context / draft_needs_review /
  draft_high_risk_review) are cautionary/negative and are NOT counted as supportive.
  The task's explicit "NOT draft_unverified/disputed/needs_official_confirmation" is a
  subset of that non-supportive group. For transparency the probe also prints the FULL
  verdict_label breakdown among all confidence<=20 rows, so the strategist can widen
  the supportive set without re-running.

SAFETY
------
  SELECT-only. postgres_storage.get_engine() + engine.connect() (never begin()), no
  commit. Lazy DB import INSIDE the live path so --selftest is fully offline (no DB, no
  network). ASCII-guarded prints so a Korean / mojibake title can never crash the shell.

Usage (real run happens in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/engine_health_probe.py
    PYTHONPATH=. python scripts/engine_health_probe.py --selftest   # offline, no DB

Requires for a real run: USE_POSTGRES_WRITE=true, DATABASE_URL=postgresql+psycopg://...

Exit codes: 0 = summary printed / engine unavailable / selftest passed; 1 = selftest
failed; 2 = CLI usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Make the project root importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Render Worker Shell is UTF-8; reconfigure defensively with errors="replace" so an
# odd byte can never raise (mirrors scripts/obit_leak_probe.py).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# SCAN WINDOW — top-of-file constant. Default None = the WHOLE corpus. Set to an int
# N to scan only the most recent N rows by id (id DESC).
# ---------------------------------------------------------------------------
SCAN_LAST_N_ROWS = None


# ---------------------------------------------------------------------------
# Import the ACTUAL filter surface from news_collector — NOT a hard-coded copy.
# (obit_leak_probe.py imports the same module at top level and runs --selftest
# offline fine, so this import is safe without a DB / network.)
# ---------------------------------------------------------------------------
from news_collector import (  # noqa: E402  (after sys.path / stdout setup)
    OBITUARY_MARKERS,
    OPINION_MARKERS,
    _has_opinion_bracket,
    _normalize_spaces,
    _reject_title_reason,
)


# The exact reason string the obituary branch returns (news_collector.py:484).
OBIT_REASON = "obituary_or_funeral_notice"

# SUPPORTIVE verdict_label tiers — the affirmative-support outputs of
# verification_card._verdict_label (L548/L559 draft_verified, L561 draft_likely_true).
SUPPORTIVE_LABELS = frozenset({"draft_verified", "draft_likely_true"})

# Stored collection_source values that mean the row went through the fallback / force
# lanes (news_collector.py:1249/1260/1270). The item-level forced_fallback flag is NOT
# persisted, so this is the faithful stored proxy for check D.
FALLBACK_COLLECTION_SOURCES = frozenset({
    "naver_fallback",
    "daum_fallback",
    "forced_search_fallback",
})

# Stored collection_source values that mean the PRIMARY selection lane won
# (news_collector.py:1219/1226). Used by check E.
PRIMARY_COLLECTION_SOURCES = frozenset({"naver_api", "google_rss"})

# The enrich whole-body 'supports' floor (official_source_body.py:604). Used as the
# ">=62" cutoff in checks A and B.
ENRICH_SUPPORT_FLOOR = 62


def p(line: str = "") -> None:
    """ASCII-guarded print — prints the UTF-8 line directly; on any encode error falls
    back to a backslash-escaped ASCII rendering so the shell never chokes."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(text) -> str:
    """json.dumps(ensure_ascii=True) rendering of a string WITHOUT the surrounding
    quotes — guarantees no mojibake for Korean titles / URLs in the shell."""
    return json.dumps(str(text if text is not None else ""), ensure_ascii=True)[1:-1]


# ---------------------------------------------------------------------------
# Faithful JSON-blob readers (loose TEXT columns; malformed legacy JSON must not crash).
# ---------------------------------------------------------------------------
def _json_obj(value) -> dict:
    """Parse a JSON-TEXT column into a dict; {} on null/blank/non-dict/malformed."""
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value) -> list:
    """Parse a JSON-TEXT column into a list; [] on null/blank/non-list/malformed."""
    if isinstance(value, list):
        return value
    if not value or not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return []
    return parsed if isinstance(parsed, list) else []


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _collection_source(debug_summary_value) -> str:
    """debug_summary.collection_source (the winning SEARCH ENGINE). '(unknown)' on
    NULL / parse failure / missing key."""
    parsed = _json_obj(debug_summary_value)
    value = parsed.get("collection_source")
    if value is None:
        return "(unknown)"
    text = str(value).strip()
    return text or "(unknown)"


def _news_collection_mode(debug_summary_value) -> str:
    parsed = _json_obj(debug_summary_value)
    value = parsed.get("news_collection_mode")
    return str(value).strip() if value is not None else "(unknown)"


# ---------------------------------------------------------------------------
# Per-check pure evaluators (used by BOTH the live path and the self-test).
# ---------------------------------------------------------------------------
def check_A_row(source_candidates: list) -> bool:
    """True iff any candidate has official_body_match falsy AND
    official_body_match_score >= 62 (enrich matched >=62, final flag False)."""
    for cand in source_candidates or []:
        if not isinstance(cand, dict):
            continue
        if (not cand.get("official_body_match")) and _to_int(
            cand.get("official_body_match_score")
        ) >= ENRICH_SUPPORT_FLOOR:
            return True
    return False


def check_B_candidate(cand: dict) -> tuple[bool, bool]:
    """Returns (zero_sentence_body, and_score_ge_62) for one candidate.
    zero_sentence_body = official_matched_sentences empty/None AND
    official_body_length > 0."""
    if not isinstance(cand, dict):
        return False, False
    sentences = cand.get("official_matched_sentences")
    empty_sentences = not sentences  # None, missing, or []
    has_body = _to_int(cand.get("official_body_length")) > 0
    zero_sentence_body = bool(empty_sentences and has_body)
    and_score = bool(
        zero_sentence_body
        and _to_int(cand.get("official_body_match_score")) >= ENRICH_SUPPORT_FLOOR
    )
    return zero_sentence_body, and_score


def check_C_row(policy_confidence_score, verdict_label: str) -> str:
    """'clamp' (==20) / 'floor' (<20) / '' — for a supportive-labelled low-conf row."""
    conf = _to_int(policy_confidence_score)
    if conf > 20:
        return ""
    if (verdict_label or "") not in SUPPORTIVE_LABELS:
        return ""
    return "clamp" if conf == 20 else "floor"


def opinion_present(title: str) -> bool:
    """Reuse the REAL opinion predicate: substring OPINION_MARKERS OR opinion-bracket."""
    normalized = _normalize_spaces(title or "")
    if any(marker in normalized for marker in OPINION_MARKERS):
        return True
    return _has_opinion_bracket(normalized)


def check_E_title(title: str) -> bool:
    """True iff the REAL reject returns the obituary reason AND an opinion marker /
    opinion-bracket is also present (opinion column leaking via the obit branch)."""
    if _reject_title_reason(title or "") != OBIT_REASON:
        return False
    return opinion_present(title)


def check_F_row(source_reliability_summary: dict) -> bool:
    """True iff summary classification is strong_official_direct_support AND
    has_genuine_official_support is not True (explicit False OR missing)."""
    srs = source_reliability_summary or {}
    classification = srs.get("official_direct_match_classification")
    if classification != "strong_official_direct_support":
        return False
    return srs.get("has_genuine_official_support") is not True


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST — no DB, no network. Proves each check's logic on synthetic cases.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== ENGINE-HEALTH-PROBE — OFFLINE SELF-TEST (no DB) ===")
    p(f"OPINION_MARKERS ({len(OPINION_MARKERS)}) / OBITUARY_MARKERS ({len(OBITUARY_MARKERS)}) imported live.")
    p(f"SUPPORTIVE_LABELS: {sorted(SUPPORTIVE_LABELS)}")
    p(f"FALLBACK_COLLECTION_SOURCES: {sorted(FALLBACK_COLLECTION_SOURCES)}")
    p("")

    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    # ---- A / B --------------------------------------------------------------
    p("A/B (resolve downgrade + zero-sentence body):")
    expect("A", "cand{match:False,score:80} -> row flagged",
           check_A_row([{"official_body_match": False, "official_body_match_score": 80}]), True)
    expect("A", "cand{match:True,score:80} -> not flagged",
           check_A_row([{"official_body_match": True, "official_body_match_score": 80}]), False)
    expect("A", "cand{match:False,score:50} -> not flagged (<62)",
           check_A_row([{"official_body_match": False, "official_body_match_score": 50}]), False)
    zsb1, sc1 = check_B_candidate({"official_matched_sentences": [], "official_body_length": 500,
                                   "official_body_match_score": 80})
    expect("B", "{sentences:[],body_len:500,score:80} -> zero_sentence_body",
           zsb1, True)
    expect("B", "  ...and score>=62 sub-count",
           sc1, True)
    zsb2, _ = check_B_candidate({"official_matched_sentences": [{"sentence": "x"}],
                                 "official_body_length": 500})
    expect("B", "{sentences:[..],body_len:500} -> NOT zero_sentence_body",
           zsb2, False)
    zsb3, _ = check_B_candidate({"official_matched_sentences": [], "official_body_length": 0})
    expect("B", "{sentences:[],body_len:0} -> NOT zero_sentence_body",
           zsb3, False)

    # ---- C ------------------------------------------------------------------
    p("C (low-confidence + supportive label):")
    expect("C", "{conf:20,label:draft_likely_true} -> 'clamp'",
           check_C_row(20, "draft_likely_true"), "clamp")
    expect("C", "{conf:15,label:draft_verified} -> 'floor'",
           check_C_row(15, "draft_verified"), "floor")
    expect("C", "{conf:20,label:draft_unverified} -> '' (not supportive)",
           check_C_row(20, "draft_unverified"), "")
    expect("C", "{conf:80,label:draft_likely_true} -> '' (conf too high)",
           check_C_row(80, "draft_likely_true"), "")

    # ---- D ------------------------------------------------------------------
    p("D (force-path reject-replay — reused _reject_title_reason):")
    opinion_title = "[특별기고] 부동산 정책의 허상을 논한다"  # OPINION_MARKERS: 특별기고
    obit_title = "김철수 전 장관 별세... 빈소는 서울대병원"     # OBITUARY_MARKERS: 별세/빈소
    plain_title = "정부, 전세대출 금리 인하 방안 발표"          # a normal policy headline
    expect("D", "opinion column title -> reject non-None",
           _reject_title_reason(opinion_title) is not None, True)
    expect("D", "obituary title -> reject non-None",
           _reject_title_reason(obit_title) is not None, True)
    expect("D", "plain policy headline -> reject is None",
           _reject_title_reason(plain_title), None)

    # ---- E ------------------------------------------------------------------
    p("E (obit-precedence interaction — obit reason + opinion marker):")
    e_flag = "[특별기고] 故 홍길동 장관을 기리며 남긴 정책 유산"  # obit(故) + opinion(특별기고)
    e_realobit = "홍길동 전 장관 별세... 향년 80세"               # real obituary, NO opinion marker
    e_opinion = "[기고] 부동산 정책에 유감을 표한다"               # opinion_or_column, NOT obit branch
    expect("E", "'[특별기고] 故 ... 기리며' -> flagged (obit reason + opinion)",
           check_E_title(e_flag), True)
    expect("E", "'... 별세 ...' (real obit, no opinion) -> NOT flagged",
           check_E_title(e_realobit), False)
    expect("E", "'[기고] ... 유감' (opinion, not obit reason) -> NOT flagged by E",
           check_E_title(e_opinion), False)
    # Sanity: e_flag really does reject via the obit branch (precedence), not opinion.
    expect("E", "  precedence sanity: e_flag reject == obituary reason",
           _reject_title_reason(e_flag), OBIT_REASON)

    # ---- F ------------------------------------------------------------------
    p("F (summary strong + genuine not True):")
    expect("F", "{class:strong, genuine:False} -> counted",
           check_F_row({"official_direct_match_classification": "strong_official_direct_support",
                        "has_genuine_official_support": False}), True)
    expect("F", "{class:strong, genuine:True} -> not",
           check_F_row({"official_direct_match_classification": "strong_official_direct_support",
                        "has_genuine_official_support": True}), False)
    expect("F", "{class:medium, genuine:False} -> not (wrong class)",
           check_F_row({"official_direct_match_classification": "medium_official_contextual_support",
                        "has_genuine_official_support": False}), False)
    expect("F", "{class:strong, genuine:missing} -> counted (not True)",
           check_F_row({"official_direct_match_classification": "strong_official_direct_support"}), True)

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: all A-F checks proven on synthetic cases ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH — SELECT-only measurement against stored rows.
# ---------------------------------------------------------------------------
def run_live() -> int:
    p("=== ENGINE-HEALTH-PROBE (READ-ONLY, SELECT-only) ===")
    p(f"scan window: {'WHOLE CORPUS' if SCAN_LAST_N_ROWS is None else f'last {SCAN_LAST_N_ROWS} rows by id'}")
    p(f"SUPPORTIVE_LABELS: {sorted(SUPPORTIVE_LABELS)}")
    p("")

    # Import postgres_storage AFTER argparse so --selftest / --help never require the DB
    # dependency (mirrors obit_leak_probe.py + observe_daily.py).
    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check that needs no DB.)")
        return 0

    sql = (
        "SELECT id, created_at, title, policy_confidence_score, verdict_label, "
        "source_candidates, source_reliability_summary, debug_summary "
        "FROM analysis_results"
    )
    if SCAN_LAST_N_ROWS is not None:
        sql += " ORDER BY id DESC LIMIT :lim"

    with engine.connect() as conn:
        stmt = sa.text(sql)
        if SCAN_LAST_N_ROWS is not None:
            stmt = stmt.bindparams(lim=int(SCAN_LAST_N_ROWS))
        rows = conn.execute(stmt).all()

    scanned = 0
    # A
    a_rows = 0
    # B
    b_candidates = 0
    b_candidates_score_ge62 = 0
    # C
    c_clamp: list[dict] = []
    c_floor: list[dict] = []
    c_label_breakdown: dict = {}   # verdict_label -> count, among ALL conf<=20 rows
    # D
    d_fallback_rows = 0
    d_would_reject = 0
    d_reason_breakdown: dict = {}
    d_examples: list[dict] = []
    d_forced_google_rss_reject = 0  # addendum: mode==forced_google_rss with reject!=None
    # E
    e_primary_rows = 0
    e_flagged: list[dict] = []
    # F
    f_rows: list[dict] = []

    for r in rows:
        m = r._mapping
        scanned += 1
        rid = m["id"]
        title = m["title"] or ""
        candidates = _json_list(m["source_candidates"])
        srs = _json_obj(m["source_reliability_summary"])
        debug_val = m["debug_summary"]
        collection_source = _collection_source(debug_val)

        # ---- A ----
        if check_A_row(candidates):
            a_rows += 1

        # ---- B ---- (per-candidate)
        for cand in candidates:
            zsb, sc = check_B_candidate(cand)
            if zsb:
                b_candidates += 1
                if sc:
                    b_candidates_score_ge62 += 1

        # ---- C ----
        conf = _to_int(m["policy_confidence_score"])
        label = m["verdict_label"] or ""
        if conf <= 20:
            c_label_breakdown[label] = c_label_breakdown.get(label, 0) + 1
        bucket = check_C_row(m["policy_confidence_score"], label)
        if bucket == "clamp":
            c_clamp.append({"id": rid, "label": label, "conf": conf})
        elif bucket == "floor":
            c_floor.append({"id": rid, "label": label, "conf": conf})

        # ---- D ---- (fallback lanes, reject-replay)
        if collection_source in FALLBACK_COLLECTION_SOURCES:
            d_fallback_rows += 1
            reason = _reject_title_reason(title)
            if reason is not None:
                d_would_reject += 1
                d_reason_breakdown[reason] = d_reason_breakdown.get(reason, 0) + 1
                if len(d_examples) < 5:
                    d_examples.append({"id": rid, "reason": reason,
                                       "src": collection_source, "title": title})
        # addendum: the forced_google_rss mode also runs _force_select_best, but its
        # collection_source is the PRIMARY value 'google_rss' so it is not in the
        # fallback set above. Surface it separately (mode is stored in debug_summary).
        if _news_collection_mode(debug_val) == "forced_google_rss":
            if _reject_title_reason(title) is not None:
                d_forced_google_rss_reject += 1

        # ---- E ---- (primary-source rows, obit-precedence interaction)
        if collection_source in PRIMARY_COLLECTION_SOURCES:
            e_primary_rows += 1
            if check_E_title(title):
                e_flagged.append({"id": rid, "src": collection_source, "title": title})

        # ---- F ----
        if check_F_row(srs):
            f_rows.append({"id": rid, "class": srs.get("official_direct_match_classification"),
                           "genuine": srs.get("has_genuine_official_support")})

    # ---- COUNT TABLE ---------------------------------------------------------
    p("=== COUNT TABLE ===")
    p(f"rows scanned:                                        {scanned}")
    p(f"A [resolve downgrade] rows w/ cand match=False & score>=62:   {a_rows}")
    p(f"B [zero-sentence body] candidates (empty sentences, body>0):  {b_candidates}")
    p(f"    of which official_body_match_score >= 62:                 {b_candidates_score_ge62}")
    p(f"C [low-conf + SUPPORTIVE label] total:                        {len(c_clamp) + len(c_floor)}")
    p(f"    ==20 (clamp): {len(c_clamp)}    <20 (floor): {len(c_floor)}")
    p(f"D [fallback-lane rows] total:                                 {d_fallback_rows}")
    p(f"    of which title WOULD be rejected (reject-replay non-None): {d_would_reject}")
    p(f"    addendum mode==forced_google_rss & would-reject:           {d_forced_google_rss_reject}")
    p(f"E [primary-source rows]:                                      {e_primary_rows}")
    p(f"    obit-branch reason + opinion marker (interaction leak):    {len(e_flagged)}")
    p(f"F [summary strong & genuine!=True] rows:                      {len(f_rows)}")

    # ---- C label breakdown (transparency) -----------------------------------
    p("")
    p("=== C — verdict_label breakdown among ALL confidence<=20 rows ===")
    p("(SUPPORTIVE labels are the C count; others shown so the set can be widened.)")
    if not c_label_breakdown:
        p("(no rows with confidence<=20)")
    for lbl, cnt in sorted(c_label_breakdown.items(), key=lambda kv: (-kv[1], kv[0])):
        tag = "  <- SUPPORTIVE" if lbl in SUPPORTIVE_LABELS else ""
        p(f"    {lbl or '(empty)'}: {cnt}{tag}")

    # ---- C examples ----------------------------------------------------------
    p("")
    p("=== C — examples (up to 5; supportive label next to clamped/floored confidence) ===")
    c_examples = (c_clamp + c_floor)[:5]
    if not c_examples:
        p("(none)")
    for ex in c_examples:
        p(f"    id={ex['id']} | verdict_label={ex['label']} | policy_confidence_score={ex['conf']}")

    # ---- D examples ----------------------------------------------------------
    p("")
    p("=== D — reason breakdown + examples (fallback-lane rows whose title WOULD reject) ===")
    if not d_reason_breakdown:
        p("(no fallback-lane row has a would-reject title)")
    for reason, cnt in sorted(d_reason_breakdown.items(), key=lambda kv: (-kv[1], kv[0])):
        p(f"    {reason}: {cnt}")
    for ex in d_examples:
        p(f"    id={ex['id']} | src={ex['src']} | reason={ex['reason']} | title={_ascii(ex['title'])[:90]}")

    # ---- E examples ----------------------------------------------------------
    p("")
    p("=== E — examples (obit-branch reject + opinion marker, on PRIMARY rows) ===")
    if not e_flagged:
        p("(none — no primary-source obituary title also carries an opinion marker)")
    for ex in e_flagged[:5]:
        p(f"    id={ex['id']} | src={ex['src']} | title={_ascii(ex['title'])[:90]}")

    # ---- F examples ----------------------------------------------------------
    p("")
    p("=== F — examples (summary strong classification but has_genuine_official_support!=True) ===")
    if not f_rows:
        p("(none)")
    for ex in f_rows[:5]:
        p(f"    id={ex['id']} | class={ex['class']} | has_genuine_official_support={ex['genuine']!r}")

    # ---- FAITHFULNESS NOTE ---------------------------------------------------
    p("")
    p("=== FAITHFULNESS NOTE ===")
    p("* A/B/F read stored candidate/summary fields VERBATIM; no re-derivation.")
    p("* C reads top-level policy_confidence_score + verdict_label VERBATIM; SUPPORTIVE")
    p("  set = the affirmative _verdict_label outputs (draft_verified / draft_likely_true).")
    p("* D/E replay the REAL imported _reject_title_reason / OPINION predicate over the")
    p("  stored title — DEFINITIVE about the current filter. forced_fallback is NOT")
    p("  persisted, so D uses collection_source (in debug_summary) as the stored proxy.")
    p("* D/E are READ-ONLY NEW-EVIDENCE measurements, not reopenings of COLUMN-LEAK/OBIT-LEAK.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY SELECT-only measurement of the Day1-2 engine-health "
                    "findings (checks A-F). Use --selftest for the offline logic check.",
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="Run the OFFLINE synthetic-case logic check (no DB / network).",
    )
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
