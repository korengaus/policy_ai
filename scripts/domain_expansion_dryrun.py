# DOMAIN-EXPANSION-DRYRUN — READ-ONLY simulation of candidate domain seeds.
#
# WHY
# ---
# scripts/unclassified_peek.py surfaced a crude token table over the ~1,674
# 기타-미분류 rows: 과학기술/AI is the loudest signal, then 청년, then 통상/수출.
# A token table is NOT a hit rate. The education expansion already taught us
# this the expensive way — a 20-row eyeball read 3% where the true
# keyword-targeted rate was 98%. So before any domain label or collection seed
# is added, this probe MEASURES what each candidate keyword set would actually
# catch.
#
# This is a SIMULATION ONLY. No row is classified, no label is written, and
# domain_classifier.py is neither imported nor touched — the keyword sets below
# are local candidates, deliberately kept separate from production logic.
#
# WHAT IT ANSWERS
# ---------------
#   1. How many unclassified rows would each candidate domain catch?
#   2. Which keywords drive each match (a set that lives or dies on ONE token is
#      fragile)?
#   3. Do the candidates overlap each other — are they cleanly separable?
#   4. BLEED: would a new seed also match rows ALREADY correctly classified
#      elsewhere, cannibalizing a domain that is not broken?
#   5. Do the matches look right to a human (samples)?
#
# SAFETY
# ------
# Every statement is a SELECT. No INSERT / UPDATE / DELETE / DDL / commit, no
# network call, no LLM call, no URL fetch. Tables touched: `analysis_results`
# only (plus `information_schema.columns` for claim-column detection).
#
# FIELD-LOCATION NOTES (carried over from the prior probes)
# --------------------------------------------------------
#   * The live PG `analysis_results` table does NOT reliably carry a top-level
#     `claim_text` column, so claim columns are DETECTED via information_schema
#     and only the present ones are selected (a bare SELECT would
#     UndefinedColumn-fail).
#   * The unclassified label is the HYPHENATED compound "기타-미분류".
#
# MATCHING CAVEAT (printed in the output too)
# -------------------------------------------
# Korean keywords use plain substring containment — correct for an agglutinative
# language (반도체 matches 반도체를/반도체의). ASCII keywords (AI, R&D, FTA,
# CPTPP, UAM, 6G) do NOT: a bare "AI" substring also fires inside MAIN, CHAIN,
# TAIWAN, AIIB. Those are matched with an ASCII word boundary instead. Without
# that guard the 과학기술 hit rate would be inflated by headline capitalization.

import collections
import json
import os
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

import psycopg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# domain_classifier.FALLBACK_LABEL — the hyphenated compound.
UNCLASSIFIED_LABEL = "기타-미분류"

CLAIM_COLUMNS = ("claim_text", "claims", "evidence_summary", "title")

DETECT_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'analysis_results' AND column_name = ANY(%s)"
)

# CANDIDATE keyword sets — local simulation only, NOT wired into any classifier.
CANDIDATE_DOMAINS = {
    "과학기술": (
        "인공지능", "AI", "반도체", "데이터센터", "디지털", "UAM", "도심항공",
        "우주", "바이오", "양자", "배터리", "이차전지", "소재부품", "R&D",
        "연구개발", "과학기술", "정보통신", "6G", "클라우드",
    ),
    "청년": (
        "청년", "청년정책", "청년일자리", "청년창업", "청년주거", "청년도약",
        "대학생", "취업준비",
    ),
    "통상": (
        "수출", "통상", "관세", "FTA", "CPTPP", "무역", "수입", "대외경제",
        "교역", "관세청",
    ),
}

TOP_KEYWORDS = 8
SAMPLE_ROWS = 8
SAMPLE_CHARS = 80
TOP_BLEED_DOMAINS = 8

_ASCII_KEYWORD = re.compile(r"^[A-Za-z0-9&]+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed."""
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


def _pct(n, total):
    """n/total as a percent string; '—' when total is 0."""
    if not total:
        return "—"
    return "%.1f%%" % (100.0 * n / total)


def resolve_claim(row, cols):
    """The rendered 핵심 주장, mirroring buildReviewerSafeClaim's chain:
    claim_text -> claims[0] -> summary -> title, restricted to columns that
    physically exist. Returns (text, source_column); ('', '') when none."""
    if "claim_text" in cols:
        text = (row.get("claim_text") or "").strip()
        if text:
            return text, "claim_text"
    if "claims" in cols and row.get("claims"):
        parsed = _j(row.get("claims"))
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    return item.strip(), "claims"
    for fallback in ("evidence_summary", "title"):
        if fallback in cols:
            text = (row.get(fallback) or "").strip()
            if text:
                return text, fallback
    return "", ""


def _build_matchers():
    """Precompile one matcher per keyword. Korean keywords use plain substring
    containment (correct for an agglutinative language). ASCII keywords get an
    ASCII word boundary so "AI" does not fire inside MAIN / CHAIN / TAIWAN —
    see the MATCHING CAVEAT at the top."""
    matchers = {}
    for domain, keywords in CANDIDATE_DOMAINS.items():
        compiled = []
        for keyword in keywords:
            if _ASCII_KEYWORD.match(keyword):
                pattern = re.compile(
                    r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(keyword)
                )
                compiled.append((keyword, pattern.search, True))
            else:
                compiled.append((keyword, None, False))
        matchers[domain] = compiled
    return matchers


MATCHERS = _build_matchers()


def match_keywords(text, domain):
    """Keywords of `domain` present in `text`, as a list (may be empty)."""
    hits = []
    for keyword, search, is_ascii in MATCHERS[domain]:
        if search(text) if is_ascii else (keyword in text):
            hits.append(keyword)
    return hits


def _truncate(text):
    flat = " ".join((text or "").split())
    return flat[:SAMPLE_CHARS] + "…" if len(flat) > SAMPLE_CHARS else flat


def _section(title):
    print("\n" + title)
    print("-" * len(title))


# ---------------------------------------------------------------------------
# Sections — each guarded so one bad row cannot abort the run.
# ---------------------------------------------------------------------------

def section_totals(unclassified, all_rows):
    _section("1. SCOPE")
    print("  Unclassified rows (domain = '%s'): %d" % (UNCLASSIFIED_LABEL, len(unclassified)))
    print("  All rows in analysis_results:        %d" % len(all_rows))
    print("  (data_health_probe.py measured ~1,674 unclassified — drift just means"
          " the corpus grew.)")
    print("  SIMULATION ONLY — no row is classified and nothing is written.")


def section_hit_rates(matched, unclassified_total):
    _section("2. CANDIDATE HIT RATES (on unclassified rows)")
    try:
        for domain in CANDIDATE_DOMAINS:
            rows = matched[domain]
            print("\n  %s — %d rows (%s of unclassified)"
                  % (domain, len(rows), _pct(len(rows), unclassified_total)))
            counts = collections.Counter()
            for _rid, _text, hits in rows:
                counts.update(hits)
            if not counts:
                print("    (no keyword fired)")
                continue
            print("    Top %d keywords by hit count:" % TOP_KEYWORDS)
            for keyword, n in counts.most_common(TOP_KEYWORDS):
                print("      %-14s %5d  (%s of this domain's matches)"
                      % (keyword, n, _pct(n, len(rows))))
            top_keyword, top_n = counts.most_common(1)[0]
            if rows and top_n >= 0.8 * len(rows):
                print("    FRAGILE: '%s' alone drives %s of matches — this set is"
                      % (top_keyword, _pct(top_n, len(rows))))
                print("    effectively a one-keyword domain. Tighten or broaden"
                      " before adopting.")
    except Exception as exc:  # noqa: BLE001 — a section must never abort the run
        print("  section unavailable: %s" % exc)


def section_overlap(matched, unclassified_total):
    _section("3. OVERLAP (are the candidate domains cleanly separable?)")
    try:
        by_row = collections.defaultdict(set)
        for domain, rows in matched.items():
            for rid, _text, _hits in rows:
                by_row[rid].add(domain)
        multi = {rid: doms for rid, doms in by_row.items() if len(doms) >= 2}
        print("  Rows matching 2+ candidate domains: %d (%s of unclassified, %s of"
              % (len(multi), _pct(len(multi), unclassified_total),
                 _pct(len(multi), len(by_row))))
        print("  all candidate-matched rows)")
        pairs = collections.Counter()
        for doms in multi.values():
            ordered = sorted(doms)
            for i in range(len(ordered)):
                for j in range(i + 1, len(ordered)):
                    pairs[(ordered[i], ordered[j])] += 1
        if pairs:
            print("  Overlapping pairs, most frequent first:")
            for (a, b), n in pairs.most_common():
                print("    %s + %s: %d rows" % (a, b, n))
        else:
            print("  No pair overlaps — the candidate sets are disjoint on this data.")
        print("  (High overlap = the two domains are not separable by keywords"
              " alone.)")
    except Exception as exc:  # noqa: BLE001
        print("  section unavailable: %s" % exc)


def section_bleed(all_rows, cols):
    _section("4. BLEED CHECK — would a new seed cannibalize an EXISTING domain?")
    try:
        classified = []
        for row in all_rows:
            domain = row.get("domain")
            if domain is None or not str(domain).strip():
                continue
            domain = str(domain).strip()
            if domain == UNCLASSIFIED_LABEL:
                continue
            classified.append((row, domain))
        print("  Rows already in a real domain (excludes NULL and '%s'): %d"
              % (UNCLASSIFIED_LABEL, len(classified)))
        if not classified:
            print("  section unavailable: no classified rows to check against")
            return
        for candidate in CANDIDATE_DOMAINS:
            affected = collections.Counter()
            for row, domain in classified:
                text, _source = resolve_claim(row, cols)
                if text and match_keywords(text, candidate):
                    affected[domain] += 1
            total_hit = sum(affected.values())
            print("\n  %s would ALSO match %d already-classified rows (%s of"
                  % (candidate, total_hit, _pct(total_hit, len(classified))))
            print("  all classified rows)")
            if not affected:
                print("    No bleed — this seed touches nothing already classified.")
                continue
            print("    Most affected existing domains:")
            for domain, n in affected.most_common(TOP_BLEED_DOMAINS):
                print("      %-20s %5d" % (domain, n))
        print("\n  READ THIS AS: bleed is NOT automatically bad — a 통상 keyword")
        print("  firing on a 경제 row may be a genuine dual-topic article. It IS a")
        print("  warning when one existing domain loses a large share to a new seed.")
        print("  This probe cannot tell those apart; it only shows you where to look.")
    except Exception as exc:  # noqa: BLE001
        print("  section unavailable: %s" % exc)


def section_samples(matched):
    _section("5. SAMPLE MATCHES (eyeball precision, %d per candidate)" % SAMPLE_ROWS)
    try:
        for domain in CANDIDATE_DOMAINS:
            rows = matched[domain]
            print("\n  %s (%d matched):" % (domain, len(rows)))
            if not rows:
                print("    (no matches)")
                continue
            # Evenly spaced across the id-ordered matches, not just the oldest 8.
            step = max(1, len(rows) // SAMPLE_ROWS)
            for rid, text, hits in rows[::step][:SAMPLE_ROWS]:
                print("    id=%-7s [%s]" % (rid, ", ".join(hits[:4])))
                print("      %s" % _truncate(text))
    except Exception as exc:  # noqa: BLE001
        print("  section unavailable: %s" % exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    print("=" * 72)
    print("DOMAIN-EXPANSION-DRYRUN — SIMULATION of candidate domain seeds")
    print("READ-ONLY, SELECT-only. Nothing is classified; nothing is written.")
    print("=" * 72)

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(DETECT_COLUMNS_SQL, (list(CLAIM_COLUMNS),))
            present = {r[0] for r in cur.fetchall()}
        cols = [c for c in CLAIM_COLUMNS if c in present]
        print("\nClaim columns physically present: %s"
              % (", ".join(cols) if cols else "(none)"))
        if not cols:
            print("No claim-bearing column exists — cannot simulate. Checked: %s"
                  % (CLAIM_COLUMNS,))
            return 1

        # ONE full read; the bleed check needs classified rows too, so pulling
        # everything once beats two scans.
        select_cols = ["id", "domain"]
        select_cols += [c for c in cols if c not in select_cols]
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM analysis_results ORDER BY id"
                        % ", ".join(select_cols))
            all_rows = [dict(zip(select_cols, r)) for r in cur.fetchall()]

    unclassified = [r for r in all_rows
                    if str(r.get("domain") or "").strip() == UNCLASSIFIED_LABEL]

    section_totals(unclassified, all_rows)

    # Match once, reuse across sections 2/3/5.
    matched = {domain: [] for domain in CANDIDATE_DOMAINS}
    try:
        for row in unclassified:
            text, _source = resolve_claim(row, cols)
            if not text:
                continue
            for domain in CANDIDATE_DOMAINS:
                hits = match_keywords(text, domain)
                if hits:
                    matched[domain].append((row.get("id"), text, hits))
    except Exception as exc:  # noqa: BLE001
        print("\n  matching pass failed: %s" % exc)

    section_hit_rates(matched, len(unclassified))
    section_overlap(matched, len(unclassified))
    section_bleed(all_rows, cols)
    section_samples(matched)

    print("\n" + "=" * 72)
    print("SIMULATION COMPLETE — READ-ONLY; no row written, updated, or deleted.")
    print("domain_classifier.py was NOT imported or modified. The keyword sets")
    print("above are local candidates only. Decide from hit rate + overlap + bleed")
    print("+ samples together — a high hit rate with heavy bleed is a bad seed.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
