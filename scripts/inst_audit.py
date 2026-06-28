"""CARD-BOX-INST Phase 1 — READ-ONLY institution-field audit.

Some GENUINE cards show the document-title first char as the avatar (e.g.
"청년미래적금" -> "청") instead of an institution initial (국/복/금...). That means
card.officialDetailInstitution rendered empty, i.e. source_reliability_summary
.top_official_institution was empty/missing for that row.

This probe samples recent GENUINE rows (has_genuine_official_support true) and,
per row, joins the slim summary against the FULL-row source_candidates so we can
tell WHY the institution is empty. Three causes are distinguished:

  (a) UNMAPPED-ENGLISH : top_official_institution holds an English source_name not
      in the frontend's English->Korean map. (Symptom would be raw ENGLISH on the
      card, NOT a document initial -- publicInstitutionName passes unmapped input
      through unchanged.)
  (b) EMPTY-SUMMARY+INST-IN-CANDIDATE : top_official_institution is empty/missing,
      but the matched genuine source object in source_candidates DOES carry an
      institution (publisher / source_name). This is the document-initial symptom:
      the genuine signal came via a provider lane (정책브리핑 / 법제처 / FSS) whose
      object lives only in source_candidates, while top_official_institution is set
      only from the crawler lane (the verification_card "if usable" branch).
  (c) SPARSE : neither the summary nor the matched candidate carries any institution
      -> document-only is the HONEST render (no bug, just sparse data).

Per genuine row it prints: id | title(30) | top_official_institution(raw) |
source_name/publisher on matched src | other inst fields present | hypothesis.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/inst_audit.py
    PYTHONPATH=. python scripts/inst_audit.py --limit 500 --show 25
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

# Mirror official_evidence_resolution.py primary-document gate (the genuine path).
PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
PRIMARY_STRONG = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75

# The English source_name catalog values the frontend publicInstitutionName() map
# recognizes (main.js ~531-551). An English value NOT in here renders as raw
# English (passthrough), not document-initial -> that is hypothesis (a).
MAPPED_ENGLISH = {
    "ibk industrial bank of korea",
    "korea housing finance corporation",
    "national tax service",
    "korean national police agency",
    "national assembly",
    "local government",
    "fair trade commission",
    "ministry of justice",
    "korea policy briefing",
    "government24",
    "current article body",
    "financial services commission",
    "financial supervisory service",
    "ministry of land, infrastructure and transport",
    "ministry of economy and finance",
    "ministry of smes and startups",
    "bank of korea",
    "korea housing & urban guarantee corporation",
    "korea land & housing corporation",
}

# Institution-bearing fields we look for on the matched candidate, in fallback order.
INST_FIELDS = ("source_name", "publisher", "institution", "agency", "ministry", "provider", "org", "body")

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


def _num(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _trunc(v, n=100) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _candidate_score(c: dict) -> float:
    return max(
        _num(c.get("official_evidence_score")),
        _num(c.get("official_final_direct_match_score")),
        _num(c.get("official_body_match_score")),
        _num(c.get("score")),
    )


def _driving_candidate(cands):
    """The candidate that makes the row genuine: a strong primary-marker match
    (preferred), else the first official_body_match=True candidate."""
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        has_marker = any(str(c.get(f) or "").strip() for f in PRIMARY_MARKER_FIELDS)
        clf = str(c.get("official_evidence_classification") or c.get("official_direct_match_classification") or "")
        if has_marker and c.get("official_body_match") and clf == PRIMARY_STRONG and _candidate_score(c) >= PRIMARY_MIN_SCORE:
            return c, "primary_marker"
    for c in cands or []:
        if isinstance(c, dict) and c.get("official_body_match"):
            return c, "body_match"
    return None, "none"


def _inst_fields_present(c: dict):
    """Return {field: value} for institution-bearing fields actually set on c."""
    out = {}
    if not isinstance(c, dict):
        return out
    for f in INST_FIELDS:
        v = c.get(f)
        if isinstance(v, str) and v.strip():
            out[f] = v.strip()
    return out


def _classify(inst_raw, inst_key_present, cand_fields):
    """Decide which hypothesis a genuine row falls under.

    inst_raw         : summary.top_official_institution value (may be None)
    inst_key_present : whether the key existed in the summary dict at all
    cand_fields      : {field: value} institution fields on the matched candidate
    """
    raw = (inst_raw or "").strip()
    if raw:
        if _HANGUL.search(raw):
            return "ok_korean", "summary inst already Korean -> renders fine"
        if raw.lower() in MAPPED_ENGLISH:
            return "ok_mapped_english", "summary inst is mapped English -> renders Korean"
        return "a_unmapped_english", f"summary inst English NOT in map -> renders raw English: {raw!r}"
    # raw is empty/missing -> the document-initial symptom
    if cand_fields:
        where = ", ".join(f"{k}={v!r}" for k, v in cand_fields.items())
        tag = "missing_key(old_row?)" if not inst_key_present else "empty_string(provider_lane?)"
        return "b_empty_inst_in_candidate", f"{tag}; institution lives on matched src: {where}"
    return "c_sparse", "no institution on summary OR matched candidate -> document-only is honest"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="inst_audit")
    parser.add_argument("--limit", type=int, default=500, help="recent rows to scan for genuine ones")
    parser.add_argument("--show", type=int, default=25, help="genuine rows to print in full")
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
            "SELECT id, title, claim_text, source_reliability_summary, source_candidates "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    genuine_rows = []
    for r in rows:
        summary = _parse_json(r.get("source_reliability_summary"))
        if not isinstance(summary, dict):
            continue
        if not bool(summary.get("has_genuine_official_support")):
            continue
        cands = _parse_json(r.get("source_candidates"))
        cands = cands if isinstance(cands, list) else []
        driver, drive_kind = _driving_candidate(cands)
        cand_fields = _inst_fields_present(driver) if driver else {}
        inst_key_present = "top_official_institution" in summary
        inst_raw = summary.get("top_official_institution")
        bucket, note = _classify(inst_raw, inst_key_present, cand_fields)
        genuine_rows.append({
            "id": r.get("id"),
            "title": _trunc(r.get("title"), 30),
            "inst_raw": inst_raw,
            "inst_key_present": inst_key_present,
            "doc_title": _trunc(summary.get("top_official_detail_title"), 40),
            "drive_kind": drive_kind,
            "cand_fields": cand_fields,
            "bucket": bucket,
            "note": note,
        })

    total = len(genuine_rows)
    print("=" * 100)
    print(f"CARD-BOX-INST audit — scanned {len(rows)} recent rows, {total} GENUINE "
          "(has_genuine_official_support=true)")
    print("=" * 100)

    counts = Counter(g["bucket"] for g in genuine_rows)
    print("\nHYPOTHESIS TALLY (over genuine rows):")
    for label, key in [
        ("(a) unmapped-English summary inst (renders raw English, not doc-initial)", "a_unmapped_english"),
        ("(b) EMPTY summary inst BUT institution on matched src (document-initial bug)", "b_empty_inst_in_candidate"),
        ("(c) sparse — no institution anywhere (document-only is honest)", "c_sparse"),
        ("ok — summary inst already Korean", "ok_korean"),
        ("ok — summary inst mapped English -> Korean", "ok_mapped_english"),
    ]:
        n = counts.get(key, 0)
        pct = (n / total * 100) if total else 0.0
        print(f"  {n:4d} ({pct:5.1f}%)  {label}")

    # The fields seen carrying the institution on the matched src for bucket (b).
    field_hits = Counter()
    for g in genuine_rows:
        if g["bucket"] == "b_empty_inst_in_candidate":
            for k in g["cand_fields"]:
                field_hits[k] += 1
    if field_hits:
        print("\nFor bucket (b) — which field on the matched src holds the institution:")
        for k, n in field_hits.most_common():
            print(f"  {n:4d}  {k}")

    # Distinct unmapped English source_name values (bucket a) -> map-extension list.
    unmapped = Counter()
    for g in genuine_rows:
        if g["bucket"] == "a_unmapped_english":
            unmapped[str(g["inst_raw"]).strip()[:40]] += 1
    if unmapped:
        print("\nFor bucket (a) — distinct unmapped English source_name values (extend the map):")
        for v, n in unmapped.most_common():
            print(f"  {n:4d}  {v!r}")

    print("\n" + "-" * 100)
    print("PER-ROW DETAIL (most recent genuine rows):")
    print("-" * 100)
    hdr = f"{'id':>7} | {'title':30} | {'inst_raw':18} | {'matched-src inst fields':40} | bucket"
    print(hdr)
    print("-" * len(hdr))
    for g in genuine_rows[: max(1, min(args.show, total or 1))]:
        cand = ", ".join(f"{k}={_trunc(v,18)}" for k, v in g["cand_fields"].items()) or "(none)"
        ir = g["inst_raw"]
        ir_disp = "(missing-key)" if not g["inst_key_present"] else ("(empty)" if not (ir or "").strip() else _trunc(ir, 18))
        print(f"{str(g['id']):>7} | {g['title']:30} | {ir_disp:18} | {_trunc(cand,40):40} | {g['bucket']}")
        print(f"        notes: drive={g['drive_kind']}; doc={g['doc_title']!r}; {g['note']}")

    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
