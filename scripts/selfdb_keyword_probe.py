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


# Korean stopword / fragment denylist — the MAIN noise lever for this probe.
# EDITABLE: grow this SET as new junk surfaces in the daily watch list. These are
# functional words, sentence fragments, and generic nouns that are NOT topic
# keywords. SELFDB-2 added the observed junk that polluted SELFDB-1's output
# (등을 / 방안 / 대상 / 부담 / 따르면 / 가능성 / 이후 / 것으 / 있는 ...).
# Borderline-token choices (commented so they can be revisited):
#   - 가격 is deliberately KEPT OUT of this set (i.e. NOT dropped): it can carry
#     real housing-price signal (집값/분양가). Add it here later if it proves noisy.
#   - 만원 IS dropped: as a bare currency unit it is never a standalone topic.
_STOPWORDS = {
    # verbs / predicates / quotatives / connectors
    "있다", "없다", "했다", "한다", "하는", "있는", "것이다", "것으", "따르면",
    "위해", "위한", "대한", "대해", "관련", "통해", "이라고", "라고", "이라며",
    "라며", "면서", "그리고", "하지만", "그러나", "또한", "또는", "이어",
    "밝혔다", "전했다", "역시", "적극", "두고", "한편", "특히", "그동안",
    # time / hedge / quantity-shape fragments
    "이번", "올해", "지난", "오는", "최근", "이후", "향후", "가운데", "가능성",
    "우려", "예정", "수준", "정도", "경우", "모든", "하나", "그것", "이것",
    # generic admin / report nouns + observed fragments
    "방안", "대상", "부담", "등을", "등의", "기자", "뉴스", "사진", "단독",
    "종합", "속보", "정부", "이날", "당국", "만원",
}

# ---------------------------------------------------------------------------
# SELFDB-3 person/office filter — PROBE-LOCAL, kept SEPARATE from _STOPWORDS
# (general junk) and from hot_topics._DENYLIST (which SELFDB-2 showed missed
# 이재명; we DIAGNOSE that in the Part-A block but do NOT rely on it here).
# Purpose: keep the daily watch list free of politician names + pure
# political-office words, which must never become an auto-card keyword
# (defamation risk). EDITABLE — current-figures list needs occasional updates.
# ---------------------------------------------------------------------------
# Unambiguous full names — matched as SUBSTRING (so 이재명정부 / 윤석열표 etc. are
# also caught). All are 3-syllable specific names with no common-word collision.
_PERSON_NAMES = {
    "이재명", "윤석열", "한동훈", "이준석", "김건희",
}
# Ambiguous-or-generic terms — matched by EXACT token equality ONLY, never
# substring, to avoid over-blocking:
#   - 조국 also means "homeland", so only a BARE 조국 token is dropped (조국통일
#     survives). Listed per spec; revisit if it proves to drop real content.
#   - office words like 의원 occur inside 병의원/의원실, so exact-only.
# NOTE: 시장 is DELIBERATELY NOT blocked here — it is ambiguous (market / mayor)
# and is handled elsewhere; blocking it would kill real economic keywords.
_PERSON_OFFICE_EXACT = {
    "조국", "대통령", "대통령실", "국무총리", "장관", "의원", "국회의원",
    "청와대", "여당", "야당", "與野",   # 與野 is hanja -> not even tokenized; belt-and-suspenders
}

# Records what the person/office filter removed THIS run, so Section 7 can show
# the operator exactly what got blocked (over-block sanity check).
_BLOCKED_PERSON_RUN: set = set()


def _is_person_or_office(tok: str) -> bool:
    """True if tok is a blocked politician name (substring) or a pure political-
    office word (exact). 시장 is intentionally absent (market/mayor ambiguity)."""
    if tok in _PERSON_OFFICE_EXACT:
        return True
    return any(name in tok for name in _PERSON_NAMES)


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


def _hangul_count(s: str) -> int:
    """Number of Hangul-syllable chars (U+AC00–U+D7A3) in s."""
    return sum(1 for ch in s if "가" <= ch <= "힣")


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
    """Distinct CLEANED keyword tokens in a piece of text (document frequency only
    cares about presence per row, hence a set).

    SELFDB-2 noise filter — applied HERE in the ONE extraction point so the
    frequency ranking, the rising computation, and the headline count all share
    the SAME cleaned set:
      (a) drop length-1 tokens,
      (b) drop tokens that are not majority-Hangul — i.e. require >=2 Hangul
          syllables, which removes English/system tokens (ai, the, news, view,
          pick, bok, lh, pdf) and bare ASCII,
      (c) drop _STOPWORDS (the main editable lever),
      (d) drop _PERSON_BLOCK person names / political-office words (SELFDB-3),
          recording each into _BLOCKED_PERSON_RUN for the Section-7 audit line."""
    out = set()
    for raw in _TOKEN_RE.findall(text or ""):
        tok = _normalize_token(raw)
        if len(tok) < 2:
            continue
        if _hangul_count(tok) < 2:   # drops pure-Latin / system tokens
            continue
        if tok in _STOPWORDS:
            continue
        if _is_person_or_office(tok):   # SELFDB-3 person/office filter
            _BLOCKED_PERSON_RUN.add(tok)
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

    print("SELFDB-2 Phase 1 — self-DB keyword aggregation probe (READ-ONLY, noise-filtered)")
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
    # COUNT-BUG FIX (SELFDB-2): the headline must agree with the displayed table.
    # SELFDB-1 reported the rising-out count over the WHOLE min_df vocabulary (a
    # noise-laden flood -> false "238"). Here the watch set is restricted to the
    # cleaned, ranked TOP_K (the same `top` shown in Sections 2-3): keywords that
    # are in top-K AND rising AND out-of-seed. That is the only number that drives
    # Sections 6-7.
    print("=== 4. RISING SIGNAL (recent half vs older half) ===")
    print("    recent half (day >= %s): %d rows | older half: %d rows" %
          (midpoint, n_recent, n_older))
    print("    rising := recent_df >= %d AND recent_df >= %.1f x older_df "
          "(older_df==0 -> brand-new)" % (RISING_MIN_RECENT_DF, RISING_RATIO))
    top_set = {kw for kw, _ in top}

    def _is_rising(kw):
        rdf, odf = df_recent[kw], df_older[kw]
        return rdf >= RISING_MIN_RECENT_DF and (odf == 0 or rdf >= RISING_RATIO * odf)

    # rising keywords WITHIN the cleaned top-K (sorted by recent df, then delta).
    rising_topk = [(kw, df_recent[kw], df_older[kw]) for kw, _ in top if _is_rising(kw)]
    rising_topk.sort(key=lambda t: (-t[1], -(t[1] - t[2])))
    # The watch set = rising AND out-of-seed AND within cleaned top-K.
    watch = [(kw, rdf, odf) for (kw, rdf, odf) in rising_topk if not _is_in_seed(kw)]

    if not rising_topk:
        print("  (no top-%d keyword met the rising threshold)" % TOP_K)
    for kw, rdf, odf in rising_topk:
        tag = "IN-SEED " if _is_in_seed(kw) else "OUT     "
        risk = "  [RISK?]" if _looks_risky(kw) else ""
        print("  %-16s recent=%-3d older=%-3d %s%s" % (kw, rdf, odf, tag, risk))
    print("  >>> clean OUT-OF-SEED & RISING (within top-%d): %d" % (TOP_K, len(watch)))
    if watch:
        print("      " + ", ".join(t[0] for t in watch))
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

    # ---- SECTION 5b: DENYLIST DIAGNOSTIC (Part A — read-only) --------------
    # WHY: SELFDB-2 surfaced 이재명 / 대통령 in the watch list yet Section 5 said
    # "no denylist hit", so hot_topics._DENYLIST missed a sitting politician. We
    # diagnose the CAUSE (not-in-list vs match-method) before deciding whether the
    # LIVE hot-topic path needs the same fix (a separate milestone). READ-ONLY.
    print("=== 5b. DENYLIST DIAGNOSTIC (Part A — hot_topics._DENYLIST, read-only) ===")
    print("  _DENYLIST source: %s" % _DENYLIST_SOURCE)
    print("  _DENYLIST size  : %d entries" % len(_DENYLIST))
    if _DENYLIST:
        sample = list(_DENYLIST)[:30]
        print("  first %d entries: %s" % (len(sample), ", ".join(map(str, sample))))
        # Does it contain political-person / office markers at all? (exact membership)
        probes = ["이재명", "대통령", "윤석열", "한동훈", "이준석"]
        print("  political-marker presence (exact membership in _DENYLIST):")
        for p in probes:
            print("    %-6s : %s" % (p, "PRESENT" if p in _DENYLIST else "absent"))
        # HOW Section 5 matches: _looks_risky tests `any(marker in keyword)` = SUBSTRING.
        print("  Section-5 match method: SUBSTRING  (any(marker in keyword) via _looks_risky)")
        target = "이재명"
        exact_hit = target in _DENYLIST
        substr_hit = any(str(m) in target for m in _DENYLIST)
        print("  test %r vs _DENYLIST:" % target)
        print("    exact membership (%r in _DENYLIST)            : %s" % (target, exact_hit))
        print("    substring match  (any marker is substr of it) : %s" % substr_hit)
        if substr_hit:
            cause = ("match-method OK — marker present in _DENYLIST; Section-5 top-K shows no hit "
                     "because the probe's own person filter (_is_person_or_office) removes such "
                     "names upstream in _keywords_of — expected, not a scan bug")
        elif exact_hit:
            cause = "match-method-mismatch (exact-present but substring-scan failed — logically odd)"
        else:
            cause = "not-in-list (hot_topics._DENYLIST contains NO marker matching 이재명)"
    else:
        cause = "denylist-not-importable/empty (cannot diagnose; import failed in this env)"
    print("  >>> CONCLUSION: denylist miss cause = [%s]" % cause)
    print("  (NOTE: hot_topics.py is NOT modified here — the probe's own Part-B person")
    print("   filter below makes the watch list safe; the LIVE fix is a separate milestone.)")
    print()

    # ---- SECTION 6: VERDICT ----------------------------------------------
    print("=== 6. VERDICT ===")
    n_out_rising = len(watch)   # cleaned top-K ∩ rising ∩ out-of-seed (count-bug fix)
    print("  OUT-OF-SEED-and-RISING count (clean, within top-%d) = %d   <<< decides Phase 2"
          % (TOP_K, n_out_rising))
    if n_out_rising == 0:
        print("  Self-DB aggregation surfaced NO clean out-of-seed rising term over the last")
        print("  %d days. The corpus mostly ECHOES the fixed cron seed list — as feared," % LOOKBACK_DAYS)
        print("  rows collected under fixed seeds reproduce those seeds. On this evidence")
        print("  SELFDB Phase 2 (engine integration) adds little NEW signal and is likely")
        print("  NOT worth building. Re-measure after more/broader data accumulates.")
    elif n_out_rising <= 3:
        print("  Self-DB aggregation surfaced a FEW (%d) clean out-of-seed rising terms. Weak" % n_out_rising)
        print("  but non-zero signal. Operator should eyeball whether those terms are real")
        print("  emerging policy issues (worth Phase 2) or extraction noise / risky names")
        print("  (see Section 5) before committing to an engine.")
    else:
        print("  Self-DB aggregation surfaced %d clean out-of-seed rising terms — a real signal" % n_out_rising)
        print("  beyond the seed echo. Phase 2 MAY be worth building, BUT only behind the")
        print("  Section-5 denylist filter (defamation-risk). Operator decides.")
    print()

    # ---- SECTION 7: DAILY WATCH LIST -------------------------------------
    # The ONE thing the operator reads each morning (observation routine step 5).
    # Short, scannable: the cleaned out-of-seed rising policy-candidate keywords,
    # each with recent/older document frequency and a simple rising indicator.
    print("=== 7. DAILY WATCH LIST (clean out-of-seed rising policy candidates) ===")
    WATCH_CAP = 15
    if not watch:
        print("  (none today — no clean out-of-seed rising keyword in the top-%d.)" % TOP_K)
    else:
        for kw, rdf, odf in watch[:WATCH_CAP]:
            indicator = "NEW" if odf == 0 else ("x%.1f" % (rdf / odf))
            flag = "  [RISK? denylist hit]" if _looks_risky(kw) else ""
            print("  %-16s recent=%-3d older=%-3d  %-5s%s" % (kw, rdf, odf, indicator, flag))
        if len(watch) > WATCH_CAP:
            print("  ... (+%d more; showing top %d by recent df)" % (len(watch) - WATCH_CAP, WATCH_CAP))
    # SELFDB-3: show what the person/office filter removed this run, so the
    # operator can sanity-check it is not over-blocking real policy terms.
    if _BLOCKED_PERSON_RUN:
        print("  [blocked this run by person/office filter: %s]"
              % ", ".join(sorted(_BLOCKED_PERSON_RUN)))
    else:
        print("  [blocked this run by person/office filter: none]")
    print()
    print("  TAKEAWAY: %d clean out-of-seed rising policy-candidate keyword(s) today "
          "(log this number)." % len(watch))
    print()
    print("[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
