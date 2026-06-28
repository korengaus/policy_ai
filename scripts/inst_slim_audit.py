"""CARD-BOX-INST-SLIM Phase 1 — READ-ONLY slim-institution audit.

The homepage source box reads source_reliability_summary.top_official_institution
(the SLIM field). CARD-BOX-INST (commit f93c0b0) populates it from the genuine
match's publisher in the elif branch — NEW-rows-only (no backfill). This probe prints
the ACTUAL stored value the card reads, per genuine row, so we can tell whether:

  (a) it's the specific ministry (금융위원회 등)  -> fix working, card should show it;
  (b) it's (missing-key)/(empty)                -> row predates the fix; new-rows-only
                                                   fallback to document-only is correct;
  (c) it's "정책브리핑" or another value          -> a real discrepancy to chase.

created_at is printed so a row can be placed before/after the fix commit.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/inst_slim_audit.py
    PYTHONPATH=. python scripts/inst_slim_audit.py --limit 500 --show 40
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="inst_slim_audit")
    parser.add_argument("--limit", type=int, default=500, help="recent rows to scan for genuine ones")
    parser.add_argument("--show", type=int, default=40, help="genuine rows to print in full")
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
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    genuine = []
    for r in rows:
        summary = _parse_json(r.get("source_reliability_summary"))
        if not isinstance(summary, dict) or not bool(summary.get("has_genuine_official_support")):
            continue
        key_present = "top_official_institution" in summary
        raw_inst = summary.get("top_official_institution")
        if not key_present:
            bucket, disp = "missing_key", "(missing-key)"
        elif not str(raw_inst or "").strip():
            bucket, disp = "empty", "(empty)"
        else:
            bucket, disp = "specific", _trunc(raw_inst, 24)
        genuine.append({
            "id": r.get("id"),
            "title": _trunc(r.get("title"), 30),
            "inst_disp": disp,
            "bucket": bucket,
            "doc_title": _trunc(summary.get("top_official_detail_title"), 36),
            "genuine": bool(summary.get("has_genuine_official_support")),
            "created_at": _trunc(r.get("created_at"), 25),
        })

    total = len(genuine)
    counts = Counter(g["bucket"] for g in genuine)
    print("=" * 110)
    print(f"CARD-BOX-INST-SLIM audit — scanned {len(rows)} recent rows, {total} GENUINE "
          "(has_genuine_official_support=true)")
    print("=" * 110)
    print(
        f"TALLY: specific-value={counts.get('specific', 0)}  "
        f"(missing-key)={counts.get('missing_key', 0)}  "
        f"(empty)={counts.get('empty', 0)}   of {total} genuine rows"
    )
    print("(specific = fix working; missing-key/empty = pre-fix row -> document-only fallback is correct)")

    print("\n" + "-" * 110)
    hdr = f"{'id':>7} | {'title':30} | {'top_official_institution':24} | {'doc_title':36} | gen | created_at"
    print(hdr)
    print("-" * len(hdr))
    for g in genuine[: max(1, min(args.show, total or 1))]:
        print(f"{str(g['id']):>7} | {g['title']:30} | {g['inst_disp']:24} | {g['doc_title']:36} | "
              f"{'Y' if g['genuine'] else 'n':^3} | {g['created_at']}")

    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
