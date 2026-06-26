"""DESIGN-3B-1e — LABEL/BOX-ONLY backfill of has_genuine_official_support (strong-only).

DESIGN-3B-1d tightened has_genuine_official_support to STRONG-only for NEW analyses,
but STORED rows still carry the OLD (loose) flag, so already-saved homepage cards
still show spurious "✓ 공식 근거 확인" boxes (e.g. welfare claim ↔ 해양수산부 연안선박).
This script recomputes the flag from each stored row's source_candidates under the
EXACT strong-only predicate and, on --apply, sets ONLY
source_reliability_summary.has_genuine_official_support = false for rows that
currently DISPLAY genuine but are NOT strong-genuine.

SCOPE (hard): writes ONLY the one JSON key inside source_reliability_summary. It
does NOT touch verdict_label, policy_alert_level, policy_confidence_score,
verification_strength, truth_claim, operator_review_required, or any other column.
It only ever sets the flag to FALSE (strict ⊆ old, so no false→true is possible).

SAFETY:
  * DEFAULT = DRY-RUN (no writes). A real write requires BOTH --apply AND --confirm.
  * IDEMPOTENT: re-running changes nothing (already-false rows are skipped).
  * RESUMABLE / batched: processes by id, commits per batch, prints progress.
  * Reads the FULL stored row (source_candidates present), NOT the slim payload.
  * Never prints DATABASE_URL or secrets.

★ Take a fresh Export backup immediately BEFORE running --apply.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/genuine_backfill.py                 # dry-run (default)
    PYTHONPATH=. python scripts/genuine_backfill.py --apply --confirm   # real write (Phase 2)
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

# REUSE the authoritative primary-document predicate (do NOT re-implement it).
from official_evidence_resolution import extract_primary_document_match

STRONG = "strong_official_direct_support"


def _strict_genuine(source_candidates) -> bool:
    """EXACT mirror of verification_card.py's strong-only _has_genuine_official_support
    (DESIGN-3B-1d): a strong primary-document match OR any candidate with
    official_body_match AND classification == strong_official_direct_support."""
    strong_count = sum(
        1
        for source in (source_candidates or [])
        if isinstance(source, dict)
        and source.get("official_body_match")
        and (
            source.get("official_evidence_classification")
            or source.get("official_direct_match_classification")
        )
        == STRONG
    )
    return bool(
        extract_primary_document_match(source_candidates or []) is not None
        or strong_count > 0
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


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _trunc(v, n=80):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="genuine_backfill")
    parser.add_argument("--apply", action="store_true", help="actually write (requires --confirm too)")
    parser.add_argument("--confirm", action="store_true", help="second gate; required with --apply")
    parser.add_argument("--batch", type=int, default=200, help="rows per commit batch")
    parser.add_argument("--limit", type=int, default=100000, help="max rows to scan")
    parser.add_argument("--show", type=int, default=20, help="flips to print")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    write_mode = bool(args.apply and args.confirm)
    if args.apply and not args.confirm:
        print("REFUSING TO WRITE: --apply requires --confirm. Running DRY-RUN instead.\n", file=sys.stderr)

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    print("=" * 88)
    print(f"DESIGN-3B-1e genuine backfill — mode={'APPLY (WRITING)' if write_mode else 'DRY-RUN (no writes)'}")
    print("=" * 88)
    if write_mode:
        print("!! WRITE MODE: setting source_reliability_summary.has_genuine_official_support=false")
        print("!! for spurious rows. Ensure a fresh Export backup was just taken.\n")

    limit = max(1, min(args.limit, 1000000))
    total = scanned = 0
    stored_true = absent_key = strict_true = 0
    flips = []                # currently display-genuine but NOT strong  -> set false
    flip_from_true = flip_from_fallback = 0
    false_to_true = []        # MUST stay empty (strict ⊆ old); flag if any
    proof_printed = False
    updated = 0

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, claim_text, source_reliability_summary, source_candidates, debug_summary "
            "FROM analysis_results ORDER BY id ASC LIMIT :n"
        ), {"n": limit}).mappings().all()

        pending = []  # (id, new_srs_json) for the current batch
        for r in rows:
            scanned += 1
            srs = _parse_json(r["source_reliability_summary"])
            if not isinstance(srs, dict):
                continue                      # can't recompute/write safely
            total += 1
            cands = _parse_json(r["source_candidates"]) or []
            debug = _parse_json(r["debug_summary"]) or {}
            debug = debug if isinstance(debug, dict) else {}

            strict = _strict_genuine(cands)
            if strict:
                strict_true += 1
            stored_flag = srs.get("has_genuine_official_support")
            has_bool = isinstance(stored_flag, bool)
            if has_bool and stored_flag:
                stored_true += 1
            if not has_bool:
                absent_key += 1

            # current on-screen genuine = stored boolean, else the frontend fallback.
            effective = stored_flag if has_bool else (_num(debug.get("official_body_matches")) > 0)

            # sanity: strict ⊆ old -> a row that is strict-true must already display genuine.
            if strict and not effective:
                false_to_true.append(r["id"])

            needs_fix = bool(effective) and (not strict)
            if not needs_fix:
                continue

            # one-key-only PROOF on the first flip (dry-run visibility)
            if not proof_printed:
                proof_printed = True
                before_keys = sorted(srs.keys())
                new_preview = dict(srs)
                new_preview["has_genuine_official_support"] = False
                after_keys = sorted(new_preview.keys())
                print("--- ONE-KEY-ONLY PROOF (sample flip row id=%s) ---" % r["id"])
                print(f"    keys BEFORE ({len(before_keys)}): {before_keys}")
                print(f"    keys AFTER  ({len(after_keys)}): {after_keys}")
                print(f"    key-set identical except target: "
                      f"{set(before_keys) == set(after_keys) or set(after_keys) - set(before_keys) == {'has_genuine_official_support'}}")
                print(f"    has_genuine_official_support: {stored_flag!r} -> False")
                changed = [k for k in after_keys if srs.get(k) != new_preview.get(k)]
                print(f"    VALUES changed: {changed}  (must be exactly ['has_genuine_official_support'])\n")

            flips.append({
                "id": r["id"],
                "claim": _trunc(r["claim_text"], 80),
                "doc": _trunc(srs.get("top_official_detail_title"), 70),
                "clf": srs.get("official_direct_match_classification"),
                "from": "stored_true" if (has_bool and stored_flag) else "fallback",
            })
            if has_bool and stored_flag:
                flip_from_true += 1
            else:
                flip_from_fallback += 1

            if write_mode:
                new_srs = dict(srs)
                new_srs["has_genuine_official_support"] = False
                pending.append((r["id"], json.dumps(new_srs, ensure_ascii=False)))
                if len(pending) >= args.batch:
                    with conn.begin():
                        for rid, blob in pending:
                            conn.execute(sa.text(
                                "UPDATE analysis_results SET source_reliability_summary = :srs WHERE id = :id"
                            ), {"srs": blob, "id": rid})
                    updated += len(pending)
                    print(f"    ... committed batch, total updated={updated}")
                    pending = []

        if write_mode and pending:
            with conn.begin():
                for rid, blob in pending:
                    conn.execute(sa.text(
                        "UPDATE analysis_results SET source_reliability_summary = :srs WHERE id = :id"
                    ), {"srs": blob, "id": rid})
            updated += len(pending)
            print(f"    ... committed final batch, total updated={updated}")

    print("\n" + "=" * 88)
    print("TALLY")
    print("=" * 88)
    print(f"  rows scanned / with parseable summary: {scanned} / {total}")
    print(f"  stored has_genuine=true (explicit):    {stored_true}")
    print(f"  stored flag ABSENT (frontend fallback): {absent_key}")
    print(f"  strong-only genuine (kept):            {strict_true}")
    print(f"  WOULD FLIP -> false (spurious display): {len(flips)}")
    print(f"      from explicit stored true: {flip_from_true}")
    print(f"      from absent-key fallback : {flip_from_fallback}")
    print(f"  UNEXPECTED false->true (must be 0):    {len(false_to_true)} {false_to_true[:10]}")
    print(f"  0 verdict fields touched (only source_reliability_summary.has_genuine_official_support)")

    print(f"\n--- {min(args.show, len(flips))} flips (EYEBALL: spurious off-topic? real strong KEPT?) ---")
    for f in flips[:args.show]:
        print(f"  id={f['id']} from={f['from']} clf={f['clf']}")
        print(f"     CLAIM   : {f['claim']}")
        print(f"     MATCHED : {f['doc']}")

    print("\n" + "=" * 88)
    if write_mode:
        print(f"APPLIED — updated {updated} rows (has_genuine true→false); 0 verdict fields touched.")
    else:
        print(f"DRY-RUN — WOULD UPDATE {len(flips)} rows (has_genuine true→false); 0 verdict fields touched.")
        print("No DB writes performed. Phase 2 write: --apply --confirm (after a fresh Export backup).")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
