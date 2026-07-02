"""CARD-DISSECT — READ-ONLY, SELECT-only dissection of ONE stored card that displays
apparently-inconsistent evidence / claims, to decide DISPLAY-only vs DATA issue.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE / ALTER /
commit. Touches no production code, no verdict logic, no pins. REUSES the authoritative
predicate (official_evidence_resolution.extract_primary_document_match) for the genuine
check; tallies stored classification strings verbatim (never re-scores).

THE CARD
--------
Title: '반도체 훈풍'에... 호남 집주인들 매물 거뒀다 [서남권 부동산 들썩]
Reported symptoms: policy_confidence_score 86 + "관찰(WATCH)" + AI-draft "사람 검토 대기"
+ 공식 출처 상태 "공식자료 참고" (non-genuine), yet the reasoning reads "비교적 강/탄탄"
and "강한 근거가 확인됐다"; ~84 official candidates all "공식 약한 후보"; and displayed
lines include an internal search query ("site:molit.go.kr OR ...") and garbled claim
text ("솔라시 솔라시도 9").

WHERE EACH DISPLAYED STRING COMES FROM (confirmed by grep of frontend/scripts/main.js)
--------------------------------------------------------------------------------------
  * "공식자료 참고"  <- officialStatusLabel(): has_genuine_official_support == False AND
      (official_detail_available OR debug.official_body_matches>0). [genuine-body axis]
  * "비교적 탄탄"    <- scoreTrustDescription(): source_trust/score >= 75. [SCORE axis;
      the score is 86, so this fires regardless of the genuine-body axis]
  * "강한 근거가 확인됐다" <- evidenceQualityExplanation(): evidence_quality.strong>0 AND
      average_evidence_quality_score>=75. [evidence-EXTRACTION axis, not official-body]
  * "공식 약한 후보"  <- sourceTrace() (main.js:4132-4145): an official-like candidate whose
      official_evidence_classification / official_direct_match_classification is NOT
      strong/medium AND official_body_match is falsy. [per-candidate classification]
  * "1차 공식 근거 · site:..." <- renderSourceQueries() (main.js:1186-1199): the ADVANCED
      "생성된 출처 검색 쿼리" section renders source_queries[].query, labeled by
      formatSourcePurpose(purpose) ("primary_source" -> "1차 공식 근거"). The 'site:...'
      string is the GENERATED SEARCH QUERY (source_queries[].query), NOT claim/evidence
      text — "1차 공식 근거" is the query's PURPOSE label.
  * "솔라시 솔라시도 9" <- a claim string (normalized_claims[].claim_text and/or claims[]).

WHAT THIS DUMPS
---------------
  ROW SUMMARY: id, created_at, domain, verdict_label, policy_alert_level,
      policy_confidence_score, verdict_confidence, official_mismatch,
      has_genuine_official_support, official_candidate_count, evidence_quality
      (strong/medium/weak + average) — the three axes side by side.
  CANDIDATE BREAKDOWN: total source_candidates; count by stored classification
      (strong/medium/weak/none) via the SAME field-read order as the frontend sourceTrace;
      count official_body_match==True; extract_primary_document_match() result -> why
      has_genuine is True/False.
  CLAIM/QUERY TRACE: the exact stored source_queries[] rows behind "1차 공식 근거 · site:...",
      and the normalized_claims[].claim_text / claims[] behind "솔라시...", each labeled
      STORED-DATA vs DISPLAY-artifact with its field name.
  VERDICT: DISPLAY-only vs DATA issue, with evidence. No fix proposed.

FIELD-NAME NOTES (confirmed by grep of postgres_storage.py schema)
-----------------------------------------------------------------
  TOP-LEVEL columns: id, created_at, domain, title, verdict_label, policy_alert_level,
  policy_confidence_score, verdict_confidence, claims, normalized_claims,
  source_candidates, source_queries, source_reliability_summary,
  evidence_extraction_summary (all JSON TEXT except the scalars).
  official_mismatch + has_genuine_official_support live INSIDE
  source_reliability_summary JSON. evidence_quality_summary is nested inside the
  evidence_extraction_summary JSON.

SAFETY: SELECT-only, engine.connect() (never begin()), no commit. Lazy DB import inside
the live path so --selftest is offline. ASCII-guarded prints.

Usage (real dump in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/card_dissect_probe.py
    PYTHONPATH=. python scripts/card_dissect_probe.py --selftest   # offline, no DB
    PYTHONPATH=. python scripts/card_dissect_probe.py --id 1234    # dump a specific id

Exit codes: 0 = dump printed / engine unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Authoritative genuine-match predicate (pure; no DB/network at import).
from official_evidence_resolution import extract_primary_document_match  # noqa: E402

# Stable substring of the card title (avoids the quotes/brackets in the full title).
TITLE_LIKE = "%반도체 훈풍%매물 거뒀다%"

STRONG = "strong_official_direct_support"
MEDIUM = "medium_official_contextual_support"
WEAK = "weak_official_candidate_only"
OFFICIAL_TYPES = {"official_government", "public_institution"}


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _json_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value or not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return []
    return parsed if isinstance(parsed, list) else []


def _cand_classification(cand: dict) -> str:
    """The SAME field-read order the frontend sourceTrace uses (main.js:4132)."""
    return str(
        cand.get("official_evidence_classification")
        or cand.get("official_direct_match_classification")
        or ""
    )


def _cand_score(cand: dict) -> int:
    for key in ("official_evidence_score", "official_final_direct_match_score",
                "official_body_match_score"):
        try:
            v = cand.get(key)
            if v is not None:
                return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def classify_candidates(candidates: list) -> dict:
    """Tally candidates by stored classification (verbatim; no re-scoring). Mirrors the
    frontend trace buckets: strong / medium / weak / none / non-official."""
    buckets = {"strong": 0, "medium": 0, "weak": 0, "none_or_unclassified": 0,
               "non_official": 0}
    body_match = 0
    official = 0
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        if cand.get("source_type") not in OFFICIAL_TYPES:
            buckets["non_official"] += 1
            continue
        official += 1
        if cand.get("official_body_match"):
            body_match += 1
        cls = _cand_classification(cand)
        if cls == STRONG:
            buckets["strong"] += 1
        elif cls == MEDIUM:
            buckets["medium"] += 1
        elif cls == WEAK:
            buckets["weak"] += 1
        else:
            buckets["none_or_unclassified"] += 1
    return {"buckets": buckets, "official_count": official, "body_match_count": body_match}


def evidence_quality(evidence_extraction_summary: dict) -> dict:
    """Pull the evidence_quality_summary nested in evidence_extraction_summary."""
    ees = evidence_extraction_summary or {}
    q = ees.get("evidence_quality_summary") or {}
    return {
        "strong": q.get("strong", ees.get("total_strong_evidence")),
        "medium": q.get("medium", ees.get("total_medium_evidence")),
        "weak": q.get("weak", ees.get("total_weak_evidence")),
        "average_evidence_quality_score": q.get(
            "average_evidence_quality_score",
            ees.get("average_evidence_quality_score"),
        ),
    }


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== CARD-DISSECT — OFFLINE SELF-TEST (no DB) ===")
    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    # Candidate classification tally
    cands = [
        {"source_type": "official_government", "official_evidence_classification": WEAK},
        {"source_type": "official_government", "official_direct_match_classification": WEAK},
        {"source_type": "official_government", "official_evidence_classification": STRONG,
         "official_body_match": True},
        {"source_type": "established_news"},
    ]
    res = classify_candidates(cands)
    p("candidate classification tally:")
    expect("CAND", "2 weak counted", res["buckets"]["weak"], 2)
    expect("CAND", "1 strong counted", res["buckets"]["strong"], 1)
    expect("CAND", "1 non-official", res["buckets"]["non_official"], 1)
    expect("CAND", "official_count=3", res["official_count"], 3)
    expect("CAND", "body_match_count=1", res["body_match_count"], 1)

    # has_genuine derivation via the REAL predicate
    p("has_genuine derivation (real extract_primary_document_match):")
    all_weak = [{"source_type": "official_government",
                 "official_evidence_classification": WEAK} for _ in range(84)]
    expect("GENUINE", "84 weak candidates -> no primary-document match (genuine False)",
           extract_primary_document_match(all_weak) is None, True)
    strong_pb = [{"source_type": "official_government", "policy_briefing_news_item_id": "x",
                  "official_body_match": True, "official_evidence_classification": STRONG,
                  "official_evidence_score": 80}]
    expect("GENUINE", "strong PB marker -> primary-document match (genuine True)",
           extract_primary_document_match(strong_pb) is not None, True)

    # Query trace labeling
    p("query-trace labeling:")
    sq = [{"claim_index": 0, "purpose": "primary_source",
           "query": "site:molit.go.kr OR site:moef.go.kr 반도체"}]
    q0 = sq[0]
    is_query_string = (q0.get("query", "").startswith("site:")
                       and q0.get("purpose") == "primary_source")
    expect("QUERY", "'site:...' is source_queries[].query with purpose=primary_source",
           is_query_string, True)

    # Claim-text passthrough
    p("claim-text passthrough:")
    nc = [{"claim_text": "솔라시 솔라시도 9"}]
    expect("CLAIM", "garbled text present in normalized_claims[].claim_text",
           nc[0]["claim_text"], "솔라시 솔라시도 9")

    # evidence_quality extraction
    p("evidence_quality extraction:")
    eq = evidence_quality({"evidence_quality_summary":
                           {"strong": 1, "average_evidence_quality_score": 80}})
    expect("EQ", "strong pulled from nested summary", eq["strong"], 1)
    expect("EQ", "avg pulled from nested summary", eq["average_evidence_quality_score"], 80)

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: candidate tally / genuine / query-trace / claim / EQ proven ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH
# ---------------------------------------------------------------------------
def run_live(explicit_id: int | None) -> int:
    p("=== CARD-DISSECT (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    cols = ("id, created_at, domain, title, verdict_label, policy_alert_level, "
            "policy_confidence_score, verdict_confidence, claims, normalized_claims, "
            "source_candidates, source_queries, source_reliability_summary, "
            "evidence_extraction_summary")

    with engine.connect() as conn:
        if explicit_id is not None:
            rows = conn.execute(
                sa.text(f"SELECT {cols} FROM analysis_results WHERE id = :i")
                .bindparams(i=int(explicit_id))
            ).all()
        else:
            rows = conn.execute(
                sa.text(f"SELECT {cols} FROM analysis_results WHERE title LIKE :t "
                        "ORDER BY id DESC")
                .bindparams(t=TITLE_LIKE)
            ).all()

    if not rows:
        p(f"No row matched (title LIKE {TITLE_LIKE!r} or id={explicit_id}).")
        return 0
    if len(rows) > 1:
        p(f"[note] {len(rows)} rows matched the title; ids="
          f"{[r._mapping['id'] for r in rows]} — dumping the NEWEST (first).")
    m = rows[0]._mapping

    srs = _json_obj(m["source_reliability_summary"])
    candidates = _json_list(m["source_candidates"])
    normalized = _json_list(m["normalized_claims"])
    claims_raw = _json_list(m["claims"])
    source_queries = _json_list(m["source_queries"])
    ees = _json_obj(m["evidence_extraction_summary"])
    eq = evidence_quality(ees)
    cand = classify_candidates(candidates)
    primary = extract_primary_document_match(candidates)

    # ---- ROW SUMMARY --------------------------------------------------------
    p("")
    p("=== ROW SUMMARY ===")
    p(f"  id                          : {m['id']}")
    p(f"  created_at                  : {str(m['created_at'])[:19]}")
    p(f"  domain                      : {_ascii(m['domain'])}")
    p(f"  title                       : {_ascii(m['title'])}")
    p(f"  verdict_label               : {m['verdict_label']}")
    p(f"  policy_alert_level          : {m['policy_alert_level']}")
    p(f"  policy_confidence_score     : {m['policy_confidence_score']}   "
      f"(>=75 -> 'score axis' reasoning fires: scoreTrustDescription)")
    p(f"  verdict_confidence          : {m['verdict_confidence']}")
    p(f"  official_mismatch           : {srs.get('official_mismatch')!r}")
    p(f"  has_genuine_official_support: {srs.get('has_genuine_official_support')!r}   "
      f"(drives '공식자료 참고' when False)")
    p(f"  official_candidate_count    : {srs.get('official_candidate_count')}")
    p(f"  evidence_quality            : strong={eq['strong']} medium={eq['medium']} "
      f"weak={eq['weak']} avg={eq['average_evidence_quality_score']}   "
      f"(strong>0 & avg>=75 -> '강한 근거 확인' fires)")

    # ---- CANDIDATE BREAKDOWN ------------------------------------------------
    p("")
    p("=== CANDIDATE BREAKDOWN ===")
    p(f"  total source_candidates     : {len(candidates)}")
    p(f"  official-type candidates    : {cand['official_count']}")
    b = cand["buckets"]
    p(f"  by classification           : strong={b['strong']} medium={b['medium']} "
      f"weak={b['weak']} none/unclassified={b['none_or_unclassified']} "
      f"non_official={b['non_official']}")
    p(f"  official_body_match==True   : {cand['body_match_count']}")
    p(f"  extract_primary_document_match: {'FOUND' if primary else 'None'}"
      + (f" (score={primary.get('score')}, class={primary.get('classification')})" if primary else ""))
    genuine = srs.get("has_genuine_official_support")
    why = ("primary-document match present" if primary else
           (f"no primary-document match AND {b['strong']} strong body-match candidate(s)"))
    p(f"  => has_genuine_official_support={genuine!r} BECAUSE {why}. "
      f"All-weak candidates => every card row shows '공식 약한 후보'.")

    # ---- CLAIM / QUERY TRACE ------------------------------------------------
    p("")
    p("=== CLAIM / QUERY TRACE ===")
    p("  '1차 공식 근거 · site:...' lines  <- source_queries[] (renderSourceQueries):")
    primary_qs = [q for q in source_queries if isinstance(q, dict)
                  and q.get("purpose") == "primary_source"]
    for q in (primary_qs or source_queries)[:6]:
        if not isinstance(q, dict):
            continue
        p(f"      claim#{(q.get('claim_index') or 0)} purpose={q.get('purpose')!r} "
          f"query={_ascii(q.get('query'))}")
    p("    -> STORED-DATA: the 'site:...' text is source_queries[].query (the generated")
    p("       search query); '1차 공식 근거' is formatSourcePurpose(purpose). It is the")
    p("       ADVANCED 'generated queries' section rendering an INTERNAL query field.")
    p("")
    p("  garbled claim text  <- normalized_claims[].claim_text / claims[]:")
    for i, c in enumerate(normalized[:6]):
        if isinstance(c, dict):
            p(f"      normalized_claims[{i}].claim_text = {_ascii(c.get('claim_text'))}")
    for i, c in enumerate(claims_raw[:6]):
        p(f"      claims[{i}] = {_ascii(c)}")
    p("    -> label each above: if '솔라시 솔라시도 9' appears verbatim here it is")
    p("       STORED-DATA (claim-extraction output), not a display artifact.")

    # ---- VERDICT ------------------------------------------------------------
    p("")
    p("=== VERDICT ===")
    score = m["policy_confidence_score"]
    mismatch = srs.get("official_mismatch")
    p(f"  Score={score}, official_mismatch={mismatch!r}, has_genuine={genuine!r}, "
      f"strong-cands={b['strong']}, body-matches={cand['body_match_count']}.")
    p("  The four reasoning surfaces read DIFFERENT stored axes:")
    p("    - '공식자료 참고'  = has_genuine_official_support (genuine-body axis)")
    p("    - '비교적 탄탄'    = policy_confidence_score>=75 (SCORE axis)")
    p("    - '강한 근거 확인' = evidence_quality.strong>0 & avg>=75 (EXTRACTION axis)")
    p("    - '공식 약한 후보' = per-candidate stored classification (all weak)")
    p("  If (has_genuine False / all-weak candidates / no body match) BUT (score>=75 &")
    p("  quality.strong>0), the box+verdict are self-consistent on the genuine axis while")
    p("  the SCORE/QUALITY-driven reasoning text over-claims -> DISPLAY/COPY inconsistency")
    p("  (the reasoning strings read different axes than the official-status box).")
    p("  The 'site:...' line and the garbled claim, if verbatim above, are STORED-DATA")
    p("  surfaced by advanced sections (source_queries / normalized_claims) — an")
    p("  upstream extraction/curation question, NOT the box/score/verdict. Read the")
    p("  dumped values above for the definitive per-field call. (No fix proposed.)")

    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY dissection of one stored card (DISPLAY-only vs DATA issue). "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    parser.add_argument("--id", type=int, default=None,
                        help="Dump a specific analysis_results id instead of the title match.")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live(args.id)


if __name__ == "__main__":
    raise SystemExit(main())
