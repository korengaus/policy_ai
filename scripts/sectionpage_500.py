"""SECTIONPAGE-500 Phase 1 — READ-ONLY ASCII-safe dump of ONE row's candidates.

The earlier sectionpage_probe output for id=500 got URL-encoded / mojibake'd in the
terminal (raw Korean + newlines). This probe re-dumps ONLY id=500 (configurable via
--id), forcing EVERY string through repr()/json.dumps(ensure_ascii=True) so each
candidate prints on its own plain-ASCII single line — nothing the terminal can
percent-encode. The goal: read id=500's candidate list to decide whether the box is
driven by a REAL release ([BOX-STRONG] non-section -> DISPLAY-only bug, safe) or by
the SECTION page itself ([BOX-STRONG] section -> verdict-adjacent IBK-class bug).

Role tags computed exactly as sectionpage_probe.py:
  [DISPLAY]     = summarize_source_reliability.top_official_body_match pick (score>=55)
  [BOX-PRIMARY] = extract_primary_document_match gate (marker + body + STRONG + >=75)
  [BOX-STRONG]  = _strong_official_body_match_count member (body + STRONG)

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell:
    PYTHONPATH=. python scripts/sectionpage_500.py
    PYTHONPATH=. python scripts/sectionpage_500.py --id 497
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

# Force ASCII so nothing downstream can percent-encode raw Korean bytes.
try:
    sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")  # type: ignore[attr-defined]
except Exception:
    pass

PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
STRONG = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75
OFFICIAL_TYPES = {"official_government", "public_institution"}
BODY_POOL_MIN_SCORE = 55

_SECTION_TITLE = re.compile(
    r"(상단주요뉴스"      # 상단주요뉴스
    r"|맞춤뉴스"                    # 맞춤뉴스
    r"|주요뉴스"                    # 주요뉴스
    r"|뉴스\s*영역"                # 뉴스 영역
    r"|섹션"                                # 섹션
    r"|카드뉴스)"                  # 카드뉴스
)
GENERIC_PUBLISHER = {
    "korea policy briefing",
    "정책브리핑",               # 정책브리핑
    "대한민국 정책브리핑",  # 대한민국 정책브리핑
    "korea.kr",
    "",
}
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


def _ascii(v, n=60) -> str:
    """Single-line, ASCII-only repr of any value (Korean -> \\uXXXX)."""
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]
    return json.dumps(s, ensure_ascii=True)


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
    if str(c.get("policy_briefing_news_item_id") or "").strip() and url and not _DETAIL_URL.search(url):
        return True
    return False


def _is_box_primary(c: dict) -> bool:
    if not any(f in c for f in PRIMARY_MARKER_FIELDS):
        return False
    if not c.get("official_body_match"):
        return False
    if _classification(c) != STRONG:
        return False
    return _score(c) >= PRIMARY_MIN_SCORE


def _is_box_strong(c: dict) -> bool:
    return bool(c.get("official_body_match")) and _classification(c) == STRONG


def _display_pick(cands):
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sectionpage_500")
    parser.add_argument("--id", type=int, default=500, help="row id to dump")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT id, title, source_reliability_summary, source_candidates, created_at "
            "FROM analysis_results WHERE id = :rid"
        ), {"rid": args.id}).mappings().first()

    if row is None:
        print(f"NO ROW with id={args.id}")
        return 0

    summary = _parse_json(row.get("source_reliability_summary"))
    summary = summary if isinstance(summary, dict) else {}
    cands = _parse_json(row.get("source_candidates"))
    cands = cands if isinstance(cands, list) else []
    display = _display_pick(cands)

    print("=" * 90)
    print(f"id={row.get('id')}  created={_ascii(row.get('created_at'), 30)}")
    print(f"row_title         = {_ascii(row.get('title'), 60)}")
    print(f"has_genuine       = {bool(summary.get('has_genuine_official_support'))}")
    print(f"slim top_official_institution  = {_ascii(summary.get('top_official_institution'), 40)}")
    print(f"slim top_official_detail_title = {_ascii(summary.get('top_official_detail_title'), 60)}")
    print("=" * 90)

    n_release = 0
    n_section = 0
    n_box_primary = 0
    n_box_strong = 0
    box_real_release = False
    box_section_only_drivers = True
    any_box_driver = False

    for i, c in enumerate(cands):
        if not isinstance(c, dict):
            continue
        is_official = (c.get("source_type") in OFFICIAL_TYPES
                       or c.get("official_body_match")
                       or any(f in c for f in PRIMARY_MARKER_FIELDS))
        if not is_official:
            continue
        sect = _is_section_page(c)
        obm = bool(c.get("official_body_match"))
        bp = _is_box_primary(c)
        bs = _is_box_strong(c)
        if obm and sect:
            n_section += 1
        elif obm:
            n_release += 1
        if bp:
            n_box_primary += 1
        if bs:
            n_box_strong += 1
        if bp or bs:
            any_box_driver = True
            if sect:
                pass
            else:
                box_real_release = True
                box_section_only_drivers = False

        marks = []
        if display is not None and c is display:
            marks.append("[DISPLAY]")
        if bp:
            marks.append("[BOX-PRIMARY]")
        if bs:
            marks.append("[BOX-STRONG]")
        role = " ".join(marks) if marks else "-"

        print(f"\ncand[{i}] {'SECTION' if sect else 'release'} obm={'Y' if obm else 'n'} "
              f"score={_score(c):>3} clf={_ascii(_classification(c) or '(none)', 36)}")
        print(f"   publisher = {_ascii(c.get('publisher'), 30)}")
        print(f"   title     = {_ascii(c.get('title'), 50)}")
        print(f"   url       = {_ascii(c.get('url') or c.get('official_detail_url'), 60)}")
        print(f"   source_type={_ascii(c.get('source_type'), 24)} "
              f"pb_news_id={_ascii(c.get('policy_briefing_news_item_id'), 16)} "
              f"law_mst={_ascii(c.get('national_law_mst'), 16)}")
        print(f"   ROLE      = {role}")

    print("\n" + "-" * 90)
    print(f"COUNTS: releases={n_release} sections={n_section} "
          f"box_primary={n_box_primary} box_strong={n_box_strong}")
    if display is not None:
        print(f"DISPLAY pick is a SECTION page: {_is_section_page(display)}")

    if not any_box_driver:
        verdict = "BOX-DRIVER=none"
    elif box_real_release:
        verdict = "BOX-DRIVER=real_release"
    elif box_section_only_drivers:
        verdict = "BOX-DRIVER=section_page"
    else:
        verdict = "BOX-DRIVER=none"
    print(f"VERDICT: {verdict}")
    print("  (real_release -> DISPLAY-only bug, safe; section_page -> verdict-adjacent, Export needed)")
    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
