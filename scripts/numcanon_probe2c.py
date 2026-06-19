# NUMCANON-2C — CORRECTED read-only probe sizing compound-amount matcher impact.
#
# Fixes the NUMCANON-2 probe's two faults (diagnosed in NUMCANON-2B):
#   (1) FAITHFUL CLAIM: the matcher binds claims by ENUMERATE POSITION
#       (resolve_official_evidence:530-536 sets _claim_index on a throwaway copy
#       that is NEVER stored). The old probe looked for a _claim_index field in
#       the stored claim and fell back to the partial claim_text COLUMN, dropping
#       quantity/object where amounts live -> RECON-MISMATCH=3434. THIS probe
#       selects claim = normalized_claims[claim_index] by position and runs the
#       REAL _claim_text (all 10 fields). A mandatory RECON gate PROVES this:
#       RECON-MISMATCH must collapse from 3434 toward ~0 (modulo the [:8] cap),
#       else STOP — do not emit impact numbers on an unfaithful base.
#   (2) OBSERVABLE DENIAL: the old probe only saw official_matched_sentences (the
#       sentences the matcher already SELECTED), so a denied compound amount was
#       invisible. THIS probe reads the full stored official_body_text (capped
#       ~5000 at official_source_body.py:753) and compares claim x FULL BODY with
#       OLD vs NEW tokenizers, so a real denial ("claim 2억5000만원, body
#       250,000,000원, didn't match") is finally visible.
#
# READ-ONLY: SELECT only, no writes, no verdict change, no matcher edit. Reuses
# the REAL _claim_text / _numbers (faithfulness). canonical_amounts is the SAME
# throwaway prototype as numcanon_probe.py (already passed the NUMCANON-1 self-
# test), kept probe-local and NOT installed into the matcher. Safe in the Worker
# Shell. Does NOT run here.

import argparse
import collections
import os
import re
import sys

# Allow `python scripts/numcanon_probe2c.py` to import repo-root modules.
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
# THROWAWAY canonical-amount prototype (identical to numcanon_probe.py). Parses
# 조/억/만/천 + digits + commas + decimals -> integer KRW, with a bare-digit guard
# (a unit-less token that is not a long/comma-grouped 원 run does NOT qualify).
# Probe-local sizing only — NOT the installed fix (that is NUMCANON-3).
# ---------------------------------------------------------------------------
_UNIT = {"조": 10 ** 12, "억": 10 ** 8, "만": 10 ** 4, "천": 10 ** 3}
_AMOUNT_CHUNK_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*[조억만천]\s*)+\d*\s*원?|\d+\s*원")
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([조억만천]?)")


def _parse_amount(chunk: str):
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
            total += value
    if not found_unit:
        if "원" in chunk:
            digits = re.sub(r"\D", "", chunk)
            if len(digits) >= 4:
                return int(total)
        return None
    return int(round(total))


def canonical_amounts(text: str) -> set:
    if not text:
        return set()
    t = text.replace(",", "")
    amounts = set()
    for match in _AMOUNT_CHUNK_RE.finditer(t):
        value = _parse_amount(match.group(0))
        if value is not None:
            amounts.add(value)
    return amounts


# Per-matched-number contribution to official_evidence_score (15*0.45 + 12*0.40).
NUMERIC_UNIT_CONTRIBUTION = 15 * 0.45 + 12 * 0.40  # 11.55
STRONG_THRESHOLD, MEDIUM_THRESHOLD, WEAK_THRESHOLD = 75, 55, 30
# Matcher truncates matched_numbers to [:8] at official_evidence_resolution.py:315.
STORED_MATCHED_CAP = 8


def _tier(score: float) -> int:
    if score >= STRONG_THRESHOLD:
        return 3
    if score >= MEDIUM_THRESHOLD:
        return 2
    if score >= WEAK_THRESHOLD:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Offline self-test (no DB) — proves the prototype before any row is trusted.
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
        passed = got == expected
        ok = ok and passed
        print("  %-22r -> %-18r  ∩claim=%-13r  %s"
              % (text, sorted(got), sorted(got & claim_amts),
                 "OK" if passed else "FAIL expected %r" % sorted(expected)))
    spurious_gone = not (canonical_amounts("2주택자 3조원 대책") & claim_amts)
    wrong_excluded = not (canonical_amounts("5000만원") & claim_amts)
    spellings = all(250000000 in canonical_amounts(s)
                    for s in ("2억5000만원", "250,000,000원", "2.5억원", "25000만원"))
    print("  all spellings -> 250000000 :", spellings)
    print("  bare-'2' spurious removed  :", spurious_gone)
    print("  5000만원(50M) != 250M       :", wrong_excluded)
    good = ok and spurious_gone and wrong_excluded and spellings
    print("  SELF-TEST:", "PASS" if good else "FAIL")
    print()
    return 0 if good else 1


# ---------------------------------------------------------------------------
# DB scan (read-only).
# ---------------------------------------------------------------------------
SELECT_SQL = (
    "SELECT id, normalized_claims, source_candidates "
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
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def _faithful_claim_text(normalized_claims, claim_index):
    """Reproduce the matcher's claim binding: claim = normalized_claims[position]
    where position == the source's claim_index (resolve_official_evidence enumerates
    normalized_claims). Returns (claim_text, ok)."""
    try:
        idx = int(claim_index or 0)
    except Exception:
        idx = 0
    if idx < 0 or idx >= len(normalized_claims):
        return "", False
    claim = normalized_claims[idx]
    if not isinstance(claim, dict):
        return "", False
    return real_claim_text(claim), True


def _body_field(source: dict) -> str:
    """The full stored official body the matcher scored against (capped ~5000 at
    official_source_body.py:753). Mirrors _resolve_source's read order (:360)."""
    return sanitize_text(
        source.get("official_body_text")
        or source.get("body_text")
        or source.get("raw_text")
        or ""
    )


def scan(conn, limit, sample_n):
    recon = collections.Counter()
    body_present = collections.Counter()
    buckets = collections.Counter()
    boundary = collections.Counter()
    samples = {"RECON-MATCH": [], "SPURIOUS-NOW": [], "DENIED-NOW": [], "RECON-MISMATCH": []}
    rows_scanned = 0
    candidates_scanned = 0

    with conn.cursor() as cur:
        cur.execute(SELECT_SQL if limit is None else SELECT_SQL + " LIMIT %s",
                    () if limit is None else (limit,))
        fetched = cur.fetchall()

    for rid, normalized_raw, source_raw in fetched:
        rows_scanned += 1
        normalized_claims = _as_list(normalized_raw)
        for source in _as_list(source_raw):
            if not isinstance(source, dict):
                continue
            # Only resolved official candidates carry matched sentences / a body.
            matched_sentences = source.get("official_matched_sentences") or []
            body = _body_field(source)
            if not matched_sentences and not body:
                continue
            candidates_scanned += 1

            claim_text, claim_ok = _faithful_claim_text(
                normalized_claims, source.get("claim_index"))
            if not claim_ok:
                recon["CLAIM-UNRESOLVABLE"] += 1
                continue

            source_title = source.get("title") or source.get("official_detail_title") or ""

            # ---- (1) RECON GATE: prove faithful claim reproduces stored matched_numbers.
            for sent in matched_sentences:
                if not isinstance(sent, dict):
                    continue
                sentence = sent.get("sentence") or ""
                combined = sanitize_text("%s %s" % (source_title, sentence))
                stored = set(sent.get("matched_numbers") or [])
                recomputed = real_numbers(claim_text) & real_numbers(combined)
                if recomputed == stored:
                    recon["RECON-MATCH"] += 1
                    if len(samples["RECON-MATCH"]) < sample_n:
                        samples["RECON-MATCH"].append({
                            "id": rid, "claim_text": claim_text[:90],
                            "stored_matched": sorted(stored),
                            "recomputed_matched": sorted(recomputed),
                        })
                elif len(recomputed) > STORED_MATCHED_CAP and stored == set(sorted(recomputed)[:STORED_MATCHED_CAP]):
                    recon["CAP-DIFF"] += 1   # differ ONLY due to the stored [:8] truncation
                else:
                    recon["RECON-MISMATCH"] += 1
                    if len(samples["RECON-MISMATCH"]) < sample_n:
                        samples["RECON-MISMATCH"].append({
                            "id": rid, "claim_text": claim_text[:90],
                            "stored_matched": sorted(stored),
                            "recomputed_matched": sorted(recomputed),
                        })

            # ---- (2) BODY-LEVEL denial/spurious sizing (only if a body is stored).
            if not body:
                body_present["BODY-ABSENT"] += 1
                continue
            body_present["BODY-PRESENT"] += 1

            old_body = real_numbers(claim_text) & real_numbers(body)
            new_body = canonical_amounts(claim_text) & canonical_amounts(body)

            if not old_body and new_body:
                bucket = "DENIED-NOW"
            elif old_body and not new_body:
                bucket = "SPURIOUS-NOW"
            else:
                buckets["STABLE"] += 1
                continue
            buckets[bucket] += 1

            # Boundary estimate (uses the best-sentence stored scores). For DENIED the
            # denied amount may sit in a NON-selected sentence whose sub-scores are not
            # stored, so the tier impact there needs the live per-sentence rerun.
            stored_score = source.get("official_evidence_score")
            semantic = source.get("semantic_match_score")
            alignment = source.get("policy_alignment_score")
            cap_saturated = (semantic == 100 or alignment == 100)
            if bucket == "DENIED-NOW":
                boundary["DENIED-NOW:tier-needs-live-rerun"] += 1
            elif stored_score is not None:
                try:
                    f = float(stored_score)
                    f_removed = f - NUMERIC_UNIT_CONTRIBUTION * len(old_body)
                    decisive = (_tier(f) != _tier(f_removed))
                    if decisive and cap_saturated:
                        boundary["SPURIOUS-NOW:DECISIVE-uncertain(cap)"] += 1
                    elif decisive:
                        boundary["SPURIOUS-NOW:DECISIVE"] += 1
                    else:
                        boundary["SPURIOUS-NOW:cosmetic(reason-only)"] += 1
                except Exception:
                    boundary["SPURIOUS-NOW:unknown-score"] += 1
            else:
                boundary["SPURIOUS-NOW:unknown-score"] += 1

            if len(samples[bucket]) < sample_n:
                samples[bucket].append({
                    "id": rid,
                    "claim_amounts": sorted(canonical_amounts(claim_text)),
                    "old_body_tokens": sorted(old_body),
                    "new_body_amounts": sorted(new_body),
                    "stored_official_evidence_score": stored_score,
                    "cap_saturated": cap_saturated,
                })

    # ---- report ----
    total_recon = recon["RECON-MATCH"] + recon["RECON-MISMATCH"] + recon["CAP-DIFF"]
    print("=== NUMCANON-2C probe — FAITHFUL stored-row impact (READ-ONLY) ===")
    print("  rows scanned          :", rows_scanned)
    print("  candidates scanned    :", candidates_scanned)
    print()
    print("  ★ RECON GATE (faithfulness proof — MISMATCH must be ~0):")
    print("      RECON-MATCH         :", recon["RECON-MATCH"])
    print("      CAP-DIFF (only [:8]):", recon["CAP-DIFF"])
    print("      RECON-MISMATCH      :", recon["RECON-MISMATCH"],
          "(was 3434 in the buggy probe — must collapse toward 0)")
    print("      CLAIM-UNRESOLVABLE  :", recon["CLAIM-UNRESOLVABLE"])
    if total_recon:
        rate = 100.0 * recon["RECON-MISMATCH"] / total_recon
        print("      mismatch rate       : %.1f%% of %d sentence-comparisons" % (rate, total_recon))
        if rate > 5.0:
            print("      *** GATE FAILED: mismatch did NOT collapse — reconstruction still")
            print("          unfaithful. The impact numbers below are NOT trustworthy. ***")
        else:
            print("      GATE PASSED: faithful claim reconstruction confirmed.")
    print()
    print("  Body observability:")
    print("      BODY-PRESENT        :", body_present["BODY-PRESENT"], "(official_body_text stored → DENIED observable)")
    print("      BODY-ABSENT         :", body_present["BODY-ABSENT"])
    print()
    print("  Impact (claim × FULL body; only meaningful if GATE PASSED):")
    print("      STABLE              :", buckets["STABLE"])
    print("      SPURIOUS-NOW        :", buckets["SPURIOUS-NOW"], "(bare-digit/fragment match the fix REMOVES)")
    print("      DENIED-NOW          :", buckets["DENIED-NOW"], "(genuine compound amount in body the fix RESTORES)")
    print()
    print("  Boundary impact (does the numeric change cross a 75/55/30 tier?):")
    for key in sorted(boundary):
        print("      %-44s %d" % (key, boundary[key]))
    print()
    for bucket in ("RECON-MATCH", "SPURIOUS-NOW", "DENIED-NOW", "RECON-MISMATCH"):
        print("  --- sample %s (eyeball: found != relevant) ---" % bucket)
        if not samples[bucket]:
            print("      (none)")
        for s in samples[bucket]:
            print("      ", s)
        print()
    print("[Safety] READ-ONLY: SELECT-only; faithful claim via enumerate-position +")
    print("         real _claim_text/_numbers; canonical_amounts throwaway (NOT installed);")
    print("         no rows written/updated/deleted; no verdict field touched.")
    return rows_scanned


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="numcanon_probe2c",
        description="Corrected read-only probe: faithful claim reconstruction + "
                    "body-based denial check for compound-amount matcher impact.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Scan at most N rows.")
    parser.add_argument("--sample", type=int, default=8, help="Sample rows per bucket (default 8).")
    parser.add_argument("--selftest-only", action="store_true",
                        help="Run only the offline prototype self-test; no DB.")
    args = parser.parse_args(argv)

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
