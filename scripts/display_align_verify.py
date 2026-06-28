"""DISPLAY-ALIGN-VERIFY Phase 1 — READ-ONLY post-fix slim-institution check.

DISPLAY-ALIGN makes the slim top_official_body_match prefer the box-driving
strong/primary candidate, so the shown institution+document match the real press
release (e.g. 금융위원회 + the real document) instead of a weaker pick (정책브리핑 /
상단주요뉴스). It is NEW-rows-only: rows analyzed BEFORE the deploy keep their stale
slim until re-analyzed — expected, not a bug.

This probe lists the most recent genuine rows NEWEST-FIRST (by created_at) and, per
row, prints the stored slim top_official_institution + top_official_detail_title and
whether the institution looks like a SPECIFIC ministry (Korean, ends with
부/처/청/위원회/원/실/공사/공단/은행) vs a GENERIC platform (Korea Policy Briefing /
정책브리핑 / empty). The script does NOT know the deploy time — compare created_at
manually against the DISPLAY-ALIGN commit time.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell:
    git log --oneline -1
    PYTHONPATH=. python scripts/display_align_verify.py
    PYTHONPATH=. python scripts/display_align_verify.py --limit 500 --show 40
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

# A specific Korean government issuer usually ends in one of these morphemes.
_MINISTRY_TAIL = re.compile(r"(부|처|청|위원회|원|실|공사|공단|은행|진흥원|관리원|연구원)$")
# Generic platform/aggregator labels (NOT a specific ministry).
GENERIC_PLATFORM = {"korea policy briefing", "정책브리핑", "대한민국 정책브리핑", "korea.kr"}
_HANGUL = re.compile(r"[가-힣]")


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


def _trunc(v, n=100) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _classify_inst(inst_raw, key_present):
    raw = str(inst_raw or "").strip()
    if not key_present:
        return "missing_key", "(missing-key)"
    if not raw:
        return "empty", "(empty)"
    if raw.lower() in GENERIC_PLATFORM:
        return "generic_platform", _trunc(raw, 24)
    if _HANGUL.search(raw) and _MINISTRY_TAIL.search(raw):
        return "specific_ministry", _trunc(raw, 24)
    if _HANGUL.search(raw):
        return "korean_other", _trunc(raw, 24)
    return "other", _trunc(raw, 24)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="display_align_verify")
    parser.add_argument("--limit", type=int, default=500, help="recent rows to scan (by created_at DESC)")
    parser.add_argument("--show", type=int, default=40, help="genuine rows to print")
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
            "SELECT id, title, source_reliability_summary, created_at "
            "FROM analysis_results ORDER BY created_at DESC NULLS LAST, id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    genuine = []
    for r in rows:
        summary = _parse_json(r.get("source_reliability_summary"))
        if not isinstance(summary, dict) or not bool(summary.get("has_genuine_official_support")):
            continue
        key_present = "top_official_institution" in summary
        bucket, disp = _classify_inst(summary.get("top_official_institution"), key_present)
        genuine.append({
            "id": r.get("id"),
            "created_at": _trunc(r.get("created_at"), 25),
            "title": _trunc(r.get("title"), 30),
            "inst_disp": disp,
            "bucket": bucket,
            "doc_title": _trunc(summary.get("top_official_detail_title"), 38),
        })

    total = len(genuine)
    counts = Counter(g["bucket"] for g in genuine)
    print("=" * 116)
    print(f"DISPLAY-ALIGN-VERIFY — scanned {len(rows)} recent rows (created_at DESC), "
          f"{total} GENUINE")
    print("=" * 116)
    print(
        f"TALLY: specific_ministry={counts.get('specific_ministry', 0)}  "
        f"generic_platform={counts.get('generic_platform', 0)}  "
        f"korean_other={counts.get('korean_other', 0)}  "
        f"(empty)={counts.get('empty', 0)}  (missing-key)={counts.get('missing_key', 0)}"
    )
    print("NOTE: this script does NOT know the DISPLAY-ALIGN deploy time — compare each")
    print("      created_at manually. Rows BEFORE the deploy keep their stale slim (normal).")

    print("\n" + "-" * 116)
    hdr = (f"{'id':>7} | {'created_at':25} | {'title':30} | "
           f"{'top_official_institution':24} | class")
    print(hdr)
    print("-" * len(hdr))
    for g in genuine[: max(1, min(args.show, total or 1))]:
        print(f"{str(g['id']):>7} | {g['created_at']:25} | {g['title']:30} | "
              f"{g['inst_disp']:24} | {g['bucket']}")
        print(f"          doc: {g['doc_title']!r}")

    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
