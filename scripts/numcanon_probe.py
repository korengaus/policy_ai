# NUMCANON-2 — read-only probe sizing the compound-Korean-amount matcher impact.
#
# WHY: NUMCANON-1 proved official_evidence_resolution._numbers collapses compound
# amounts to bare digits ("2억5000만원" -> {'2','5000'}), so a correct official doc
# ("250,000,000원") is DENIED a numeric match, while a bare '2' from "2주택자"
# SPURIOUSLY matches (+15 semantic / +12 alignment, strongest reason). Before the
# fix (NUMCANON-3) we must SIZE how many STORED rows are affected and how many
# would actually move 신뢰도 (cross the 75 strong threshold → primary_document_match
# → Lane-B 70). This probe produces that count and the "answer key" to verify the
# fix against.
#
# READ-ONLY: SELECT only, no writes, no verdict change. Reuses the REAL matcher
# functions (_numbers / _canonicalize_numeric_text / _claim_text / sanitize_text)
# for the OLD side — faithfulness. The NEW side is a THROWAWAY local prototype
# (canonical_amounts) used ONLY to size impact; it is NOT installed into the
# matcher. Safe to run in the Render Worker Shell.
#
# FAITHFULNESS (STAGE-0 lesson — reached-NOW != THEN): the stored
# official_matched_sentences[].matched_numbers IS the actual value the matcher
# computed at analysis time (ground truth for OLD). The probe ALSO recomputes OLD
# from the stored sentence + reconstructed claim text using the real _numbers, and
# GATES on agreement: a row whose recompute != stored value is flagged
# RECON-MISMATCH and EXCLUDED from the SPURIOUS/DENIED verdicts (counted, not
# guessed). Boundary (75-crossing) claims are ESTIMATES from stored sub-scores and
# are flagged uncertain when a sub-score is cap-saturated (==100).
#
# WHAT IT DOES NOT: no INSERT/UPDATE/DELETE, no schema change, no verdict write, no
# matcher edit, no normalizer install, no secret printed.

import argparse
import collections
import os
import re
import sys

# Allow `python scripts/numcanon_probe.py` to import repo-root modules (the
# matcher lives in the repo root, not in scripts/). Mirrors the test convention.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

# Reuse the REAL matcher functions — faithfulness, never re-implement the OLD side.
from official_evidence_resolution import (
    _numbers as real_numbers,
    _claim_text as real_claim_text,
)
from text_utils import sanitize_text

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# THROWAWAY prototype of the NEW canonical-amount matcher (sizing only).
# NOT the real fix (NUMCANON-3 builds the installed helper). Parses 조/억/만/천 +
# digits + commas + decimals into integer KRW, with a bare-digit guard so a token
# with no monetary unit (and not a long/comma-grouped 원 run) does NOT qualify.
# ---------------------------------------------------------------------------
_UNIT = {"조": 10 ** 12, "억": 10 ** 8, "만": 10 ** 4, "천": 10 ** 3}
# A monetary chunk: one-or-more (digits[.dec] + unit) groups, optional trailing
# digits, optional 원  — OR  a bare digit run immediately followed by 원.
_AMOUNT_CHUNK_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*[조억만천]\s*)+\d*\s*원?|\d+\s*원")
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([조억만천]?)")


def _parse_amount(chunk: str):
    """Parse one monetary chunk to an integer KRW, or None if it doesn't qualify
    as a real amount (bare-digit guard). Compound parts are summed
    (2억 + 5000만 = 250,000,000)."""
    total = 0.0
    found_unit = False
    for part in _PART_RE.finditer(chunk):
        num, unit = part.group(1), part.group(2)
        if not num:
            continue
        value = float(num)
        if unit:
            total += value * _UNIT[unit]
            found_unit = True
        else:
            total += value  # trailing bare 원 amount or sub-unit remainder
    if not found_unit:
        # bare-digit guard: a unit-less chunk qualifies ONLY as an explicit 원
        # amount with a long digit run (>=4 digits) — so "2" / "2주택" never count.
        if "원" in chunk:
            digits = re.sub(r"\D", "", chunk)
            if len(digits) >= 4:
                return int(total)
        return None
    return int(round(total))


def canonical_amounts(text: str) -> set:
    """Set of canonical integer-KRW amounts in text. All spellings of one amount
    collapse to the same integer, and bare digits / counts / years / percents do
    NOT qualify. THROWAWAY sizing prototype — not the installed fix."""
    if not text:
        return set()
    t = text.replace(",", "")
    amounts = set()
    for match in _AMOUNT_CHUNK_RE.finditer(t):
        value = _parse_amount(match.group(0))
        if value is not None:
            amounts.add(value)
    return amounts


# Per-matched-number contribution to official_evidence_score:
#   semantic_match_score += 15  (weight 0.45);  policy_alignment_score += 12 (0.40)
# => 15*0.45 + 12*0.40 = 11.55 per matched number (linear, ignoring the min(100)
# caps — hence the cap-saturation caveat below).
NUMERIC_UNIT_CONTRIBUTION = 15 * 0.45 + 12 * 0.40  # 11.55
STRONG_THRESHOLD = 75   # _classify_official_evidence :327
MEDIUM_THRESHOLD = 55   # :329
WEAK_THRESHOLD = 30     # :331
_TIER_CUTS = (WEAK_THRESHOLD, MEDIUM_THRESHOLD, STRONG_THRESHOLD)


def _tier(score: float) -> int:
    """Which classification tier a score falls in (0=none,1=weak,2=medium,3=strong)."""
    if score >= STRONG_THRESHOLD:
        return 3
    if score >= MEDIUM_THRESHOLD:
        return 2
    if score >= WEAK_THRESHOLD:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Self-test (offline, no DB) — proves the prototype before trusting row verdicts.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    print("=== canonical_amounts prototype self-test (offline) ===")
    claim = "2억5000만원"
    claim_amts = canonical_amounts(claim)
    cases = [
        ("2억5000만원", {250000000}),
        ("250,000,000원", {250000000}),
        ("2.5억원", {250000000}),
        ("25000만원", {250000000}),
        ("2주택자 3조원 대책", {3000000000000}),
        ("5000만원", {50000000}),
    ]
    ok = True
    print("  claim %r -> %r" % (claim, sorted(claim_amts)))
    for text, expected in cases:
        got = canonical_amounts(text)
        inter = sorted(got & claim_amts)
        passed = got == expected
        ok = ok and passed
        print("  %-22r -> %-18r  ∩claim=%-13r  %s"
              % (text, sorted(got), inter, "OK" if passed else "FAIL expected %r" % sorted(expected)))
    # Key assertions for the sizing premise:
    spurious_gone = not (canonical_amounts("2주택자 3조원 대책") & claim_amts)
    wrong_amount_excluded = not (canonical_amounts("5000만원") & claim_amts)
    spellings_match = all(
        250000000 in canonical_amounts(s)
        for s in ("2억5000만원", "250,000,000원", "2.5억원", "25000만원")
    )
    print("  all spellings -> 250000000 :", spellings_match)
    print("  bare-'2' spurious removed  :", spurious_gone)
    print("  5000만원(50M) != 250M       :", wrong_amount_excluded)
    print("  SELF-TEST:", "PASS" if (ok and spurious_gone and wrong_amount_excluded and spellings_match) else "FAIL")
    print()
    return 0 if (ok and spurious_gone and wrong_amount_excluded and spellings_match) else 1


# ---------------------------------------------------------------------------
# DB scan (read-only).
# ---------------------------------------------------------------------------
SELECT_SQL = (
    "SELECT id, claim_text, claims, normalized_claims, source_candidates "
    "FROM analysis_results "
    "WHERE source_candidates IS NOT NULL "
    "ORDER BY id"
)


def _normalize_url(raw_url: str) -> str:
    return (raw_url.replace("postgresql+psycopg://", "postgresql://")
                   .replace("postgresql+psycopg2://", "postgresql://"))


def _as_list(value):
    import json
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value if isinstance(value, list) else [value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def _claim_by_index(claims_list, normalized_list, claim_index, fallback_claim_text):
    """Reconstruct the matcher's claim text for a given claim_index, preferring
    normalized_claims then claims, falling back to the row's claim_text column.
    Faithfulness is then GATED downstream by recompute-vs-stored agreement."""
    for source_list in (normalized_list, claims_list):
        for claim in source_list or []:
            if not isinstance(claim, dict):
                continue
            idx = claim.get("_claim_index", claim.get("claim_index"))
            if idx is not None and int(idx) == int(claim_index):
                return real_claim_text(claim)
    # fall back to the single claim_text column (less faithful; gate will catch)
    return sanitize_text(fallback_claim_text or "")


def scan(conn, limit, sample_n):
    counts = collections.Counter()
    boundary = collections.Counter()
    samples = {"SPURIOUS-NOW": [], "DENIED-NOW": [], "RECON-MISMATCH": []}
    rows_scanned = 0

    with conn.cursor() as cur:
        cur.execute(SELECT_SQL if limit is None else SELECT_SQL + " LIMIT %s",
                    () if limit is None else (limit,))
        fetched = cur.fetchall()

    for rid, claim_text_col, claims_raw, normalized_raw, source_raw in fetched:
        rows_scanned += 1
        claims_list = _as_list(claims_raw)
        normalized_list = _as_list(normalized_raw)
        for source in _as_list(source_raw):
            if not isinstance(source, dict):
                continue
            matched_sentences = source.get("official_matched_sentences") or []
            if not matched_sentences:
                continue  # only resolved official sources carry these
            best = matched_sentences[0]  # stored sorted by score desc
            sentence = best.get("sentence") or ""
            source_title = source.get("title") or source.get("official_detail_title") or ""
            combined_text = sanitize_text("%s %s" % (source_title, sentence))

            claim_index = source.get("claim_index") or 0
            claim_text = _claim_by_index(claims_list, normalized_list, claim_index, claim_text_col)

            # OLD ground truth = stored matched_numbers; recompute for faithfulness gate.
            old_stored = set(best.get("matched_numbers") or [])
            old_recomputed = real_numbers(claim_text) & real_numbers(combined_text)
            faithful = (old_recomputed == old_stored)

            new_amounts = canonical_amounts(claim_text) & canonical_amounts(combined_text)

            if not faithful:
                counts["RECON-MISMATCH"] += 1
                if len(samples["RECON-MISMATCH"]) < sample_n:
                    samples["RECON-MISMATCH"].append({
                        "id": rid, "claim_amounts": sorted(canonical_amounts(claim_text)),
                        "old_stored": sorted(old_stored), "old_recomputed": sorted(old_recomputed),
                    })
                continue  # do NOT issue a SPURIOUS/DENIED verdict on an unfaithful row

            old_nonempty = bool(old_stored)
            new_nonempty = bool(new_amounts)

            if old_nonempty and not new_nonempty:
                bucket = "SPURIOUS-NOW"
            elif not old_nonempty and new_nonempty:
                bucket = "DENIED-NOW"
            else:
                counts["STABLE"] += 1
                continue
            counts[bucket] += 1

            # Boundary estimate: does the numeric signal move the tier across 75/55/30?
            stored_score = source.get("official_evidence_score")
            semantic = source.get("semantic_match_score")
            alignment = source.get("policy_alignment_score")
            cap_saturated = (semantic == 100 or alignment == 100)
            decisive = None
            if stored_score is not None:
                try:
                    f = float(stored_score)
                    if bucket == "SPURIOUS-NOW":
                        f_other = f - NUMERIC_UNIT_CONTRIBUTION * len(old_stored)
                    else:  # DENIED-NOW: fix ADDS the numeric signal
                        f_other = f + NUMERIC_UNIT_CONTRIBUTION * len(new_amounts)
                    decisive = (_tier(f) != _tier(f_other))
                except Exception:
                    decisive = None

            if decisive and cap_saturated:
                boundary["%s:DECISIVE-uncertain(cap)" % bucket] += 1
            elif decisive:
                boundary["%s:DECISIVE" % bucket] += 1
            elif decisive is False:
                boundary["%s:cosmetic(reason-only)" % bucket] += 1
            else:
                boundary["%s:unknown-score" % bucket] += 1

            if len(samples[bucket]) < sample_n:
                samples[bucket].append({
                    "id": rid,
                    "claim_amounts": sorted(canonical_amounts(claim_text)),
                    "old_tokens": sorted(old_stored),
                    "new_amounts": sorted(new_amounts),
                    "stored_score": stored_score,
                    "decisive": decisive,
                    "cap_saturated": cap_saturated,
                })

    # ---- report ----
    print("=== NUMCANON-2 probe — stored-row impact (READ-ONLY) ===")
    print("  rows scanned        :", rows_scanned)
    print("  STABLE              :", counts["STABLE"])
    print("  SPURIOUS-NOW        :", counts["SPURIOUS-NOW"], "(current bare-digit/fragment match the fix REMOVES)")
    print("  DENIED-NOW          :", counts["DENIED-NOW"], "(genuine compound amount the fix RESTORES)")
    print("  RECON-MISMATCH      :", counts["RECON-MISMATCH"], "(recompute != stored — EXCLUDED from verdicts, faithfulness gate)")
    print()
    print("  Boundary impact (does the numeric change cross a 75/55/30 tier?):")
    for key in sorted(boundary):
        print("      %-40s %d" % (key, boundary[key]))
    print("    DECISIVE rows = real 신뢰도 / primary_document / Lane-B-70 move.")
    print("    cosmetic rows = only the evidence reason string changes.")
    print()
    for bucket in ("SPURIOUS-NOW", "DENIED-NOW", "RECON-MISMATCH"):
        print("  --- sample %s (eyeball the amounts; found != relevant) ---" % bucket)
        if not samples[bucket]:
            print("      (none)")
        for s in samples[bucket]:
            print("      ", s)
        print()
    print("[Safety] READ-ONLY: SELECT-only; reused real _numbers/_claim_text for OLD;")
    print("         canonical_amounts is a throwaway sizing prototype (NOT installed);")
    print("         no rows written/updated/deleted; no verdict field touched.")
    return rows_scanned


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="numcanon_probe",
        description="Read-only probe sizing compound-amount matcher impact on stored rows.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Scan at most N rows.")
    parser.add_argument("--sample", type=int, default=8, help="Sample rows per bucket (default 8).")
    parser.add_argument("--selftest-only", action="store_true",
                        help="Run only the offline prototype self-test; no DB.")
    args = parser.parse_args(argv)

    # Offline self-test ALWAYS runs first (no DB needed) — proves the prototype.
    selftest_rc = run_selftest()
    if args.selftest_only:
        return selftest_rc

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell "
              "(or locally with $env:DATABASE_URL pointed at the external DB). "
              "Self-test above ran offline.")
        return 0

    url = _normalize_url(raw_url)
    with psycopg.connect(url) as conn:
        scan(conn, args.limit, args.sample)
    return 0


if __name__ == "__main__":
    sys.exit(main())
