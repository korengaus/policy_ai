"""EVIDENCE-AUDIT Phase 1 — READ-ONLY forensic probe.

Two concerns, both diagnosed by reading STORED rows (no writes, no verdict path):

  PART A — IBK card evidence provenance: is the ~90 신뢰도 / "공식 근거 확인"
           backed by a PRIMARY official document (정책브리핑 M21 marker
           policy_briefing_news_item_id / 법제처 M23 marker national_law_mst),
           or by the originating news (cbci.co.kr) / other news echoing it?

  PART B — opinion/column detection corpus-wide: how many 칼럼/사설/기고/… pieces
           are in the corpus, how they're currently scored, and whether any code
           already filters them (it does NOT — see report).

SELECT / read-only ONLY. No UPDATE/INSERT/DELETE. Pin-OUT (new scripts/ file).

Run in the Render Worker Shell after confirming the deploy commit:

    git log --oneline -1
    PYTHONPATH=. python scripts/evidence_audit.py
    PYTHONPATH=. python scripts/evidence_audit.py --limit 200 --idxno 583625

Code facts this probe relies on (verified in repo, quoted in the report):
  * official_evidence_resolution.py:464
      _PRIMARY_DOCUMENT_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
  * providers/fss_press_release.py:515,550
      fss_bodo_content_id is DEDUP/PROVENANCE ONLY — NOT a primary-document marker.
  * main.js officialStatusLabel: "공식 근거 확인" fires when
      source_reliability_summary.official_detail_available OR
      debug_summary.official_body_matches > 0  — neither requires a primary marker.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---- marker / classification constants (mirror the verdict-path source) ----
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
FSS_MARKER_FIELD = "fss_bodo_content_id"

# Conservative OPINION title markers (substring match). High-precision set:
# these brackets/words are editorially unambiguous as opinion/column genre.
OPINION_TITLE_MARKERS = (
    "칼럼", "사설", "기고", "시론", "논단", "논평", "시평", "기자수첩", "데스크",
    "오피니언", "여적", "발언대", "왜냐면", "직설", "특별기고", "독자기고",
    "[사설]", "[칼럼]", "[기고]", "[시론]", "[기자수첩]", "[데스크]", "[독자",
)
# Bracket-author-column patterns: a [...] containing one of these genre words,
# e.g. "[전성인 칼럼]", "[OOO의 시선]", "[OOO의 창]".
OPINION_BRACKET_WORDS = ("칼럼", "시선", "시각", "창", "단상", "시평", "논단")
# URL path markers for opinion sections.
OPINION_URL_MARKERS = ("/opinion", "/column", "/editorial", "/sasul", "/oped", "/cln", "opinion/")

# AMBIGUOUS markers we DELIBERATELY DO NOT count as opinion (false-positive risk):
# these appear in plain fact reporting too.
AMBIGUOUS_MARKERS = ("주장", "분석", "전망", "진단", "해설", "인터뷰", "기획", "이슈")

# Official (non-press) host hints.
OFFICIAL_HOST_HINTS = (".go.kr", "korea.kr", "law.go.kr", "moleg.go.kr", "fss.or.kr", "fsc.go.kr")


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


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _classify_source(src: dict) -> str:
    """PRIMARY_OFFICIAL / FSS_OFFICIAL / OFFICIAL_SEARCH_URL / OFFICIAL_OTHER /
    NEWS / OTHER — by the same markers the verdict path keys on."""
    if not isinstance(src, dict):
        return "OTHER"
    for f in PRIMARY_MARKER_FIELDS:
        if str(src.get(f) or "").strip():
            return "PRIMARY_OFFICIAL"
    if str(src.get(FSS_MARKER_FIELD) or "").strip():
        return "FSS_OFFICIAL"
    url = str(src.get("url") or src.get("source_url") or "")
    host = _host(url)
    if "search.do" in url or url.rstrip("/").endswith("/search"):
        return "OFFICIAL_SEARCH_URL"
    stype = str(src.get("source_type") or "")
    if any(h in host for h in OFFICIAL_HOST_HINTS) or stype == "official_government":
        return "OFFICIAL_OTHER"
    if host:
        return "NEWS"
    return "OTHER"


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


def _src_brief(src: dict) -> str:
    title = str(src.get("title") or src.get("source_title") or "")[:70]
    url = str(src.get("url") or src.get("source_url") or "")
    host = _host(url)
    cls = _classify_source(src)
    markers = [f for f in (*PRIMARY_MARKER_FIELDS, FSS_MARKER_FIELD) if str(src.get(f) or "").strip()]
    score = src.get("score") or src.get("semantic_match_score") or ""
    clf = src.get("classification") or ""
    return (f"    [{cls:18}] host={host:24} score={score} clf={clf} "
            f"markers={markers}\n        title={title}")


# ===========================================================================
# PART A — IBK card provenance
# ===========================================================================
def part_a(conn, sa, idxno: str) -> None:
    print("=" * 78)
    print("PART A — IBK CARD EVIDENCE PROVENANCE")
    print("=" * 78)

    # 0a — locate the row.
    rows = conn.execute(sa.text(
        "SELECT id, title, original_url, created_at FROM analysis_results "
        "WHERE original_url LIKE :u OR title LIKE :t1 "
        "ORDER BY id DESC LIMIT 10"
    ), {"u": f"%idxno={idxno}%", "t1": "%IBK기업은행%"}).mappings().all()

    if not rows:
        print(f"NO ROW matched original_url idxno={idxno} or title '%IBK기업은행%'.")
        return
    print(f"Candidate rows ({len(rows)}):")
    for r in rows:
        print(f"  id={r['id']}  {r['created_at']}  {str(r['title'])[:70]}")
        print(f"      url={r['original_url']}")
    chosen_id = None
    for r in rows:
        if f"idxno={idxno}" in str(r["original_url"] or ""):
            chosen_id = r["id"]
            break
    note = ""
    if chosen_id is None:
        chosen_id = rows[0]["id"]
        note = " (URL idxno did NOT match — using most recent title match; SAID SO)"
    print(f"\n>>> CHOSEN row id={chosen_id}{note}\n")

    # 1a — verdict numbers (stored).
    full_cols = (
        "id, title, original_url, created_at, domain, policy_confidence_score, "
        "verdict_confidence, verdict_label, policy_alert_level, review_status, "
        "source_reliability_score, evidence_summary, evidence_sources, "
        "source_candidates, evidence_snippets, claim_evidence_map, "
        "source_reliability_summary, debug_summary"
    )
    row = conn.execute(sa.text(
        f"SELECT {full_cols} FROM analysis_results WHERE id = :i"
    ), {"i": chosen_id}).mappings().first()

    print("--- 1a VERDICT NUMBERS (stored) ---")
    for k in ("id", "title", "original_url", "created_at", "domain",
              "policy_confidence_score", "verdict_confidence", "verdict_label",
              "policy_alert_level", "review_status", "source_reliability_score"):
        print(f"  {k:26}= {row[k]}")
    print(f"  evidence_summary          = {str(row['evidence_summary'])[:300]}")

    # 2a — matched evidence.
    ev_sources = _parse_json(row["evidence_sources"]) or []
    candidates = _parse_json(row["source_candidates"]) or []
    snippets = _parse_json(row["evidence_snippets"]) or []
    cem = _parse_json(row["claim_evidence_map"]) or {}
    srs = _parse_json(row["source_reliability_summary"]) or {}
    debug = _parse_json(row["debug_summary"]) or {}

    print("\n--- 2a MATCHED OFFICIAL EVIDENCE ---")
    print(f"evidence_sources: {len(ev_sources) if isinstance(ev_sources, list) else 'n/a'} | "
          f"source_candidates: {len(candidates) if isinstance(candidates, list) else 'n/a'} | "
          f"evidence_snippets: {len(snippets) if isinstance(snippets, list) else 'n/a'} | "
          f"claim_evidence_map keys: {list(cem.keys()) if isinstance(cem, dict) else 'n/a'}")

    cls_counter: Counter = Counter()
    origin_host = _host(str(row["original_url"] or ""))
    news_domain_in_evidence = []
    primary_present = False

    def _walk(label, arr):
        nonlocal primary_present
        if not isinstance(arr, list):
            return
        print(f"\n  {label} ({len(arr)}):")
        for src in arr:
            if not isinstance(src, dict):
                continue
            cls = _classify_source(src)
            cls_counter[cls] += 1
            if cls == "PRIMARY_OFFICIAL":
                primary_present = True
            h = _host(str(src.get("url") or src.get("source_url") or ""))
            if cls in ("NEWS", "OTHER") and h:
                news_domain_in_evidence.append(h)
            print(_src_brief(src))

    _walk("evidence_sources", ev_sources)
    _walk("source_candidates", candidates)

    print("\n  CLASS COUNTS (evidence_sources + source_candidates):")
    for k, v in cls_counter.most_common():
        print(f"    {k:20} {v}")
    print(f"\n  originating news host = {origin_host}")
    print(f"  news/other hosts found in matched evidence: {sorted(set(news_domain_in_evidence)) or 'NONE'}")
    print(f"  originating host present in evidence? "
          f"{'YES' if origin_host and origin_host in news_domain_in_evidence else 'NO'}")
    print(f"  ANY primary-official document attached (policy_briefing_news_item_id "
          f"/ national_law_mst)? {'YES' if primary_present else 'NO'}")

    # 3a — primary-document presence from debug_summary.
    print("\n--- 3a PRIMARY-DOCUMENT PRESENCE (debug_summary scalars) ---")
    interesting = sorted(k for k in (debug.keys() if isinstance(debug, dict) else [])
                         if any(t in k for t in ("official", "policy_brief", "primary",
                                                 "law", "fss", "body", "direct", "match")))
    if interesting:
        for k in interesting:
            print(f"  {k:42}= {debug.get(k)}")
    else:
        print("  (no official/primary/law/fss/body keys found in debug_summary)")
    print(f"  source_reliability_summary.official_detail_available = {srs.get('official_detail_available')}")
    print(f"  source_reliability_summary.official_candidate_count  = {srs.get('official_candidate_count')}")

    # 4a / 5a — label driver + lane inference.
    label = _official_status_label(srs, debug)
    print("\n--- 4a/5a LABEL DRIVER + LANE INFERENCE ---")
    print(f"  officialStatusLabel(...) reproduces as: {label}")
    print(f"  label requires a PRIMARY marker? NO — it fires on "
          f"official_detail_available OR official_body_matches>0.")
    if primary_present:
        print("  => INFER: a PRIMARY document is attached (B-leaning).")
    elif label == "공식 근거 확인":
        print("  => INFER: '공식 근거 확인' shown WITHOUT any primary-document marker "
              "in the stored evidence (C-leaning: non-primary/news-or-official-body match drove the label).")
    else:
        print("  => INFER: see class counts above.")


# ===========================================================================
# PART B — opinion / column detection corpus-wide
# ===========================================================================
def _opinion_hit(title: str, url: str) -> str | None:
    t = title or ""
    for m in OPINION_TITLE_MARKERS:
        if m in t:
            return m
    import re
    mm = re.search(r"\[[^\]]*(" + "|".join(OPINION_BRACKET_WORDS) + r")[^\]]*\]", t)
    if mm:
        return f"bracket:{mm.group(0)[:20]}"
    low = (url or "").lower()
    for m in OPINION_URL_MARKERS:
        if m in low:
            return f"url:{m}"
    return None


def part_b(conn, sa, limit: int) -> None:
    print("\n" + "=" * 78)
    print(f"PART B — OPINION / COLUMN DETECTION (latest {limit} rows)")
    print("=" * 78)

    rows = conn.execute(sa.text(
        "SELECT id, title, original_url, query, created_at, policy_confidence_score, "
        "verdict_label, review_status, source_reliability_summary, debug_summary "
        "FROM analysis_results ORDER BY id DESC LIMIT :n"
    ), {"n": limit}).mappings().all()
    n = len(rows)
    print(f"scanned rows: {n}")

    flagged = []
    ambiguous_only = []
    for r in rows:
        title = str(r["title"] or "")
        url = str(r["original_url"] or "")
        hit = _opinion_hit(title, url)
        if hit:
            flagged.append((r, hit))
        else:
            if any(a in title for a in AMBIGUOUS_MARKERS):
                ambiguous_only.append((r, [a for a in AMBIGUOUS_MARKERS if a in title]))

    share = (len(flagged) / n * 100) if n else 0
    print(f"\n--- 1b COUNTS ---")
    print(f"OPINION-flagged: {len(flagged)} / {n} ({share:.1f}%)")
    print(f"ambiguous-only (excluded, NOT counted): {len(ambiguous_only)}")

    print(f"\n--- flagged titles (print ALL up to 40 — eyeball precision) ---")
    for r, hit in flagged[:40]:
        print(f"  [{hit:14}] score={r['policy_confidence_score']} id={r['id']} :: {str(r['title'])[:80]}")
    if len(flagged) > 40:
        print(f"  ... +{len(flagged) - 40} more")

    print(f"\n--- ~12 ambiguous/excluded titles (the boundary) ---")
    for r, hits in ambiguous_only[:12]:
        print(f"  [{','.join(hits):16}] id={r['id']} :: {str(r['title'])[:80]}")

    # 2b — how opinion rows are currently scored.
    print(f"\n--- 2b HOW OPINION ROWS ARE SCORED ---")
    if flagged:
        scores = [int(r["policy_confidence_score"] or 0) for r, _ in flagged]
        bands = Counter()
        official_label_count = 0
        examples = []
        for r, hit in flagged:
            srs = _parse_json(r["source_reliability_summary"]) or {}
            debug = _parse_json(r["debug_summary"]) or {}
            lbl = _official_status_label(srs, debug)
            if lbl == "공식 근거 확인":
                official_label_count += 1
            sc = int(r["policy_confidence_score"] or 0)
            band = "0-39" if sc < 40 else "40-59" if sc < 60 else "60-74" if sc < 75 else "75-100"
            bands[band] += 1
            if len(examples) < 8:
                examples.append((r["id"], sc, lbl, str(r["title"])[:60]))
        print(f"  policy_confidence_score: min={min(scores)} avg={sum(scores)/len(scores):.1f} max={max(scores)}")
        print(f"  band counts: {dict(bands)}")
        print(f"  opinion rows showing '공식 근거 확인' label: {official_label_count} / {len(flagged)}")
        print(f"  examples (id, score, label, title):")
        for ex in examples:
            print(f"    id={ex[0]} score={ex[1]} label={ex[2]} :: {ex[3]}")
    else:
        print("  (no opinion rows flagged in this window)")

    # 4b — seed exposure.
    print(f"\n--- 4b SEED EXPOSURE (which query/seed pulled the opinion rows) ---")
    qcount = Counter(str(r["query"] or "(none)") for r, _ in flagged)
    for q, c in qcount.most_common(15):
        print(f"  {c:3}  query={q}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evidence_audit")
    parser.add_argument("--idxno", default="583625", help="news idxno for the IBK URL match")
    parser.add_argument("--limit", type=int, default=200, help="Part B scan window (latest N rows)")
    parser.add_argument("--part", choices=["a", "b", "all"], default="all")
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

    with engine.connect() as conn:
        if args.part in ("a", "all"):
            part_a(conn, sa, args.idxno)
        if args.part in ("b", "all"):
            part_b(conn, sa, max(1, min(args.limit, 2000)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
