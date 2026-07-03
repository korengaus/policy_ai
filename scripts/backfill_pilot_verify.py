"""BACKFILL-PILOT-VERIFY — READ-ONLY acceptance check for the bounded backfill pilot.

Runs AFTER Joe's Worker-Shell pilot (backfill_orchestrator.py --run). Verifies the four
acceptance criteria from _backfill_orch_design.md §4 over the rows tagged
``debug_summary.ingest_origin = 'backfill_pilot'``:

  (a) COPYRIGHT — no stored column contains a contiguous >=200-char substring of the
      article's original body. The body was DISCARDED at analysis time (structural), so
      the check re-fetches it read-only via the same ``fetch_article_body`` the pipeline
      used and window-scans every column value — both the raw stored text AND the
      JSON-unescaped variant (stored JSON is ensure_ascii-escaped, so a Korean substring
      would otherwise hide from a raw scan). Fetch failure => that row's check is
      reported SKIPPED (fail-soft), not passed.
  (b) VERDICT SANITY — every pilot row has review_status == 'ai_draft_pending_human_review'
      (the persisted operator-review invariant; per-row ``truth_claim`` does not exist —
      it lives in the source-registry table, forced 0 on every row by contract), AND no
      politician-marker title carries a confident-supportive verdict
      (draft_verified / draft_likely_true).
  (c) LABELS — domain is present (post-analysis classifier); content_nature is REPORTED
      (may be NULL when CONTENT_NATURE_ENABLED was off during the pilot — reported, not failed).
  (d) DEDUPE — tagged rows <= the pilot cap; no duplicate original_url among them; and
      every tagged URL now hits result_exists_by_url — i.e. a re-run of the same pilot
      skips 100% of them at gate 3 BEFORE any LLM spend => 0 new rows. (This proves the
      dedupe key without re-spending; an actual re-run is Joe's optional double-check.)

MEASUREMENT ONLY. SELECT-only on our DB (engine.connect(); no commit); the only network
is the read-only body re-fetch for check (a). No analysis, no writes, no git.

Usage:
    PYTHONPATH=. python scripts/backfill_pilot_verify.py                    # live checks
    PYTHONPATH=. python scripts/backfill_pilot_verify.py --selftest         # offline, no DB/net
    PYTHONPATH=. python scripts/backfill_pilot_verify.py --tag backfill_pilot --cap 30

Exit codes: 0 = all checks pass (or engine unavailable / selftest pass);
            1 = selftest failed OR an acceptance check FAILED.
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

DEFAULT_TAG = "backfill_pilot"
DEFAULT_CAP = 30

# Copyright check: window size (the assertion threshold) and stride.
WINDOW = 200
STRIDE = 100

REVIEW_STATUS_EXPECTED = "ai_draft_pending_human_review"
SUPPORTIVE_CONFIDENT_LABELS = ("draft_verified", "draft_likely_true")

# ELECTION/POLITICIAN subset (identical to the POLLEAK probes; validated against the
# live hot_topics._DENYLIST at --selftest) — used for check (b)'s person guard.
ELECTION_MARKERS = (
    "선거", "당선", "득표", "지방선거", "여당", "야당", "대선", "총선", "공천", "탄핵",
    "이재명", "윤석열", "한동훈", "이준석", "김건희",
)


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _column_texts(value) -> list[str]:
    """The searchable text variants of one stored column value: the raw string AND,
    when it parses as JSON, the ensure_ascii=False re-dump (stored JSON escapes Korean
    as \\uXXXX, which would hide a Korean body substring from a raw scan)."""
    if value is None:
        return []
    raw = str(value)
    out = [raw]
    if raw and raw[:1] in "[{\"":
        try:
            out.append(json.dumps(json.loads(raw), ensure_ascii=False))
        except Exception:  # noqa: BLE001 — non-JSON text columns are fine
            pass
    return out


def _body_overlap_hit(body: str, texts: list[str]) -> str:
    """Return the first >=WINDOW-char contiguous body substring found in any stored
    text ('' when none). Windows slide by STRIDE; the tail window is included."""
    body = str(body or "")
    if len(body) < WINDOW or not texts:
        return ""
    starts = list(range(0, len(body) - WINDOW + 1, STRIDE))
    tail = len(body) - WINDOW
    if starts and starts[-1] != tail:
        starts.append(tail)
    for s in starts:
        window = body[s:s + WINDOW]
        for text in texts:
            if window in text:
                return window
    return ""


def _markers_in(text: str) -> list[str]:
    t = str(text or "")
    return [mk for mk in ELECTION_MARKERS if mk in t]


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== BACKFILL-PILOT-VERIFY --selftest (offline; no DB, no network) ===")
    failures = []

    # 1. ELECTION_MARKERS ⊆ live denylist (reuse, not re-implementation).
    try:
        from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
        missing = [mk for mk in ELECTION_MARKERS if mk not in set(_HT_DENYLIST)]
        if missing:
            failures.append(f"ELECTION_MARKERS not in live _DENYLIST: {[_ascii(x) for x in missing]}")
        else:
            p(f"  [ok] ELECTION_MARKERS ({len(ELECTION_MARKERS)}) ⊆ live hot_topics._DENYLIST.")
    except Exception as exc:  # noqa: BLE001
        p(f"  [note] could not import hot_topics offline ({str(exc)[:80]}).")

    # 2. Copyright window detector — planted overlap detected, clean row passes.
    body = "가" * 150 + "정책 발표 내용 " * 40 + "나" * 150          # >600 chars
    planted = body[100:100 + 250]                                     # a 250-char excerpt
    dirty_col = json.dumps({"note": "x" * 50 + planted + "y" * 20}, ensure_ascii=True)
    clean_col = json.dumps({"claims": ["짧은 사실 문장 하나", "둘"]}, ensure_ascii=True)
    hit = _body_overlap_hit(body, _column_texts(dirty_col))
    miss = _body_overlap_hit(body, _column_texts(clean_col))
    if not hit:
        failures.append("copyright detector missed a planted 250-char excerpt inside escaped JSON")
    if miss:
        failures.append("copyright detector false-positived on a clean column")
    p(f"  [{'ok' if not failures else 'xx'}] copyright detector: planted={'HIT' if hit else 'MISS'}, "
      f"clean={'clean' if not miss else 'FALSE-HIT'} (window={WINDOW}, escaped-JSON variant scanned)")

    # 3. Short factual claim sentences (< WINDOW chars) never trip the detector.
    short_claim_col = json.dumps({"claim_text": body[:150]}, ensure_ascii=False)
    if _body_overlap_hit(body, _column_texts(short_claim_col)):
        failures.append("detector tripped on a <200-char factual excerpt (should be allowed)")
    else:
        p("  [ok] <200-char factual excerpts (claim sentences) do not trip the assertion.")

    # 4. Verdict-sanity + dedupe logic on synthetic rows.
    rows = [
        {"id": 1, "title": "복지 예산 확대 발표", "verdict_label": "draft_needs_review",
         "review_status": REVIEW_STATUS_EXPECTED, "original_url": "https://a.example/1"},
        {"id": 2, "title": "이재명 정부 복지 예산 평가", "verdict_label": "draft_verified",
         "review_status": REVIEW_STATUS_EXPECTED, "original_url": "https://a.example/2"},
    ]
    person_supportive = [r["id"] for r in rows
                         if _markers_in(r["title"]) and r["verdict_label"] in SUPPORTIVE_CONFIDENT_LABELS]
    if person_supportive != [2]:
        failures.append(f"person-supportive flag logic wrong: {person_supportive}")
    review_bad = [r["id"] for r in rows if r["review_status"] != REVIEW_STATUS_EXPECTED]
    if review_bad:
        failures.append(f"review_status logic wrong: {review_bad}")
    urls = [r["original_url"] for r in rows]
    if len(urls) != len(set(urls)):
        failures.append("dupe-url logic wrong on unique synthetic rows")
    p(f"  [{'ok' if len(person_supportive) == 1 else 'xx'}] verdict-sanity: politician+supportive "
      f"correctly flagged (synthetic id=2); review_status + dupe-url logic checked.")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (denylist subset + copyright detector + short-excerpt allowance + verdict/dedupe logic)")
    return 0


# ---------------------------------------------------------------------------
# LIVE RUN (SELECT-only + read-only body re-fetch)
# ---------------------------------------------------------------------------
def run_live(tag: str, cap: int) -> int:
    p(f"=== BACKFILL-PILOT-VERIFY (READ-ONLY) — tag={_ascii(tag)} cap={cap} ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    # Robust tag match (separator-agnostic LIKE on the TEXT debug_summary column).
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT * FROM analysis_results "
                    "WHERE debug_summary LIKE :pat ORDER BY id")
            .bindparams(pat=f"%ingest_origin%{tag}%")
        ).all()

    p(f"  tagged pilot rows found: {len(rows)}")
    if not rows:
        p("  Nothing to verify — has the pilot run yet?")
        return 0

    failed = False

    # ---- (a) COPYRIGHT ------------------------------------------------------
    p("")
    p(f"=== (a) COPYRIGHT — no column holds a >={WINDOW}-char body substring ===")
    from article_extractor import fetch_article_body  # the SAME fetcher the pipeline used
    copy_violations, copy_skipped = [], []
    for r in rows:
        m = dict(r._mapping)
        url = str(m.get("original_url") or "")
        body = ""
        try:
            body = fetch_article_body(url) or ""
        except Exception:  # noqa: BLE001
            body = ""
        if len(body) < WINDOW:
            copy_skipped.append(m["id"])
            continue
        for col, val in m.items():
            hit = _body_overlap_hit(body, _column_texts(val))
            if hit:
                copy_violations.append((m["id"], col))
                p(f"    !! id={m['id']} column={col} holds a body excerpt: {_ascii(hit[:60])}...")
                break
    p(f"  violations: {len(copy_violations)}  |  skipped (body unfetchable/short): {len(copy_skipped)} {copy_skipped}")
    if copy_violations:
        failed = True
        p("  => (a) FAIL — protected expression retained; STOP and review before any scale.")
    else:
        p(f"  => (a) PASS on {len(rows) - len(copy_skipped)} checked rows"
          + (" (skipped rows need manual eyeball)" if copy_skipped else ""))

    # ---- (b) VERDICT SANITY --------------------------------------------------
    p("")
    p("=== (b) VERDICT SANITY ===")
    review_bad = [r._mapping["id"] for r in rows
                  if str(r._mapping.get("review_status") or "") != REVIEW_STATUS_EXPECTED]
    person_supportive = [
        (r._mapping["id"], r._mapping.get("verdict_label"))
        for r in rows
        if _markers_in(r._mapping.get("title"))
        and str(r._mapping.get("verdict_label") or "") in SUPPORTIVE_CONFIDENT_LABELS
    ]
    p(f"  review_status != '{REVIEW_STATUS_EXPECTED}': {review_bad or 'none'}")
    p(f"  politician-marker title with confident-supportive verdict: {person_supportive or 'none'}")
    p("  (note: per-row truth_claim does not exist; the source-registry table forces truth_claim=0 by contract)")
    if review_bad or person_supportive:
        failed = True
        p("  => (b) FAIL")
    else:
        p("  => (b) PASS")

    # ---- (c) LABELS ----------------------------------------------------------
    p("")
    p("=== (c) LABELS (domain required; content_nature reported) ===")
    no_domain = [r._mapping["id"] for r in rows if not r._mapping.get("domain")]
    cn_present = sum(1 for r in rows if r._mapping.get("content_nature"))
    p(f"  rows missing domain: {no_domain or 'none'}")
    p(f"  content_nature present: {cn_present}/{len(rows)} "
      f"(NULL is expected if CONTENT_NATURE_ENABLED was off during the pilot)")
    if no_domain:
        failed = True
        p("  => (c) FAIL (domain missing — was CLASSIFY_ENABLED on for the pilot shell?)")
    else:
        p("  => (c) PASS")

    # ---- (d) DEDUPE ----------------------------------------------------------
    p("")
    p("=== (d) DEDUPE (a re-run would add 0 new rows) ===")
    from database import result_exists_by_url
    urls = [str(r._mapping.get("original_url") or "") for r in rows]
    dupes = len(urls) - len(set(urls))
    over_cap = len(rows) > cap
    all_exist = all(result_exists_by_url(u) for u in urls if u)
    p(f"  tagged rows {len(rows)} <= cap {cap}: {'YES' if not over_cap else 'NO'}")
    p(f"  duplicate original_url among tagged rows: {dupes}")
    p(f"  every tagged URL now hits result_exists_by_url (gate 3 skips it pre-spend): "
      f"{'YES' if all_exist else 'NO'}")
    p("  (dedupe keys: gate-3 result_exists_by_url pre-LLM + in-run make_article_id + "
      "save_analysis_result's duplicate backstop)")
    if over_cap or dupes or not all_exist:
        failed = True
        p("  => (d) FAIL")
    else:
        p("  => (d) PASS — re-running the same pilot creates 0 new rows.")

    p("")
    p(f"=== OVERALL: {'FAIL — see checks above' if failed else 'ALL CHECKS PASS'} ===")
    p("[Safety] READ-ONLY verify — no rows written/updated/deleted; only SELECTs + read-only body re-fetch.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY acceptance verify for the backfill pilot. "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="ingest_origin tag to verify")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP, help="the pilot's hard item cap")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live(args.tag, args.cap)


if __name__ == "__main__":
    raise SystemExit(main())
