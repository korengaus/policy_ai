"""DESIGN-2 Phase 1 — READ-ONLY data-availability audit for the homepage redesign.

Measures, over a recent sample of analysis_results rows, whether each element the
redesign mockup shows is backed by stored data:

  * AI summary (card-face 2-3 line)         -> claim_text / claims / evidence_summary
  * keyword hashtags (#공시가격 …)          -> any stored keyword/concept list?
  * "1차 출처" primary-source chip           -> source_reliability_summary primary doc
  * DIRECT QUOTE box                         -> any stored matched-sentence/snippet?
  * 조회순 ranking (view count)              -> any view/click/hit column?
  * 이번 주 검증 현황 aggregates (47/31/6)   -> grade/label + domain + created_at
  * 정정·업데이트 corrections list           -> any corrected/updated/revision marker?

For each it reports fill rate over the sample + a few truncated examples so quality
can be eyeballed (found != usable). It also prints the full column inventory and
marks which columns are in the SLIM homepage payload (_SLIM_LIST_COLUMNS) vs which
live only in the full /history/{id} row — decisive for what the redesigned card can
show WITHOUT a payload change.

STRICTLY SELECT / READ-ONLY. No INSERT/UPDATE/DELETE/ALTER, no schema change, no
writes of any kind. Never prints DATABASE_URL or any secret.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/design2_field_audit.py
    PYTHONPATH=. python scripts/design2_field_audit.py --limit 200 --examples 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Connection — SELECT-only. Primary: DATABASE_URL (per the milestone brief),
# normalizing the SQLAlchemy driver scheme. Fallback: the proven
# postgres_storage.get_engine() (the path the other committed probes use in this
# exact Worker Shell). NEVER echoes the URL.
# ---------------------------------------------------------------------------
def _get_engine():
    import sqlalchemy as sa

    raw = os.environ.get("DATABASE_URL")
    if raw:
        url = raw.replace("postgresql+psycopg://", "postgresql://")
        # also normalize the bare libpq scheme some providers emit
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        try:
            engine = sa.create_engine(url)
            with engine.connect() as conn:  # validate before returning
                conn.execute(sa.text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001 — fall back, never leak the URL
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


def _nonempty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _trunc(value, n=90) -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


def _pct(num, den) -> str:
    return f"{num}/{den} ({(num / den * 100):.1f}%)" if den else "0/0 (n/a)"


def _first_claim_concept_tokens(normalized_claims) -> list:
    """Tokens that COULD seed hashtags from normalized_claims (actor/target/object).
    Inventory only — not a proposal."""
    out = []
    for nc in normalized_claims or []:
        if isinstance(nc, dict):
            for k in ("target", "object", "actor"):
                v = str(nc.get(k) or "").strip()
                if v:
                    out.append(v)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="design2_field_audit")
    parser.add_argument("--limit", type=int, default=150, help="recent rows to sample (by id desc)")
    parser.add_argument("--examples", type=int, default=5, help="examples to print per field")
    parser.add_argument("--window-days", type=int, default=7, help="window for '이번 주 검증 현황'")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa

    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    ex = max(1, args.examples)
    limit = max(1, min(args.limit, 2000))

    # ---- 1. COLUMN INVENTORY (information_schema) + SLIM marking ----------
    try:
        from postgres_storage import _SLIM_LIST_COLUMNS as SLIM
        slim = set(SLIM)
        slim_source = "imported from postgres_storage._SLIM_LIST_COLUMNS"
    except Exception:
        slim = set()
        slim_source = "NOT importable — slim membership shown as '?'"

    with engine.connect() as conn:
        cols = conn.execute(sa.text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'analysis_results' ORDER BY ordinal_position"
        )).mappings().all()
        total = conn.execute(sa.text("SELECT COUNT(*) AS c FROM analysis_results")).scalar() or 0

        # cheap exact aggregates (no JSON parse) for the counts section
        by_verdict = conn.execute(sa.text(
            "SELECT COALESCE(verdict_label,'(null)') AS k, COUNT(*) AS c "
            "FROM analysis_results GROUP BY verdict_label ORDER BY c DESC"
        )).mappings().all()
        by_domain = conn.execute(sa.text(
            "SELECT COALESCE(domain,'(null)') AS k, COUNT(*) AS c "
            "FROM analysis_results GROUP BY domain ORDER BY c DESC"
        )).mappings().all()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.window_days)).isoformat()
        window_total = conn.execute(sa.text(
            "SELECT COUNT(*) AS c FROM analysis_results WHERE created_at >= :cut"
        ), {"cut": cutoff}).scalar() or 0
        window_by_verdict = conn.execute(sa.text(
            "SELECT COALESCE(verdict_label,'(null)') AS k, COUNT(*) AS c "
            "FROM analysis_results WHERE created_at >= :cut GROUP BY verdict_label ORDER BY c DESC"
        ), {"cut": cutoff}).mappings().all()

        # ---- sample the recent rows (full row — measuring what is STORED) --
        col_names = [c["column_name"] for c in cols]
        select_cols = [c for c in (
            "id", "title", "claim_text", "claims", "evidence_summary", "domain",
            "verdict_label", "policy_confidence_score", "verification_strength",
            "review_status", "created_at", "last_checked_at", "human_reviewed_at",
            "source_reliability_summary", "debug_summary", "normalized_claims",
            "source_candidates", "evidence_snippets", "source_queries",
        ) if c in col_names]
        rows = conn.execute(sa.text(
            f"SELECT {', '.join(select_cols)} FROM analysis_results "
            "ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)

    print("=" * 88)
    print(f"DESIGN-2 field audit — total rows={total} — sampled most-recent {n} (--limit {limit})")
    print("=" * 88)

    # ---- 1. column inventory print ---------------------------------------
    print("\n[1] COLUMN INVENTORY of analysis_results  (SLIM = reaches homepage card)")
    print(f"    slim source: {slim_source}")
    for c in cols:
        name = c["column_name"]
        mark = "SLIM" if name in slim else ("    " if slim else "  ? ")
        print(f"    [{mark}] {name:32} {c['data_type']}")
    if slim:
        missing_from_table = [c for c in slim if c not in col_names]
        if missing_from_table:
            print(f"    !! slim columns not found in table: {missing_from_table}")

    # ---- 2. AI SUMMARY ----------------------------------------------------
    print("\n[2] AI SUMMARY availability (card reads claim_text -> claims[0] -> evidence_summary)")
    have_claim = have_claims0 = have_evsum = have_any = 0
    collapses = 0
    summary_examples = []
    for r in rows:
        claim_text = r.get("claim_text") if "claim_text" in r else None
        claims = _parse_json(r.get("claims")) if "claims" in r else None
        claims0 = claims[0] if isinstance(claims, list) and claims else None
        evsum = r.get("evidence_summary") if "evidence_summary" in r else None
        title = r.get("title")
        c1, c2, c3 = _nonempty(claim_text), _nonempty(claims0), _nonempty(evsum)
        have_claim += c1
        have_claims0 += c2
        have_evsum += c3
        chosen = claim_text if c1 else (claims0 if c2 else (evsum if c3 else None))
        if _nonempty(chosen):
            have_any += 1
            # approximate the NARRATIVE-3B "collapses to title" hide case
            norm = re.sub(r"[\s.,!?。·…\"'“”‘’()\[\]]", "", str(chosen).lower())
            tnorm = re.sub(r"[\s.,!?。·…\"'“”‘’()\[\]]", "", str(title or "").lower())
            if norm and norm == tnorm:
                collapses += 1
            if len(summary_examples) < ex:
                summary_examples.append((r.get("id"), _trunc(chosen, 120)))
    print(f"    claim_text non-empty:     {_pct(have_claim, n)}")
    print(f"    claims[0] non-empty:      {_pct(have_claims0, n)}")
    print(f"    evidence_summary non-empty:{_pct(have_evsum, n)}")
    print(f"    ANY card summary present: {_pct(have_any, n)}")
    print(f"    ~collapses to title (hidden card-face, approx): {_pct(collapses, n)}")
    for rid, s in summary_examples:
        print(f"      id={rid}: {s}")

    # ---- 3. KEYWORDS / HASHTAGS ------------------------------------------
    print("\n[3] KEYWORD/HASHTAG candidates (is there a ready-made keyword list?)")
    kw_col_candidates = [c for c in col_names if re.search(r"keyword|tag|concept|hashtag", c, re.I)]
    print(f"    columns named like keyword/tag/concept: {kw_col_candidates or 'NONE'}")
    nc_fill = sc_concept_fill = claims_fill = 0
    nc_examples, sc_examples = [], []
    for r in rows:
        nc = _parse_json(r.get("normalized_claims")) if "normalized_claims" in r else None
        toks = _first_claim_concept_tokens(nc)
        if toks:
            nc_fill += 1
            if len(nc_examples) < ex:
                nc_examples.append((r.get("id"), toks[:6]))
        sc = _parse_json(r.get("source_candidates")) if "source_candidates" in r else None
        concepts = []
        for cand in sc or []:
            if isinstance(cand, dict):
                for key in ("matched_concepts", "matched_query_terms"):
                    v = cand.get(key)
                    if isinstance(v, list):
                        concepts.extend(v)
                    elif isinstance(v, str) and v:
                        concepts.extend([t for t in v.split(",") if t.strip()])
        if concepts:
            sc_concept_fill += 1
            if len(sc_examples) < ex:
                sc_examples.append((r.get("id"), sorted(set(concepts))[:8]))
        claims = _parse_json(r.get("claims")) if "claims" in r else None
        if isinstance(claims, list) and claims:
            claims_fill += 1
    print(f"    normalized_claims actor/target/object tokens present (FULL row): {_pct(nc_fill, n)}")
    for rid, toks in nc_examples:
        print(f"      id={rid}: {toks}")
    print(f"    source_candidates matched_concepts/query_terms present (FULL row): {_pct(sc_concept_fill, n)}")
    for rid, toks in sc_examples:
        print(f"      id={rid}: {toks}")
    print(f"    claims[] present (SLIM, but free-text claim sentences, not tags): {_pct(claims_fill, n)}")
    print("    -> NOTE: normalized_claims & source_candidates are FULL-row only (NOT slim).")

    # ---- 4. PRIMARY-SOURCE CHIP ------------------------------------------
    print("\n[4] PRIMARY-SOURCE chip (institution + doc title)")
    srs_fill = sc_marker_fill = 0
    srs_examples, marker_examples = [], []
    for r in rows:
        srs = _parse_json(r.get("source_reliability_summary")) if "source_reliability_summary" in r else {}
        srs = srs if isinstance(srs, dict) else {}
        title = (srs.get("top_official_detail_title")
                 or srs.get("selected_primary_source")
                 or srs.get("top_source_title"))
        if _nonempty(title):
            srs_fill += 1
            if len(srs_examples) < ex:
                srs_examples.append((r.get("id"),
                                     _trunc(title, 60),
                                     srs.get("official_direct_match_score"),
                                     srs.get("has_genuine_official_support")))
        sc = _parse_json(r.get("source_candidates")) if "source_candidates" in r else None
        for cand in sc or []:
            if isinstance(cand, dict) and (cand.get("policy_briefing_news_item_id")
                                           or cand.get("national_law_mst")) and cand.get("official_body_match"):
                sc_marker_fill += 1
                if len(marker_examples) < ex:
                    marker_examples.append((r.get("id"), _trunc(cand.get("title"), 60)))
                break
    print(f"    source_reliability_summary primary title present (SLIM JSON): {_pct(srs_fill, n)}")
    for rid, t, score, genuine in srs_examples:
        print(f"      id={rid}: title='{t}' direct_match_score={score} genuine={genuine}")
    print(f"    source_candidates genuine primary-marker match present (FULL row): {_pct(sc_marker_fill, n)}")
    for rid, t in marker_examples:
        print(f"      id={rid}: '{t}'")
    print("    -> top_official_detail_title IS in the slim source_reliability_summary;")
    print("       the genuine primary-MARKER (policy_briefing/national_law) is FULL-row only.")

    # ---- 5. DIRECT QUOTE --------------------------------------------------
    print("\n[5] DIRECT QUOTE box (stored matched sentence / snippet)")
    matched_sent_fill = snippet_fill = 0
    quote_examples = []
    for r in rows:
        sc = _parse_json(r.get("source_candidates")) if "source_candidates" in r else None
        got = None
        for cand in sc or []:
            if isinstance(cand, dict):
                for ms in cand.get("official_matched_sentences") or []:
                    if isinstance(ms, dict) and _nonempty(ms.get("sentence")):
                        got = ms.get("sentence")
                        break
            if got:
                break
        if got:
            matched_sent_fill += 1
            if len(quote_examples) < ex:
                quote_examples.append((r.get("id"), _trunc(got, 90)))
        sn = _parse_json(r.get("evidence_snippets")) if "evidence_snippets" in r else None
        if isinstance(sn, list) and any(
            isinstance(s, dict) and _nonempty(s.get("snippet") or s.get("text") or s.get("sentence"))
            for s in sn
        ):
            snippet_fill += 1
    print(f"    source_candidates official_matched_sentences present (FULL row): {_pct(matched_sent_fill, n)}")
    for rid, q in quote_examples:
        print(f"      id={rid}: “{q}”")
    print(f"    evidence_snippets present (FULL row): {_pct(snippet_fill, n)}")
    print("    -> both are FULL-row only (NOT slim).")

    # ---- 6. VIEW-COUNT / TRAFFIC -----------------------------------------
    print("\n[6] VIEW-COUNT / TRAFFIC column (the 조회순 question)")
    view_cols = [c for c in col_names if re.search(r"view|click|hit|visit|popular|traffic|count", c, re.I)]
    # 'count' is broad; show them but label
    print(f"    columns matching view/click/hit/visit/popular/traffic/count: {view_cols or 'NONE'}")
    print(f"    -> VIEW/TRAFFIC COUNTER EXISTS: {'YES (inspect above)' if any(re.search(r'view|click|hit|visit|popular|traffic', c, re.I) for c in col_names) else 'NO — no 조회순 data to sort by today'}")

    # ---- 7. RANKING-FALLBACK candidates ----------------------------------
    print("\n[7] RANKING-FALLBACK candidates (what can order a 'popular' list now)")
    for f in ("created_at", "policy_confidence_score", "verdict_label", "verification_strength",
              "policy_alert_level", "domain"):
        present = f in col_names
        in_slim = f in slim
        print(f"    {f:26} stored={present}  slim={in_slim if slim else '?'}")
    print("    -> hot_topics engine: '뜨는순' is a CLIENT composite (alert→freshness→confidence),")
    print("       computed in main.js sortTopicCards — no stored popularity score column.")

    # ---- 8. AGGREGATE COUNTS ('이번 주 검증 현황') ------------------------
    print(f"\n[8] AGGREGATE COUNTS — window = last {args.window_days} days (created_at >= cutoff)")
    print(f"    total rows in window: {window_total}")
    print(f"    window by verdict_label:")
    for r in window_by_verdict:
        print(f"      {r['k']:32} {r['c']}")
    print(f"    ALL-TIME by verdict_label:")
    for r in by_verdict:
        print(f"      {r['k']:32} {r['c']}")
    print(f"    ALL-TIME by domain:")
    for r in by_domain:
        print(f"      {r['k']:24} {r['c']}")
    # '공식 확인' count needs the genuine flag (JSON) — sample-based estimate
    genuine_in_sample = 0
    for r in rows:
        srs = _parse_json(r.get("source_reliability_summary")) if "source_reliability_summary" in r else {}
        srs = srs if isinstance(srs, dict) else {}
        debug = _parse_json(r.get("debug_summary")) if "debug_summary" in r else {}
        debug = debug if isinstance(debug, dict) else {}
        genuine = srs.get("has_genuine_official_support")
        if genuine is None:
            genuine = (float(debug.get("official_body_matches") or 0) > 0)
        if genuine:
            genuine_in_sample += 1
    print(f"    '공식 확인'(genuine official support) over the {n}-row sample: {_pct(genuine_in_sample, n)}")
    print("    -> verdict_label & domain counts are exact SQL GROUP BY (cheap, computable).")
    print("       '공식 확인' needs the JSON genuine flag → sample-based here (or a parsed pass).")

    # ---- 9. CORRECTIONS list ('정정·업데이트') ---------------------------
    print("\n[9] CORRECTIONS / UPDATE marker ('정정·업데이트' list)")
    corr_cols = [c for c in col_names if re.search(r"correct|updated|revision|revise|edited|amend", c, re.I)]
    print(f"    columns matching correct/updated/revision/edited/amend: {corr_cols or 'NONE'}")
    has_updated_at = "updated_at" in col_names
    # last_checked_at vs created_at differ?
    differ = 0
    for r in rows:
        lc, cr = r.get("last_checked_at"), r.get("created_at")
        if _nonempty(lc) and _nonempty(cr) and str(lc)[:19] != str(cr)[:19]:
            differ += 1
    print(f"    distinct updated_at column: {has_updated_at}")
    print(f"    rows where last_checked_at != created_at (proxy for re-check): {_pct(differ, n)}")
    print("    -> human_reviewed_at marks operator review, NOT a content correction.")
    print("       No dedicated correction/revision field unless listed above.")

    print("\n" + "=" * 88)
    print("DONE — SELECT-only audit complete. No rows were written or altered.")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
