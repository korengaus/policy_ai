"""SECTIONPAGE Phase 1 — READ-ONLY section-page-vs-real-release audit.

A NEW genuine row (id=500) showed the "✓ 공식 근거 확인" box while the slim summary
stored a Policy-Briefing SECTION/LANDING page as the matched source:
    top_official_institution = "Korea Policy Briefing"
    top_official_detail_title = "상단주요뉴스, MY 맞춤뉴스 영역"
yet an earlier probe found the row's top candidate was a REAL release (금융위원회 ·
"청년미래적금 출시…"). So the SAME row holds BOTH, and two DIFFERENT selections
disagree. This probe separates them, because the box and the display use
DIFFERENT selections in the code:

  * THE BOX  — has_genuine_official_support (verification_card.py:708-721): computed
    from source_candidates via extract_primary_document_match (STRONG + score>=75)
    OR a strong_official_direct_support count. It does NOT use top_official_body_match.
  * THE DISPLAY — top_official_institution / _detail_title come from
    summarize_source_reliability.top_official_body_match (official_body_match pool
    with score>=55, picked by max(score, reliability_score, title)). This is the
    selection that surfaced the section page.

For each genuine row this probe enumerates every official-body candidate and prints,
per candidate: title, publisher, official_body_match, score, classification,
official_document_kind, url, a SECTION-PAGE heuristic flag, and three role marks:
  [DISPLAY]  = the candidate summarize_source_reliability would pick (top body match)
  [BOX-PRIMARY] = qualifies extract_primary_document_match (marker + body + STRONG + >=75)
  [BOX-STRONG]  = counts toward _strong_official_body_match_count (body + STRONG)
Then a TALLY: rows whose DISPLAY pick is a section page; of those, whether a REAL
release also existed (section outranked it); and — crucially — whether the BOX is
driven by a section page (a section page is BOX-PRIMARY/BOX-STRONG) vs a real release.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/sectionpage_probe.py
    PYTHONPATH=. python scripts/sectionpage_probe.py --limit 500 --show 12 --focus 500
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

# Mirror the authoritative gates (do not re-implement "genuine"; just replicate the
# exact field reads so the probe reports what the code already decides).
# extract_primary_document_match (official_evidence_resolution.py:464-523):
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
STRONG = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75
# summarize_source_reliability official_body_matches pool (source_reliability_agent.py:329-345):
OFFICIAL_TYPES = {"official_government", "public_institution"}
BODY_POOL_MIN_SCORE = 55

# Section/landing-page signals (heuristic — operator reads the titles, found!=relevant).
_SECTION_TITLE = re.compile(
    r"(상단주요뉴스|맞춤뉴스|주요뉴스|뉴스\s*영역|섹션|카드뉴스|기획&연재|핫이슈|정책뉴스\s*$)"
)
GENERIC_PUBLISHER = {"korea policy briefing", "정책브리핑", "대한민국 정책브리핑", "korea.kr", ""}
# A real korea.kr detail URL carries a specific article id segment.
_DETAIL_URL = re.compile(r"(newsId=|pressReleaseView|policyNewsView|bbsSeqNo=|nttId=)", re.I)


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


def _num(v) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _trunc(v, n=100) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _score(c: dict) -> int:
    return max(
        _num(c.get("official_evidence_score")),
        _num(c.get("official_final_direct_match_score")),
        _num(c.get("official_body_match_score")),
    )


def _classification(c: dict) -> str:
    return str(c.get("official_evidence_classification") or c.get("official_direct_match_classification") or "")


def _is_section_page(c: dict) -> bool:
    title = str(c.get("title") or "")
    publisher = str(c.get("publisher") or "").strip().lower()
    url = str(c.get("url") or c.get("official_detail_url") or "")
    if _SECTION_TITLE.search(title):
        return True
    if publisher in GENERIC_PUBLISHER:
        return True
    # Policy-Briefing candidate whose URL lacks any article-detail segment.
    if str(c.get("policy_briefing_news_item_id") or "").strip() and url and not _DETAIL_URL.search(url):
        return True
    return False


def _is_box_primary(c: dict) -> bool:
    """Replicate extract_primary_document_match's per-candidate gate."""
    if not any(f in c for f in PRIMARY_MARKER_FIELDS):
        return False
    if not c.get("official_body_match"):
        return False
    if _classification(c) != STRONG:
        return False
    return _score(c) >= PRIMARY_MIN_SCORE


def _is_box_strong(c: dict) -> bool:
    """Replicate _strong_official_body_match_count membership."""
    return bool(c.get("official_body_match")) and _classification(c) == STRONG


def _display_pick(cands):
    """Replicate summarize_source_reliability.top_official_body_match selection."""
    pool = [
        c for c in cands
        if isinstance(c, dict)
        and c.get("source_type") in OFFICIAL_TYPES
        and c.get("raw_text_available")
        and c.get("official_body_match")
        and _score(c) >= BODY_POOL_MIN_SCORE
    ]
    if not pool:
        return None
    return max(pool, key=lambda c: (_score(c), _num(c.get("reliability_score")), c.get("title") or ""))


def _official_candidates(cands):
    """Candidates worth showing: official-type OR carrying a primary marker OR body-matched."""
    out = []
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        if (c.get("source_type") in OFFICIAL_TYPES
                or c.get("official_body_match")
                or any(f in c for f in PRIMARY_MARKER_FIELDS)):
            out.append(c)
    return out


def _print_candidate(c, marks):
    title = _trunc(c.get("title"), 40)
    publisher = _trunc(c.get("publisher"), 16)
    obm = "Y" if c.get("official_body_match") else "n"
    sc = _score(c)
    clf = _classification(c) or "(none)"
    kind = _trunc(c.get("official_document_kind"), 18) or "-"
    url = _trunc(c.get("url") or c.get("official_detail_url"), 50)
    sect = "SECTION" if _is_section_page(c) else "release"
    tag = " ".join(marks) if marks else ""
    print(f"    [{sect:7}] obm={obm} score={sc:>3} clf={clf:32} kind={kind:18}")
    print(f"        title : {title!r}")
    print(f"        pub   : {publisher!r}   url: {url}")
    if tag:
        print(f"        ROLE  : {tag}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sectionpage_probe")
    parser.add_argument("--limit", type=int, default=500, help="recent rows to scan for genuine ones")
    parser.add_argument("--show", type=int, default=12, help="genuine rows to print in full")
    parser.add_argument("--focus", type=int, default=500, help="row id to always dump in full")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 5000))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, title, source_reliability_summary, source_candidates, created_at "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    audited = []
    for r in rows:
        summary = _parse_json(r.get("source_reliability_summary"))
        if not isinstance(summary, dict) or not bool(summary.get("has_genuine_official_support")):
            continue
        cands = _parse_json(r.get("source_candidates"))
        cands = cands if isinstance(cands, list) else []
        officials = _official_candidates(cands)
        display = _display_pick(cands)
        box_primary = [c for c in officials if _is_box_primary(c)]
        box_strong = [c for c in officials if _is_box_strong(c)]
        releases = [c for c in officials if c.get("official_body_match") and not _is_section_page(c)]
        sections = [c for c in officials if c.get("official_body_match") and _is_section_page(c)]

        display_is_section = bool(display is not None and _is_section_page(display))
        real_release_existed = bool(releases)
        # Is the BOX itself driven by a section page (honesty problem) vs a real release?
        box_drivers = box_primary or box_strong
        box_has_release = any(not _is_section_page(c) for c in box_drivers)
        box_only_section = bool(box_drivers) and all(_is_section_page(c) for c in box_drivers)

        audited.append({
            "id": r.get("id"),
            "row_title": _trunc(r.get("title"), 30),
            "created_at": _trunc(r.get("created_at"), 25),
            "officials": officials,
            "display": display,
            "display_is_section": display_is_section,
            "real_release_existed": real_release_existed,
            "n_release": len(releases),
            "n_section": len(sections),
            "box_primary": box_primary,
            "box_strong": box_strong,
            "box_has_release": box_has_release,
            "box_only_section": box_only_section,
            "slim_inst": _trunc(summary.get("top_official_institution"), 24),
            "slim_doc": _trunc(summary.get("top_official_detail_title"), 36),
        })

    total = len(audited)
    print("=" * 110)
    print(f"SECTIONPAGE audit — scanned {len(rows)} recent rows, {total} GENUINE "
          "(has_genuine_official_support=true)")
    print("=" * 110)

    disp_section = [a for a in audited if a["display_is_section"]]
    disp_section_with_release = [a for a in disp_section if a["real_release_existed"]]
    box_only_section_rows = [a for a in audited if a["box_only_section"]]
    box_has_release_rows = [a for a in audited if a["box_has_release"]]

    print("\nTALLY (over genuine rows):")
    print(f"  DISPLAY pick is a SECTION page                         : {len(disp_section)}/{total}")
    print(f"    ...of those, a REAL release ALSO existed (section won): {len(disp_section_with_release)}")
    print(f"  BOX driven ONLY by section page(s) (honesty problem)   : {len(box_only_section_rows)}/{total}")
    print(f"  BOX has a REAL release driver (box honest)             : {len(box_has_release_rows)}/{total}")
    print("\nINTERPRETATION:")
    print("  * DISPLAY-section but BOX-has-release  -> DISPLAY/selection bug (box honest, wrong source shown).")
    print("  * BOX-only-section                     -> verdict-adjacent honesty bug (section page drives the box).")

    print("\n" + "-" * 110)
    print("PER-ROW DETAIL:")
    print("-" * 110)
    to_show = audited[: max(1, min(args.show, total or 1))]
    # Ensure the focus row is included even if outside the show window.
    if args.focus is not None and not any(a["id"] == args.focus for a in to_show):
        focus_rows = [a for a in audited if a["id"] == args.focus]
        to_show = focus_rows + to_show
    for a in to_show:
        flags = []
        if a["display_is_section"]:
            flags.append("DISPLAY=SECTION")
        if a["box_only_section"]:
            flags.append("BOX=ONLY-SECTION!")
        if a["box_has_release"]:
            flags.append("BOX=has-release")
        print(f"\nid={a['id']}  {a['row_title']!r}  created={a['created_at']}  {'  '.join(flags)}")
        print(f"  slim shows: inst={a['slim_inst']!r}  doc={a['slim_doc']!r}")
        print(f"  counts: releases={a['n_release']} sections={a['n_section']} "
              f"box_primary={len(a['box_primary'])} box_strong={len(a['box_strong'])}")
        for c in a["officials"]:
            marks = []
            if a["display"] is not None and c is a["display"]:
                marks.append("[DISPLAY]")
            if _is_box_primary(c):
                marks.append("[BOX-PRIMARY]")
            if _is_box_strong(c):
                marks.append("[BOX-STRONG]")
            _print_candidate(c, marks)

    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
