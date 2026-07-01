"""COLUMN-LEAK Phase 1 / 1b — READ-ONLY probe: how did opinion/column pieces become
stored feed cards after the 6/26 COLUMN-FILTER shipped?

Phase 1b refinement (this revision) adds, on top of Phase 1's buckets:
  * B1 DATE-SPLIT around COLUMN_FILTER_SHIP_DATE — B1_pre (before the filter =
    EXPECTED, not a bypass) vs B1_post (on/after = candidate REAL mode1 bypass),
    with a full per-row dump of B1_post + its origin split; plus B2_pre/B2_post.
  * MODE2 TOKEN CLASSIFICATION — sorts the mode2 bracket tokens into OPINION /
    FACTUAL-KEEP / UNCLASSIFIED (probe-side ADVISORY heuristic, NOT applied to
    production) so a later fix blocks ONLY safe opinion labels, never a source
    like [정책브리핑].
  * A FUNNEL summary: recent genuine leak = B1_post + B2_post, and how B2_post
    splits across OPINION / FACTUAL-KEEP / UNCLASSIFIED (the real fix-target size).

This is a MEASUREMENT-ONLY diagnostic. Every database statement is a SELECT; the
script issues NO INSERT / UPDATE / DELETE / ALTER, never touches verdict logic,
the pipeline, the scheduler, the frontend, pins, or any test. It reads the ACTUAL
current filter surface from news_collector (it does NOT hard-code a stale copy of
the marker set) and re-runs it over the stored titles.

WHAT IT MEASURES
----------------
Two competing hypotheses for why a column headline is a stored row:

  mode1 (plumbing gap): the title DOES contain a marker that COLUMN-FILTER's
      news_collector._reject_title_reason would catch, yet the row entered via a
      path that bypassed that filter (suspected: the hot_topics keyword path —
      hot_topics extracts a keyword from a column headline, that keyword is
      searched, and the column re-enters and is analyzed without the collector
      reject running).

  mode2 (coverage gap): the bracket/label is simply NOT in the marker set, so the
      collector filter passes it by design (e.g. "[전문가의 눈]", "[규제의 역설]").

We DISTINGUISH the two by measurement, not assumption:

  1. INVENTORY   — scan every stored title with two independent tests:
     (a) DIRECT-MARKER — title contains a token from the ACTUAL OPINION_MARKERS
         set, OR sits in an opinion bracket (news_collector._has_opinion_bracket).
     (b) BRACKET     — title has a [..]/【..】 whose inner token is NOT a known
         opinion token (candidate mode2 labels; we print the distinct tokens).
  2. REJECT-REPLAY — re-run news_collector._reject_title_reason over each flagged
     stored title. That function is a PURE function of the title string (its
     `query` param is not referenced in the opinion/obituary branches), so the
     replay is FAITHFUL to the live filter: it answers "would the collector title
     filter have caught this title if it had gone through the collector?"
  3. BUCKET:
       B1 (mode1 candidate) = flagged AND reject == "opinion_or_column"
            -> the filter WOULD catch it, yet it is stored -> it bypassed the
               collector reject on the path it entered by.
       B2 (mode2 coverage gap) = flagged AND reject is None (passes entirely)
            -> the label is not in the marker set; the filter passes it by design.
       B_OTHER = flagged AND reject is some OTHER reason (obituary/too-short/…)
            -> reported separately; neither mode1 nor mode2.
  4. ORIGIN-CORRELATION — for flagged rows, the closest stored origin signal is
     the `query` column vs the fixed scheduler seed set (scheduler.DEFAULT_QUERIES,
     read via ast WITHOUT importing the heavy pipeline). See the FAITHFULNESS note
     below for the (important) limits of this signal.

FAITHFULNESS / LIMITS (printed at run time too)
-----------------------------------------------
  * REJECT-REPLAY is DEFINITIVE: _reject_title_reason is a pure title function, so
    "would the filter catch this title" is answered exactly, over the real stored
    string, using the imported (not copied) marker set.
  * ORIGIN is only SUGGESTIVE. There is NO stored field that records "this row
    entered via hot_topics vs a seed search." debug_summary.collection_source
    records only WHICH SEARCH ENGINE won (naver_api / google_rss / naver_fallback
    / daum_fallback / forced_search_fallback / none) — NOT the hot-topic-vs-seed
    origin. The only origin hint is the stored `query`: a query NOT in
    scheduler.DEFAULT_QUERIES is EITHER a dynamic hot-topic keyword OR a live user
    search — the DB alone cannot separate those two. So the mode1-vs-mode2 split is
    reported as: reject-replay definitive; origin suggestive.

SAFETY
------
  SELECT-only. Mirrors scripts/observe_daily.py: postgres_storage.get_engine(),
  engine.connect() (never begin()), no commit. ASCII-guarded prints so a Korean /
  mojibake title can never crash the Worker Shell.

Usage (real run happens in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/column_leak_probe.py
    PYTHONPATH=. python scripts/column_leak_probe.py --selftest   # offline logic check, no DB

Requires for a real run:
    USE_POSTGRES_WRITE=true
    DATABASE_URL=postgresql+psycopg://...   (postgres_storage handles the URL)

Exit codes:
    0 — summary printed, OR engine unavailable (clean message, no crash), OR
        --selftest passed
    1 — --selftest FAILED (offline logic regression)
    2 — CLI usage error (argparse)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# Make the project root importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Render Worker Shell is UTF-8; reconfigure defensively with errors="replace" so
# an odd byte can never raise (mirrors scripts/observe_daily.py).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# SCAN WINDOW — top-of-file constant. Default None = the WHOLE corpus. Set to an
# int N to scan only the most recent N rows by id (id DESC).
# ---------------------------------------------------------------------------
SCAN_LAST_N_ROWS = None

# ---------------------------------------------------------------------------
# COLUMN-FILTER ship date — the B1 date-split boundary (Phase 1b). Rows created
# BEFORE this date predate the filter (their being stored is EXPECTED, not a
# bypass); rows ON/AFTER are candidate REAL mode1 bypasses. Resolved from git:
# commit 54e5ce2 "COLUMN-FILTER: reject opinion/column pieces at collection",
# committed 2026-06-26 03:52 KST. Compared as a 'YYYY-MM-DD' string prefix of
# created_at (lexicographic == chronological). NOTE: the deploy landed ~03:52 KST
# on 06-26, so a handful of very-early-06-26 rows could be borderline pre-deploy;
# the whole ship date is treated as 'post' here (conservative — it can only
# OVER-count post, never hide a real bypass).
COLUMN_FILTER_SHIP_DATE = "2026-06-26"


# ---------------------------------------------------------------------------
# Import the ACTUAL filter surface from news_collector — NOT a hard-coded copy.
# These are all module-level and import-safe (no network at import).
# ---------------------------------------------------------------------------
from news_collector import (  # noqa: E402  (after sys.path/​stdout setup)
    OPINION_MARKERS,
    _OPINION_BRACKET_TOKENS,
    _OPINION_BRACKET_RE,
    _has_opinion_bracket,
    _reject_title_reason,
    _normalize_spaces,
)


def p(line: str = "") -> None:
    """ASCII-guarded print. Prints the (UTF-8) line directly; on any encode error
    falls back to a backslash-escaped ASCII rendering so the shell never chokes."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _norm_q(text) -> str:
    """Case/space-normalized query key — mirrors hot_topics._normalize so seed
    membership is compared the same way the merge dedup does."""
    return " ".join(str(text or "").split()).strip().lower()


def _load_default_queries():
    """Return (list_of_seed_queries | None, note). Reads scheduler.DEFAULT_QUERIES
    by AST-parsing scheduler.py — deliberately WITHOUT importing scheduler, which
    pulls in main.analyze_pipeline (the whole heavy pipeline). This keeps the probe
    light and side-effect-free while still reading the REAL (non-stale) seed list."""
    path = _PROJECT_ROOT / "scheduler.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DEFAULT_QUERIES":
                        seeds = list(ast.literal_eval(node.value))
                        return seeds, "scheduler.DEFAULT_QUERIES (ast-read, no import)"
    except Exception as error:  # noqa: BLE001 — degrade, never crash
        return None, f"unavailable ({error})"
    return None, "unavailable (DEFAULT_QUERIES not found in scheduler.py)"


def _collection_source(debug_summary_text) -> str:
    """Extract debug_summary.collection_source (the WINNING SEARCH ENGINE, not the
    hot-topic origin). '(unknown)' on NULL / non-str / parse failure / missing key.
    Pure-Python json.loads in try/except — debug_summary is loose TEXT, not jsonb."""
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


def _bracket_inner_tokens(title: str):
    """Return the inner token of every [..] / 【..】 chunk in the title, stripped of
    the bracket glyphs. Uses the SAME regex the filter uses (_OPINION_BRACKET_RE)."""
    tokens = []
    for chunk in _OPINION_BRACKET_RE.findall(title or ""):
        inner = chunk.strip("[]").strip("【】").strip()  # 【 = U+3010, 】 = U+3011
        if inner:
            tokens.append(inner)
    return tokens


def _token_is_known_opinion(token: str) -> bool:
    """True iff a bracket inner token is already covered by the filter — either it
    contains a substring OPINION_MARKER, or a bracket-scoped opinion token. Such a
    token is NOT a mode2 candidate (the filter already catches it)."""
    return (
        any(marker in token for marker in OPINION_MARKERS)
        or any(bt in token for bt in _OPINION_BRACKET_TOKENS)
    )


def classify_title(title: str) -> dict:
    """Pure title classification. Returns:
        direct          : list of substring OPINION_MARKERS present
        opinion_bracket : bool — filter's bracket-scoped opinion test fires
        bracket_tokens  : list of ALL bracket inner tokens (any [..]/【..】)
        unknown_tokens  : bracket tokens NOT already covered by the filter
                          (candidate mode2 labels)
        reject_reason   : news_collector._reject_title_reason(title) verbatim
    """
    raw = title or ""
    normalized = _normalize_spaces(raw)
    direct = [marker for marker in OPINION_MARKERS if marker in normalized]
    opinion_bracket = _has_opinion_bracket(normalized)
    bracket_tokens = _bracket_inner_tokens(raw)
    unknown_tokens = [t for t in bracket_tokens if not _token_is_known_opinion(t)]
    # _reject_title_reason normalizes internally and is a pure function of the
    # title (its `query` arg is unused in the opinion/obituary branches).
    reject_reason = _reject_title_reason(raw)
    return {
        "direct": direct,
        "opinion_bracket": opinion_bracket,
        "bracket_tokens": bracket_tokens,
        "unknown_tokens": unknown_tokens,
        "reject_reason": reject_reason,
    }


def is_flagged(info: dict) -> bool:
    """A row LOOKS like an opinion/column piece iff test (a) OR test (b):
      (a) a substring OPINION_MARKER is present, OR it sits in an opinion bracket;
      (b) it has ANY bracket whose inner token is NOT already a known opinion token
          (candidate mode2 label — advisory; the operator judges which are columns).
    """
    if info["direct"] or info["opinion_bracket"]:
        return True
    return bool(info["unknown_tokens"])


def matched_test(info: dict) -> str:
    """'DIRECT' when the filter's own opinion signal fires; else 'BRACKET' (flagged
    only by the unknown-bracket heuristic)."""
    if info["direct"] or info["opinion_bracket"]:
        return "DIRECT"
    return "BRACKET"


def matched_token(info: dict) -> str:
    """Human-readable matched token(s) for the row line."""
    if info["direct"]:
        return "+".join(info["direct"])
    if info["opinion_bracket"]:
        # a bracket token from the opinion set is what fired _has_opinion_bracket
        hit = [t for t in info["bracket_tokens"] if _token_is_known_opinion(t)]
        return "bracket:" + ("+".join(hit) if hit else "?")
    return "bracket:" + "+".join(info["unknown_tokens"])


def bucket_of(info: dict) -> str:
    """B1 = filter would catch (opinion_or_column); B2 = filter passes (None);
    B_OTHER = rejected for an unrelated reason."""
    reason = info["reject_reason"]
    if reason == "opinion_or_column":
        return "B1"
    if reason is None:
        return "B2"
    return "B_OTHER"


# ---------------------------------------------------------------------------
# Phase 1b — B1 DATE-SPLIT (pre/post COLUMN-FILTER ship date).
# ---------------------------------------------------------------------------
def _date10(created_at) -> str:
    """'YYYY-MM-DD' from a str or datetime created_at; '(unknown)' if unusable.
    Guards malformed/short/NULL so a bad timestamp can never crash the split."""
    if created_at is None:
        return "(unknown)"
    if isinstance(created_at, str):
        s = created_at.strip()
        return s[:10] if len(s) >= 10 else "(unknown)"
    try:
        return created_at.isoformat()[:10]  # datetime / date
    except Exception:  # noqa: BLE001 — unexpected type must not crash
        s = str(created_at)
        return s[:10] if len(s) >= 10 else "(unknown)"


def filter_era(created_at) -> str:
    """'pre' / 'post' / 'unknown' relative to COLUMN_FILTER_SHIP_DATE. Uses a
    'YYYY-MM-DD' string compare (lexicographic == chronological for that format)."""
    day = _date10(created_at)
    if day == "(unknown)":
        return "unknown"
    return "post" if day >= COLUMN_FILTER_SHIP_DATE else "pre"


# ---------------------------------------------------------------------------
# Phase 1b — MODE2 TOKEN CLASSIFICATION (probe-side heuristic; ADVISORY ONLY).
#
# THIS IS NOT A PRODUCTION FILTER. These sets live INSIDE the probe purely to
# SORT the mode2 bracket tokens into OPINION / FACTUAL-KEEP / UNCLASSIFIED so an
# operator can approve a precise, safe block-list in a LATER milestone. NONE of
# this is imported by, or applied to, news_collector / hot_topics. Blocking a
# FACTUAL/curation label ([정책브리핑] is a CORE source) would be an honesty/recall
# regression, so the classifier is deliberately CONSERVATIVE: FACTUAL wins ties,
# and factual matches use EXACT tokens + safe suffix rules (never a bare-substring
# like '전문', which is a substring of the opinion label '전문가의 눈').
# ---------------------------------------------------------------------------
# Opinion signals (personal column / byline). The highest-value rule is
# "ends with 칼럼" (the [○○ 칼럼] family). NOTE: in practice tokens containing an
# existing marker/bracket-token (칼럼/시선/시각/단상/시평/논단) are already caught by
# the live filter (they land in B1, not the mode2 set), so these rules mostly
# document intent + catch any that slip through; they are harmless if they never
# fire on the actual mode2 list.
_OPINION_SUFFIXES = ("칼럼", "눈")
_OPINION_SUBSTRINGS = ("시선", "시각", "단상", "직썰", "정조준", "역설")
_EXPLICIT_OPINION = {
    "규제의 역설", "전문가의 눈", "아하대만", "청계광장", "임나래 직썰",
    "양준서의 정조준", "주정완의 시선", "송윤주의 부동산생태계", "서리풀 연구通",
}
# Factual / news-section / curation labels — MUST NOT be blocked.
_EXPLICIT_FACTUAL = {
    "정책브리핑", "단독", "전문", "인터뷰", "밀착취재", "산업브리핑", "유통브리핑",
    "예산소식", "청양소식", "장성군 소식", "오늘의 정책 픽", "초밀착 정책 라이프",
    "안심전세정보안전망", "금융 Pick", "CEO PICK", "아침에 PICK",
}


def _is_factual_token(token: str) -> bool:
    """Exact-match + safe suffix/curation rules only (NO bare substrings that could
    swallow an opinion label)."""
    t = token.strip()
    if t in _EXPLICIT_FACTUAL:
        return True
    if t.endswith("브리핑") or t.endswith("소식"):
        return True
    low = t.lower()
    if "pick" in low or "픽" in t:      # curation picks ([오늘의 정책 픽], [금융 Pick])
        return True
    return False


def _is_opinion_token(token: str) -> bool:
    t = token.strip()
    if t in _EXPLICIT_OPINION:
        return True
    if any(t.endswith(suffix) for suffix in _OPINION_SUFFIXES):
        return True
    if any(sub in t for sub in _OPINION_SUBSTRINGS):
        return True
    return False


def classify_mode2_token(token: str) -> str:
    """OPINION / FACTUAL-KEEP / UNCLASSIFIED. FACTUAL wins ties — conservative: the
    dangerous error is mislabeling a factual SOURCE as opinion (-> proposing to
    block it), so factual matches are checked first."""
    if _is_factual_token(token):
        return "FACTUAL-KEEP"
    if _is_opinion_token(token):
        return "OPINION"
    return "UNCLASSIFIED"


def row_mode2_class(info: dict) -> str:
    """Classify a B2 row by its unknown token(s): OPINION if ANY token is opinion,
    else FACTUAL-KEEP if any is factual, else UNCLASSIFIED. (Opinion-presence wins
    for a ROW because that row IS a genuine leak target.)"""
    classes = [classify_mode2_token(t) for t in info["unknown_tokens"]]
    if "OPINION" in classes:
        return "OPINION"
    if "FACTUAL-KEEP" in classes:
        return "FACTUAL-KEEP"
    return "UNCLASSIFIED"


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST — validates the probe's logic against the 4 observed ids
# WITHOUT any DB. Runnable locally (no DATABASE_URL).
# ---------------------------------------------------------------------------
_SELFTEST_CASES = [
    # (id, title, expect_flagged, expect_test, expect_reject_is_opinion, note)
    (546, "[기고] 비수도권 세제 차등 지원으로 지역 균형발전을 이끌어야 한다는 제언",
     True, "DIRECT", True, "기고 IS a direct marker -> B1 (mode1 candidate)"),
    (512, "[보라매칼럼] 부동산 세제개편 논의가 시장에 던지는 신호를 읽는 법",
     True, "DIRECT", True, "칼럼 substring + opinion bracket -> B1 (mode1 candidate)"),
    (559, "[전문가의 눈] 농업위성 원년, 정밀농업 시대를 여는 정책 과제를 짚어본다",
     True, "BRACKET", False, "전문가의 눈 NOT in marker set -> filter passes -> B2 (mode2)"),
    (529, "[규제의 역설] 금리·전세·공급 정책이 서로 부딪칠 때 생기는 왜곡을 분석한다",
     True, "BRACKET", False, "규제의 역설 NOT in marker set -> filter passes -> B2 (mode2)"),
]


def run_selftest() -> int:
    p("=== COLUMN-LEAK PROBE — OFFLINE SELF-TEST (no DB) ===")
    p(f"OPINION_MARKERS ({len(OPINION_MARKERS)}): {', '.join(OPINION_MARKERS)}")
    p(f"_OPINION_BRACKET_TOKENS ({len(_OPINION_BRACKET_TOKENS)}): "
      f"{', '.join(_OPINION_BRACKET_TOKENS)}")
    p("")
    failures = 0
    for row_id, title, exp_flagged, exp_test, exp_reject_opinion, note in _SELFTEST_CASES:
        info = classify_title(title)
        flagged = is_flagged(info)
        test = matched_test(info)
        bucket = bucket_of(info)
        reject_is_opinion = (info["reject_reason"] == "opinion_or_column")
        ok = (
            flagged == exp_flagged
            and test == exp_test
            and reject_is_opinion == exp_reject_opinion
        )
        if not ok:
            failures += 1
        p(f"[{'PASS' if ok else 'FAIL'}] id {row_id} | test={test} bucket={bucket} "
          f"reject={info['reject_reason']!r}")
        p(f"        title: {title[:70]}")
        p(f"        expect: flagged={exp_flagged} test={exp_test} "
          f"reject_is_opinion={exp_reject_opinion}  ({note})")

    # Phase 1b — mode2 token classifier assertions.
    p("")
    p("--- mode2 token classifier ---")
    token_cases = [
        ("규제의 역설", "OPINION"),
        ("전문가의 눈", "OPINION"),
        ("임나래 직썰", "OPINION"),
        ("양준서의 정조준", "OPINION"),
        ("박근종 칼럼", "OPINION"),          # ends-with 칼럼 rule
        ("정책브리핑", "FACTUAL-KEEP"),      # CORE source — must not block
        ("단독", "FACTUAL-KEEP"),
        ("전문", "FACTUAL-KEEP"),            # exact — NOT swallowed by opinion '전문가의 눈'
        ("산업브리핑", "FACTUAL-KEEP"),      # endswith 브리핑
        ("예산소식", "FACTUAL-KEEP"),        # endswith 소식
        ("오늘의 정책 픽", "FACTUAL-KEEP"),  # curation pick
        ("금융 Pick", "FACTUAL-KEEP"),
        ("기자24시", "UNCLASSIFIED"),        # borderline -> human eyeball
    ]
    for token, expected in token_cases:
        got = classify_mode2_token(token)
        ok = got == expected
        if not ok:
            failures += 1
        p(f"[{'PASS' if ok else 'FAIL'}] {token} -> {got} (expect {expected})")

    # Phase 1b — date-split (filter_era) assertions.
    p("")
    p(f"--- filter_era (COLUMN_FILTER_SHIP_DATE = {COLUMN_FILTER_SHIP_DATE}) ---")
    era_cases = [
        ("2026-06-06 12:00:00", "pre"),
        ("2026-06-19T09:30:00", "pre"),
        ("2026-06-26 04:00:00", "post"),
        ("2026-06-30 08:15:00", "post"),
        ("2026-07-01", "post"),
        (None, "unknown"),
    ]
    for created_at, expected in era_cases:
        got = filter_era(created_at)
        ok = got == expected
        if not ok:
            failures += 1
        p(f"[{'PASS' if ok else 'FAIL'}] {created_at!r} -> {got} (expect {expected})")

    p("")
    if failures:
        p(f"SELF-TEST FAILED: {failures} case(s) mismatched.")
        return 1
    p("SELF-TEST PASSED: 4 observed ids (546/512 -> B1 mode1; 559/529 -> B2 mode2) "
      "+ mode2 token classifier + filter_era date-split all correct.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="column_leak_probe",
        description="READ-ONLY probe: how did opinion/column pieces become stored cards.",
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

    # HEADER — printed before any DB work so the operator always sees when the
    # snapshot was taken, even if the engine turns out to be unavailable.
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()
    p("=== COLUMN-LEAK Phase 1b PROBE (READ-ONLY) ===")
    p(f"local: {now_local.isoformat(timespec='seconds')}")
    p(f"UTC:   {now_utc.isoformat(timespec='seconds')}")
    p(f"scan window: {'WHOLE CORPUS' if SCAN_LAST_N_ROWS is None else f'last {SCAN_LAST_N_ROWS} rows by id'}")
    p(f"COLUMN_FILTER_SHIP_DATE (B1 date-split boundary): {COLUMN_FILTER_SHIP_DATE}")

    # Echo the ACTUAL filter surface being replayed (imported, not copied).
    p("")
    p(f"OPINION_MARKERS ({len(OPINION_MARKERS)}): {', '.join(OPINION_MARKERS)}")
    p(f"_OPINION_BRACKET_TOKENS ({len(_OPINION_BRACKET_TOKENS)}): "
      f"{', '.join(_OPINION_BRACKET_TOKENS)}")

    seed_queries, seed_note = _load_default_queries()
    seed_norm = {_norm_q(q) for q in (seed_queries or [])}
    p(f"seed set for origin inference: {seed_note}"
      + (f" — {len(seed_queries)} queries" if seed_queries else ""))

    # Import postgres_storage AFTER argparse so --selftest/--help never require the
    # DB dependency (mirrors scripts/observe_daily.py).
    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("\nEngine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check that needs no DB.)")
        return 0

    sql = "SELECT id, created_at, query, title, debug_summary FROM analysis_results"
    if SCAN_LAST_N_ROWS is not None:
        sql += " ORDER BY id DESC LIMIT :lim"

    # All reads in a single read-only connection. engine.connect() (not begin())
    # — no transaction is committed; we only ever SELECT.
    with engine.connect() as conn:
        stmt = sa.text(sql)
        if SCAN_LAST_N_ROWS is not None:
            stmt = stmt.bindparams(lim=int(SCAN_LAST_N_ROWS))
        rows = conn.execute(stmt).all()

    scanned = 0
    flagged_rows = []          # list of dicts with id/created_at/query/title/info/src/origin
    for r in rows:
        m = r._mapping
        scanned += 1
        title = m["title"] or ""
        info = classify_title(title)
        if not is_flagged(info):
            continue
        query = m["query"] or ""
        origin = "seed" if _norm_q(query) in seed_norm else "non-seed"
        flagged_rows.append({
            "id": m["id"],
            "created_at": m["created_at"],
            "query": query,
            "title": title,
            "info": info,
            "src": _collection_source(m["debug_summary"]),
            "origin": origin,
        })

    # ---- SECTION 1: INVENTORY (per flagged row) --------------------------------
    p("")
    p("=== SECTION 1 — INVENTORY (flagged rows) ===")
    p("id | created_at | TEST | token | bucket | reject | src | origin | title")
    if not flagged_rows:
        p("(no flagged rows)")
    for fr in flagged_rows:
        info = fr["info"]
        p(f"{fr['id']} | {str(fr['created_at'])[:19]} | {matched_test(info)} | "
          f"{matched_token(info)} | {bucket_of(info)} | {info['reject_reason']!r} | "
          f"{fr['src']} | {fr['origin']} | {str(fr['title'])[:80]}")

    # ---- SECTION 2: REJECT-REPLAY BUCKETS --------------------------------------
    buckets = {"B1": [], "B2": [], "B_OTHER": []}
    for fr in flagged_rows:
        buckets[bucket_of(fr["info"])].append(fr)
    p("")
    p("=== SECTION 2 — REJECT-REPLAY BUCKETS ===")
    p("B1 (mode1 candidate): filter WOULD catch (reject == opinion_or_column), "
      "yet stored -> it bypassed the collector reject.")
    p(f"    count = {len(buckets['B1'])}  sample ids = "
      f"{[fr['id'] for fr in buckets['B1'][:10]]}")
    p("B2 (mode2 coverage gap): filter PASSES (reject == None) -> label not in the "
      "marker set by design.")
    p(f"    count = {len(buckets['B2'])}  sample ids = "
      f"{[fr['id'] for fr in buckets['B2'][:10]]}")
    p("B_OTHER: flagged by heuristic but rejected for an UNRELATED reason "
      "(obituary/too-short/…); neither mode1 nor mode2.")
    p(f"    count = {len(buckets['B_OTHER'])}  sample ids = "
      f"{[fr['id'] for fr in buckets['B_OTHER'][:10]]}")

    # ---- SECTION 2b: B1/B2 DATE-SPLIT around the COLUMN-FILTER ship date --------
    # B1_pre  = created_at <  ship date  -> EXPECTED (no filter yet), NOT a bypass.
    # B1_post = created_at >= ship date  -> candidate REAL mode1 bypass.
    def _split_era(rows):
        pre = [fr for fr in rows if filter_era(fr["created_at"]) == "pre"]
        post = [fr for fr in rows if filter_era(fr["created_at"]) == "post"]
        unk = [fr for fr in rows if filter_era(fr["created_at"]) == "unknown"]
        return pre, post, unk

    b1_pre, b1_post, b1_unk = _split_era(buckets["B1"])
    b2_pre, b2_post, b2_unk = _split_era(buckets["B2"])

    p("")
    p("=== SECTION 2b — B1/B2 DATE-SPLIT ===")
    p(f"boundary: COLUMN_FILTER_SHIP_DATE = {COLUMN_FILTER_SHIP_DATE} "
      f"(created_at < boundary = pre = expected; >= boundary = post = candidate leak)")
    p(f"B1_pre  (before filter, EXPECTED)          = {len(b1_pre)}  "
      f"ids={[fr['id'] for fr in b1_pre]}")
    p(f"B1_post (on/after filter, CANDIDATE BYPASS) = {len(b1_post)}  "
      f"ids={[fr['id'] for fr in b1_post]}")
    if b1_unk:
        p(f"B1_unknown (unparseable created_at)        = {len(b1_unk)}  "
          f"ids={[fr['id'] for fr in b1_unk]}")
    p(f"B2_pre  (before filter)                     = {len(b2_pre)}")
    p(f"B2_post (on/after filter)                   = {len(b2_post)}")
    if b2_unk:
        p(f"B2_unknown                                 = {len(b2_unk)}")

    # Per-row dump of the genuine-bypass candidates (B1_post) so we can eyeball them.
    p("")
    p("--- B1_post rows (candidate REAL mode1 bypasses) — full lines ---")
    p("id | date | token | query | origin | title")
    if not b1_post:
        p("(none — no B1 row was created on/after the filter ship date)")
    for fr in b1_post:
        info = fr["info"]
        p(f"{fr['id']} | {_date10(fr['created_at'])} | {matched_token(info)} | "
          f"{str(fr['query'])[:30]} | {fr['origin']} | {str(fr['title'])[:70]}")
    # B1_post origin split (the mode1 signal) — origin is SUGGESTIVE (see Section 5).
    b1p_seed = [fr for fr in b1_post if fr["origin"] == "seed"]
    b1p_nonseed = [fr for fr in b1_post if fr["origin"] == "non-seed"]
    p(f"B1_post origin: seed={len(b1p_seed)} ids={[fr['id'] for fr in b1p_seed]}  |  "
      f"non-seed(dynamic-kw OR user)={len(b1p_nonseed)} ids={[fr['id'] for fr in b1p_nonseed]}")

    # ---- SECTION 3: ORIGIN-CORRELATION (of B1 = mode1 candidates) --------------
    p("")
    p("=== SECTION 3 — ORIGIN-CORRELATION (B1 rows) ===")
    if not seed_queries:
        p("origin seed set UNAVAILABLE — cannot classify seed vs non-seed. "
          "Reporting collection_source only.")
    b1 = buckets["B1"]
    b1_seed = [fr for fr in b1 if fr["origin"] == "seed"]
    b1_nonseed = [fr for fr in b1 if fr["origin"] == "non-seed"]
    p(f"B1 total = {len(b1)}")
    p(f"    query IN scheduler.DEFAULT_QUERIES (seed origin)      = {len(b1_seed)}  "
      f"ids={[fr['id'] for fr in b1_seed[:10]]}")
    p(f"    query NOT in DEFAULT_QUERIES (dynamic-kw OR user)     = {len(b1_nonseed)}  "
      f"ids={[fr['id'] for fr in b1_nonseed[:10]]}")
    # collection_source cross-tab (search-engine, NOT origin — shown for context).
    src_tab = {}
    for fr in b1:
        src_tab[fr["src"]] = src_tab.get(fr["src"], 0) + 1
    p("    B1 by collection_source (search engine, NOT hot-topic origin): "
      + (", ".join(f"{k}={v}" for k, v in sorted(src_tab.items(), key=lambda kv: (-kv[1], kv[0]))) or "(none)"))
    p("    NOTE: 'non-seed' conflates dynamic hot-topic keywords with live user")
    p("    searches — the DB has no field that separates them. This is the KEY")
    p("    mode1 signal but it is SUGGESTIVE, not definitive.")

    # ---- SECTION 4: MODE2 TOKEN CLASSIFICATION (advisory) ----------------------
    unknown_counter = {}
    unknown_example = {}
    for fr in flagged_rows:
        for tok in fr["info"]["unknown_tokens"]:
            unknown_counter[tok] = unknown_counter.get(tok, 0) + 1
            if tok not in unknown_example:
                unknown_example[tok] = str(fr["title"])[:70]
    p("")
    p("=== SECTION 4 — MODE2 TOKEN CLASSIFICATION (probe-side heuristic; ADVISORY) ===")
    p("Each distinct bracket token NOT in the live marker set, sorted into one of:")
    p("  OPINION      -> safe-to-block candidate (personal column/byline)")
    p("  FACTUAL-KEEP -> MUST NOT block (news-section/curation/source; kills recall)")
    p("  UNCLASSIFIED -> needs a human eyeball before any decision")
    p("This sorting is NOT applied to production; it only shapes a later, operator-")
    p("approved block-list. FACTUAL wins ties (never mislabel a source as opinion).")
    grouped = {"OPINION": [], "FACTUAL-KEEP": [], "UNCLASSIFIED": []}
    for tok, n in unknown_counter.items():
        grouped[classify_mode2_token(tok)].append((tok, n))
    for group in ("OPINION", "FACTUAL-KEEP", "UNCLASSIFIED"):
        items = sorted(grouped[group], key=lambda kv: (-kv[1], kv[0]))
        p("")
        p(f"[{group}] {len(items)} distinct token(s):")
        if not items:
            p("    (none)")
        for tok, n in items:
            p(f"    {tok}  (x{n})  e.g. {unknown_example.get(tok, '')}")

    # ---- SECTION 5: FAITHFULNESS + SUMMARY -------------------------------------
    p("")
    p("=== SECTION 5 — FAITHFULNESS NOTE ===")
    p("* REJECT-REPLAY is DEFINITIVE: _reject_title_reason is a pure title function,")
    p("  replayed over the real stored title with the IMPORTED (not copied) marker set.")
    p("  B1 vs B2 is therefore a hard fact about the current filter.")
    p("* ORIGIN is SUGGESTIVE only: no stored field marks hot_topics vs seed.")
    p("  collection_source = winning SEARCH ENGINE, not entry path. The query-vs-seed")
    p("  split is the closest signal and it conflates dynamic keywords with user")
    p("  searches. Read Section 3 as 'reject-replay definitive; origin suggestive'.")
    p("* The mode2 TOKEN CLASSIFICATION (Section 4) is a PROBE-SIDE HEURISTIC for")
    p("  operator review — it is NOT applied to any production filter. FACTUAL-KEEP")
    p("  labels ([정책브리핑] etc.) must never be blocked; UNCLASSIFIED needs a human.")

    # Funnel: how much of the leak is RECENT (post-filter) vs legacy (pre-filter).
    b2p_class = {"OPINION": 0, "FACTUAL-KEEP": 0, "UNCLASSIFIED": 0}
    for fr in b2_post:
        b2p_class[row_mode2_class(fr["info"])] += 1
    recent_leak = len(b1_post) + len(b2_post)

    p("")
    p("=== SUMMARY (FUNNEL) ===")
    p(f"rows scanned:            {scanned}")
    p(f"flagged (opinion-like):  {len(flagged_rows)}")
    p(f"B1 total = {len(buckets['B1'])}  ->  B1_pre (expected) = {len(b1_pre)} | "
      f"B1_post (candidate bypass) = {len(b1_post)}"
      + (f" | B1_unknown = {len(b1_unk)}" if b1_unk else ""))
    p(f"B2 total = {len(buckets['B2'])}  ->  B2_pre = {len(b2_pre)} | "
      f"B2_post = {len(b2_post)}"
      + (f" | B2_unknown = {len(b2_unk)}" if b2_unk else ""))
    p(f"B_OTHER (unrelated reject reason): {len(buckets['B_OTHER'])}")
    p("")
    p(f"RECENT GENUINE LEAK (created on/after the filter) = B1_post + B2_post = "
      f"{len(b1_post)} + {len(b2_post)} = {recent_leak}")
    p(f"  of B2_post ({len(b2_post)}), by token class:  "
      f"OPINION={b2p_class['OPINION']}  "
      f"FACTUAL-KEEP={b2p_class['FACTUAL-KEEP']}  "
      f"UNCLASSIFIED={b2p_class['UNCLASSIFIED']}")
    p(f"  -> the actual mode2 FIX TARGET is the B2_post OPINION count "
      f"({b2p_class['OPINION']}) + any UNCLASSIFIED an operator confirms as opinion "
      f"({b2p_class['UNCLASSIFIED']}); FACTUAL-KEEP ({b2p_class['FACTUAL-KEEP']}) "
      f"must stay unblocked.")
    p(f"distinct mode2 tokens: {len(unknown_counter)}  "
      f"(OPINION={len(grouped['OPINION'])}, FACTUAL-KEEP={len(grouped['FACTUAL-KEEP'])}, "
      f"UNCLASSIFIED={len(grouped['UNCLASSIFIED'])})")

    p("\n[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
