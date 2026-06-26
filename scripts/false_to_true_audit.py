"""DESIGN-3B-1e Phase 1b — READ-ONLY forensics on the backfill dry-run's
unexpected false->true rows (default 219,230,240,327,379).

The genuine_backfill dry-run reported 5 rows where the strong-only predicate
recomputes TRUE but the row currently DISPLAYS not-genuine (stored flag false /
fallback). The 'must be 0' assertion assumed stored-flag ⊇ strict; this probe tests
H1 (legit stale false-NEGATIVE — the stored flag predates the fixed predicate, e.g.
the pre-M22-1b extract_primary_document_match retrieval_method bug) vs H2 (script
diverges from production).

Per row it prints: id, claim, created_at, stored has_genuine (raw + python type),
debug.official_body_matches, each official_body_match candidate's
publisher/source_name + official_evidence_classification (+ sibling) + score + title
+ body snippet + marker fields, the extract_primary_document_match result, the
strict recompute + WHICH disjunct, and an on-topic hint (does the matched doc share
the claim's material entity?). It uses the EXACT production predicate
(extract_primary_document_match imported; strict count mirrored from
verification_card.py:687) so 'script says true' == 'production says true' (rules out H2).

STRICTLY SELECT / READ-ONLY. No writes. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell:
    PYTHONPATH=. python scripts/false_to_true_audit.py
    PYTHONPATH=. python scripts/false_to_true_audit.py --ids 219,230,240,327,379
"""

from __future__ import annotations

import argparse
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

from official_evidence_resolution import extract_primary_document_match

STRONG = "strong_official_direct_support"
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")

# light material-overlap hint (institution / specific token vs generic)
INSTITUTIONS = ["금융위원회", "금융위", "금융감독원", "금감원", "국토교통부", "국토부",
                "기획재정부", "기재부", "한국은행", "국세청", "경찰청", "법무부", "고용노동부",
                "보건복지부", "복지부", "교육부", "중소벤처기업부", "해양수산부", "해수부",
                "공정거래위원회", "여성가족부", "농림축산식품부", "행정안전부", "산업통상자원부",
                "국회", "주택도시보증공사", "신용보증기금", "기업은행", "두나무", "정책브리핑", "법제처"]
GENERIC = frozenset({"지원", "금융", "정책", "확대", "강화", "추진", "검토", "발표", "운영",
                     "시행", "사업", "제도", "관리", "대책", "방안", "계획", "관련", "정부",
                     "공식", "회의", "출범", "행사", "동향", "점검"})


def _overlap(claim, doc):
    c, d = str(claim or ""), str(doc or "")
    if {i for i in INSTITUTIONS if i in c} & {i for i in INSTITUTIONS if i in d}:
        return True
    ct = {t for t in re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}", c) if t not in GENERIC}
    dt = {t for t in re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}", d) if t not in GENERIC}
    return bool(ct & dt)


def _strict_count(cands):
    return sum(
        1 for s in (cands or [])
        if isinstance(s, dict) and s.get("official_body_match")
        and (s.get("official_evidence_classification") or s.get("official_direct_match_classification")) == STRONG
    )


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


def _trunc(v, n=150):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _cand_score(c):
    def _n(x):
        try:
            return float(x)
        except Exception:
            return 0.0
    return max(_n(c.get("official_evidence_score")), _n(c.get("official_final_direct_match_score")),
              _n(c.get("official_body_match_score")), _n(c.get("score")))


def _get_engine():
    import sqlalchemy as sa
    raw = os.environ.get("DATABASE_URL")
    if raw:
        url = raw.replace("postgresql+psycopg://", "postgresql://")
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        try:
            engine = sa.create_engine(url)
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            print(f"NOTE: direct DATABASE_URL engine unavailable ({type(exc).__name__}); "
                  "falling back to postgres_storage.get_engine().", file=sys.stderr)
    try:
        import postgres_storage
        return postgres_storage.get_engine()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no engine available ({type(exc).__name__}).", file=sys.stderr)
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="false_to_true_audit")
    parser.add_argument("--ids", type=str, default="219,230,240,327,379")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    ids = [int(x) for x in str(args.ids).split(",") if x.strip().isdigit()]

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, claim_text, created_at, source_reliability_summary, "
            "source_candidates, debug_summary "
            "FROM analysis_results WHERE id = ANY(:ids) ORDER BY id ASC"
        ), {"ids": ids}).mappings().all()

    print("=" * 92)
    print(f"DESIGN-3B-1e Phase 1b — false->true forensics for ids {ids}")
    print("=" * 92)
    h1 = h2 = 0
    for r in rows:
        srs = _parse_json(r["source_reliability_summary"]) or {}
        srs = srs if isinstance(srs, dict) else {}
        cands = _parse_json(r["source_candidates"]) or []
        debug = _parse_json(r["debug_summary"]) or {}
        debug = debug if isinstance(debug, dict) else {}

        stored = srs.get("has_genuine_official_support")
        prim = extract_primary_document_match(cands or [])
        strong_n = _strict_count(cands)
        strict = bool(prim is not None or strong_n > 0)
        disjunct = ("primary_document_match" if prim is not None else
                    ("strong_official_body_match" if strong_n > 0 else "NONE(!?)"))

        print(f"\n[id={r['id']}] created_at={r['created_at']}")
        print(f"  CLAIM            : {_trunc(r['claim_text'], 120)}")
        print(f"  stored has_genuine: {stored!r}  (python type: {type(stored).__name__})")
        print(f"  debug.official_body_matches: {debug.get('official_body_matches')}")
        print(f"  STRICT recompute : {strict}   via disjunct: {disjunct}   (strong_body_count={strong_n})")
        if prim is not None:
            print(f"  extract_primary_document_match -> marker_match: clf={prim.get('classification')} "
                  f"score={prim.get('score')} title={_trunc(prim.get('title'), 60)}")
        # show the official_body_match candidates (the strong/medium matches)
        shown = 0
        for c in cands or []:
            if isinstance(c, dict) and c.get("official_body_match"):
                shown += 1
                clf = c.get("official_evidence_classification") or c.get("official_direct_match_classification")
                marker = [f for f in PRIMARY_MARKER_FIELDS if str(c.get(f) or "").strip()]
                body = ""
                for k in ("official_body_text", "raw_text", "body_text"):
                    if isinstance(c.get(k), str) and c[k].strip():
                        body = _trunc(c[k], 130); break
                if not body:
                    for ms in c.get("official_matched_sentences") or []:
                        if isinstance(ms, dict) and ms.get("sentence"):
                            body = _trunc(ms["sentence"], 130); break
                title = c.get("title") or c.get("official_detail_title") or ""
                print(f"    cand#{shown}: publisher={c.get('publisher') or c.get('source_name') or '(none)'} "
                      f"type={c.get('source_type')} clf={clf} score={round(_cand_score(c))} marker={marker or '(none)'}")
                print(f"            title: {_trunc(title, 70)}")
                print(f"            body : {body or '(none)'}")
                print(f"            on-topic vs claim? {_overlap(r['claim_text'], str(title) + ' ' + body)}")
        # H1 vs H2 hint: strict True (which == production) + stored not-true => stale false-negative (H1)
        if strict and stored is not True:
            print(f"  => H1 LIKELY: production predicate yields TRUE but stored is {stored!r} "
                  f"(stale false-negative; recompute repairs it). Eyeball on-topic above.")
            h1 += 1
        elif not strict:
            print(f"  => H2/ANOMALY: strict recompute is FALSE here — investigate "
                  f"(script/data mismatch vs the dry-run's count).")
            h2 += 1

    print("\n" + "=" * 92)
    print(f"SUMMARY: H1 (stale false-negative, correct repair): {h1}   "
          f"H2/anomaly (strict False here): {h2}")
    print("If all rows are H1 AND on-topic strong -> backfill should be BIDIRECTIONAL")
    print("(set flag = strict, both true and false) to repair these honest genuines too.")
    print("If any H2/anomaly -> do NOT --apply; reconcile the script/data first.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
