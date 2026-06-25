"""LABEL-HONESTY Phase 1 — READ-ONLY relabel-impact probe.

Measures how the corpus splits if "공식 근거 확인" required GENUINE verification
instead of merely official_detail_available. DISPLAY-ONLY diagnosis: reads stored
rows, changes nothing. Pin-OUT, SELECT-only.

GENUINE (candidate definition, validated here):
  (A) a strong primary-document match — a candidate carrying a marker in
      (policy_briefing_news_item_id, national_law_mst) AND classification ==
      strong_official_direct_support AND score >= 75   [== _is_strong_primary_document_match]
  OR
  (B) official_body_matches > 0  (a real body-sentence match; debug_summary scalar)

Anything else currently showing "공식 근거 확인" (a relevance-passing fetched page,
no primary marker, no body match — the IBK pattern) would be DOWNGRADED.

Also measures the FRONTEND-VISIBILITY gap: rows that are GENUINE only via (A) the
primary marker but have official_body_matches == 0. Those are invisible to the
frontend (source_candidates was dropped from the slim /history payload in PERF-4),
so a non-zero count here means Phase 2 needs a backend boolean.

Run in the Render Worker Shell after confirming the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/label_impact_probe.py --limit 200
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


def _current_strong_label(srs: dict, debug: dict) -> bool:
    """Reproduce main.js officialStatusLabel == '공식 근거 확인' (the strong label)."""
    return bool(srs.get("official_detail_available")) or _num(debug.get("official_body_matches")) > 0


def _strong_primary(cands) -> tuple[bool, float, str]:
    """(_is_strong_primary_document_match present?, best primary score, driving title)."""
    best, strong, title = 0.0, False, ""
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        if not any(str(c.get(f) or "").strip() for f in PRIMARY_MARKER_FIELDS):
            continue
        if not c.get("official_body_match"):
            # extract_primary_document_match also requires official_body_match True
            pass
        sc = max(_num(c.get("official_evidence_score")), _num(c.get("official_final_direct_match_score")),
                 _num(c.get("official_body_match_score")), _num(c.get("score")))
        clf = str(c.get("official_evidence_classification") or c.get("official_direct_match_classification") or "")
        if sc > best:
            best = sc
        if clf == PRIMARY_STRONG_CLASSIFICATION and sc >= PRIMARY_MIN_SCORE and c.get("official_body_match"):
            strong = True
            title = str(c.get("title") or c.get("document_title") or "")[:70]
    return strong, best, title


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="label_impact_probe")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--downgrade-examples", type=int, default=20)
    parser.add_argument("--body-examples", type=int, default=8)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable (dual-write disabled / DATABASE_URL unset).", file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 2000))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, title, policy_confidence_score, source_reliability_summary, "
            "debug_summary, source_candidates FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)
    current_strong = 0
    keep = []          # genuine → keep strong label
    downgrade = []     # IBK pattern → weaker label
    primary_no_body = []   # genuine via marker only, body_matches==0 (frontend-invisible gap)
    body_examples = []     # body_matches>0 trust check

    for r in rows:
        srs = _parse_json(r["source_reliability_summary"]) or {}
        debug = _parse_json(r["debug_summary"]) or {}
        cands = _parse_json(r["source_candidates"]) or []
        title = str(r["title"] or "")[:70]
        body_matches = _num(debug.get("official_body_matches"))
        strong_primary, best_primary, primary_title = _strong_primary(cands)
        genuine = strong_primary or body_matches > 0

        if body_matches > 0 and len(body_examples) < args.body_examples:
            body_examples.append((r["id"], title, int(body_matches),
                                  str(srs.get("top_official_detail_title") or srs.get("top_source_title") or "")[:60]))

        if strong_primary and body_matches == 0:
            primary_no_body.append((r["id"], title, round(best_primary), primary_title))

        if _current_strong_label(srs, debug):
            current_strong += 1
            rec = {
                "id": r["id"], "title": title, "score": r["policy_confidence_score"],
                "body_matches": int(body_matches),
                "strong_primary": strong_primary,
                "best_primary_score": round(best_primary),
                "direct_match_score": srs.get("official_direct_match_score"),
                "driving": str(srs.get("top_official_detail_title") or srs.get("selected_primary_source")
                               or srs.get("top_source_title") or "(unknown)")[:70],
                "reason": str(srs.get("official_direct_match_reason") or "")[:110],
            }
            (keep if genuine else downgrade).append(rec)

    def pct(x): return f"{x/n*100:.1f}%" if n else "n/a"

    print("=" * 82)
    print(f"LABEL-HONESTY relabel impact — scanned {n} rows")
    print("=" * 82)
    print(f"currently show '공식 근거 확인':     {current_strong} ({pct(current_strong)})")
    print(f"  would KEEP (genuine):            {len(keep)} ({pct(len(keep))})")
    print(f"  would DOWNGRADE (IBK pattern):   {len(downgrade)} ({pct(len(downgrade))})")
    print(f"FRONTEND-VISIBILITY GAP — genuine via primary MARKER but body_matches==0: "
          f"{len(primary_no_body)}")
    print("  (if > 0, the frontend can't see these from the slim payload → Phase 2 needs a")
    print("   backend boolean; if 0, gating on official_body_matches>0 alone is sufficient.)")

    print(f"\n--- KEEP set (ALL {len(keep)} — eyeball that each is truly genuine) ---")
    for k in keep:
        how = f"primary>={PRIMARY_MIN_SCORE}({k['best_primary_score']})" if k["strong_primary"] else f"body_matches={k['body_matches']}"
        print(f"  id={k['id']} score={k['score']} [{how}] :: {k['title']}")
        print(f"      driving: {k['driving']}")

    print(f"\n--- DOWNGRADE set (first {args.downgrade_examples} — eyeball that each is IBK pattern) ---")
    for d in downgrade[:args.downgrade_examples]:
        print(f"  id={d['id']} score={d['score']} direct_match={d['direct_match_score']} "
              f"body_matches={d['body_matches']} best_primary={d['best_primary_score']}")
        print(f"      ARTICLE: {d['title']}")
        print(f"      DRIVING: {d['driving']}")
        print(f"      reason : {d['reason']}")

    print(f"\n--- body_matches>0 trust check ({len(body_examples)} examples — is the matched doc on-topic?) ---")
    for bid, btitle, bm, bdoc in body_examples:
        print(f"  id={bid} body_matches={bm}")
        print(f"      ARTICLE: {btitle}")
        print(f"      BODY DOC: {bdoc}")

    print(f"\n--- gap examples (primary marker, body_matches==0) ---")
    for gid, gtitle, gsc, gdoc in primary_no_body[:10]:
        print(f"  id={gid} primary_score={gsc} :: {gtitle}  <= {gdoc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
