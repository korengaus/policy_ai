"""TOPICGATE Phase 1 — READ-ONLY topic/entity gate simulation.

Designs and SIMULATES a GENERAL material-entity/topic gate that would make the
IBK-pattern SCORE honest by generalizing verification_card._official_topic_mismatch_reason
(today HOUSING-ONLY) so a fetched official doc must share the claim's MATERIAL
entity (named institution OR a specific, non-generic policy term) — not merely a
generic concept (지원/금융/정책/확대) — before it counts as a usable official
detail. When the gate flags the doc, source_reliability_summary.official_mismatch
becomes True, and the EXISTING post-card clamp at main.py:934-939 lowers the
stored policy_confidence_score to <=20 (verification_strength -> "none"). No
scorer weight, no threshold, no selection-ordering change — only the
mismatch-eligibility flag, which already has a score lever.

WHY this is the score path (authoritative code read):
  * official_crawler.py:1468 sets result["usable"]=True on relevance>=40 AND
    grade in {A,B,C}. The IBK<->FSC doc (relevance 77) passes.
  * policy_confidence._best_official_evidence (65-85) picks it -> raw score ~90.
  * verification_card._official_verification_summary uses _is_usable_official_detail
    -> _official_topic_mismatch_reason (HOUSING-ONLY) -> for a financial/SME query
    the housing branches never fire -> usable non-empty -> official_mismatch=False.
  * main.py:934: official_mismatch False -> NO clamp -> 90 stands.
  Generalize the gate -> official_mismatch True -> main.py:934 clamps -> ~20.

WHAT THIS PROBE MEASURES (changes NOTHING):
  For the latest N rows, of those whose score is currently driven by an official
  detail doc (official_source_used_in_final_scoring / official_detail_available
  True, NOT already mismatched), simulate two gate variants on the (claim, driving
  doc title) pair and report, per variant:
    * NEWLY-FLAGGED (would clamp, score drops) vs KEPT.
    * Cross-tab NEWLY-FLAGGED against the LABEL-HONESTY genuine flag
      (_is_strong_primary_document_match OR official_body_matches>0):
        - genuine  -> MUST be 0 (a genuine clamp is the FORBIDDEN failure)
        - non-genuine (IBK / off-topic) -> the intended drops
    * Prints EVERY genuine row the gate would flag (the zero-loss SAFETY proof),
      and titles for every drop/keep so precision is eyeballed (found != relevant).

GENUINE (mirrors scripts/label_impact_probe.py exactly):
  (A) a candidate with a primary marker (policy_briefing_news_item_id |
      national_law_mst) AND classification == strong_official_direct_support AND
      score >= 75  [== _is_strong_primary_document_match], OR
  (B) debug_summary.official_body_matches > 0.

LIMITATION (stated honestly): official_evidence_results (the Lane-A list the
SCORE selects from) is NOT a stored column. The probe approximates the score's
driving doc via the verification_card MIRROR fields in source_reliability_summary
(top_official_detail_title / selected_primary_source / top_source_title). The
Phase-2 gate runs at verification_card._official_topic_mismatch_reason where it
sees the RICHER per-item fields (search_query_used, matched_query_terms,
matched_concepts, document_title, site_key); the probe uses the row-level claim
text + the mirrored driving-doc title, so it is a CONSERVATIVE approximation of
that gate, not a byte-exact replay.

SELECT / read-only ONLY. No writes, no verdict-path change, no scorer/threshold
change. Pin-OUT.

Run in the Render Worker Shell after confirming the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/topicgate_probe.py --limit 200
    PYTHONPATH=. python scripts/topicgate_probe.py --limit 200 --examples 12
"""

from __future__ import annotations

import argparse
import json
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

# Mirror official_evidence_resolution.py:464-466 (genuine primary-doc gate).
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
PRIMARY_STRONG_CLASSIFICATION = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75

# ---------------------------------------------------------------------------
# MATERIAL-ENTITY vocabulary for the simulated gate.
#
# Named institutions / issuers. Substring match against claim text and doc
# title. Includes central gov, regulators, the central bank, public financial
# institutions, and the named private issuers that appear in the corpus
# (두나무 etc.). Local governments are matched separately by regex.
# ---------------------------------------------------------------------------
INSTITUTIONS = [
    "금융위원회", "금융위", "금융감독원", "금감원", "국토교통부", "국토부",
    "기획재정부", "기재부", "한국은행", "국세청", "관세청", "경찰청", "검찰",
    "법무부", "고용노동부", "노동부", "행정안전부", "행안부", "보건복지부",
    "복지부", "교육부", "산업통상자원부", "산업부", "중소벤처기업부", "중기부",
    "공정거래위원회", "공정위", "방송통신위원회", "방통위", "환경부",
    "여성가족부", "여가부", "농림축산식품부", "농식품부", "해양수산부",
    "국무회의", "국회", "대통령실",
    "주택도시보증공사", "신용보증기금", "기술보증기금", "예금보험공사",
    "한국주택금융공사", "주택금융공사", "산업은행", "수출입은행",
    "기업은행", "IBK", "국민은행", "신한은행", "우리은행", "하나은행",
    "농협", "수협", "새마을금고",
    "두나무", "업비트", "빗썸", "카카오", "카카오뱅크", "네이버", "토스",
    "케이뱅크", "코레일", "한국전력", "한전", "예탁결제원", "거래소",
    "국민연금", "건강보험공단", "근로복지공단", "도로공사", "LH", "토지주택공사",
]

# Local-government detector. Uses ONLY unambiguous administrative suffixes so a
# common noun like 도시 (city) can't be mistaken for a 도+시. Captures 괴산군 /
# 순창군 / 청도군 / 제주특별자치도 / 종로구청 / 수원시청 etc. Over-matching here would
# only make the gate LOOSER (fewer drops) — never a genuine loss — so the
# conservative-narrow form is the safe choice for the measurement.
_LOCALGOV_RE = re.compile(
    r"[가-힣]{1,3}(?:특별자치도|특별자치시|광역시|특별시|시청|군청|구청|도청|군)"
)

# ---------------------------------------------------------------------------
# GENERIC concept tokens — present in almost every policy doc. The off-topic
# IBK<->FSC match scored 77 almost entirely on these (지원/금융/정책 overlap +
# the CONCEPT_SYNONYMS_RELEVANCE generic buckets subsidy_support / implementation
# / review_stage / official_statement). A doc whose ONLY overlap with the claim
# is in this set is NOT topically verifying the claim.
# ---------------------------------------------------------------------------
GENERIC_TOKENS = frozenset({
    "지원", "금융", "정책", "확대", "강화", "추진", "검토", "발표", "운영",
    "시행", "사업", "제도", "관리", "대책", "방안", "계획", "활성화", "개선",
    "혜택", "보조", "보조금", "협력", "회의", "간담회", "안내", "서비스",
    "행사", "발언", "참석", "개최", "관련", "주요", "뉴스", "기사", "정부",
    "이번", "해당", "대한", "공식", "출처", "내용", "결과", "현황", "공고",
    "공지", "보도자료", "설명자료", "브리핑", "확인", "필요", "추가", "마련",
    "제공", "실시", "도입", "방침", "예정", "오늘", "기관", "당국", "조치",
    "맞춤", "상단", "정보", "전체", "홈페이지", "이용", "민원",
})

# Tokens that are SHORT but MATERIAL (domain anchors) — never treated as generic.
MATERIAL_SHORT = frozenset({
    "전세", "월세", "주택", "대출", "금리", "규제", "세금", "양도세", "청년",
    "신혼", "출산", "귀농", "노인", "복지", "다문화", "DSR", "전세사기",
})


def _num(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _parse_json(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _strong_primary(cands) -> tuple[bool, float, str]:
    """(_is_strong_primary_document_match present?, best primary score, title).
    Mirrors scripts/label_impact_probe.py:_strong_primary."""
    best, strong, title = 0.0, False, ""
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        if not any(str(c.get(f) or "").strip() for f in PRIMARY_MARKER_FIELDS):
            continue
        sc = max(
            _num(c.get("official_evidence_score")),
            _num(c.get("official_final_direct_match_score")),
            _num(c.get("official_body_match_score")),
            _num(c.get("score")),
        )
        clf = str(c.get("official_evidence_classification")
                  or c.get("official_direct_match_classification") or "")
        if sc > best:
            best = sc
        if clf == PRIMARY_STRONG_CLASSIFICATION and sc >= PRIMARY_MIN_SCORE and c.get("official_body_match"):
            strong = True
            title = str(c.get("title") or c.get("document_title") or "")[:70]
    return strong, best, title


def _tokens(text: str) -> set[str]:
    """Material tokens: Hangul runs len>=2 and ascii runs len>=2, minus generics.
    Short domain anchors in MATERIAL_SHORT are retained."""
    raw = re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}", text or "")
    out = set()
    for tok in raw:
        t = tok if re.match(r"[A-Za-z]", tok) else tok
        low = t.lower() if re.match(r"[A-Za-z]", t) else t
        if t in MATERIAL_SHORT:
            out.add(t)
            continue
        if t in GENERIC_TOKENS:
            continue
        if re.match(r"[A-Za-z]", t) and low in {g.lower() for g in GENERIC_TOKENS}:
            continue
        out.add(low if re.match(r"[A-Za-z]", t) else t)
    # explicit short anchors found anywhere in the text
    for anchor in MATERIAL_SHORT:
        if anchor in (text or ""):
            out.add(anchor)
    return out


def _institutions(text: str) -> set[str]:
    found = set()
    t = text or ""
    for name in INSTITUTIONS:
        if name in t:
            found.add(name)
    for m in _LOCALGOV_RE.finditer(t):
        found.add(m.group(0))
    return found


def _gate_decision(claim_text: str, doc_title: str) -> dict:
    """Simulate the generalized material-entity topic gate for one (claim, doc).

    Returns the overlap sets + two mismatch verdicts:
      variant_A (material): mismatch if NO shared institution AND NO shared
                            specific (non-generic) token.
      variant_B (institution-only): mismatch if NO shared institution.
    A 'mismatch' verdict means the doc would be flagged off-topic -> usable empties
    -> official_mismatch True -> main.py:934 clamp -> score drops to <=20.
    """
    inst_claim = _institutions(claim_text)
    inst_doc = _institutions(doc_title)
    spec_claim = _tokens(claim_text)
    spec_doc = _tokens(doc_title)
    shared_inst = inst_claim & inst_doc
    shared_spec = spec_claim & spec_doc
    return {
        "shared_inst": sorted(shared_inst),
        "shared_spec": sorted(shared_spec),
        "variant_A_mismatch": not shared_inst and not shared_spec,
        "variant_B_mismatch": not shared_inst,
    }


def _claim_text(row, claims, normalized) -> str:
    parts = [str(row.get("title") or ""), str(row.get("claim_text") or "")]
    for c in claims or []:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict):
            parts.append(str(c.get("sentence") or c.get("claim_text") or ""))
    for nc in normalized or []:
        if isinstance(nc, dict):
            for k in ("actor", "action", "target", "object", "sentence"):
                parts.append(str(nc.get(k) or ""))
    return " ".join(p for p in parts if p)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="topicgate_probe")
    parser.add_argument("--limit", type=int, default=200, help="rows to scan (latest N)")
    parser.add_argument("--examples", type=int, default=12, help="drop/keep examples to print per bucket")
    parser.add_argument("--ids", type=str, default="394,258",
                        help="comma-separated row ids to print in full (canonical IBK / KEEP)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    focus_ids = {int(x) for x in str(args.ids).split(",") if x.strip().isdigit()}

    import sqlalchemy as sa
    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable (dual-write disabled / DATABASE_URL unset).",
              file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 2000))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, title, claim_text, policy_confidence_score, verification_strength, "
            "claims, normalized_claims, source_reliability_summary, debug_summary, "
            "source_candidates "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)
    official_driven = 0          # score currently driven by an official detail doc
    genuine_total = 0
    # buckets keyed by variant -> list of records
    newly_flagged = {"A": [], "B": []}     # would clamp (drop best_evidence)
    kept = {"A": [], "B": []}              # stays usable (score preserved)
    genuine_flagged = {"A": [], "B": []}   # SAFETY: genuine rows the gate would clamp (must be empty)
    focus_records = []

    for r in rows:
        srs = _parse_json(r["source_reliability_summary"]) or {}
        debug = _parse_json(r["debug_summary"]) or {}
        cands = _parse_json(r["source_candidates"]) or []
        claims = _parse_json(r["claims"]) or []
        normalized = _parse_json(r["normalized_claims"]) or []

        body_matches = _num(debug.get("official_body_matches"))
        strong_primary, best_primary, primary_title = _strong_primary(cands)
        genuine = strong_primary or body_matches > 0
        if genuine:
            genuine_total += 1

        used_official = bool(srs.get("official_source_used_in_final_scoring")
                             or srs.get("official_detail_available"))
        current_mismatch = bool(srs.get("official_mismatch"))
        driving_doc = (srs.get("top_official_detail_title")
                       or srs.get("selected_primary_source")
                       or srs.get("top_source_title") or "")
        claim_text = _claim_text(r, claims, normalized)
        decision = _gate_decision(claim_text, str(driving_doc))

        rec = {
            "id": r["id"],
            "title": str(r["title"] or "")[:74],
            "score": r["policy_confidence_score"],
            "vstrength": r["verification_strength"],
            "genuine": genuine,
            "genuine_how": (f"primary>={PRIMARY_MIN_SCORE}({round(best_primary)})"
                           if strong_primary else (f"body_matches={int(body_matches)}"
                           if body_matches > 0 else "—")),
            "driving_doc": str(driving_doc)[:74],
            "shared_inst": decision["shared_inst"],
            "shared_spec": decision["shared_spec"],
            "current_mismatch": current_mismatch,
            "direct_match_score": srs.get("official_direct_match_score"),
        }

        if r["id"] in focus_ids:
            focus_records.append({**rec, "decision": decision, "claim_text": claim_text[:200]})

        # Only rows whose score is currently driven by an official doc AND not
        # already mismatched can be NEWLY clamped by the gate. Others are out of
        # scope for the score-drop simulation (but genuine ones are still safety-checked).
        in_scope = used_official and not current_mismatch
        for variant in ("A", "B"):
            would_flag = decision[f"variant_{variant}_mismatch"]
            if not in_scope:
                # safety: a genuine, currently-usable row the gate would flag is a loss
                if genuine and would_flag and used_official:
                    genuine_flagged[variant].append(rec)
                continue
            if would_flag:
                newly_flagged[variant].append(rec)
                if genuine:
                    genuine_flagged[variant].append(rec)
            else:
                kept[variant].append(rec)

    # official_driven = in-scope rows (used_official & not current_mismatch),
    # counted once via the union of variant-A flagged+kept (every in-scope row
    # lands in exactly one of those two for variant A).
    official_driven = len({rec["id"] for rec in newly_flagged["A"] + kept["A"]})

    def pct(x):
        return f"{x/n*100:.1f}%" if n else "n/a"

    print("=" * 86)
    print(f"TOPICGATE Phase 1 — material-entity gate simulation — scanned {n} rows")
    print("=" * 86)
    print(f"genuine rows (primary>=75 OR body_matches>0):        {genuine_total} ({pct(genuine_total)})")
    print(f"score currently driven by an official detail doc:    {official_driven} ({pct(official_driven)})")
    print("  (in-scope = official_source_used_in_final_scoring/official_detail_available")
    print("   True AND NOT already official_mismatch — only these can be newly clamped)")
    print()

    for variant, label in (("A", "VARIANT A — material entity OR specific term must overlap"),
                           ("B", "VARIANT B — named INSTITUTION must overlap (stricter)")):
        flagged = newly_flagged[variant]
        kept_v = kept[variant]
        gflag = genuine_flagged[variant]
        gflag_inscope = [g for g in flagged if g["genuine"]]
        ibk = [g for g in flagged if not g["genuine"]]
        print("-" * 86)
        print(f"{label}")
        print("-" * 86)
        print(f"  in-scope rows:                 {len(flagged) + len(kept_v)}")
        print(f"  NEWLY-FLAGGED (score -> <=20): {len(flagged)}")
        print(f"      of which GENUINE (FORBIDDEN, must be 0): {len(gflag_inscope)}")
        print(f"      of which non-genuine (IBK/off-topic, intended): {len(ibk)}")
        print(f"  KEPT (score preserved):        {len(kept_v)}")
        print(f"  SAFETY — ANY genuine+usable row this variant would flag (incl. out-of-scope): {len(gflag)}")
        print()
        if gflag:
            print(f"  *** ZERO-GENUINE-LOSS VIOLATION — variant {variant} would flag {len(gflag)} genuine row(s): ***")
            for g in gflag:
                print(f"    id={g['id']} score={g['score']} [{g['genuine_how']}] sharedInst={g['shared_inst']} sharedSpec={g['shared_spec']}")
                print(f"        ARTICLE: {g['title']}")
                print(f"        DRIVING: {g['driving_doc']}")
        else:
            print(f"  *** ZERO-GENUINE-LOSS HELD — variant {variant} flags NO genuine row. ***")
        print()
        print(f"  --- NEWLY-FLAGGED non-genuine examples (EYEBALL (a) off-topic vs (b) loosely-related) ---")
        for d in ibk[:args.examples]:
            print(f"    id={d['id']} score={d['score']}->~20 direct_match={d['direct_match_score']} "
                  f"sharedInst={d['shared_inst']} sharedSpec={d['shared_spec']}")
            print(f"        ARTICLE: {d['title']}")
            print(f"        DRIVING: {d['driving_doc']}")
        print()
        print(f"  --- KEPT examples (gate leaves score intact — confirm these deserve to stay) ---")
        for k in kept_v[:max(6, args.examples // 2)]:
            tag = "GENUINE" if k["genuine"] else "kept"
            print(f"    id={k['id']} score={k['score']} [{tag}/{k['genuine_how']}] "
                  f"sharedInst={k['shared_inst']} sharedSpec={k['shared_spec']}")
            print(f"        ARTICLE: {k['title']}")
            print(f"        DRIVING: {k['driving_doc']}")
        print()

    if focus_records:
        print("=" * 86)
        print(f"FOCUS ROWS ({sorted(focus_ids)}) — canonical IBK (should FLAG) / KEEP (should stay)")
        print("=" * 86)
        for f in focus_records:
            print(f"  id={f['id']} score={f['score']} vstrength={f['vstrength']} genuine={f['genuine']}({f['genuine_how']})")
            print(f"      ARTICLE   : {f['title']}")
            print(f"      DRIVING   : {f['driving_doc']}")
            print(f"      claim_text: {f['claim_text']}")
            print(f"      sharedInst={f['shared_inst']} sharedSpec={f['shared_spec']}")
            print(f"      variant_A_flag={f['decision']['variant_A_mismatch']} "
                  f"variant_B_flag={f['decision']['variant_B_mismatch']} "
                  f"current_mismatch={f['current_mismatch']}")
            print()

    print("Interpretation:")
    print("  * NEWLY-FLAGGED rows are where official_mismatch would flip True and the")
    print("    EXISTING main.py:934 clamp lowers the stored score to <=20 (honest).")
    print("  * 'of which GENUINE' MUST be 0 for a variant to be acceptable.")
    print("  * Read ARTICLE vs DRIVING for each newly-flagged row: (a) clearly off-topic")
    print("    (두나무<->다문화가족) = good drop; (b) loosely-related (부동산<->주거국정과제)")
    print("    = falls to a weak '관련 자료' label, score clamps — operator judges acceptability.")
    print("  * KEPT genuine rows prove the gate preserves honest matches (zero recall loss).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
