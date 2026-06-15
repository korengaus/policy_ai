# SELFDB-1 Phase 1 — self-DB keyword aggregation probe. SELECT-only, no writes,
# no network, safe to run in the Render Worker Shell.
#
# QUESTION THIS PROBE ANSWERS
# ---------------------------
# Hot-topic auto-detection (hot_topics.py) is LIVE but only picks keywords from
# titles we collect under the FIXED cron seed queries (scheduler.DEFAULT_QUERIES:
# 주택담보대출 규제, 스트레스 DSR 가계부채, ...). It cannot catch breaking issues
# OUTSIDE those seeds. External trend sources (빅카인즈/구글/네이버실검/다음) are all
# blocked/unsuitable. The only remaining incremental idea is "self-DB keyword
# aggregation": count the keywords ALREADY present in our accumulated Postgres
# analysis rows to surface rising/frequent terms.
#
# BUT this may be a dead end: because every row was collected UNDER a fixed seed
# query, aggregation might just echo the seed list back (no new signal). The value
# only exists if terms NOT in the seed list actually RISE over time. This probe
# MEASURES that before any Phase 2 engine is built — the established
# measurement-before-surgery pattern.
#
# IT DOES NOT decide the design. It prints numbers; the operator decides whether
# SELFDB Phase 2 (engine integration) is worth building.
#
# WHAT IT TOUCHES
# ---------------
# Reads `analysis_results` (SELECT-only) via the same psycopg connection pattern
# as scripts/body2_overlap.py. Imports denylist constants from hot_topics /
# news_collector for the noise scan (read-only import). Modifies NO row, NO
# pipeline code, NO config, NO frontend. Issues NO INSERT/UPDATE/DELETE/DDL and
# makes NO network call.
#
# KEYWORD EXTRACTION NOTE
# -----------------------
# hot_topics.py selects keywords with a TOOL-FREE LLM pick, not an importable
# tokenizer — there is nothing to reuse. So this probe uses a deliberately simple
# Korean-noun-friendly regex split (length>=2 Hangul/Latin runs, light trailing
# 1-char particle strip, small stopword set). It is intentionally NOT the verdict
# tokenizer (evidence_comparator / official_*) — those are off-limits and would be
# the wrong tool for surfacing topic keywords anyway.

import os
import re
import sys
import collections
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

# Make the project root importable so the denylist imports below work when the
# script is launched as `python scripts/selfdb_keyword_probe.py` (mirrors
# scripts/observe_daily.py). The operator runs it with PYTHONPATH=. as well.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunable constants (commented, top-of-file as required).
# ---------------------------------------------------------------------------
# How many days back to scan. analysis_results.created_at is loose TEXT (ISO),
# so the window is applied Python-side on the first 10 chars (YYYY-MM-DD).
LOOKBACK_DAYS = 14
# How many top keywords (by document frequency) to report / classify.
TOP_K = 30
# A keyword counts as "rising" if its recent-half document frequency is at least
# RISING_RATIO times its older-half frequency AND it appears in at least
# RISING_MIN_RECENT_DF distinct recent-half rows (so a single fluke row does not
# register as a trend). Older-half == 0 is treated as brand-new -> rising.
RISING_RATIO = 2.0
RISING_MIN_RECENT_DF = 2
# Minimum document frequency for a keyword to enter the raw-frequency table at
# all (filters one-off noise before the top-K cut).
MIN_DF = 2


# Cron seed queries — IMPORTED conceptually but COPIED here read-only to avoid
# importing scheduler.py (which is pin-IN for the 331/16 log pins). This list is
# the authority for IN-SEED vs OUT-OF-SEED classification. If scheduler's
# DEFAULT_QUERIES ever changes, update this copy. (Verified equal to
# scheduler.DEFAULT_QUERIES at HEAD d60804498b.)
SEED_QUERIES = [
    "주택담보대출 규제",
    "스트레스 DSR 가계부채",
    "전세 공급 대책",
    "청년 정책 지원",
    "양도세 세제 개편",
    "소상공인 지원",
    "복지 예산",
]


# Common Korean functional words / verb-y tokens to drop from the keyword pool.
# Small and conservative — just enough to stop the frequency table being topped
# by glue words. NOT a domain filter.
_STOPWORDS = {
    "있다", "없다", "했다", "한다", "하는", "위해", "위한", "대한", "대해", "관련",
    "이번", "올해", "지난", "오는", "최근", "그리고", "하지만", "이라고", "라고",
    "이라며", "면서", "통해", "라며", "기자", "뉴스", "사진", "단독", "종합", "속보",
    "정부", "이날", "당국", "그러나", "또한", "이어", "밝혔다", "전했다", "예정",
}

# Trailing single-syllable Korean particles (josa) to strip so e.g. "대출을" and
# "대출이" both fold to "대출". Only applied when the remaining stem is >=2 chars.
_TRAILING_PARTICLES = ("은", "는", "이", "가", "을", "를", "에", "의", "도", "로",
                       "과", "와", "만", "께", "서", "야", "라")

# Hangul or Latin runs of length >= 2.
_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# Noise/safety scan denylist — IMPORTED from existing modules (read-only). If the
# import fails (e.g. a dependency missing in the shell), the noise scan degrades
# to a note rather than reimplementing the filter.
# ---------------------------------------------------------------------------
_DENYLIST = ()
_DENYLIST_SOURCE = "none"
try:
    # hot_topics._DENYLIST == local off-topic markers (election/politician/
    # securities/foreign) UNION news_collector.OBITUARY_MARKERS.
    from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
    _DENYLIST = tuple(_HT_DENYLIST)
    _DENYLIST_SOURCE = "hot_topics._DENYLIST (election/politician/securities/foreign + OBITUARY_MARKERS)"
except Exception:
    try:
        from news_collector import OBITUARY_MARKERS as _OBIT  # type: ignore
        _DENYLIST = tuple(_OBIT)
        _DENYLIST_SOURCE = "news_collector.OBITUARY_MARKERS (obituary only; full denylist unavailable)"
    except Exception:
        _DENYLIST = ()
        _DENYLIST_SOURCE = "none"


def _normalize_token(tok: str) -> str:
    """Light noun-friendly normalization: strip ONE trailing particle when the
    stem stays >=2 chars. Latin tokens are returned lowercased unchanged."""
    if not tok:
        return ""
    if re.fullmatch(r"[A-Za-z]{2,}", tok):
        return tok.lower()
    if len(tok) >= 3 and tok[-1] in _TRAILING_PARTICLES:
        return tok[:-1]
    return tok


def _keywords_of(text: str) -> set:
    """Distinct normalized keyword tokens in a piece of text (for document
    frequency we only care about presence per row, hence a set)."""
    out = set()
    for raw in _TOKEN_RE.findall(text or ""):
        tok = _normalize_token(raw)
        if len(tok) < 2:
            continue
        if tok in _STOPWORDS:
            continue
        out.add(tok)
    return out


# Seed token pool for IN-SEED / OUT-OF-SEED classification: every seed phrase AND
# each of its whitespace-split words (e.g. "스트레스 DSR 가계부채" ->
# {스트레스, dsr, 가계부채}). A keyword is IN-SEED when it bidirectionally
# substring-overlaps any seed token (keyword in seedtok OR seedtok in keyword),
# which catches 대출 <-> 주택담보대출 style stem overlaps.
def _build_seed_tokens() -> set:
    toks = set()
    for phrase in SEED_QUERIES:
        toks.add(phrase.strip().lower())
        for word in phrase.split():
            w = _normalize_token(word)
            if len(w) >= 2:
                toks.add(w.lower())
    return toks


_SEED_TOKENS = _build_seed_tokens()


def _is_in_seed(keyword: str) -> bool:
    k = keyword.lower()
    for st in _SEED_TOKENS:
        if k in st or st in k:
            return True
    return False


def _looks_risky(keyword: str) -> bool:
    """Noise/safety flag: keyword contains any denylist marker (obituary /
    politician / election / securities / foreign). Advisory only."""
    if not _DENYLIST:
        return False
    return any(marker in keyword for marker in _DENYLIST)


def _row_date(created_at) -> str:
    """First 10 chars (YYYY-MM-DD) of a loose-TEXT created_at, or '' if unusable."""
    if created_at is None:
        return ""
    s = str(created_at)
    return s[:10] if len(s) >= 10 else ""


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    # SELECT-only. Pull the text columns + created_at; aggregate Python-side.
    # NOTE: `query` is the seed query string itself, so it is DELIBERATELY
    # EXCLUDED from the keyword text (including it would make every row trivially
    # echo its seed). Keywords are extracted from title + claim_text only.
    rows = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, title, claim_text "
            "FROM analysis_results ORDER BY id"
        )
        for rid, created_at, title, claim_text in cur.fetchall():
            day = _row_date(created_at)
            if day and day < cutoff:
                continue  # outside the lookback window
            rows.append((rid, day, f"{title or ''}\n{claim_text or ''}"))

    print("SELFDB-1 Phase 1 — self-DB keyword aggregation probe (READ-ONLY)")
    print(f"  lookback={LOOKBACK_DAYS}d  top_k={TOP_K}  rising_ratio={RISING_RATIO}  "
          f"min_df={MIN_DF}  cutoff>={cutoff}")
    print(f"  denylist source: {_DENYLIST_SOURCE}")
    print()

    # ---- SECTION 1: CORPUS SIZE -------------------------------------------
    print("=== 1. CORPUS SIZE ===")
    print("  rows scanned (in window):", len(rows))
    dated = [d for _, d, _ in rows if d]
    if dated:
        print("  date range:", min(dated), "->", max(dated))
        per_day = collections.Counter(dated)
        print("  rows per day:")
        for day in sorted(per_day):
            print("    %s : %d" % (day, per_day[day]))
    else:
        print("  date range: (no usable created_at values)")
    if not rows:
        print("\n  No rows in window — nothing to aggregate. (Widen LOOKBACK_DAYS?)")
        print("\n[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
        return 0
    print()

    # ---- pre-compute per-row keyword sets + halves ------------------------
    # Split the window into older half vs recent half by calendar midpoint so
    # the rising measure compares like-for-like time spans.
    midpoint = (datetime.now() - timedelta(days=LOOKBACK_DAYS / 2.0)).strftime("%Y-%m-%d")
    df_total = collections.Counter()   # keyword -> # distinct rows containing it
    df_recent = collections.Counter()
    df_older = collections.Counter()
    n_recent = n_older = 0
    for _rid, day, text in rows:
        kws = _keywords_of(text)
        is_recent = bool(day) and day >= midpoint
        if is_recent:
            n_recent += 1
        elif day:
            n_older += 1
        for kw in kws:
            df_total[kw] += 1
            if is_recent:
                df_recent[kw] += 1
            elif day:
                df_older[kw] += 1

    # ---- SECTION 2: RAW KEYWORD FREQUENCY ---------------------------------
    print("=== 2. RAW KEYWORD FREQUENCY (document frequency = # distinct rows) ===")
    print("    (over title + claim_text only; `query` seed string excluded)")
    ranked = [(kw, n) for kw, n in df_total.items() if n >= MIN_DF]
    ranked.sort(key=lambda kv: (-kv[1], kv[0]))
    top = ranked[:TOP_K]
    if not top:
        print("  (no keyword reached MIN_DF=%d)" % MIN_DF)
    for kw, n in top:
        tag = "IN-SEED " if _is_in_seed(kw) else "OUT     "
        risk = "  [RISK?]" if _looks_risky(kw) else ""
        print("  %-16s df=%-4d %s%s" % (kw, n, tag, risk))
    print()

    # ---- SECTION 3: SEED CLASSIFICATION (key measurement) -----------------
    print("=== 3. SEED CLASSIFICATION of the top %d keywords ===" % len(top))
    in_seed = [kw for kw, _ in top if _is_in_seed(kw)]
    out_seed = [kw for kw, _ in top if not _is_in_seed(kw)]
    print("  IN-SEED  (echoes a cron seed term):")
    print("    " + (", ".join(in_seed) if in_seed else "(none)"))
    print("  OUT-OF-SEED (NOT in any seed term — the potential new signal):")
    print("    " + (", ".join(out_seed) if out_seed else "(none)"))
    pct = (100.0 * len(out_seed) / len(top)) if top else 0.0
    print("  >>> OUT-OF-SEED: %d of %d top keywords (%.0f%%)" % (len(out_seed), len(top), pct))
    print()

    # ---- SECTION 4: RISING SIGNAL (the other key measurement) -------------
    print("=== 4. RISING SIGNAL (recent half vs older half) ===")
    print("    recent half (day >= %s): %d rows | older half: %d rows" %
          (midpoint, n_recent, n_older))
    print("    rising := recent_df >= %d AND recent_df >= %.1f x older_df "
          "(older_df==0 -> brand-new)" % (RISING_MIN_RECENT_DF, RISING_RATIO))
    rising = []
    for kw in df_total:
        rdf = df_recent[kw]
        odf = df_older[kw]
        if rdf < RISING_MIN_RECENT_DF:
            continue
        if odf == 0 or rdf >= RISING_RATIO * odf:
            rising.append((kw, rdf, odf))
    # Sort by recent df, then by absolute delta.
    rising.sort(key=lambda t: (-t[1], -(t[1] - t[2])))
    rising_out = [t for t in rising if not _is_in_seed(t[0])]
    if not rising:
        print("  (no keyword met the rising threshold)")
    for kw, rdf, odf in rising[:TOP_K]:
        tag = "IN-SEED " if _is_in_seed(kw) else "OUT     "
        risk = "  [RISK?]" if _looks_risky(kw) else ""
        print("  %-16s recent=%-3d older=%-3d %s%s" % (kw, rdf, odf, tag, risk))
    print("  >>> RISING & OUT-OF-SEED keywords: %d" % len(rising_out))
    if rising_out:
        print("      " + ", ".join(t[0] for t in rising_out))
    print()

    # ---- SECTION 5: NOISE / SAFETY SCAN -----------------------------------
    print("=== 5. NOISE / SAFETY SCAN (defamation-risk exposure) ===")
    if not _DENYLIST:
        print("  Denylist NOT importable in this environment — noise scan skipped.")
        print("  (Phase 2 MUST reuse hot_topics._DENYLIST / news_collector.OBITUARY_MARKERS;")
        print("   do NOT feed self-DB keywords to the pipeline without that filter.)")
    else:
        flagged = [kw for kw, _ in top if _looks_risky(kw)]
        print("  denylist markers: %d   (source: %s)" % (len(_DENYLIST), _DENYLIST_SOURCE))
        if flagged:
            print("  TOP-K keywords hitting the denylist (person/politician/obituary/"
                  "securities/foreign):")
            print("    " + ", ".join(flagged))
        else:
            print("  No top-%d keyword hit the denylist." % len(top))
        print("  (If Phase 2 ever feeds these keywords into the pipeline, this filter")
        print("   MUST be applied first — same as hot_topics safeguard (c).)")
    print()

    # ---- SECTION 6: VERDICT ----------------------------------------------
    print("=== 6. VERDICT ===")
    n_out_rising = len(rising_out)
    print("  OUT-OF-SEED-and-RISING count = %d   <<< this number decides Phase 2" % n_out_rising)
    if n_out_rising == 0:
        print("  Self-DB aggregation surfaced NO out-of-seed rising term over the last")
        print("  %d days. The corpus mostly ECHOES the fixed cron seed list — as feared," % LOOKBACK_DAYS)
        print("  rows collected under fixed seeds reproduce those seeds. On this evidence")
        print("  SELFDB Phase 2 (engine integration) adds little NEW signal and is likely")
        print("  NOT worth building. Re-measure after more/broader data accumulates.")
    elif n_out_rising <= 3:
        print("  Self-DB aggregation surfaced a FEW (%d) out-of-seed rising terms. Weak but" % n_out_rising)
        print("  non-zero signal. Operator should eyeball whether those terms are real")
        print("  emerging policy issues (worth Phase 2) or extraction noise / risky names")
        print("  (see Section 5) before committing to an engine.")
    else:
        print("  Self-DB aggregation surfaced %d out-of-seed rising terms — a real signal" % n_out_rising)
        print("  beyond the seed echo. Phase 2 MAY be worth building, BUT only behind the")
        print("  Section-5 denylist filter (defamation-risk). Operator decides.")
    print()
    print("[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
