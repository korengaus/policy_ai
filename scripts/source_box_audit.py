"""DESIGN-3B-1c Phase 1 — READ-ONLY source-box honesty + slim-cost audit.

For the card "primary-source compare box" we want to show {institution} · {document}.
This probe samples recent GENUINE rows (has_genuine_official_support true) and, per
row, prints the ACTUAL matched official document so we can judge:

  * Is it a GENUINE official primary doc (official publisher + on-topic body), so the
    box just needs the institution field added (a DISPLAY fix), OR
  * Is it an IBK-style spurious match (off-topic / news-article / generic overlap),
    which would be a verdict-adjacent HONESTY gap to fix first — NOT a display change.

It also MEASURES the slim byte cost of adding a short institution string
(top_official_institution) to source_reliability_summary, to protect PERF-4.

Per genuine row it prints: id, claim, the driving candidate's publisher /
source_type / classification / title / official_evidence_score / body-snippet, and
what the box shows today (top_official_detail_title + official_direct_match_score).

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/source_box_audit.py
    PYTHONPATH=. python scripts/source_box_audit.py --limit 250 --show 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Mirror official_evidence_resolution.py primary-document gate.
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
PRIMARY_STRONG = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75
OFFICIAL_TYPES = {"official_government", "public_institution"}


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
        except Exception as exc:  # noqa: BLE001 — never leak the URL
            print(f"NOTE: direct DATABASE_URL engine unavailable ({type(exc).__name__}); "
                  "falling back to postgres_storage.get_engine().", file=sys.stderr)
    try:
        import postgres_storage
        return postgres_storage.get_engine()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no engine available ({type(exc).__name__}).", file=sys.stderr)
        return None


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


def _num(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _trunc(v, n=100) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _candidate_score(c: dict) -> float:
    return max(
        _num(c.get("official_evidence_score")),
        _num(c.get("official_final_direct_match_score")),
        _num(c.get("official_body_match_score")),
        _num(c.get("score")),
    )


def _body_snippet(c: dict) -> str:
    for key in ("official_body_text", "raw_text", "body_text", "official_detail_body"):
        v = c.get(key)
        if isinstance(v, str) and v.strip():
            return _trunc(v, 100)
    for ms in c.get("official_matched_sentences") or []:
        if isinstance(ms, dict) and ms.get("sentence"):
            return _trunc(ms.get("sentence"), 100)
    return ""


def _driving_candidate(cands):
    """The candidate that makes the row genuine: a strong primary-marker match
    (preferred), else the first official_body_match=True candidate."""
    best = None
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        has_marker = any(str(c.get(f) or "").strip() for f in PRIMARY_MARKER_FIELDS)
        clf = str(c.get("official_evidence_classification") or c.get("official_direct_match_classification") or "")
        if has_marker and c.get("official_body_match") and clf == PRIMARY_STRONG and _candidate_score(c) >= PRIMARY_MIN_SCORE:
            return c, "primary_marker"
    for c in cands or []:
        if isinstance(c, dict) and c.get("official_body_match"):
            return c, "body_match"
    return best, "none"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="source_box_audit")
    parser.add_argument("--limit", type=int, default=250, help="recent rows to scan for genuine ones")
    parser.add_argument("--show", type=int, default=10, help="genuine rows to print in full")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 3000))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, claim_text, source_reliability_summary, source_candidates, debug_summary "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)
    genuine = []
    for r in rows:
        srs = _parse_json(r["source_reliability_summary"]) or {}
        srs = srs if isinstance(srs, dict) else {}
        debug = _parse_json(r["debug_summary"]) or {}
        debug = debug if isinstance(debug, dict) else {}
        flag = srs.get("has_genuine_official_support")
        if flag is None:
            flag = _num(debug.get("official_body_matches")) > 0
        if flag:
            genuine.append((r, srs))

    # ---- per-row honesty detail + institution-availability + cost tally ----
    inst_present = 0          # driving candidate has a non-empty publisher/source_name
    official_type_rows = 0    # driving candidate source_type is official
    title_is_sentence = 0     # current box title looks like a long sentence (>40 chars, has spaces)
    inst_byte_total = 0
    publishers = Counter()

    print("=" * 90)
    print(f"DESIGN-3B-1c source-box audit — scanned {n} rows, {len(genuine)} genuine "
          f"({(len(genuine)/n*100 if n else 0):.1f}%)")
    print("=" * 90)

    shown = 0
    for r, srs in genuine:
        cands = _parse_json(r["source_candidates"]) or []
        cand, how = _driving_candidate(cands)
        publisher = ""
        source_type = clf = title = body = ""
        score = 0.0
        if isinstance(cand, dict):
            publisher = str(cand.get("publisher") or cand.get("source_name") or "").strip()
            source_type = str(cand.get("source_type") or "")
            clf = str(cand.get("official_evidence_classification")
                      or cand.get("official_direct_match_classification") or "")
            title = str(cand.get("title") or cand.get("official_detail_title") or "")
            body = _body_snippet(cand)
            score = _candidate_score(cand)
        box_title = str(srs.get("top_official_detail_title") or "")
        if publisher:
            inst_present += 1
            inst_byte_total += len(publisher.encode("utf-8"))
            publishers[publisher[:30]] += 1
        if source_type in OFFICIAL_TYPES:
            official_type_rows += 1
        if len(box_title) > 40 and " " in box_title:
            title_is_sentence += 1

        if shown < args.show:
            shown += 1
            print(f"\n[{shown}] id={r['id']}  driving={how}  score={round(score)}")
            print(f"    CLAIM       : {_trunc(r['claim_text'], 110)}")
            print(f"    publisher   : {publisher or '(none)'}   source_type={source_type or '(none)'}")
            print(f"    classification: {clf or '(none)'}")
            print(f"    cand.title  : {_trunc(title, 90)}")
            print(f"    cand.body   : {body or '(none)'}")
            print(f"    BOX SHOWS NOW (top_official_detail_title): {_trunc(box_title, 90)}")
            print(f"    official_direct_match_score: {srs.get('official_direct_match_score')}")
            print(f"    JUDGE -> publisher official? {source_type in OFFICIAL_TYPES}  "
                  f"| box title looks like a sentence? {len(box_title) > 40 and ' ' in box_title}")

    g = max(1, len(genuine))
    print("\n" + "=" * 90)
    print("PART B — HONESTY TALLY (genuine rows)")
    print("=" * 90)
    print(f"  driving candidate has an OFFICIAL source_type: {official_type_rows}/{g} "
          f"({official_type_rows/g*100:.1f}%)   <- if low, possible IBK-style mis-fire")
    print(f"  driving candidate has a publisher/source_name: {inst_present}/{g} "
          f"({inst_present/g*100:.1f}%)   <- institution availability for the box")
    print(f"  current box title looks like a SENTENCE (>40 chars, spaces): {title_is_sentence}/{g} "
          f"({title_is_sentence/g*100:.1f}%)   <- why the box reads odd today")
    print("  top publishers seen (the would-be institution field):")
    for p, c in publishers.most_common(12):
        print(f"     {c:3}  {p}")

    print("\n" + "=" * 90)
    print("PART C — SLIM COST of adding top_official_institution (short string)")
    print("=" * 90)
    avg_inst = (inst_byte_total / inst_present) if inst_present else 0
    # JSON overhead per row: the key '"top_official_institution":' ~ 27 bytes + quotes/comma ~4
    per_row_added = avg_inst + 31
    for slim_n in (50, 100):
        total = per_row_added * slim_n
        print(f"  ~{slim_n} slim rows: +{round(total)} bytes (~{total/1024:.2f} kB) added "
              f"(avg institution {avg_inst:.0f} B + ~31 B key overhead per row)")
    print(f"  Reference: current slim payload ~106 kB for ~50 rows (post-PERF-4).")
    print(f"  -> Verdict: {'NEGLIGIBLE (<2 kB, safe for PERF-4)' if per_row_added*50 < 2048 else 'CHECK — larger than expected'}")
    print("=" * 90)
    print("Interpretation: if 'OFFICIAL source_type' is ~100%, the matches are genuine and the")
    print("box just needs the institution field (DISPLAY fix, Phase 2). If it's low, some genuine")
    print("rows are spurious -> verdict-adjacent honesty fix first (NOT a display change).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
