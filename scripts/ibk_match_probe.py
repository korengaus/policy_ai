"""IBK-MATCH Phase 1 — READ-ONLY generalization probe.

Sizes how widespread the IBK pattern is: rows that show a "공식 근거 확인"-driving
signal (official_detail_available True OR official_body_matches>0) but have NO
genuine primary-document match (a candidate carrying policy_briefing_news_item_id
or national_law_mst AND classification == strong_official_direct_support AND
score >= 75 — i.e. _is_strong_primary_document_match). For those rows it prints
the driving doc title + official_direct_match_score so topical relevance can be
EYEBALLED (found != relevant), distinguishing the IBK "label-fires-on-word-overlap"
pattern from genuine primary matches.

SELECT / read-only ONLY. No writes, no verdict-path change. Pin-OUT.

Run in the Render Worker Shell after confirming the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/ibk_match_probe.py
    PYTHONPATH=. python scripts/ibk_match_probe.py --limit 150 --examples 15
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

# Mirror official_evidence_resolution.py:464-466.
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
PRIMARY_STRONG_CLASSIFICATION = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75


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


def _official_status_label(srs: dict, debug: dict) -> str:
    """Reproduce main.js officialStatusLabel exactly."""
    srs = srs or {}
    debug = debug or {}
    if srs.get("official_detail_available") or _num(debug.get("official_body_matches")) > 0:
        return "공식 근거 확인"
    if _num(debug.get("official_body_candidates") or srs.get("official_candidate_count")) > 0:
        if _num(debug.get("official_bodies_fetched")) > 0:
            return "공식 본문 확인 제한"
        return "공식 출처 확인 필요"
    return "뉴스 출처 기반 보조 근거"


def _candidate_primary_score(c: dict) -> float:
    return max(
        _num(c.get("official_evidence_score")),
        _num(c.get("official_final_direct_match_score")),
        _num(c.get("official_body_match_score")),
        _num(c.get("score")),
    )


def _has_strong_primary(candidates) -> tuple[bool, float]:
    """True iff any candidate is a genuine strong primary-document match
    (primary marker + strong classification + score >= 75). Returns the best
    primary score seen regardless of threshold (for context)."""
    best = 0.0
    strong = False
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        has_marker = any(str(c.get(f) or "").strip() for f in PRIMARY_MARKER_FIELDS)
        if not has_marker:
            continue
        sc = _candidate_primary_score(c)
        best = max(best, sc)
        clf = str(c.get("official_direct_match_classification")
                  or c.get("official_evidence_classification") or "")
        if clf == PRIMARY_STRONG_CLASSIFICATION and sc >= PRIMARY_MIN_SCORE:
            strong = True
    return strong, best


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ibk_match_probe")
    parser.add_argument("--limit", type=int, default=150, help="rows to scan (latest N)")
    parser.add_argument("--examples", type=int, default=15, help="label-but-no-primary examples to print")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

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
            "SELECT id, title, original_url, policy_confidence_score, "
            "source_reliability_summary, debug_summary, source_candidates "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)
    label_signal = 0
    genuine_primary = 0
    label_no_primary = []   # the IBK pattern
    for r in rows:
        srs = _parse_json(r["source_reliability_summary"]) or {}
        debug = _parse_json(r["debug_summary"]) or {}
        cands = _parse_json(r["source_candidates"]) or []
        label = _official_status_label(srs, debug)
        has_signal = (label == "공식 근거 확인")
        strong, best_primary = _has_strong_primary(cands)
        if strong:
            genuine_primary += 1
        if has_signal:
            label_signal += 1
            if not strong:
                driving = (srs.get("top_official_detail_title")
                           or srs.get("selected_primary_source")
                           or srs.get("top_source_title") or "(unknown)")
                label_no_primary.append({
                    "id": r["id"],
                    "title": str(r["title"] or "")[:70],
                    "score": r["policy_confidence_score"],
                    "direct_match_score": srs.get("official_direct_match_score"),
                    "direct_match_clf": srs.get("official_direct_match_classification"),
                    "top_score": debug.get("official_resolution_top_score"),
                    "body_matches": debug.get("official_body_matches"),
                    "best_primary_score": round(best_primary),
                    "driving_doc": str(driving)[:70],
                    "reason": str(srs.get("official_direct_match_reason") or "")[:120],
                })

    print("=" * 80)
    print(f"IBK-MATCH generalization — scanned {n} rows")
    print("=" * 80)
    print(f"rows with '공식 근거 확인' label signal:      {label_signal} ({label_signal/n*100:.1f}%)" if n else "no rows")
    print(f"rows with GENUINE primary>=75 strong match:  {genuine_primary} ({genuine_primary/n*100:.1f}%)" if n else "")
    print(f"** IBK PATTERN (label signal, NO primary>=75): {len(label_no_primary)} "
          f"({len(label_no_primary)/n*100:.1f}% of all) **" if n else "")
    print()
    print(f"--- {min(args.examples, len(label_no_primary))} label-but-no-primary examples "
          f"(EYEBALL: is the driving doc topically related to the article?) ---")
    for ex in label_no_primary[:args.examples]:
        print(f"\n  id={ex['id']} score={ex['score']} direct_match={ex['direct_match_score']}"
              f"({ex['direct_match_clf']}) top_score={ex['top_score']} body_matches={ex['body_matches']} "
              f"best_primary={ex['best_primary_score']}")
        print(f"    ARTICLE : {ex['title']}")
        print(f"    DRIVING DOC: {ex['driving_doc']}")
        print(f"    reason  : {ex['reason']}")

    print()
    print("Interpretation: a high count here = the label/score fire on a relevance-passing")
    print("official doc that is NOT a genuine primary match (the IBK pattern). Read the")
    print("ARTICLE vs DRIVING DOC pairs to judge whether the driving doc is topically related")
    print("or word-overlap noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
