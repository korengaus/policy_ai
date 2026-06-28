"""MATCH-INSTABILITY Phase 1 — READ-ONLY characterization of run-to-run box variation.

The same search term sometimes yields a genuine "공식 근거 확인" box and sometimes
does not. THE QUESTION: is that variation HONEST (a different CLAIM was extracted, or
the relevant official doc was simply ABSENT from that run's date-window candidates) OR
a REAL DEFECT (the SAME relevant official document was present in BOTH runs' candidates
and reached genuine on one but not the other for an essentially-SAME claim — i.e.
non-deterministic matching/scoring)?

This probe reads STORED analysis_results rows ONLY. It groups rows that share a
query/claim, measures whether the stored genuine signal is CONSTANT or VARYING within
each group, and for VARYING groups classifies each non-genuine ("box-NO") row by WHY
it differs from its genuine ("box-YES") siblings:

  (a) DIFFERENT-CLAIM       — news varied upstream            -> honest
  (b) DOC-ABSENT            — box-YES driver doc not in this run's candidates (date-window) -> honest
  (c) DOC-PRESENT-UNMATCHED — SAME doc in both runs' candidates, genuine in one not the other
        (c1) CLAIM-DIFFERS  — doc legitimately matches one claim not the other -> honest
        (c2) CLAIM-SAME     — same doc + same claim, divergent result -> REAL NON-DETERMINISM DEFECT

The (c2) count in SECTION 4 is the decision: ==0 -> instability is HONEST (close B);
>0 -> a real non-determinism defect (Phase-2 target; row-id pairs listed).

REUSES the authoritative predicates by import (does NOT re-define "genuine"):
extract_primary_document_match, PRIMARY_DOCUMENT_STRONG_CLASSIFICATION,
PRIMARY_DOCUMENT_MIN_SCORE, _PRIMARY_DOCUMENT_MARKER_FIELDS — exactly the names
verification_card.py / deploy_check.py use.

STRICTLY SELECT / READ-ONLY. No writes/DDL. No re-analysis, no provider/LLM/network
call — touches ONLY the database (read). Never prints DATABASE_URL or secrets.
ASCII-safe stdout (json.dumps(ensure_ascii=True)) so the Worker Shell renders it.

Run in the Render Worker Shell:
    git log --oneline -1
    PYTHONPATH=. python scripts/match_instability_probe.py
    PYTHONPATH=. python scripts/match_instability_probe.py --min-group 2 --show 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Force ASCII so nothing downstream can percent-encode raw Korean bytes.
try:
    sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")  # type: ignore[attr-defined]
except Exception:
    pass

# Authoritative predicates — imported, never re-implemented.
from official_evidence_resolution import (  # noqa: E402
    extract_primary_document_match,
    PRIMARY_DOCUMENT_STRONG_CLASSIFICATION,
    PRIMARY_DOCUMENT_MIN_SCORE,
    _PRIMARY_DOCUMENT_MARKER_FIELDS,
)

NAMED_TERMS = ("청년미래적금", "소상공인 보증", "가계대출 DSR", "부동산 불법행위")

# Token-overlap threshold above which two claim strings are treated as
# "essentially the same" (the conservative gate for the c2 defect bucket).
CLAIM_SAME_JACCARD = 0.70


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


def _ascii(v, n=80) -> str:
    """Single-line, ASCII-only repr of any value (Korean -> \\uXXXX)."""
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]
    return json.dumps(s, ensure_ascii=True)


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip().lower()


def _num(v) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _cand_score(c: dict) -> int:
    """Mirror extract_primary_document_match's score extraction."""
    return max(
        _num(c.get("official_evidence_score")),
        _num(c.get("official_final_direct_match_score")),
        _num(c.get("official_body_match_score")),
    )


def _cand_classification(c: dict) -> str:
    return str(
        c.get("official_evidence_classification")
        or c.get("official_direct_match_classification")
        or ""
    )


def _is_strong_body(c: dict) -> bool:
    """A candidate that drives the genuine signal via the strong-body disjunct
    (verification_card.py:708-721): official_body_match AND strong classification.
    This is a superset of the primary-document drivers (which additionally require a
    marker field + score>=75), so the strong-body set covers every genuine driver."""
    return bool(c.get("official_body_match")) and _cand_classification(c) == PRIMARY_DOCUMENT_STRONG_CLASSIFICATION


def _strong_body_count(cands) -> int:
    return sum(1 for c in cands if isinstance(c, dict) and _is_strong_body(c))


def _derive_genuine(cands) -> bool:
    """The authoritative genuine predicate over stored candidates, exactly as
    verification_card.build_verification_card computes _has_genuine_official_support."""
    return bool(extract_primary_document_match(cands) is not None or _strong_body_count(cands) > 0)


def _doc_key(c: dict) -> str:
    """Stable doc identity for cross-run comparison: publisher + normalized title,
    falling back to the detail/url when the title is empty."""
    pub = _norm(c.get("publisher") or c.get("source_name"))
    title = _norm(c.get("title"))
    if title:
        return f"{pub}||{title}"
    url = _norm(c.get("official_detail_url") or c.get("url"))
    return f"{pub}||url:{url}"


def _claim_of(row) -> str:
    """First claim text for a row: prefer the stored scalar claim_text, fall back to
    the first element of the claims JSON array."""
    ct = str(row.get("claim_text") or "").strip()
    if ct:
        return ct
    claims = _parse_json(row.get("claims"))
    if isinstance(claims, list) and claims:
        first = claims[0]
        if isinstance(first, dict):
            return str(first.get("claim") or first.get("text") or first.get("normalized_claim") or "").strip()
        return str(first or "").strip()
    return ""


def _jaccard(a: str, b: str) -> float:
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _claims_same(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if na and na == nb:
        return True
    return _jaccard(a, b) >= CLAIM_SAME_JACCARD


# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="match_instability_probe")
    parser.add_argument("--min-group", type=int, default=2,
                        help="min rows for a recurring-query group to be kept")
    parser.add_argument("--show", type=int, default=8,
                        help="max rows to list per group in SECTION 2")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    # ---- Pass 1: light columns for the whole corpus (NO source_candidates) ---- #
    with engine.connect() as conn:
        all_cols = conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'analysis_results' ORDER BY ordinal_position"
        )).scalars().all()
        rows = conn.execute(sa.text(
            "SELECT id, query, title, claim_text, claims, "
            "source_reliability_summary, created_at "
            "FROM analysis_results ORDER BY id"
        )).mappings().all()

    by_id = {}
    for r in rows:
        by_id[r.get("id")] = {
            "id": r.get("id"),
            "query": r.get("query"),
            "title": r.get("title"),
            "claim_text": r.get("claim_text"),
            "claims": r.get("claims"),
            "summary": _parse_json(r.get("source_reliability_summary")),
            "created_at": r.get("created_at"),
        }

    # ===================================================================== #
    # SECTION 0 — SCHEMA & SIGNAL INVENTORY
    # ===================================================================== #
    print("=" * 100)
    print("SECTION 0 — SCHEMA & SIGNAL INVENTORY")
    print("=" * 100)
    print(f"analysis_results columns ({len(all_cols)}):")
    print("  " + ", ".join(str(c) for c in all_cols))
    has_query_col = "query" in all_cols
    print(f"\noriginating-query column present : query={has_query_col}  "
          f"(also have: title={'title' in all_cols}, claim_text={'claim_text' in all_cols})")
    print("  -> grouping uses 'query' as the originating topic; claim_text/claims for the CLAIM.")

    recent_summary = None
    for r in reversed(rows):
        s = _parse_json(r.get("source_reliability_summary"))
        if isinstance(s, dict):
            recent_summary = s
            break
    if isinstance(recent_summary, dict):
        keys = sorted(recent_summary.keys())
        print(f"\nsource_reliability_summary keys on a recent row ({len(keys)}):")
        print("  " + _ascii(", ".join(keys), 600))
        for k in ("has_genuine_official_support", "top_official_institution", "top_official_detail_title"):
            print(f"  contains {k} : {k in recent_summary}")
    else:
        print("\nNOTE: no parseable source_reliability_summary found on any row.")

    n_total = len(rows)
    created_vals = [str(r.get("created_at") or "") for r in rows if r.get("created_at")]
    n_stored_flag = sum(
        1 for r in rows
        if isinstance(_parse_json(r.get("source_reliability_summary")), dict)
        and "has_genuine_official_support" in _parse_json(r.get("source_reliability_summary"))
    )
    print(f"\ntotal rows                         : {n_total}")
    if created_vals:
        print(f"created_at MIN / MAX               : {_ascii(min(created_vals), 30)} .. {_ascii(max(created_vals), 30)}")
    print(f"rows WITH stored has_genuine flag  : {n_stored_flag}  "
          f"(post-fix rows); WITHOUT : {n_total - n_stored_flag}  (older / derive-only)")

    # ===================================================================== #
    # SECTION 1 — GROUP CONSTRUCTION
    # ===================================================================== #
    print("\n" + "=" * 100)
    print("SECTION 1 — GROUP CONSTRUCTION")
    print("=" * 100)

    groups = {}  # group_key -> {"label","ids"}

    # (i) NAMED-TERM groups
    print("\n(i) NAMED-TERM groups (query OR title OR first-claim LIKE the term):")
    for term in NAMED_TERMS:
        ids = []
        for rid, row in by_id.items():
            hay = " ".join([
                str(row.get("query") or ""),
                str(row.get("title") or ""),
                _claim_of(row),
            ])
            if term in hay:
                ids.append(rid)
        ids.sort()
        print(f"  term {_ascii(term, 24):28} -> {len(ids):3} rows  "
              f"ids={ids[:20]}{' ...' if len(ids) > 20 else ''}")
        if len(ids) >= 1:
            groups[f"TERM::{term}"] = {"label": f"TERM {term}", "ids": ids}

    # (ii) RECURRING-QUERY groups (group by originating query, keep >= min-group)
    by_query = defaultdict(list)
    for rid, row in by_id.items():
        q = _norm(row.get("query"))
        if q:
            by_query[q].append(rid)
    recurring = {q: sorted(ids) for q, ids in by_query.items() if len(ids) >= args.min_group}
    print(f"\n(ii) RECURRING-QUERY groups (distinct query with >= {args.min_group} rows): "
          f"{len(recurring)} groups")
    for q in sorted(recurring, key=lambda k: -len(recurring[k]))[:25]:
        ids = recurring[q]
        groups[f"QUERY::{q}"] = {"label": f"QUERY {q}", "ids": ids}
        print(f"  {len(ids):3} rows  query={_ascii(q, 48)}")
    if len(recurring) > 25:
        print(f"  ... and {len(recurring) - 25} more recurring-query groups (added to analysis).")
        for q in sorted(recurring, key=lambda k: -len(recurring[k]))[25:]:
            groups[f"QUERY::{q}"] = {"label": f"QUERY {q}", "ids": recurring[q]}

    # Working set: every row that belongs to at least one group needs candidates.
    grouped_ids = sorted({rid for g in groups.values() for rid in g["ids"]})
    print(f"\nTotal groups: {len(groups)}   distinct grouped rows: {len(grouped_ids)}")

    # ---- Pass 2: source_candidates ONLY for grouped rows (bounded working set) ---- #
    cand_by_id = {}
    if grouped_ids:
        with engine.connect() as conn:
            CHUNK = 200
            for i in range(0, len(grouped_ids), CHUNK):
                chunk = grouped_ids[i:i + CHUNK]
                crows = conn.execute(sa.text(
                    "SELECT id, source_candidates FROM analysis_results "
                    "WHERE id = ANY(:ids)"
                ), {"ids": chunk}).mappings().all()
                for cr in crows:
                    parsed = _parse_json(cr.get("source_candidates"))
                    cand_by_id[cr.get("id")] = parsed if isinstance(parsed, list) else []

    # ---- genuine signal per grouped row: READ stored, else DERIVE ---- #
    # source: "read" | "derived" | "none"
    genuine = {}    # rid -> (source, bool|None)
    for rid in grouped_ids:
        row = by_id[rid]
        s = row.get("summary")
        if isinstance(s, dict) and "has_genuine_official_support" in s:
            genuine[rid] = ("read", bool(s.get("has_genuine_official_support")))
        elif rid in cand_by_id:
            genuine[rid] = ("derived", _derive_genuine(cand_by_id[rid]))
        else:
            genuine[rid] = ("none", None)

    # ---- faithfulness check: stored flag vs derived, on rows that have BOTH ---- #
    agree = total_both = 0
    for rid in grouped_ids:
        row = by_id[rid]
        s = row.get("summary")
        if isinstance(s, dict) and "has_genuine_official_support" in s and rid in cand_by_id:
            stored = bool(s.get("has_genuine_official_support"))
            derived = _derive_genuine(cand_by_id[rid])
            total_both += 1
            if stored == derived:
                agree += 1
    agreement = (agree / total_both) if total_both else None
    use_derived = (agreement is not None and agreement >= 0.99) or total_both == 0

    def usable_genuine(rid):
        """Return bool genuine value if usable for variance, else None."""
        src, val = genuine.get(rid, ("none", None))
        if val is None:
            return None
        if src == "derived" and not use_derived:
            return None
        return val

    # ===================================================================== #
    # SECTION 2 — GENUINE-FLAG VARIANCE PER GROUP
    # ===================================================================== #
    print("\n" + "=" * 100)
    print("SECTION 2 — GENUINE-FLAG VARIANCE PER GROUP")
    print("=" * 100)
    if agreement is not None:
        print(f"(derived-genuine faithfulness vs stored flag: {agree}/{total_both} = "
              f"{agreement*100:.1f}%  -> derived rows {'INCLUDED' if use_derived else 'EXCLUDED'})")

    varying_groups = []
    n_const_gen = n_const_nongen = n_varying = n_undetermined = 0

    for gkey, g in groups.items():
        ids = g["ids"]
        vals = [usable_genuine(rid) for rid in ids]
        present = [v for v in vals if v is not None]
        date_span = ""
        cas = [str(by_id[rid].get("created_at") or "") for rid in ids if by_id[rid].get("created_at")]
        if cas:
            date_span = f"{min(cas)[:19]} .. {max(cas)[:19]}"

        if len(present) < 2:
            status = "UNDETERMINED (<2 usable genuine signals)"
            n_undetermined += 1
        elif all(present) :
            status = "CONSTANT-genuine"
            n_const_gen += 1
        elif not any(present):
            status = "CONSTANT-non-genuine"
            n_const_nongen += 1
        else:
            status = "VARYING"
            n_varying += 1
            varying_groups.append(gkey)

        print(f"\n[{status}]  {_ascii(g['label'], 60)}")
        print(f"   rows={len(ids)}  usable-signals={len(present)}  span={_ascii(date_span, 45)}")
        for rid in ids[: max(1, args.show)]:
            src, val = genuine.get(rid, ("none", None))
            disp = "YES" if val else ("NO" if val is False else "??")
            if src == "derived" and not use_derived and val is not None:
                disp += "(derived-excluded)"
            elif src == "derived":
                disp += "(derived)"
            elif src == "none":
                disp = "no-stored-flag/no-cands"
            driver = ""
            s = by_id[rid].get("summary")
            if val and isinstance(s, dict):
                driver = (f"  driver={_ascii(s.get('top_official_institution'), 20)}/"
                          f"{_ascii(s.get('top_official_detail_title'), 32)}")
            print(f"     id={rid:>5} {_ascii(str(by_id[rid].get('created_at'))[:19], 21)} "
                  f"GEN={disp:<24}{driver}")
            print(f"            claim={_ascii(_claim_of(by_id[rid]), 80)}")
        if len(ids) > args.show:
            print(f"     ... (+{len(ids) - args.show} more rows)")

    print("\n" + "-" * 100)
    print(f"TALLY: CONSTANT-genuine={n_const_gen}  CONSTANT-non-genuine={n_const_nongen}  "
          f"VARYING={n_varying}  UNDETERMINED={n_undetermined}  (of {len(groups)} groups)")

    # ===================================================================== #
    # SECTION 3 — WHY (VARYING groups only)
    # ===================================================================== #
    print("\n" + "=" * 100)
    print("SECTION 3 — WHY (per VARYING group; real data, not counts)")
    print("=" * 100)

    bucket_a = []   # (no_id, yes_id)
    bucket_b = []
    bucket_c1 = []
    bucket_c2 = []  # the only real-defect signal
    bucket_ambiguous = []

    if not varying_groups:
        print("\n(no VARYING groups — nothing to explain)")

    for gkey in varying_groups:
        g = groups[gkey]
        ids = g["ids"]
        yes_ids = [rid for rid in ids if usable_genuine(rid) is True]
        no_ids = [rid for rid in ids if usable_genuine(rid) is False]
        print("\n" + "-" * 100)
        print(f"VARYING GROUP: {_ascii(g['label'], 70)}")
        print(f"   box-YES rows={yes_ids}   box-NO rows={no_ids}")

        # Build the union of box-YES driver docs: doc_key -> {yes_id, claim, title, publisher}
        yes_driver_docs = {}
        for yid in yes_ids:
            cands = cand_by_id.get(yid) or []
            yclaim = _claim_of(by_id[yid])
            for c in cands:
                if not isinstance(c, dict) or not _is_strong_body(c):
                    continue
                k = _doc_key(c)
                # keep first-seen per yes row; prefer one that records the claim
                yes_driver_docs.setdefault(k, {
                    "yes_id": yid, "claim": yclaim,
                    "publisher": c.get("publisher") or c.get("source_name"),
                    "title": c.get("title"),
                    "clf": _cand_classification(c), "score": _cand_score(c),
                })

        if not yes_driver_docs:
            print("   AMBIGUOUS: box-YES rows expose no parseable strong-body driver doc "
                  "(candidates missing/unparseable). Not bucketing.")
            for nid in no_ids:
                bucket_ambiguous.append((nid, None))
            continue

        for nid in no_ids:
            ncands = cand_by_id.get(nid)
            nclaim = _claim_of(by_id[nid])
            if ncands is None:
                print(f"   id={nid}: AMBIGUOUS — source_candidates unavailable. Not bucketing.")
                bucket_ambiguous.append((nid, None))
                continue
            # index this box-NO row's candidate docs by key
            n_doc_index = {}
            for c in ncands:
                if isinstance(c, dict):
                    n_doc_index.setdefault(_doc_key(c), c)

            # (c) shared doc present in BOTH?
            shared_key = None
            for k in yes_driver_docs:
                if k in n_doc_index:
                    shared_key = k
                    break

            if shared_key is not None:
                ydoc = yes_driver_docs[shared_key]
                ncand = n_doc_index[shared_key]
                n_clf = _cand_classification(ncand)
                n_score = _cand_score(ncand)
                n_reaches = _is_strong_body(ncand) and n_score >= PRIMARY_DOCUMENT_MIN_SCORE
                same = _claims_same(ydoc["claim"], nclaim)
                bucket = "c2" if same else "c1"
                print(f"   id={nid}: BUCKET (c) DOC-PRESENT-UNMATCHED  [{'c2 SAME-CLAIM' if same else 'c1 CLAIM-DIFFERS'}]")
                print(f"        shared doc : pub={_ascii(ydoc['publisher'], 24)} title={_ascii(ydoc['title'], 44)}")
                print(f"        in box-YES id={ydoc['yes_id']}: clf={_ascii(ydoc['clf'] or '(none)', 30)} score={ydoc['score']}  (reaches genuine: YES)")
                print(f"        in box-NO  id={nid}: clf={_ascii(n_clf or '(none)', 30)} score={n_score}  (reaches strong+>=75: {n_reaches})")
                print(f"        claim YES  : {_ascii(ydoc['claim'], 80)}")
                print(f"        claim NO   : {_ascii(nclaim, 80)}")
                print(f"        jaccard={_jaccard(ydoc['claim'], nclaim):.2f}  claims-same={same}")
                if same:
                    bucket_c2.append((nid, ydoc["yes_id"], shared_key))
                    print("        -> *** c2: REAL NON-DETERMINISM DEFECT CANDIDATE ***")
                else:
                    bucket_c1.append((nid, ydoc["yes_id"]))
                continue

            # No shared driver doc. Distinguish (a) different-claim vs (b) doc-absent.
            same_claim_sibling = None
            for k, ydoc in yes_driver_docs.items():
                if _claims_same(ydoc["claim"], nclaim):
                    same_claim_sibling = ydoc
                    break
            if same_claim_sibling is None:
                # claim differs from every box-YES sibling -> news varied upstream
                ref = next(iter(yes_driver_docs.values()))
                print(f"   id={nid}: BUCKET (a) DIFFERENT-CLAIM  (honest — news varied)")
                print(f"        claim NO   : {_ascii(nclaim, 80)}")
                print(f"        claim YES  : {_ascii(ref['claim'], 80)}  (id={ref['yes_id']})")
                print(f"        best jaccard vs any YES sibling="
                      f"{max((_jaccard(d['claim'], nclaim) for d in yes_driver_docs.values()), default=0):.2f}")
                bucket_a.append((nid, ref["yes_id"]))
            else:
                # essentially-same claim but the driver doc is ABSENT here -> date-window
                print(f"   id={nid}: BUCKET (b) DOC-ABSENT  (honest — reached THEN / date-window)")
                print(f"        box-YES driver doc (id={same_claim_sibling['yes_id']}): "
                      f"pub={_ascii(same_claim_sibling['publisher'], 24)} title={_ascii(same_claim_sibling['title'], 44)}")
                print(f"        confirmed ABSENT from box-NO id={nid} candidates ({len(ncands)} cands)")
                print(f"        claim YES  : {_ascii(same_claim_sibling['claim'], 80)}")
                print(f"        claim NO   : {_ascii(nclaim, 80)}  (essentially same)")
                bucket_b.append((nid, same_claim_sibling["yes_id"]))

    # ===================================================================== #
    # SECTION 4 — VERDICT-INPUT SUMMARY
    # ===================================================================== #
    print("\n" + "=" * 100)
    print("SECTION 4 — VERDICT-INPUT SUMMARY")
    print("=" * 100)
    print(f"  (a) DIFFERENT-CLAIM        : {len(bucket_a)}   (honest — upstream news varied)")
    print(f"  (b) DOC-ABSENT             : {len(bucket_b)}   (honest — date-window availability)")
    print(f"  (c1) DOC-PRESENT, CLAIM-DIFFERS : {len(bucket_c1)}   (honest — doc matches one claim)")
    print(f"  (c2) DOC-PRESENT, CLAIM-SAME    : {len(bucket_c2)}   (*** REAL NON-DETERMINISM DEFECT ***)")
    print(f"  AMBIGUOUS (not bucketed)   : {len(bucket_ambiguous)}")
    print()
    if not bucket_c2:
        print("  VERDICT: (c2) == 0  ->  instability is HONEST (upstream news variation +")
        print("           date-window document availability). NO verdict-path defect. B can close.")
    else:
        print("  VERDICT: (c2) > 0  ->  REAL non-determinism defect present. Phase-2 target rows:")
        for nid, yid, k in bucket_c2:
            print(f"     box-NO id={nid}  vs  box-YES id={yid}   shared-doc-key={_ascii(k, 60)}")

    # ===================================================================== #
    # SECTION 5 — PROBE SELF-FAITHFULNESS (RECON gate)
    # ===================================================================== #
    print("\n" + "=" * 100)
    print("SECTION 5 — PROBE SELF-FAITHFULNESS (RECON gate)")
    print("=" * 100)
    print("  READ-from-storage  : genuine flag (source_reliability_summary.has_genuine_official_support),")
    print("                       driver institution/detail-title, claim_text/claims, source_candidates.")
    print("  DERIVED            : genuine for rows lacking the stored flag, via the AUTHORITATIVE")
    print("                       predicate (extract_primary_document_match OR strong-body-count>0).")
    print("                       Bucket classification (a/b/c1/c2) is derived from stored candidates.")
    n_read = sum(1 for rid in grouped_ids if genuine.get(rid, (None,))[0] == "read")
    n_der = sum(1 for rid in grouped_ids if genuine.get(rid, (None,))[0] == "derived")
    n_non = sum(1 for rid in grouped_ids if genuine.get(rid, (None,))[0] == "none")
    n_usable = sum(1 for rid in grouped_ids if usable_genuine(rid) is not None)
    print(f"\n  grouped rows={len(grouped_ids)}  genuine-source: read={n_read} derived={n_der} none={n_non}")
    if agreement is not None:
        print(f"  faithfulness (stored vs derived on rows with BOTH): {agree}/{total_both} = {agreement*100:.1f}%")
        if not use_derived:
            print("  -> agreement < 99%: derived rows EXCLUDED from SECTIONS 2-4 variance.")
        else:
            print("  -> agreement >= 99%: derived rows treated as faithful and INCLUDED.")
    else:
        print("  faithfulness: no rows had BOTH a stored flag and loaded candidates (cannot cross-check).")
    cov = (n_usable / len(grouped_ids) * 100) if grouped_ids else 0.0
    print(f"  coverage (grouped rows with a usable genuine signal): {n_usable}/{len(grouped_ids)} = {cov:.1f}%")
    print(f"  rows dropped as AMBIGUOUS in SECTION 3 (unparseable candidates / no driver doc): {len(bucket_ambiguous)}")

    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
