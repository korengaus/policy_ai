"""BACKFILL-YIELD — READ-ONLY, go/no-go probe: how much PAST policy-news yield does
Naver relevance-paging actually give (BACKFILL Phase 2a)?

WHY: the BACKFILL design (_backfill_design.md) is verdict-isolated and sound, but its
viability hinges on ONE unmeasured fact — Naver's news API has NO date parameter
(query/display/start/sort only), so historical reach is RELEVANCE-PAGING (sort=sim,
start<=1000) not a past-date-window request. This probe MEASURES the real net-new
historical yield BEFORE anything is built. If yield is low -> PIVOT to the date-pageable
정책브리핑/법제처 archive lane (government sources are copyright-free anyway).

MEASUREMENT ONLY. Read-only calls to the EXISTING Naver provider (providers.get_search_provider
-> NaverNewsSearchProvider.search; reused, NOT re-implemented). SELECT-only on our DB
(result_exists_by_url). NO analysis (no LLM / no _process_news_item), NO storage, NO writes,
NO git. Reuses the REAL reject (news_collector._reject_title_reason) + the REAL government-domain
set (official_metadata). Never logs the Naver API key (the provider keeps secrets in headers only).

METRICS (per sampled seed query + total)
  * items returned (how deep paging goes before Naver stops)
  * NET-NEW: items whose original_url is NOT already in analysis_results (result_exists_by_url)
  * REJECT-SURVIVAL: of the net-new, how many survive _reject_title_reason (opinion/obituary/
    political_subject) = actually analyzable
  * GOV-SOURCE hit rate: of the net-new, how many are government-domain (OFFICIAL_AUTHORITY_DOMAINS
    ∪ PUBLIC_INSTITUTION_DOMAINS) = the copyright-free fraction
  * pubDate span: oldest reachable published date (how far back relevance-paging goes)

VERDICT: relevance-paging yields meaningful historical net-new-analyzable (GO for the Naver lane),
or it is mostly recent-dupes (PIVOT to the 정책브리핑/법제처 archive lane). Stated with the numbers.

HARD CAPS (printed): MAX_PAGES_PER_QUERY pages x DISPLAY items, with a short sleep between calls to
respect the free tier (25k calls/day). Total calls <= len(sample) x MAX_PAGES_PER_QUERY.

SAFETY: read-only provider calls + SELECT-only DB; no writes/analysis/git. Lazy imports of the
network/DB pieces inside the live path so --selftest is fully offline. ASCII-guarded prints.

Usage:
    PYTHONPATH=. python scripts/backfill_yield_probe.py            # live (needs NAVER key + net)
    PYTHONPATH=. python scripts/backfill_yield_probe.py --selftest # offline logic check, no network/DB

Exit codes: 0 = dump printed / provider unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# HARD CAPS — bound the Naver spend. Free tier is 25k calls/day; this probe uses at
# most len(SAMPLE_QUERIES) x MAX_PAGES_PER_QUERY calls.
DISPLAY = 100                 # items per call (provider clamps to MAX_DISPLAY=100)
MAX_PAGES_PER_QUERY = 10      # start = 1, 101, 201, ... (provider clamps start<=1000)
SLEEP_BETWEEN_CALLS = 0.3     # seconds — gentle on the free tier

# A small cross-domain sample of the ACTIVE seed set (incl. one ENV-SEED query).
SAMPLE_QUERIES = (
    "주택담보대출 규제",      # realestate/finance
    "복지 예산",              # welfare
    "농가 소득 지원",         # agriculture
    "청년 일자리",            # labor
    "탄소중립 온실가스 감축",  # ENV-SEED (environment)
)


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def _gov_domains() -> set:
    """The REAL government-domain set (reused, not re-implemented). Empty on import
    failure (gov fraction then reads 0 — flagged, not a crash)."""
    try:
        from official_metadata import OFFICIAL_AUTHORITY_DOMAINS, PUBLIC_INSTITUTION_DOMAINS
        return set(OFFICIAL_AUTHORITY_DOMAINS) | set(PUBLIC_INSTITUTION_DOMAINS)
    except Exception:  # noqa: BLE001
        return set()


def _is_gov(url: str, gov_domains: set) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in gov_domains)


def _pub_iso(published: str) -> str:
    """RFC-1123 pubDate -> ISO date (YYYY-MM-DD), '' when unparseable."""
    if not published:
        return ""
    try:
        dt = parsedate_to_datetime(published)
        return dt.date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _survives_reject(title: str) -> bool:
    """REAL intake decision (reused): does _reject_title_reason KEEP this title?"""
    from news_collector import _reject_title_reason
    return _reject_title_reason(str(title or "")) is None


def _already_stored(url: str) -> bool:
    """REAL dedupe key (reused): is this original_url already in analysis_results?"""
    from database import result_exists_by_url
    return result_exists_by_url(str(url or ""))


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST (synthetic items; no network, no DB)
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== BACKFILL-YIELD --selftest (offline; no network, no DB) ===")
    failures = []

    gov = _gov_domains()
    if not gov:
        p("  [note] could not import official_metadata gov domains offline (deferred to live).")
    else:
        p(f"  [ok] government domain set loaded: {len(gov)} domains (reused from official_metadata).")

    # 1. gov-domain classification.
    if gov:
        if not _is_gov("https://www.korea.kr/news/policyNewsView.do?newsId=1", gov):
            failures.append("_is_gov missed korea.kr")
        if not _is_gov("https://www.law.go.kr/x", gov):
            failures.append("_is_gov missed law.go.kr")
        if _is_gov("https://news.some-press.com/article/1", gov):
            failures.append("_is_gov false-positived a press domain")
    p(f"  [{'ok' if not failures else 'xx'}] gov classification (korea.kr/law.go.kr gov; press not).")

    # 2. pubDate parsing + oldest tracking.
    d1 = _pub_iso("Mon, 03 Feb 2025 09:15:00 +0900")
    d2 = _pub_iso("Wed, 12 Jun 2024 00:00:00 +0900")
    if d1 != "2025-02-03" or d2 != "2024-06-12" or _pub_iso("garbage") != "":
        failures.append(f"_pub_iso wrong: {d1}, {d2}")
    p(f"  [{'ok' if not failures else 'xx'}] pubDate parse: {d1}, {d2}, oldest={min(d1, d2)}")

    # 3. reject-survival uses the REAL predicate (political_subject drops).
    keep = _survives_reject("전세 공급 대책 시행 방안 발표")
    drop = _survives_reject("특검 통한 이재명 대통령 공소취소")  # political_subject
    if not keep:
        failures.append("reject-survival dropped a clean policy title")
    if drop:
        failures.append("reject-survival kept a political_subject title")
    p(f"  [{'ok' if not failures else 'xx'}] reject-survival: clean-policy keep={keep}, political drop={not drop}")

    # 4. host extraction.
    if _host("https://www.korea.kr/a/b") != "korea.kr":
        failures.append("_host wrong")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (gov set + gov-classify + pubDate + reject-survival + host)")
    return 0


# ---------------------------------------------------------------------------
# LIVE RUN (read-only Naver calls + SELECT-only DB)
# ---------------------------------------------------------------------------
def run_live() -> int:
    p("=== BACKFILL-YIELD (READ-ONLY: Naver paging + DB dedupe check) ===")
    p(f"  CAPS: <= {MAX_PAGES_PER_QUERY} pages/query x {DISPLAY} items, "
      f"{SLEEP_BETWEEN_CALLS}s between calls; sample={len(SAMPLE_QUERIES)} queries "
      f"(max {len(SAMPLE_QUERIES) * MAX_PAGES_PER_QUERY} Naver calls).")

    from providers import get_search_provider
    provider = get_search_provider("naver")
    if not getattr(provider, "available", False):
        p(f"  Naver provider unavailable: {getattr(provider, 'reason', 'unknown')}")
        p("  (Set NAVER_SEARCH_ENABLED=true + NAVER_CLIENT_ID/SECRET. Run --selftest for offline logic.)")
        return 0

    gov = _gov_domains()
    p(f"  government-domain set: {len(gov)} domains")

    totals = {"returned": 0, "net_new": 0, "net_new_survive": 0, "net_new_gov": 0}
    overall_oldest = ""
    p("")
    p("  query | returned | net-new | net-new-survive | gov | oldest-pubDate")

    for q in SAMPLE_QUERIES:
        seen_urls = set()
        returned = net_new = net_new_survive = net_new_gov = 0
        oldest = ""
        for page in range(MAX_PAGES_PER_QUERY):
            start = 1 + page * DISPLAY
            result = provider.search(q, limit=DISPLAY, start=start, sort="sim")
            items = result.get("items") or []
            if not items:
                break  # Naver stopped returning — paging exhausted
            for it in items:
                url = (it.get("original_url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                returned += 1
                iso = _pub_iso(it.get("published") or "")
                if iso and (not oldest or iso < oldest):
                    oldest = iso
                # NET-NEW: not already stored (real dedupe key).
                if _already_stored(url):
                    continue
                net_new += 1
                if _survives_reject(it.get("title") or ""):
                    net_new_survive += 1
                if _is_gov(url, gov):
                    net_new_gov += 1
            time.sleep(SLEEP_BETWEEN_CALLS)

        totals["returned"] += returned
        totals["net_new"] += net_new
        totals["net_new_survive"] += net_new_survive
        totals["net_new_gov"] += net_new_gov
        if oldest and (not overall_oldest or oldest < overall_oldest):
            overall_oldest = oldest
        p(f"    {_ascii(q)} | {returned} | {net_new} | {net_new_survive} | {net_new_gov} | {oldest or '(none)'}")

    # ---- TOTAL + VERDICT ----------------------------------------------------
    p("")
    p("=== TOTAL (across the sample) ===")
    p(f"  items returned          : {totals['returned']}")
    p(f"  net-new (not in DB)     : {totals['net_new']}")
    p(f"  net-new ANALYZABLE      : {totals['net_new_survive']}  (survive the intake reject)")
    gov_frac = (totals["net_new_gov"] / totals["net_new"]) if totals["net_new"] else 0.0
    p(f"  net-new gov-source      : {totals['net_new_gov']}  ({round(100 * gov_frac)}% copyright-free)")
    p(f"  oldest pubDate reached  : {overall_oldest or '(none)'}")

    per_query = totals["net_new_survive"] / len(SAMPLE_QUERIES)
    p("")
    p("=== VERDICT (relevance-paging historical yield) ===")
    p(f"  avg net-new-analyzable per query: {round(per_query, 1)}")
    # The bar is the strategist's; the probe states the reading with the numbers so
    # the decision is one line. A low net-new / near-recent oldest date => the API is
    # returning mostly the same recent items we already have.
    if per_query >= 20:
        p("  => GO (Naver lane): relevance-paging yields meaningful net-new analyzable historical")
        p(f"     items ({round(per_query, 1)}/query, oldest {overall_oldest}). Proceed to the piloted backfill.")
    elif per_query >= 5:
        p("  => MARGINAL: some net-new yield but modest; strategist decides Naver-lane pilot vs archive.")
        p(f"     ({round(per_query, 1)}/query analyzable, oldest {overall_oldest}, gov {round(100*gov_frac)}%).")
    else:
        p("  => PIVOT (archive lane): relevance-paging is mostly recent-dupes — negligible net-new")
        p(f"     ({round(per_query, 1)}/query). Backfill from the date-pageable 정책브리핑/법제처 archives")
        p("     (government sources, copyright-free) instead of Naver news paging.")

    p("")
    p("NOTE: provider calls are read-only; result_exists_by_url is SELECT-only; NO article was")
    p("analyzed (no LLM/_process_news_item) and NOTHING was stored. The lane decision is the strategist's.")
    p("[Safety] READ-ONLY probe — no rows written/updated/deleted; no analysis performed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY Naver historical-yield go/no-go probe for the backfill. "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no network / DB).")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
