# URL-GATE-SCOPE-PROBE — THROWAWAY read-only probe scoping the 'menu' URL-gate
# misclassification: is it FSS-only or a general .go.kr/korea.kr bug, and would a
# minimal fix regress any currently-matching row?
#
# SELECT-only over analysis_results, NO writes, NO DDL, NO network. Imports and
# calls ONLY pure functions. The "simulated fix" lives ONLY in this probe's local
# code — it NEVER edits score_official_url / WEAK_URL_SIGNALS / production.
#
# BACKGROUND (from scripts/fss_url_gate_probe.py results)
# -------------------------------------------------------
# FSS originUrls look like
#   https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=NNNNNN&menuNo=200218
# score_official_url scores official_domain+25, detail_url_pattern[/bbs/]+28,
# numeric_detail_id+10, BUT search_or_index_like[WEAK='menu']-30 => 33 < 35 gate
# => weak_or_search_page. The -30 fires on the substring 'menu' (from menuNo=)
# even though view.do is a DETAIL page. PB (korea.kr/.../pressReleaseView.do?
# newsId=...) has no 'menu' param => 63 => passes. A URL fix ALONE recovers only
# 2/329 FSS bodies (327 fail for non-URL reasons), so before designing the
# surgery we must scope it: FSS-only vs general, and regression surface.
#
# CONFIRMED INTERNALS (official_evidence_resolution.score_official_url):
#   * 'menu' is a RAW SUBSTRING in WEAK_URL_SIGNALS; match = any(sig in url.lower()).
#   * weights +25 official_domain / +28 detail_url_pattern / +10 numeric_detail_id
#     / +15 official_content_title / -30 search_or_index_like / -40 url_missing.
#   * status: score>=65 detail_page_likely; >=35 candidate_needs_body_check; else
#     weak_or_search_page. PASS = NOT weak_or_search_page (score>=35).
#   * _classify_official_evidence special-cases ONLY 'weak_or_search_page'.
#
# PART A — corpus-wide 'menu'-penalised-but-detail-looking URLs, bucketed by host.
# PART B — SIMULATED minimal fix (suppress -30 iff the SOLE weak reason is 'menu'
#          AND a positive detail signal is present); recovery set by host, downgrade
#          set (must be 0), and verdict-regression cross-check vs currently-matching
#          rows. SIMULATION ONLY, not production.
# PART C — token-distinguisher table for a surgical guard condition.

import os
import re
import json
import sys
import collections
from urllib.parse import urlparse
from datetime import datetime, timedelta

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# --- PURE leaf imports (no network at import; no enrich/fetch) ----------------
from text_utils import sanitize_text
from official_evidence_resolution import (
    score_official_url,            # authoritative URL gate
    DETAIL_URL_SIGNALS,            # +28 detail-path predicate set
    WEAK_URL_SIGNALS,              # -30 weak/search predicate set (contains 'menu')
)
from official_metadata import (
    is_official_domain,
    looks_like_official_search_or_index_url,
)


# ---------------------------------------------------------------------------
# Tunable constants.
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 0            # 0 = whole corpus
FSS_MARKER = "fss_bodo_content_id"
PB_MARKER = "policy_briefing_news_item_id"
LAW_MARKER = "national_law_mst"

# score_official_url internals mirrored locally (read-only; self-checked vs the
# real function so any drift is surfaced, not hidden).
PASS_THRESHOLD = 35          # score>=35 -> non-weak (passes the gate)
WEAK_PENALTY = 30
TITLE_KEYWORDS = ["보도자료", "설명자료", "브리핑", "공고", "공지", "정책"]

# The WEAK_URL_SIGNALS token that matches 'menuNo=' is the raw substring 'menu'.
MENU_WEAK_TOKENS = {"menu"}

# Tokens for the PART-C distinguisher table.
DISTINGUISHER_TOKENS = ["view.do", "list.do", "menuno", "search", "press"]

EXAMPLES_PER_HOST = 3


def _j(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _row_date(created_at) -> str:
    if created_at is None:
        return ""
    s = str(created_at)
    return s[:10] if len(s) >= 10 else ""


def _is_dict(c):
    return isinstance(c, dict)


def _cand_url(item):
    """Mirror _resolve_source's url selection: official_detail_url -> official_body_url
    -> url. For FSS this is the originUrl."""
    return item.get("official_detail_url") or item.get("official_body_url") or item.get("url") or ""


def _host(url):
    try:
        return urlparse(url or "").netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _score_url_local(url, title="", *, suppress_menu=False):
    """Local replica of score_official_url's scoring, used to (a) self-check
    against the real function and (b) simulate the candidate minimal fix. Mirrors
    the real branch order/weights/thresholds EXACTLY.

    SIMULATED FIX (suppress_menu=True): skip the -30 search/index penalty IFF the
    penalty would fire SOLELY because of a 'menu' token (no other WEAK signal, and
    looks_like_search is False) AND a positive detail signal (detail_url_pattern
    OR numeric_detail_id) is present. SIMULATION ONLY — production is untouched."""
    nu = (url or "").lower()
    tt = sanitize_text(title or "")
    score = 0
    reasons = []
    if is_official_domain(url):
        score += 25
        reasons.append("official_domain")
    if nu.endswith(".pdf") or ".pdf" in nu:
        score += 12
        reasons.append("pdf_policy_document")
    detail_hits = [s for s in DETAIL_URL_SIGNALS if s in nu]
    if detail_hits:
        score += 28
        reasons.append("detail_url_pattern")
    has_numeric = bool(re.search(r"\d{4,}", nu))
    if has_numeric:
        score += 10
        reasons.append("numeric_detail_id")
    if any(k in tt for k in TITLE_KEYWORDS):
        score += 15
        reasons.append("official_content_title")

    looks = looks_like_official_search_or_index_url(url)
    weak_hits = [s for s in WEAK_URL_SIGNALS if s in nu]
    penalty_fires = bool(looks or weak_hits)
    non_menu_weak = [s for s in weak_hits if s not in MENU_WEAK_TOKENS]
    # The candidate guard: menu is the SOLE thing tripping the penalty, and the URL
    # looks like a genuine detail page.
    menu_is_sole_cause = (
        penalty_fires
        and not looks
        and not non_menu_weak
        and ("menu" in weak_hits)
        and (bool(detail_hits) or has_numeric)
    )
    suppress = bool(suppress_menu and menu_is_sole_cause)
    if penalty_fires and not suppress:
        score -= WEAK_PENALTY
        reasons.append("search_or_index_like")
    if not url:
        score -= 40
        reasons.append("url_missing")

    score = max(0, min(100, score))
    if score >= 65:
        status = "detail_page_likely"
    elif score >= PASS_THRESHOLD:
        status = "candidate_needs_body_check"
    else:
        status = "weak_or_search_page"
    return {
        "score": score,
        "status": status,
        "reasons": reasons,
        "weak_hits": weak_hits,
        "non_menu_weak": non_menu_weak,
        "looks": looks,
        "detail_hits": detail_hits,
        "has_numeric": has_numeric,
        "menu_is_sole_cause": menu_is_sole_cause,
    }


def _passes(status):
    return status != "weak_or_search_page"


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    cutoff = ""
    if LOOKBACK_DAYS and LOOKBACK_DAYS > 0:
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    rows = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, source_candidates FROM analysis_results ORDER BY id"
        )
        for rid, created_at, sc in cur.fetchall():
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue
            rows.append((rid, _j(sc) or []))

    print("URL-GATE-SCOPE-PROBE — read-only scope of the 'menu' URL-gate misclassification")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   (LOOKBACK_DAYS=%d; 0 = whole corpus)" % (scope, LOOKBACK_DAYS))
    print("  PASS threshold: score>=%d   weak penalty: -%d   menu token(s): %s"
          % (PASS_THRESHOLD, WEAK_PENALTY, sorted(MENU_WEAK_TOKENS)))
    print("  NOTE: url scored with title='' to ISOLATE URL shape (title bonus excluded).")
    print()
    if not rows:
        print("  No rows in scope — nothing to diagnose.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted.")
        return 0

    # ---- collect distinct official candidate URLs + provenance ---------------
    # url -> {hosts, markers, matched(any candidate with official_body_match=True)}
    url_meta = {}
    matched_urls = set()       # URLs on a candidate with official_body_match=True
    for rid, cands in rows:
        for c in cands:
            if not _is_dict(c):
                continue
            u = _cand_url(c)
            if not u:
                continue
            m = url_meta.setdefault(u, {"markers": set(), "matched": False})
            if FSS_MARKER in c:
                m["markers"].add("FSS")
            if PB_MARKER in c:
                m["markers"].add("PB")
            if LAW_MARKER in c:
                m["markers"].add("LAW")
            if bool(c.get("official_body_match")):
                m["matched"] = True
                matched_urls.add(u)

    distinct_urls = sorted(url_meta.keys())
    print("  distinct official candidate URLs (any candidate carrying a URL): %d"
          % len(distinct_urls))
    print()

    # ---- score every distinct URL (real + self-check + simulated) ------------
    self_check_ok = 0
    self_check_bad = []
    real = {}      # url -> local no-suppress result (self-checked against real fn)
    sim = {}       # url -> local suppress-menu result
    for u in distinct_urls:
        real_fn = score_official_url(u, "")
        loc = _score_url_local(u, "", suppress_menu=False)
        if (loc["status"] == real_fn.get("official_url_resolution_status")
                and loc["score"] == int(real_fn.get("official_url_score") or 0)):
            self_check_ok += 1
        else:
            self_check_bad.append((u, loc["status"], loc["score"],
                                   real_fn.get("official_url_resolution_status"),
                                   real_fn.get("official_url_score")))
        real[u] = loc
        sim[u] = _score_url_local(u, "", suppress_menu=True)

    print("  local-vs-real score_official_url self-check: %d/%d agree"
          % (self_check_ok, len(distinct_urls)))
    if self_check_bad:
        print("  ★ MISMATCH between local replica and real score_official_url — local")
        print("    scoring has drifted from production; treat simulation with caution:")
        for u, ls, lsc, rs, rsc in self_check_bad[:10]:
            print("      %s  local=%s/%s  real=%s/%s" % (u, ls, lsc, rs, rsc))
    print()

    # =======================================================================
    print("=== PART A — 'menu'-penalised-but-detail-looking URLs (by host) ===")
    menu_victims = [u for u in distinct_urls
                    if real[u]["menu_is_sole_cause"] and not _passes(real[u]["status"])]
    by_host = collections.defaultdict(list)
    for u in menu_victims:
        by_host[_host(u)].append(u)
    print("  total menu-penalised-but-detail-looking URLs (currently weak): %d"
          % len(menu_victims))
    if not menu_victims:
        print("  (none — no URL is killed SOLELY by the 'menu' penalty while looking like a detail page)")
    else:
        for host in sorted(by_host, key=lambda h: (-len(by_host[h]), h)):
            urls = by_host[host]
            markers = set()
            for u in urls:
                markers |= url_meta[u]["markers"]
            print("  host=%-22s count=%-4d markers=%s" % (host, len(urls), sorted(markers) or "-"))
            for u in urls[:EXAMPLES_PER_HOST]:
                print("      e.g. %s" % u)
    non_fss_hosts = [h for h in by_host if h and "fss.or.kr" not in h]
    print()
    print("  >>> FSS-only? %s"
          % ("YES — every menu-victim host is fss.or.kr" if menu_victims and not non_fss_hosts
             else ("NO — other hosts also affected: %s" % sorted(non_fss_hosts) if non_fss_hosts
                   else "N/A — no menu-victims found")))
    print()

    # =======================================================================
    print("=== PART B — SIMULATED minimal fix (SIMULATION ONLY, not production) ===")
    print("  rule: suppress the -%d penalty IFF its SOLE cause is a 'menu' token AND a"
          % WEAK_PENALTY)
    print("        positive detail signal (detail_url_pattern OR numeric_detail_id) is present.")
    print()
    recovery = [u for u in distinct_urls if not _passes(real[u]["status"]) and _passes(sim[u]["status"])]
    downgrade = [u for u in distinct_urls if _passes(real[u]["status"]) and not _passes(sim[u]["status"])]

    rec_by_host = collections.Counter(_host(u) for u in recovery)
    print("  RECOVERY set (weak -> pass under the simulated fix): %d" % len(recovery))
    for host, n in sorted(rec_by_host.items(), key=lambda kv: (-kv[1], kv[0])):
        markers = set()
        for u in recovery:
            if _host(u) == host:
                markers |= url_meta[u]["markers"]
        print("    host=%-22s recovered=%-4d markers=%s" % (host, n, sorted(markers) or "-"))
    print()
    print("  DOWNGRADE set (pass -> weak; MUST be 0 for a safe fix): %d" % len(downgrade))
    if downgrade:
        print("  ★ NON-ZERO DOWNGRADE — the simulated fix is NOT monotonic-safe; inspect:")
        for u in downgrade[:20]:
            print("      %s  real=%s  sim=%s" % (u, real[u]["status"], sim[u]["status"]))
    else:
        print("    (0 — the fix only ever REMOVES a penalty, so no URL can lose its pass)")
    print()

    # verdict-regression surface: any currently-matching row whose url changes status
    changed_urls = set(recovery) | set(downgrade)
    matched_changed = sorted(matched_urls & changed_urls)
    print("  VERDICT-REGRESSION SURFACE — currently-matching (official_body_match=True)")
    print("    URLs whose url_status changes under the simulated fix: %d" % len(matched_changed))
    if matched_changed:
        for u in matched_changed[:20]:
            print("      %s  markers=%s  real=%s -> sim=%s"
                  % (u, sorted(url_meta[u]["markers"]), real[u]["status"], sim[u]["status"]))
        print("    >>> these rows already MATCH; a status change here is the regression risk.")
    else:
        print("    (0 — no currently-matching row's URL changes; matching URLs already pass")
        print("     the gate, and the fix only raises weak scores, so none are touched)")
    print()
    safe_general = (not downgrade) and (not matched_changed)
    print("  >>> safe to fix generally? %s"
          % ("YES (0 downgrades, 0 verdict-regression surface)" if safe_general
             else "NO — see downgrade / verdict-regression lines above; prefer FSS-narrow"))
    print()

    # =======================================================================
    print("=== PART C — token-distinguisher table (surgical guard precision) ===")
    # menu-victim group vs genuine-index group (weak for NON-menu reasons).
    genuine_index = [u for u in distinct_urls
                     if not _passes(real[u]["status"]) and not real[u]["menu_is_sole_cause"]
                     and (real[u]["looks"] or real[u]["non_menu_weak"])]
    print("  groups:  menu-victims=%d   genuine-index(weak for non-menu reasons)=%d"
          % (len(menu_victims), len(genuine_index)))
    print("  token presence (substring, case-insensitive):")
    print("    %-12s | %-14s | %-14s" % ("token", "menu-victims", "genuine-index"))
    print("    %s" % ("-" * 46))

    def _pct(group, token):
        if not group:
            return "0/0"
        n = sum(1 for u in group if token in u.lower())
        return "%d/%d" % (n, len(group))

    for tok in DISTINGUISHER_TOKENS:
        print("    %-12s | %-14s | %-14s"
              % (tok, _pct(menu_victims, tok), _pct(genuine_index, tok)))
    # numeric detail id is a regex, handle separately
    def _pct_numeric(group):
        if not group:
            return "0/0"
        n = sum(1 for u in group if re.search(r"\d{4,}", u.lower()))
        return "%d/%d" % (n, len(group))
    print("    %-12s | %-14s | %-14s"
          % ("numeric_id", _pct_numeric(menu_victims), _pct_numeric(genuine_index)))
    print()
    print("  >>> surgical guard (from the table): suppress the menu penalty only when a")
    print("      strong detail token (view.do / press*View.do / \\d{4,} id) is present AND")
    print("      no genuine-index token (list.do / search) is present. Confirm the menu-")
    print("      victim column shows view.do/numeric_id while genuine-index shows list.do/search.")
    print()

    # =======================================================================
    print("=== CLOSING — scope recommendation ===")
    print("  menu-victims=%d (FSS-only=%s); URL-fix recovery=%d; downgrades=%d; verdict-"
          % (len(menu_victims), (menu_victims and not non_fss_hosts),
             len(recovery), len(downgrade)))
    print("  regression surface=%d." % len(matched_changed))
    if not menu_victims or len(recovery) <= 2 and not non_fss_hosts:
        print("  RECOMMENDATION (iii): a URL fix barely helps (recovery tiny, FSS-only) — likely")
        print("  NOT worth it on its own; the binding constraint is retrieval relevance + score<55.")
    elif non_fss_hosts:
        print("  RECOMMENDATION (ii): a GENERAL 'menu-with-detail-signal' guard helps multiple")
        print("  hosts; safe iff downgrades=0 and verdict-regression surface=0 (see PART B).")
    else:
        print("  RECOMMENDATION (i): an FSS-narrow URL fix — affects only fss.or.kr; smallest")
        print("  blast radius. Pair with retrieval-relevance work since URL alone recovers few.")
    print("  (Numbers above are SIMULATION ONLY; production score_official_url is unchanged.)")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted; no network.")
    print("[Safety] Simulated fix is LOCAL to this probe — no production code/threshold/signal changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
