"""DESIGN-3B Phase 1 — READ-ONLY keyword/hashtag token-quality probe.

Decides show-now vs defer for card HASHTAGS. Samples recent analysis_results rows
and, for each, derives candidate keyword tokens from:
  * normalized_claims  (actor / target / object / quantity / location)  [FULL row]
  * source_candidates  (matched_concepts / matched_query_terms)         [FULL row]
  * claims             (first claim sentence, only as a fallback signal) [slim]

For each row it classifies tokens as USABLE vs JUNK (empty / 'unknown'/'미상' /
single-char / overly-generic like 정부·정책·뉴스·기관·관련), derives the "#hashtag"
set it WOULD render today, and prints 10-15 samples so the operator can EYEBALL
clean (#공시가격 #서울아파트) vs junk (#unknown #정부). Ends with a HASHTAGS-READY
verdict.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/keyword_quality_probe.py
    PYTHONPATH=. python scripts/keyword_quality_probe.py --limit 150 --samples 15
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


# Tokens that are NOT useful as a hashtag (too generic to distinguish a story, or
# explicit placeholders). A token made only of these / matching these is JUNK.
_GENERIC = frozenset({
    "정부", "정책", "뉴스", "기사", "기관", "관련", "발표", "지원", "금융", "확대",
    "강화", "추진", "검토", "운영", "계획", "방안", "사업", "제도", "관리", "대책",
    "당국", "공식", "내용", "결과", "오늘", "이번", "해당", "전체", "주요", "상황",
})
_PLACEHOLDER = frozenset({"unknown", "미상", "없음", "n/a", "na", "null", "none", "-", "기타", "미분류"})


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


def _clean(tok) -> str:
    return re.sub(r"\s+", " ", str(tok or "")).strip().strip("#·,.\"'()[]")


def _is_junk(tok: str) -> bool:
    t = _clean(tok)
    if not t:
        return True
    low = t.lower()
    if low in _PLACEHOLDER:
        return True
    # single Hangul/letter char, or pure digits/punct
    if len(t) < 2:
        return True
    if re.fullmatch(r"[\d\W_]+", t):
        return True
    if t in _GENERIC:
        return True
    return False


def _tokens_from_normalized(normalized_claims) -> list:
    out = []
    for nc in normalized_claims or []:
        if isinstance(nc, dict):
            for k in ("target", "object", "actor", "quantity", "location"):
                v = _clean(nc.get(k))
                if v:
                    out.append(v)
    return out


def _tokens_from_candidates(source_candidates) -> list:
    out = []
    for cand in source_candidates or []:
        if not isinstance(cand, dict):
            continue
        for key in ("matched_concepts", "matched_query_terms"):
            v = cand.get(key)
            if isinstance(v, list):
                out.extend(_clean(x) for x in v if _clean(x))
            elif isinstance(v, str) and v:
                out.extend(_clean(x) for x in v.split(",") if _clean(x))
    return out


def _hashtags(tokens, k=4) -> list:
    """The #hashtag set we'd render today: usable tokens, deduped, length-capped."""
    seen, tags = set(), []
    for t in tokens:
        if _is_junk(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append("#" + t.replace(" ", ""))
        if len(tags) >= k:
            break
    return tags


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="keyword_quality_probe")
    parser.add_argument("--limit", type=int, default=120, help="recent rows to sample")
    parser.add_argument("--samples", type=int, default=15, help="sample rows to print")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 2000))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, title, claims, normalized_claims, source_candidates "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)
    rows_with_usable = 0
    rows_with_usable_normonly = 0
    junk_counter = Counter()
    usable_counter = Counter()
    samples = []

    for r in rows:
        nc = _parse_json(r["normalized_claims"]) or []
        sc = _parse_json(r["source_candidates"]) or []
        norm_tokens = _tokens_from_normalized(nc)
        cand_tokens = _tokens_from_candidates(sc)
        all_tokens = norm_tokens + cand_tokens

        usable = [t for t in all_tokens if not _is_junk(t)]
        junk = [t for t in all_tokens if _is_junk(t)]
        for t in usable:
            usable_counter[t] += 1
        for t in junk:
            junk_counter[_clean(t).lower() or "(empty)"] += 1
        if usable:
            rows_with_usable += 1
        if any(not _is_junk(t) for t in norm_tokens):
            rows_with_usable_normonly += 1

        if len(samples) < args.samples:
            samples.append({
                "id": r["id"],
                "title": re.sub(r"\s+", " ", str(r["title"] or ""))[:48],
                "norm": norm_tokens[:8],
                "cand": sorted(set(cand_tokens))[:8],
                "hashtags": _hashtags(all_tokens),
            })

    def pct(x):
        return f"{x}/{n} ({x / n * 100:.1f}%)" if n else "0/0"

    print("=" * 84)
    print(f"DESIGN-3B keyword/hashtag quality — sampled {n} recent rows")
    print("=" * 84)
    print(f"rows with >=1 USABLE token (norm OR candidates): {pct(rows_with_usable)}")
    print(f"rows with >=1 USABLE token from normalized_claims ONLY: {pct(rows_with_usable_normonly)}")
    print()
    print("--- top JUNK tokens (generic/placeholder/too-short — would make bad #tags) ---")
    for tok, c in junk_counter.most_common(15):
        print(f"   {c:4}  {tok}")
    print()
    print("--- top USABLE tokens (specific — would make good #tags) ---")
    for tok, c in usable_counter.most_common(15):
        print(f"   {c:4}  {tok}")
    print()
    print(f"--- {len(samples)} SAMPLE rows (EYEBALL: are the #hashtags clean or junk?) ---")
    for s in samples:
        print(f"\n  id={s['id']}  {s['title']}")
        print(f"     normalized tokens: {s['norm']}")
        print(f"     candidate concepts: {s['cand']}")
        print(f"     => #hashtags now : {s['hashtags'] or '(none usable)'}")

    usable_rate = rows_with_usable / n if n else 0
    print("\n" + "=" * 84)
    ready = usable_rate >= 0.7
    print(f"HASHTAGS-READY: {'yes' if ready else 'no'}  "
          f"(usable-token coverage {usable_rate * 100:.1f}%; threshold 70%)")
    print("Reason: high coverage of SPECIFIC tokens + clean samples = show now; lots of")
    print("'unknown'/generic tokens or sparse coverage = defer (or curate) hashtags.")
    print("Read the SAMPLE #hashtags above — coverage % alone isn't enough; eyeball cleanliness.")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
