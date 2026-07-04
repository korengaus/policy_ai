"""BACKFILL-VIOLATION-DIAG — READ-ONLY: show the exact copyright-violation row(s).

backfill_pilot_verify.py check (a) reported "violations: 1" for a tag but only prints
a 60-char teaser and the column name. This diagnostic re-runs the SAME check (a) — by
IMPORTING backfill_pilot_verify's exact helpers (WINDOW / STRIDE / _column_texts /
_body_overlap_hit) and the SAME body fetcher (article_extractor.fetch_article_body), so
the row it surfaces is byte-identical to the one the verify probe flagged — and, for each
violating row, prints the full judgment payload:

    id | title | overlapping_field | overlap_len | overlap_substring (~300 chars, ascii-safe)

WHY the extra fields matter: the key operator judgment is WHICH stored column overlapped.
An overlap inside ``claim_text`` (a designated extracted-fact column) is expected and
low-risk — a long factual sentence can legitimately coincide with the body. An overlap of
a 200+ char raw run inside any OTHER column (source_candidates / source_reliability_summary
/ debug_summary / ...) is a genuine retained-expression leak. This script names the column
and says which case it is.

DETECTION is unchanged: a row/column is flagged IFF backfill_pilot_verify._body_overlap_hit
returns a hit (the >=WINDOW sliding-window test). The only addition is MEASUREMENT of the
already-flagged overlap: from the flagged window we expand left/right to the maximal
contiguous body substring still present in that column's text, purely to report how long
the overlap really is and to show ~300 chars of it. That expansion invents no new check —
a row with no _body_overlap_hit is never reported.

verification_card is NOT a top-level column (it lives inside the JSON of debug_summary /
source_candidates); like the verify probe, this reads real columns via SELECT *, so the
column name printed is a genuine analysis_results column.

MEASUREMENT ONLY. SELECT-only on our DB (engine.connect(); no commit); the only network is
the same read-only body re-fetch the verify probe / pipeline use. No analysis, no writes, no
git, no row modification/deletion.

Usage:
    PYTHONPATH=. python scripts/backfill_violation_diag.py --selftest
    PYTHONPATH=. python scripts/backfill_violation_diag.py --tag backfill_scale_20260703
    PYTHONPATH=. python scripts/backfill_violation_diag.py --tag backfill_scale_20260703 --context 300

Exit codes: 0 = ran (or engine unavailable / selftest pass); 1 = selftest failed.
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

# Reuse the verify probe's EXACT copyright logic — never re-implement it. If these
# ever change in the probe, this diagnostic tracks them automatically.
from scripts.backfill_pilot_verify import (  # noqa: E402
    WINDOW,
    STRIDE,  # noqa: F401 — imported to document the shared stride; used via _body_overlap_hit
    _column_texts,
    _body_overlap_hit,
    _ascii,
)

DEFAULT_TAG = "backfill_scale_20260703"
DEFAULT_CONTEXT = 300  # how many chars of the overlapping substring to display

# Columns whose CONTENTS are designated extracted facts — an overlap here is expected
# and low-risk (a long factual sentence coinciding with the body). Any other column
# holding a >=WINDOW raw-body run is a genuine retained-expression leak.
_FACT_FIELDS = {"claim_text"}


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _maximal_overlap(body: str, text: str, window: str) -> str:
    """MEASUREMENT of an ALREADY-FLAGGED overlap (invents no new check): given a
    >=WINDOW body substring ``window`` known to appear in ``text``, expand it left/right
    to the maximal contiguous body substring still contained in ``text``. Returns that
    substring (>= len(window)). Bounded by len(body), so it always terminates."""
    i = body.find(window)
    if i < 0:  # defensive — window came from body, so this should not happen
        return window
    end = i + len(window)
    while end < len(body) and body[i:end + 1] in text:
        end += 1
    start = i
    while start > 0 and body[start - 1:end] in text:
        start -= 1
    return body[start:end]


def _hit_text_variant(body_window: str, texts: list[str]) -> str:
    """Return the specific text variant (raw or JSON-unescaped) that contains the flagged
    window, so the maximal-overlap expansion measures against the right string."""
    for text in texts:
        if body_window in text:
            return text
    return ""


def _field_note(col: str) -> str:
    if col in _FACT_FIELDS:
        return ("EXTRACTED-FACT field (expected, LOW risk) — a long factual sentence can "
                "legitimately coincide with the body; eyeball the text to confirm it reads "
                "as a claim, not a copied paragraph.")
    return ("NOT a designated fact field — a >=%d-char raw-body run here is a GENUINE "
            "retained-expression leak; this one row warrants tagged operator cleanup." % WINDOW)


def run_selftest() -> int:
    p("=== BACKFILL-VIOLATION-DIAG --selftest (offline; no DB, no network) ===")
    failures = []

    # Reuse the probe's detector on a planted excerpt, then confirm the measurement
    # expansion recovers (at least) the planted length from the escaped-JSON variant.
    body = "가" * 120 + "정책 발표 상세 내용 문장 " * 30 + "나" * 120
    planted = body[100:100 + 260]                       # a 260-char contiguous excerpt
    dirty_col = json.dumps({"note": "x" * 40 + planted + "y" * 15}, ensure_ascii=True)
    texts = _column_texts(dirty_col)
    hit = _body_overlap_hit(body, texts)                # the probe's exact detection
    if not hit:
        failures.append("shared detector missed the planted excerpt (probe logic drift?)")
    else:
        variant = _hit_text_variant(hit, texts)
        full = _maximal_overlap(body, variant, hit)
        if len(full) < 260:
            failures.append(f"maximal-overlap under-measured: got {len(full)} < planted 260")
        elif full not in variant:
            failures.append("maximal-overlap produced a substring not actually in the column text")
        else:
            p(f"  [ok] detect+measure: flagged window={len(hit)} chars -> full overlap "
              f"measured={len(full)} chars (planted 260), substring verified in-column.")

    # A clean column (short factual claims) must not be flagged.
    clean_col = json.dumps({"claim_text": ["짧은 사실 문장", "또 다른 짧은 문장"]}, ensure_ascii=False)
    if _body_overlap_hit(body, _column_texts(clean_col)):
        failures.append("clean short-claim column false-flagged")
    else:
        p("  [ok] clean short-claim column not flagged (no false positive).")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (shared probe detector + maximal-overlap measurement + no false positive)")
    return 0


def run_live(tag: str, context: int) -> int:
    p(f"=== BACKFILL-VIOLATION-DIAG (READ-ONLY) — tag={_ascii(tag)} window>={WINDOW} ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    # SAME tag match + SAME SELECT * as backfill_pilot_verify (real columns; verification_card
    # is not among them — it lives inside debug_summary/source_candidates JSON).
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT * FROM analysis_results "
                    "WHERE debug_summary LIKE :pat ORDER BY id")
            .bindparams(pat=f"%ingest_origin%{tag}%")
        ).all()

    p(f"  tagged rows found: {len(rows)}")
    if not rows:
        p("  Nothing to diagnose — is the tag correct / has the run happened?")
        return 0

    from article_extractor import fetch_article_body  # the SAME fetcher the verify probe uses

    violations = 0
    skipped = []
    for r in rows:
        m = dict(r._mapping)
        rid = m.get("id")
        url = str(m.get("original_url") or "")
        try:
            body = fetch_article_body(url) or ""
        except Exception:  # noqa: BLE001 — fail-soft, same as the verify probe
            body = ""
        if len(body) < WINDOW:
            skipped.append(rid)
            continue
        # First flagged column per row (matches the verify probe's break-on-first-hit).
        for col, val in m.items():
            texts = _column_texts(val)
            hit = _body_overlap_hit(body, texts)
            if not hit:
                continue
            variant = _hit_text_variant(hit, texts)
            full = _maximal_overlap(body, variant, hit) if variant else hit
            violations += 1
            title = str(m.get("title") or "")
            p("")
            p(f"  !! VIOLATION #{violations}")
            p(f"     id                : {rid}")
            p(f"     title             : {_ascii(title)}")
            p(f"     overlapping_field : {col}")
            p(f"     overlap_len       : {len(full)} chars (flagged window {WINDOW}; expanded to maximal contiguous)")
            p(f"     overlap_substring : {_ascii(full[:context])}")
            p(f"     field_note        : {_field_note(col)}")
            break

    p("")
    p(f"  === SUMMARY: {violations} violation row(s) | {len(skipped)} skipped (body short/unfetchable): {skipped} ===")
    if violations == 0:
        p("  No violation surfaced under this tag with the shared detector "
          "(if the verify probe reported one, re-check the tag / that the body still re-fetches).")
    p("[Safety] READ-ONLY diagnostic — SELECT-only + read-only body re-fetch; no rows written/updated/deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY: print the exact copyright-violation row(s) for a backfill tag, "
                    "reusing backfill_pilot_verify's exact overlap logic. --selftest for offline check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="ingest_origin tag to diagnose")
    parser.add_argument("--context", type=int, default=DEFAULT_CONTEXT,
                        help="chars of the overlapping substring to display (default 300)")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live(args.tag, max(1, int(args.context)))


if __name__ == "__main__":
    raise SystemExit(main())
