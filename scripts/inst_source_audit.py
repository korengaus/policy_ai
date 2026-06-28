"""CARD-BOX-INST-VERIFY Phase 1 — READ-ONLY institution-source audit.

Live "청년미래적금" analysis showed the source box institution = "정책브리핑" (the
generic platform/aggregator name) instead of a specific ministry like "금융위원회".
top_official_institution comes from the matched genuine candidate's `publisher`
(source_reliability_agent.summarize_source_reliability), and for the Policy Briefing
lane `publisher` = the API's MinisterCode (providers/policy_briefing.py:598). That
field NORMALLY carries the real ministry name — so "정책브리핑" means either the feed
returned the generic name for that record, OR the matched row is a section/landing
entry (title like "상단주요뉴스") rather than a real press release.

This probe samples recent GENUINE rows and, for the matched official-body candidate,
prints from the FULL stored source_candidates EVERY institution-relevant field that
is actually persisted — publisher, source_name, title, url, a head of the stored body
(raw_text), and the full key set — so we can judge per row:

  (A) "정책브리핑"/generic is the ONLY issuer info on the stored record (no specific
      ministry recoverable without re-fetching the feed) -> HONEST, ship as-is; OR
  (B) a specific ministry IS present on the same stored record (publisher already a
      real ministry, or discoverable in body/title) -> recoverable accuracy bug.

NOTE: SubTitle1 (which can hold "금융위원회 보도자료") is parsed to doc["subtitle"] in
the provider but is NOT copied into the stored candidate, so it is NOT visible here —
recovering it is a provider-side (re-fetch) change, flagged for Phase 2 if needed.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/inst_source_audit.py
    PYTHONPATH=. python scripts/inst_source_audit.py --limit 500 --show 25
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

# Mirror summarize_source_reliability's official-body gate (the matched candidate
# whose `publisher` becomes top_official_institution).
OFFICIAL_TYPES = {"official_government", "public_institution"}
OFFICIAL_BODY_MIN_SCORE = 55

# Generic platform/aggregator labels (NOT a specific ministry).
GENERIC_PLATFORM = {"정책브리핑", "대한민국 정책브리핑", "korea policy briefing", "korea.kr", ""}

# A specific Korean government issuer usually ends in one of these morphemes.
_MINISTRY_TAIL = re.compile(r"(부|처|청|위원회|원|실|공사|공단|은행|진흥원|관리원|연구원)$")

# Section/landing labels that are NOT a real document headline.
_SECTION_LABEL = re.compile(r"(상단주요뉴스|주요뉴스|정책뉴스|카드뉴스|보도자료\s*$|브리핑\s*$|기획&연재|핫이슈)")

# Institution-relevant fields we scan for on the matched candidate.
INST_FIELDS = ("publisher", "source_name", "ministry", "dept", "department",
               "org", "organization", "agency", "issuer", "author",
               "fss_bodo_publish_org", "subtitle")

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


def _body_score(c: dict) -> float:
    return max(
        _num(c.get("official_evidence_score")),
        _num(c.get("official_final_direct_match_score")),
        _num(c.get("official_body_match_score")),
    )


def _top_official_body_match(cands):
    """Mirror source_reliability_agent.summarize_source_reliability: the official
    body-match candidate whose `publisher` becomes top_official_institution."""
    pool = [
        c for c in (cands or [])
        if isinstance(c, dict)
        and c.get("source_type") in OFFICIAL_TYPES
        and c.get("raw_text_available")
        and c.get("official_body_match")
        and _body_score(c) >= OFFICIAL_BODY_MIN_SCORE
    ]
    if not pool:
        return None
    return max(pool, key=lambda c: (_body_score(c), _num(c.get("reliability_score")), c.get("title") or ""))


def _inst_fields_present(c: dict):
    out = {}
    if not isinstance(c, dict):
        return out
    for f in INST_FIELDS:
        v = c.get(f)
        if isinstance(v, str) and v.strip():
            out[f] = v.strip()
    return out


def _classify_publisher(publisher: str):
    p = (publisher or "").strip()
    if p.lower() in GENERIC_PLATFORM:
        return "generic_platform"
    if _MINISTRY_TAIL.search(p):
        return "specific_ministry"
    if _HANGUL.search(p):
        return "korean_other"
    return "english_or_other"


def _body_mentions_ministry(body: str):
    """Does the stored body start with a recognizable ministry token? (secondary,
    messy signal — the clean field SubTitle1 is not persisted)."""
    head = _trunc(body, 60)
    m = re.search(r"([가-힣]{2,12}(?:부|처|청|위원회|실))", head)
    return m.group(1) if m else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="inst_source_audit")
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
            "SELECT id, title, source_reliability_summary, source_candidates "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    audited = []
    for r in rows:
        summary = _parse_json(r.get("source_reliability_summary"))
        if not isinstance(summary, dict) or not bool(summary.get("has_genuine_official_support")):
            continue
        cands = _parse_json(r.get("source_candidates"))
        cands = cands if isinstance(cands, list) else []
        match = _top_official_body_match(cands)
        if match is None:
            continue
        publisher = str(match.get("publisher") or "").strip()
        fields = _inst_fields_present(match)
        body_min = _body_mentions_ministry(match.get("raw_text") or match.get("official_body_text") or "")
        title = str(match.get("title") or "")
        audited.append({
            "id": r.get("id"),
            "row_title": _trunc(r.get("title"), 30),
            "publisher": publisher,
            "pub_class": _classify_publisher(publisher),
            "fields": fields,
            "all_keys": sorted(match.keys()),
            "url": _trunc(match.get("url") or match.get("official_detail_url"), 50),
            "doc_title": _trunc(title, 40),
            "title_is_section": bool(_SECTION_LABEL.search(title)),
            "body_ministry": body_min,
            "is_policy_briefing": bool(str(match.get("policy_briefing_news_item_id") or "").strip()
                                       or match.get("retrieval_method") == "policy_briefing_api"),
        })

    total = len(audited)
    print("=" * 100)
    print(f"CARD-BOX-INST-VERIFY audit — scanned {len(rows)} recent rows, "
          f"{total} GENUINE with an official body match")
    print("=" * 100)

    print("\nPUBLISHER CLASS TALLY (over matched genuine candidates):")
    cls = Counter(a["pub_class"] for a in audited)
    for label, key in [
        ("specific_ministry (a real issuer — e.g. 금융위원회)", "specific_ministry"),
        ("generic_platform (정책브리핑 / korea.kr / empty)", "generic_platform"),
        ("korean_other (Korean, no ministry tail)", "korean_other"),
        ("english_or_other", "english_or_other"),
    ]:
        n = cls.get(key, 0)
        pct = (n / total * 100) if total else 0.0
        print(f"  {n:4d} ({pct:5.1f}%)  {label}")

    pb = [a for a in audited if a["is_policy_briefing"]]
    print(f"\nPolicy-Briefing-lane matches: {len(pb)}/{total}")

    # For the generic-platform rows: is a specific ministry recoverable from stored data?
    gen = [a for a in audited if a["pub_class"] == "generic_platform"]
    recoverable = [a for a in gen if a["body_ministry"]]
    section_titled = [a for a in gen if a["title_is_section"]]
    print(f"\nGENERIC-PLATFORM rows: {len(gen)}")
    print(f"  of those, a ministry token appears in the STORED body head: {len(recoverable)}")
    print(f"  of those, the document title looks like a SECTION label (상단주요뉴스 등): {len(section_titled)}")

    print("\n" + "-" * 100)
    print("PER-ROW DETAIL (most recent genuine rows):")
    print("-" * 100)
    for a in audited[: max(1, min(args.show, total or 1))]:
        other = ", ".join(f"{k}={_trunc(v,20)}" for k, v in a["fields"].items() if k != "publisher") or "(none)"
        print(f"\nid={a['id']}  [{a['pub_class']}]  policy_briefing={a['is_policy_briefing']}")
        print(f"  row_title : {a['row_title']}")
        print(f"  publisher : {a['publisher']!r}")
        print(f"  other inst fields on matched src : {other}")
        print(f"  doc_title : {a['doc_title']!r}   section_label={a['title_is_section']}")
        print(f"  url       : {a['url']}")
        print(f"  body head ministry token (secondary, messy): {a['body_ministry'] or '(none)'}")
        print(f"  ALL keys on matched candidate : {a['all_keys']}")

    print("\nDONE (read-only). NOTE: SubTitle1 (clean ministry label) is NOT persisted in")
    print("source_candidates, so it cannot appear above — recovering it is a provider re-fetch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
