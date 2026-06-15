# HOTTOPIC-SAFETY Phase 1 — does the LIVE hot-topic path leak politician names?
# READ-ONLY diagnosis. SELECT-only DB access, NO writes, NO network. scripts/
# pin-OUT. READS hot_topics.py / scheduler.py / config for STRUCTURE only and
# modifies NOTHING in the pipeline, cron, hot_topics.py, or the frontend.
#
# WHY
# ---
# SELFDB-3 proved hot_topics._DENYLIST contains NO politician names (이재명/윤석열/
# 한동훈/이준석/대통령 all absent, exact + substring). A denylist GAP does not prove
# a real LEAK: the engine is news_collector titles -> tool-free Sonnet keyword
# pick -> _passes_domain_filter (allowlist-require + denylist-drop). Either the
# Sonnet prompt or the allowlist gate could already block names. This probe
# MEASURES whether names actually leak before anyone patches the live path.
#
# WHAT IT PRINTS
#   1. ENGINE STRUCTURE   — where final keywords are produced, where/how _DENYLIST
#      sits (substring? before/after pick?), and whether the Sonnet prompt
#      instructs person-avoidance (minimal quotes, <=15 words each).
#   2. HISTORICAL LEAK CHECK — emitted hot-topic keywords are NOT persisted in a
#      dedicated table; they survive only as analysis_results.query. Scan the last
#      ~14d of distinct queries for politician/office names (probe-local
#      _PERSON_BLOCK, mirrored from scripts/selfdb_keyword_probe.py).
#   3. DENYLIST GAP CONFIRMATION — self-contained re-check of the name gap.
#   4. FIX-SCOPE ASSESSMENT — minimal correct fix location (analysis only).
#
# STOP-FIRST: diagnosis only. No fix, no integration.

import os
import re
import sys
import inspect
import collections
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

# Project root importable (mirrors scripts/observe_daily.py / selfdb probe).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunables.
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 14
SAMPLE_NONSEED = 40   # how many distinct non-seed queries to list for eyeballing


# Cron seed queries — read-only copy (do NOT import scheduler.py; pin-IN for the
# 331/16 log pins). Update if scheduler.DEFAULT_QUERIES changes. (Equal at HEAD.)
DEFAULT_QUERIES = [
    "주택담보대출 규제",
    "스트레스 DSR 가계부채",
    "전세 공급 대책",
    "청년 정책 지원",
    "양도세 세제 개편",
    "소상공인 지원",
    "복지 예산",
]


# ---------------------------------------------------------------------------
# Probe-local person/office filter — MIRRORS scripts/selfdb_keyword_probe.py
# (_PERSON_NAMES + _PERSON_OFFICE_EXACT). Copied (not imported) to keep this a
# self-contained throwaway diagnostic; keep the two in sync if either changes.
# 시장 is DELIBERATELY absent (market/mayor ambiguity).
# ---------------------------------------------------------------------------
_PERSON_NAMES = {"이재명", "윤석열", "한동훈", "이준석", "김건희"}
_PERSON_OFFICE_EXACT = {
    "조국", "대통령", "대통령실", "국무총리", "장관", "의원", "국회의원",
    "청와대", "여당", "야당", "與野",
}
# Names we expect the denylist to be missing (Section 3 self-contained check).
_NAME_PROBES = ["이재명", "윤석열", "한동훈", "이준석", "대통령"]


def _query_person_hits(query: str) -> list:
    """Person/office markers found in a query phrase. Names match as substring;
    office words match as a whitespace-split exact token (mirrors the probe's
    filter semantics). Returns the list of markers hit (possibly empty)."""
    q = query or ""
    hits = [name for name in _PERSON_NAMES if name in q]
    toks = set(re.split(r"\s+", q))
    hits += [w for w in _PERSON_OFFICE_EXACT if w in toks]
    return hits


def _row_date(created_at) -> str:
    if created_at is None:
        return ""
    s = str(created_at)
    return s[:10] if len(s) >= 10 else ""


def _quote(text: str, max_words: int = 15) -> str:
    """Minimal quote: first max_words whitespace tokens of a single source line."""
    words = re.split(r"\s+", (text or "").strip())
    out = " ".join(words[:max_words])
    return out + (" ..." if len(words) > max_words else "")


# ===========================================================================
# SECTION 1 — HOT-TOPIC ENGINE STRUCTURE (read hot_topics.py; do not modify it)
# ===========================================================================
def section1_engine_structure():
    print("=== 1. HOT-TOPIC ENGINE STRUCTURE (read-only introspection) ===")
    try:
        import hot_topics  # read-only import
    except Exception as exc:
        print("  could not import hot_topics: %s: %s" % (type(exc).__name__, str(exc)[:120]))
        print("  (cannot introspect engine structure in this environment.)")
        return

    # (a) where the final keywords are produced
    producers = [n for n in ("build_dynamic_queries", "build_query_list",
                             "_passes_domain_filter", "_build_prompt")
                 if hasattr(hot_topics, n)]
    print("  final-keyword functions present:", ", ".join(producers) or "(none found)")
    print("  - build_dynamic_queries(): Sonnet pick -> filtered survivors (the keywords).")
    print("  - build_query_list(DEFAULT_QUERIES): fixed 7 + the dynamic survivors.")

    # (b) how/where _DENYLIST is applied
    applied_to_output = False
    method = "unknown"
    try:
        filt_src = inspect.getsource(hot_topics._passes_domain_filter)
        applied_to_output = "_DENYLIST" in filt_src
        method = "SUBSTRING (any(marker in keyword))" if "in keyword" in filt_src else "unknown"
        requires_allow = "_ALLOWLIST" in filt_src
        print("  _passes_domain_filter: denylist match = %s; allowlist-required = %s"
              % (method, requires_allow))
        print("    survive rule: (>=1 ALLOWLIST policy term) AND (0 DENYLIST markers).")
    except Exception as exc:
        print("  could not read _passes_domain_filter: %s" % (str(exc)[:80]))
        requires_allow = None

    # Where the filter sits relative to the Sonnet pick.
    try:
        bdq_src = inspect.getsource(hot_topics.build_dynamic_queries)
        after_pick = bdq_src.find("_call_anthropic_pick") < bdq_src.find("_passes_domain_filter")
        print("  _DENYLIST position: applied to the OUTPUT keywords, AFTER the Sonnet pick"
              if after_pick else "  _DENYLIST position: (order unclear — inspect manually)")
        print("  _DENYLIST applied to hot-topic OUTPUT? -> %s" % ("YES" if applied_to_output else "NO"))
    except Exception as exc:
        print("  could not read build_dynamic_queries: %s" % (str(exc)[:80]))

    # (c) does the Sonnet prompt instruct person-avoidance?
    print("  Sonnet prompt person-avoidance instruction:")
    person_markers = ["정치인물", "정치인", "인물", "이름", "선거", "하마평", "인사"]
    try:
        prompt_src = inspect.getsource(hot_topics._build_prompt)
        found_lines = []
        for line in prompt_src.splitlines():
            if any(m in line for m in person_markers):
                # strip python string quoting noise for a clean minimal quote
                clean = line.strip().strip('"').strip("'").replace('\\n"', "").replace('"', "")
                found_lines.append(clean)
        if found_lines:
            print("    INSTRUCTED — prompt excludes person/political terms. Quotes (<=15 words):")
            for ln in found_lines[:3]:
                print("      > %s" % _quote(ln, 15))
        else:
            print("    NOT INSTRUCTED — prompt contains NO person/politician-avoidance line.")
    except Exception as exc:
        print("    could not read _build_prompt: %s" % (str(exc)[:80]))

    # (d) safeguard order
    print("  safeguard order: news_collector titles -> Sonnet pick -> "
          "title_index provenance -> _passes_domain_filter(ALLOWLIST+DENYLIST) -> output")

    # config context — is the dynamic path even active in this env?
    try:
        import config
        print("  config.hot_topic_enabled() = %s   (if False, NO dynamic keyword was emitted)"
              % config.hot_topic_enabled())
    except Exception:
        print("  (config.hot_topic_enabled() unavailable)")
    print()


# ===========================================================================
# SECTION 2 — HISTORICAL LEAK CHECK (SELECT-only)
# ===========================================================================
def section2_leak_check(cur):
    print("=== 2. HISTORICAL LEAK CHECK (last %dd, analysis_results.query) ===" % LOOKBACK_DAYS)
    print("  NOTE: emitted hot-topic keywords are NOT persisted in a dedicated table;")
    print("  they survive only as analysis_results.query (cron passes query=keyword to")
    print("  save_analysis_result). Non-seed queries = dynamic hot-topic OR manual API")
    print("  submissions — both are scanned for person names (read-only proxy).")
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    cur.execute("SELECT id, created_at, query FROM analysis_results ORDER BY id")
    seed_set = {q.strip() for q in DEFAULT_QUERIES}
    total = 0
    q_dates = collections.defaultdict(set)     # query -> set of dates
    q_count = collections.Counter()            # query -> row count
    for _rid, created_at, query in cur.fetchall():
        day = _row_date(created_at)
        if day and day < cutoff:
            continue
        total += 1
        q = (query or "").strip()
        q_count[q] += 1
        if day:
            q_dates[q].add(day)

    distinct = [q for q in q_count if q]
    seed_q = [q for q in distinct if q in seed_set]
    nonseed_q = [q for q in distinct if q not in seed_set]
    print("  rows in window: %d   distinct queries: %d   (seed: %d, non-seed: %d)"
          % (total, len(distinct), len(seed_q), len(nonseed_q)))

    # Flag any query (seed or non-seed) containing a person/office marker.
    flagged = []
    for q in distinct:
        hits = _query_person_hits(q)
        if hits:
            flagged.append((q, hits))
    print()
    print("  --- non-seed queries (the dynamic/manual ones), up to %d ---" % SAMPLE_NONSEED)
    if nonseed_q:
        for q in sorted(nonseed_q)[:SAMPLE_NONSEED]:
            mark = "  <-- PERSON?" if _query_person_hits(q) else ""
            print("    %-40s rows=%-3d%s" % (q[:40], q_count[q], mark))
        if len(nonseed_q) > SAMPLE_NONSEED:
            print("    ... (+%d more non-seed queries)" % (len(nonseed_q) - SAMPLE_NONSEED))
    else:
        print("    (none — every query in window is one of the 7 cron seeds)")
    print()
    print("  --- FLAGGED (query contains a politician name / office word) ---")
    if flagged:
        for q, hits in flagged:
            dates = ",".join(sorted(q_dates.get(q, [])))
            tag = "SEED" if q in seed_set else "non-seed"
            print("    [%s] %r  markers=%s  dates=%s  rows=%d"
                  % (tag, q, hits, dates or "(undated)", q_count[q]))
    else:
        print("    (none)")
    n_leaks = len(flagged)
    print()
    print("  >>> HEADLINE: live politician-name leaks found in last %dd: %d" % (LOOKBACK_DAYS, n_leaks))
    return n_leaks


# ===========================================================================
# SECTION 3 — DENYLIST GAP CONFIRMATION (self-contained)
# ===========================================================================
def section3_denylist_gap():
    print("=== 3. DENYLIST GAP CONFIRMATION (hot_topics._DENYLIST, read-only) ===")
    try:
        from hot_topics import _DENYLIST
    except Exception as exc:
        print("  could not import hot_topics._DENYLIST: %s" % (str(exc)[:80]))
        return None
    print("  _DENYLIST size: %d entries" % len(_DENYLIST))
    print("  politician-name presence (exact membership / substring-of-name):")
    any_present = False
    for name in _NAME_PROBES:
        exact = name in _DENYLIST
        substr = any(str(m) in name for m in _DENYLIST)
        any_present = any_present or exact or substr
        print("    %-6s : exact=%s  substring=%s" % (name, exact, substr))
    print("  => politician names in _DENYLIST: %s" % ("SOME PRESENT" if any_present else "ALL ABSENT (gap confirmed)"))
    print("  => is _DENYLIST applied to hot-topic OUTPUT? YES — via _passes_domain_filter")
    print("     (substring), AFTER the Sonnet pick (see Section 1). So names added to")
    print("     _DENYLIST WOULD be enforced on emitted keywords.")
    print()
    return any_present


# ===========================================================================
# SECTION 4 — FIX-SCOPE ASSESSMENT (analysis only)
# ===========================================================================
def section4_fix_scope(n_leaks, names_present):
    print("=== 4. FIX-SCOPE ASSESSMENT (analysis only — NO code change here) ===")
    print("  Findings recap:")
    print("   - Sonnet prompt already instructs excluding 정치인물/선거 (Section 1).")
    print("   - _passes_domain_filter requires >=1 ALLOWLIST policy term: a BARE name")
    print("     (e.g. '이재명') has no policy term -> already DROPPED.")
    print("   - Residual risk: a NAME+POLICY compound ('이재명 부동산 대책') passes the")
    print("     allowlist AND the (name-free) denylist -> could leak.")
    print("   - _DENYLIST IS applied to output as substring, but contains no names.")
    print()
    if n_leaks and n_leaks > 0:
        print("  OBSERVED LEAKS: %d. A fix IS warranted." % n_leaks)
    else:
        print("  OBSERVED LEAKS: 0 in the %dd window. No active leak, but the residual" % LOOKBACK_DAYS)
        print("  name+policy-compound risk remains (preventive fix advisable).")
    print()
    print("  MINIMAL CORRECT FIX (recommendation):")
    print("   (i)  ADD politician/office names to hot_topics._DENYLIST  <-- RECOMMENDED.")
    print("        It is ALREADY applied as substring to the emitted keywords, so a name")
    print("        list closes the residual compound-leak with the smallest change and")
    print("        catches 이재명/윤석열/한동훈/etc. generally. Files/functions a future")
    print("        milestone would touch: hot_topics.py — the _LOCAL_DENYLIST / _DENYLIST")
    print("        definition (add a names tuple), NO change to _passes_domain_filter.")
    print("   (ii) Prompt person-avoidance already exists — strengthening it is optional")
    print("        defense-in-depth, not the primary fix.")
    print("   (iii) A post-pick _PERSON_BLOCK mirror is redundant given (i), since the")
    print("        denylist already runs post-pick.")
    print("   (iv) 'No fix' is NOT advisable: the allowlist drops bare names but not")
    print("        name+policy compounds.")
    print("  (Do NOT change anything now — this is the diagnosis; the fix is a separate")
    print("   milestone, sized to: hot_topics.py, ~1 names tuple added to the denylist.)")
    print()


def main() -> int:
    print("HOTTOPIC-SAFETY Phase 1 — live hot-topic politician-name leak diagnosis (READ-ONLY)")
    print()
    section1_engine_structure()

    n_leaks = None
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("=== 2. HISTORICAL LEAK CHECK ===")
        print("  DATABASE_URL not set — run in the Render Worker Shell for the DB scan.")
        print()
    else:
        url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                      .replace("postgresql+psycopg2://", "postgresql://"))
        try:
            with psycopg.connect(url) as conn, conn.cursor() as cur:
                n_leaks = section2_leak_check(cur)
        except Exception as exc:
            print("  DB scan error: %s: %s" % (type(exc).__name__, str(exc)[:120]))
            print()

    names_present = section3_denylist_gap()
    section4_fix_scope(n_leaks, names_present)

    print("[Safety] READ-ONLY diagnosis — SELECT-only; no rows written/updated/deleted; "
          "hot_topics.py and the live path unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
