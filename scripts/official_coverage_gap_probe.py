"""OFFICIAL-COVERAGE-GAP PROBE — READ-ONLY measurement of WHERE official-evidence
coverage is missing across the FULL corpus (by domain, content nature, supporting
source, and time), so any future primary-source addition is chosen by data.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE.
No pipeline / verdict / matcher / display / seed change. No new predicate, no
threshold, no candidate source proposed — the numbers come first, the source
decision comes later, elsewhere.

★NOT-A-GAP GUARD: a market/commercial card (기업 제품 출시 등) legitimately has
no official government document. The report therefore ALWAYS splits by
content_nature (government_policy / market_commercial / mixed_or_unclear —
the actual live values, confirmed 2026-07-22) and ranks "gap" ONLY within
policy-ish rows. ★This probe must not be read as an argument to lower any
matching threshold — matcher sensitivity is measurement-closed.

DATA SHAPE (confirmed on live rows, 2026-07-22)
-----------------------------------------------
  * domain          : plain column (welfare/finance/labor/... + '기타-미분류';
                      NULL/'' possible on old rows -> reported as '(null)').
  * content_nature  : plain column (government_policy 10913 / market_commercial
                      1376 / mixed_or_unclear 1299 at capture).
  * claims          : JSON list of plain STRINGS (the live table PREDATES
                      claim_text — never read claim_text).
  * published_at    : 98.8% filled at capture -> month bucketing is usable;
                      blank/NULL rows fall into an explicit '(no date)' bucket
                      (never dropped silently).
  * GENUINE predicate (MIRRORED, not re-invented):
      source_reliability_summary["has_genuine_official_support"] when it is a
      real bool, else debug_summary.official_body_matches > 0 — exactly
      postgres_storage.py:1549-1557 (the top-line/frontend predicate).
  * SUPPORTING document (for genuine rows): candidates passing the agent's own
      rule (source_reliability_agent.py:337-344): official-like source_type AND
      raw_text_available AND official_body_match AND 3-way score >= 55. The
      supporting HOST is the normalized host of the candidate's url
      (e.g. korea.kr = 정책브리핑, law.go.kr = 법제처).

WHAT IT PRINTS (denominators always shown)
------------------------------------------
  1. GENUINE-SUPPORT RATE BY DOMAIN — rows / genuine / %, sorted by row count,
     including '(null)' and 기타-미분류.
  2. SPLIT BY CONTENT NATURE — the same rate per content_nature value found
     (values reported as-is, not assumed), and domain x policy-ish detail.
  3. SUPPORTING-SOURCE DISTRIBUTION — for genuine rows, which host(s) carry the
     support (per-row dedup), so a dormant source type is visible.
  4. TIME TREND — genuine rate by published_at month (+ '(no date)' bucket).
     If newer rows match far better, the gap is historical and shrinks on its
     own — an argument AGAINST adding a source.
  5. QUALITATIVE SAMPLE — for the lowest-genuine-rate domains among
     policy-ish rows (min --min-rows rows), ~--sample RANDOM claims lacking
     genuine support (seeded reservoir — random, not id-front), so a human can
     judge what KIND of document would have been needed.

SAFETY: engine.connect() only, no commit. Paged by id cursor (source_candidates
is ~1.2MB/row). Lazy DB import so --selftest is fully offline. UTF-8 guarded.

Usage (Render Worker Shell, after commit+push+redeploy+reopen Shell):
    PYTHONPATH=. python scripts/official_coverage_gap_probe.py
    PYTHONPATH=. python scripts/official_coverage_gap_probe.py --selftest
    ... --page 200 --sample 30 --seed 42 --min-rows 100 --gap-domains 3

Exit codes: 0 = report printed / engine unavailable; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


OFFICIAL_TYPES = {"official_government", "public_institution"}
MIN_CLAIM_CHARS = 15
POLICYISH = "government_policy"  # confirmed live vocabulary; others reported as found


def p(message: str = "") -> None:
    print(message, flush=True)


def _loads(raw) -> object:
    if raw in (None, ""):
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def is_genuine(srs_raw, debug_raw) -> bool:
    """MIRROR of postgres_storage.py:1549-1557 (the top-line predicate):
    persisted has_genuine_official_support bool, else
    debug_summary.official_body_matches > 0. Never re-derived."""
    srs = _loads(srs_raw)
    if isinstance(srs, dict):
        genuine = srs.get("has_genuine_official_support")
        if isinstance(genuine, bool):
            return genuine
    debug = _loads(debug_raw)
    if isinstance(debug, dict):
        try:
            return int(debug.get("official_body_matches") or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


def _score(candidate: dict) -> int:
    """The agent's own 3-way chain (source_reliability_agent.py:343)."""
    try:
        return int(
            candidate.get("official_evidence_score")
            or candidate.get("official_final_direct_match_score")
            or candidate.get("official_body_match_score")
            or 0
        )
    except (TypeError, ValueError):
        return 0


def supporting_hosts(candidates_raw) -> set:
    """Hosts of the candidates that pass the agent's supporting-match rule
    (source_reliability_agent.py:337-344). Per-row set (deduped)."""
    candidates = _loads(candidates_raw)
    hosts = set()
    if not isinstance(candidates, list):
        return hosts
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if (
            str(candidate.get("source_type") or "") in OFFICIAL_TYPES
            and candidate.get("raw_text_available")
            and candidate.get("official_body_match")
            and _score(candidate) >= 55
        ):
            host = host_of(candidate.get("url") or candidate.get("official_detail_url"))
            if host:
                hosts.add(host)
    return hosts


def host_of(url) -> str:
    """Normalized host: lowercase, creds/port dropped, www./m. stripped —
    same normalization family the outlet counter uses."""
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except ValueError:
        return ""
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def first_claim(claims_raw) -> str:
    """First substantive claim string from the claims JSON list (plain
    strings on the live table)."""
    claims = _loads(claims_raw)
    if not isinstance(claims, list):
        return ""
    texts = [str(c or "").strip() for c in claims if isinstance(c, str)]
    for text in texts:
        if len(text) >= MIN_CLAIM_CHARS:
            return text
    return texts[0] if texts else ""


def month_bucket(published_at) -> str:
    value = str(published_at or "").strip()
    return value[:7] if len(value) >= 7 else "(no date)"


def norm_key(value) -> str:
    value = str(value or "").strip()
    return value if value else "(null)"


class Reservoir:
    """Seeded uniform reservoir sample (random, not id-front)."""

    def __init__(self, cap: int, seed):
        self.cap = cap
        self.rng = random.Random(seed)
        self.items: list = []
        self.seen = 0

    def offer(self, item) -> None:
        self.seen += 1
        if len(self.items) < self.cap:
            self.items.append(item)
        else:
            j = self.rng.randrange(self.seen)
            if j < self.cap:
                self.items[j] = item


class Tally:
    """rows/genuine counters keyed by an arbitrary label."""

    def __init__(self):
        self.rows = Counter()
        self.genuine = Counter()

    def add(self, key: str, genuine: bool) -> None:
        self.rows[key] += 1
        if genuine:
            self.genuine[key] += 1

    def rate_rows(self, sort_by_count=True):
        keys = sorted(self.rows, key=lambda k: (-self.rows[k], k)) if sort_by_count \
            else sorted(self.rows)
        return [(k, self.rows[k], self.genuine[k]) for k in keys]


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def _rate_table(tally: Tally, indent="    ", sort_by_count=True) -> None:
    for key, rows, genuine in tally.rate_rows(sort_by_count):
        p(f"{indent}{key:<22}{rows:>7} rows   genuine {genuine:>6}   {_pct(genuine, rows)}")


def report(state: dict, sample: int, gap_domains: int, min_rows: int) -> None:
    total, total_genuine = state["total"], state["total_genuine"]
    p("")
    p("=== 1. GENUINE-SUPPORT RATE BY DOMAIN (all rows) ===")
    p(f"  corpus: {total} rows, genuine {total_genuine} ({_pct(total_genuine, total)})")
    _rate_table(state["by_domain"])

    p("")
    p("=== 2. SPLIT BY CONTENT NATURE ===")
    p("  (market/commercial cards legitimately lack official documents — only the")
    p("   policy-ish shortfall is a candidate 'gap')")
    _rate_table(state["by_nature"])
    p(f"  domain x {POLICYISH} only:")
    _rate_table(state["by_domain_policyish"])

    p("")
    p("=== 3. SUPPORTING-SOURCE DISTRIBUTION (genuine rows, per-row dedup) ===")
    # RECON FIDELITY (same gate as the echo-independence probe's reproduction
    # check): the hosts below are RE-DERIVED via the agent rule
    # (source_reliability_agent.py:337-344), not stored — so first report how
    # many stored-genuine rows the re-derivation actually reproduces.
    with_host = state["genuine_with_host"]
    zero = total_genuine - with_host
    p(f"  RECON fidelity: {with_host} / {total_genuine} genuine rows "
      f"({_pct(with_host, total_genuine)}) yield >=1 re-derived supporter; "
      f"{zero} ({_pct(zero, total_genuine)}) yield ZERO")
    if total_genuine and zero / total_genuine > 0.10:
        p("  ★>10% of genuine rows are NOT reproduced by the agent rule — the host")
        p("   distribution below is PARTIAL and must not be read as the full picture.")
    p(f"  host distribution covers {with_host} of {total_genuine} genuine rows:")
    for host, count in state["host_counts"].most_common(15):
        p(f"    {host:<34}{count:>6} rows   {_pct(count, with_host)}")

    p("")
    p("=== 4. TIME TREND (published_at month; '(no date)' shown, never dropped) ===")
    _rate_table(state["by_month"], sort_by_count=False)
    p("  ^ if newer months match far better, the shortfall is historical and")
    p("    shrinks on its own — that argues AGAINST adding a source.")

    p("")
    p(f"=== 5. QUALITATIVE SAMPLE — lowest-genuine-rate {POLICYISH} domains ===")
    eligible = [
        (genuine / rows, key, rows, genuine)
        for key, rows, genuine in state["by_domain_policyish"].rate_rows()
        if rows >= min_rows
    ]
    worst = sorted(eligible)[:gap_domains]
    if not worst:
        p(f"    (no domain reaches the {min_rows}-row floor)")
    per_domain = max(1, sample // max(1, len(worst)))
    for rate, domain, rows, genuine in worst:
        p(f"  [{domain}] {rows} policy-ish rows, genuine {genuine} ({_pct(genuine, rows)})"
          f" — {per_domain} random unsupported claims:")
        reservoir = state["reservoirs"].get(domain)
        for row_id, claim in (reservoir.items[:per_domain] if reservoir else []):
            p(f"    #{row_id}  {claim[:120]}")
    p("")
    p("  Read the samples for what KIND of document was missing (statute? ministry")
    p("  release? local notice? court/agency filing? statistics table?) — the answer")
    p("  is a document TYPE, never a matcher-threshold change (that path is closed).")


def run_live(page: int, sample: int, seed, gap_domains: int, min_rows: int) -> int:
    p("=== OFFICIAL-COVERAGE-GAP PROBE (READ-ONLY, SELECT-only, full corpus) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        return 0

    state = {
        "total": 0, "total_genuine": 0, "genuine_with_host": 0,
        "by_domain": Tally(), "by_nature": Tally(),
        "by_domain_policyish": Tally(), "by_month": Tally(),
        "host_counts": Counter(),
        "reservoirs": defaultdict(lambda: Reservoir(sample, seed)),
    }
    last_id = 0
    with engine.connect() as conn:
        while True:
            rows = conn.execute(
                sa.text(
                    "SELECT id, domain, content_nature, published_at, claims, "
                    "source_reliability_summary, debug_summary, source_candidates "
                    "FROM analysis_results WHERE id > :last ORDER BY id LIMIT :lim"
                ).bindparams(last=last_id, lim=page)
            ).all()
            if not rows:
                break
            for (row_id, domain, nature, published_at, claims_raw,
                 srs_raw, debug_raw, cands_raw) in rows:
                last_id = row_id
                state["total"] += 1
                genuine = is_genuine(srs_raw, debug_raw)
                domain_key, nature_key = norm_key(domain), norm_key(nature)
                if genuine:
                    state["total_genuine"] += 1
                state["by_domain"].add(domain_key, genuine)
                state["by_nature"].add(nature_key, genuine)
                state["by_month"].add(month_bucket(published_at), genuine)
                if nature_key == POLICYISH:
                    state["by_domain_policyish"].add(domain_key, genuine)
                    if not genuine:
                        claim = first_claim(claims_raw)
                        if claim:
                            state["reservoirs"][domain_key].offer((row_id, claim))
                if genuine:
                    hosts = supporting_hosts(cands_raw)
                    if hosts:
                        state["genuine_with_host"] += 1
                        for host in hosts:
                            state["host_counts"][host] += 1
            p(f"  ... scanned {state['total']} rows (last id {last_id})")

    report(state, sample, gap_domains, min_rows)
    return 0


def _selftest() -> int:
    """Offline logic check — no DB, no network."""
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # Predicate mirror (postgres_storage.py:1549-1557).
    check("genuine-bool", is_genuine('{"has_genuine_official_support": true}', None), True)
    check("genuine-false-bool", is_genuine('{"has_genuine_official_support": false}',
                                           '{"official_body_matches": 3}'), False)
    check("genuine-fallback", is_genuine("{}", '{"official_body_matches": 2}'), True)
    check("genuine-zero", is_genuine("{}", '{"official_body_matches": 0}'), False)
    check("genuine-null", is_genuine(None, None), False)

    # Supporting-candidate rule (source_reliability_agent.py:337-344).
    good = {"source_type": "official_government", "raw_text_available": True,
            "official_body_match": True, "official_evidence_score": 60,
            "url": "https://www.korea.kr/briefing/x"}
    low = dict(good, official_evidence_score=54)
    wrong_type = dict(good, source_type="established_news")
    no_body = dict(good, official_body_match=False)
    check("support-host", supporting_hosts(json.dumps([good, low, wrong_type, no_body])),
          {"korea.kr"})
    check("support-chain-fallback",
          supporting_hosts(json.dumps([{
              "source_type": "public_institution", "raw_text_available": True,
              "official_body_match": True, "official_body_match_score": 55,
              "url": "http://law.go.kr/a"}])),
          {"law.go.kr"})
    check("support-empty", supporting_hosts(None), set())

    check("host-norm", host_of("https://M.korea.KR:443/x"), "korea.kr")
    check("host-empty", host_of(None), "")

    check("claim-substantive", first_claim(json.dumps(["짧다", "정부는 최저임금 인상안을 발표했다"])),
          "정부는 최저임금 인상안을 발표했다")
    check("claim-none", first_claim("[]"), "")

    check("month", month_bucket("2026-07-21T09:00:00"), "2026-07")
    check("month-none", month_bucket(""), "(no date)")
    check("key-null", norm_key(None), "(null)")

    tally = Tally()
    tally.add("welfare", True)
    tally.add("welfare", False)
    tally.add("finance", False)
    check("tally", tally.rate_rows(), [("welfare", 2, 1), ("finance", 1, 0)])

    reservoir = Reservoir(2, seed=42)
    for i in range(100):
        reservoir.offer(i)
    again = Reservoir(2, seed=42)
    for i in range(100):
        again.offer(i)
    check("reservoir-cap", len(reservoir.items), 2)
    check("reservoir-deterministic", reservoir.items, again.items)

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK (19 checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OFFICIAL-COVERAGE-GAP probe (read-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check")
    parser.add_argument("--page", type=int, default=200, help="id-cursor page size")
    parser.add_argument("--sample", type=int, default=30, help="qualitative sample size (total)")
    parser.add_argument("--seed", type=int, default=42, help="reservoir sample seed")
    parser.add_argument("--gap-domains", type=int, default=3,
                        help="how many lowest-rate domains to sample from")
    parser.add_argument("--min-rows", type=int, default=100,
                        help="minimum policy-ish rows for a domain to be ranked")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    return run_live(args.page, args.sample, args.seed, args.gap_domains, args.min_rows)


if __name__ == "__main__":
    raise SystemExit(main())
