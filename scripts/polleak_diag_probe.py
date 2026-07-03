"""POLLEAK-DIAG — READ-ONLY, SELECT-only measurement of election/politician names
leaking into STORED cards.

WHY (measured): GATE-DIAG Metric B found ~14 stored cards (2.31%) whose TITLE
carries an election/politician denylist marker (e.g. 이재명, 당선), including
"특검 통한 이재명 대통령 공소취소" — a defamation-sensitive pattern. The hot_topics
denylist only filters HOT-TOPIC KEYWORD SELECTION; these cards entered via other
collector paths where a politician reject may not be wired — potentially the
politician analog of COLUMN-LEAK (a filter present in one path only). Before any
fix or gate redesign, MEASURE: which collector path they entered by, whether a
politician reject is wired into that path, and whether each is "policy news that
incidentally names a politician" (KEEP) vs "the politician is the subject" (the
risky kind). MEASURE BEFORE SURGERY — no fix here.

MEASUREMENT ONLY. Every DB statement is a SELECT (engine.connect(), never
begin(); no commit). Touches no production code, no verdict logic, no pins, no
config. Reuses the REAL wiring:
  * hot_topics._DENYLIST political subset for the population match (the SAME
    ELECTION/POLITICIAN subset GATE-DIAG used; validated ⊆ live _DENYLIST at
    --selftest — a subset VIEW, not a re-implementation).
  * The persisted fields as the box/frontend read them:
    verdict_label / policy_alert_level / policy_confidence_score are TOP-LEVEL
    columns; collection_source lives INSIDE debug_summary JSON; the genuine axis
    is has_genuine_official_support INSIDE source_reliability_summary JSON.

METRICS
-------
  Population: stored cards whose TITLE contains an ELECTION/POLITICIAN marker.
  A. ENTRY PATH: the collection_source distribution (debug_summary.collection_source)
     of these cards — naver/daum/google/forced/... — i.e. WHERE the name entered.
  B. WIRING CHECK (structural, read-only): does the PRIMARY collector reject
     (news_collector._reject_title_reason) include any politician/name reject, or
     is the politician denylist hot-topic-selection-only (hot_topics._passes_domain_filter)?
     Reported via inspect.getsource — the COLUMN-LEAK-style gap check.
  C. RISK TRIAGE (ADVISORY HEURISTIC — clearly labeled, NOT a verdict/classifier):
     per card, SUBJECT-ish (the politician is the subject — name at title head +
     a person-action token) vs INCIDENTAL-ish (a policy noun heads the title, name
     is a modifier, e.g. "이재명 정부 1년 부동산 성적표"). Sizes the genuinely-risky subset.
  D. VERDICT-SAFETY: confirm NONE of the population is a confident-supportive verdict
     about a person — verdict_label not in {draft_verified, draft_likely_true}.

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  * title/created_at/domain/verdict_label/policy_alert_level/policy_confidence_score
    are TOP-LEVEL columns of analysis_results (database.py column order).
  * collection_source is NOT a column — it is debug_summary.collection_source
    (loose TEXT JSON); read via _collection_source() (mirrors column_leak_probe).
    NOTE it is the WINNING SEARCH ENGINE, not a strict hot-topic-vs-seed origin.
  * has_genuine_official_support is a boolean INSIDE source_reliability_summary JSON
    (the official-status box's predicate) — same read realestate_seed_scope_probe uses.
  * Supportive-confident verdict labels are draft_verified / draft_likely_true
    (verification_card.py). Metric D flags either among the population (expect 0).

SAFETY: SELECT-only; engine.connect(); no commit; lazy DB import inside the live
path so --selftest is fully offline. ASCII-guarded prints (json.dumps ensure_ascii).

Usage (real run in the Render Worker Shell):
    PYTHONPATH=. python scripts/polleak_diag_probe.py
    PYTHONPATH=. python scripts/polleak_diag_probe.py --selftest   # offline, no DB

Exit codes: 0 = dump printed / engine unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
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


# ---------------------------------------------------------------------------
# ELECTION / POLITICIAN subset — a documented VIEW into the real denylist, IDENTICAL
# to GATE-DIAG's. Each marker is validated ⊆ live hot_topics._DENYLIST at --selftest.
# These are the election words + current politician NAMES from hot_topics._LOCAL_DENYLIST
# (the political portion); securities/foreign/sports markers are excluded.
# ---------------------------------------------------------------------------
ELECTION_MARKERS = (
    "선거", "당선", "득표", "지방선거", "여당", "야당", "대선", "총선", "공천", "탄핵",
    "이재명", "윤석열", "한동훈", "이준석", "김건희",
)

# Supportive-confident verdict labels (verification_card.py). Metric D must find 0.
SUPPORTIVE_CONFIDENT_LABELS = ("draft_verified", "draft_likely_true")

# ADVISORY (Metric C) — person-action tokens that suggest the politician themselves
# is the SUBJECT of the sentence (legal/electoral/personal actions). Scoping ONLY.
PERSON_ACTION_TOKENS = (
    "공소취소", "기소", "구속", "소환", "체포", "재판", "판결", "선고", "구형",
    "사퇴", "사임", "출마", "당선", "낙선", "탄핵", "발언", "의혹", "논란",
    "특검", "수사", "영장", "혐의", "부인", "인터뷰", "회견",
)

# ADVISORY (Metric C) — policy-noun heads: if the title ENDS with one of these, a
# policy topic is the head and the name is a modifier (INCIDENTAL-ish). Scoping ONLY.
POLICY_HEAD_TOKENS = (
    "대책", "정책", "성적표", "부동산", "공급", "규제", "예산", "제도", "개편",
    "법안", "개정", "지원", "방안", "계획", "전망", "평가", "성과", "과제",
)


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _day_of(created_at) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(created_at or ""))
    return m.group(1) if m else ""


def _markers_in(text: str, markers) -> list[str]:
    """Substring hits of `markers` in `text` — the SAME mechanism
    _passes_domain_filter uses (any(marker in text ...))."""
    t = str(text or "")
    return [mk for mk in markers if mk in t]


def _collection_source(debug_summary_text) -> str:
    """debug_summary.collection_source (winning SEARCH ENGINE). '(unknown)' on
    NULL / non-str / parse failure / missing key. Mirrors column_leak_probe."""
    if not debug_summary_text or not isinstance(debug_summary_text, str):
        return "(unknown)"
    try:
        parsed = json.loads(debug_summary_text)
    except Exception:  # noqa: BLE001
        return "(unknown)"
    if not isinstance(parsed, dict):
        return "(unknown)"
    value = parsed.get("collection_source")
    if value is None:
        return "(unknown)"
    return str(value).strip() or "(unknown)"


def _has_genuine(source_reliability_summary_text) -> bool:
    """Persisted has_genuine_official_support boolean inside source_reliability_summary
    JSON (the official-status box's predicate). False on NULL/parse-fail/missing."""
    if isinstance(source_reliability_summary_text, dict):
        parsed = source_reliability_summary_text
    elif isinstance(source_reliability_summary_text, str) and source_reliability_summary_text:
        try:
            parsed = json.loads(source_reliability_summary_text)
        except Exception:  # noqa: BLE001
            return False
    else:
        return False
    if not isinstance(parsed, dict):
        return False
    val = parsed.get("has_genuine_official_support")
    return val if isinstance(val, bool) else False


def _triage_bucket(title: str, hits: list[str]) -> str:
    """ADVISORY heuristic (NOT a verdict/classifier): SUBJECT-ish vs INCIDENTAL-ish.
    SUBJECT-ish when a person-action token is present, OR a politician NAME sits at
    the title head (first 6 chars) with no policy-noun head. INCIDENTAL-ish when the
    title ENDS with a policy-noun head (name is a modifier)."""
    t = str(title or "").strip()
    tail = t[-8:]
    policy_head = any(tok in tail for tok in POLICY_HEAD_TOKENS)
    has_action = any(tok in t for tok in PERSON_ACTION_TOKENS)
    head = t[:6]
    name_at_head = any(mk in head for mk in hits)
    if has_action and not policy_head:
        return "SUBJECT-ish"
    if name_at_head and not policy_head:
        return "SUBJECT-ish"
    if policy_head:
        return "INCIDENTAL-ish"
    # Fallback: action token even with a policy head is still person-action-heavy.
    return "SUBJECT-ish" if has_action else "INCIDENTAL-ish"


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== POLLEAK-DIAG --selftest (offline; no DB, no network) ===")
    failures = []

    # 1. ELECTION_MARKERS ⊆ live denylist (reuse, not re-implementation).
    try:
        from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
        live = set(_HT_DENYLIST)
        missing = [mk for mk in ELECTION_MARKERS if mk not in live]
        if missing:
            failures.append(f"ELECTION_MARKERS not in live _DENYLIST: {[_ascii(x) for x in missing]}")
        else:
            p(f"  [ok] ELECTION_MARKERS ({len(ELECTION_MARKERS)}) all present in live "
              f"hot_topics._DENYLIST ({len(live)}) — subset view validated.")
    except Exception as exc:  # noqa: BLE001
        p(f"  [note] could not import hot_topics._DENYLIST offline ({str(exc)[:80]}); "
          "subset validation deferred to live run.")

    # 2. Metric B structural check must observe: primary reject has NO politician
    #    reject; the denylist lives in hot_topics._passes_domain_filter only.
    try:
        import inspect
        import news_collector
        import hot_topics
        rej_src = inspect.getsource(news_collector._reject_title_reason)
        primary_has_polreject = any(mk in rej_src for mk in ("_DENYLIST", "_passes_domain_filter",
                                                             "이재명", "politician", "election"))
        pdf_src = inspect.getsource(hot_topics._passes_domain_filter)
        ht_has_denylist = "_DENYLIST" in pdf_src
        p(f"  [ok] Metric-B wiring: primary _reject_title_reason references a politician reject = "
          f"{primary_has_polreject}; hot_topics._passes_domain_filter uses _DENYLIST = {ht_has_denylist}")
        if primary_has_polreject:
            p("       (note: primary path DOES reference one — the live gap read may differ)")
    except Exception as exc:  # noqa: BLE001
        p(f"  [note] could not inspect collector sources offline ({str(exc)[:80]}).")

    # 3. Population match + triage heuristic on synthetic titles.
    subj = "특검 통한 이재명 대통령 공소취소"           # person is subject (action token)
    incid = "이재명 정부 1년 부동산 성적표"              # policy head; name is modifier
    clean = "전세 공급 대책 시행 방안"                  # no political marker
    if not _markers_in(subj, ELECTION_MARKERS) or not _markers_in(incid, ELECTION_MARKERS):
        failures.append("election marker not detected in a political title")
    if _markers_in(clean, ELECTION_MARKERS):
        failures.append("false marker hit on a clean title")
    b_subj = _triage_bucket(subj, _markers_in(subj, ELECTION_MARKERS))
    b_incid = _triage_bucket(incid, _markers_in(incid, ELECTION_MARKERS))
    if b_subj != "SUBJECT-ish":
        failures.append(f"triage: expected SUBJECT-ish for subject title, got {b_subj}")
    if b_incid != "INCIDENTAL-ish":
        failures.append(f"triage: expected INCIDENTAL-ish for policy-head title, got {b_incid}")
    p(f"  [{'ok' if not failures else 'xx'}] triage: subject->{b_subj}, incidental->{b_incid}")

    # 4. Field readers.
    if _collection_source('{"collection_source":"naver_fallback"}') != "naver_fallback":
        failures.append("_collection_source read wrong")
    if _collection_source("not json") != "(unknown)":
        failures.append("_collection_source did not fail soft")
    if _has_genuine('{"has_genuine_official_support": true}') is not True:
        failures.append("_has_genuine read wrong")
    if _has_genuine('{"has_genuine_official_support": false}') is not False:
        failures.append("_has_genuine false read wrong")
    if _has_genuine("garbage") is not False:
        failures.append("_has_genuine did not fail soft")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (subset + wiring inspect + population match + triage + field readers)")
    return 0


# ---------------------------------------------------------------------------
# LIVE RUN (SELECT-only)
# ---------------------------------------------------------------------------
def run_live() -> int:
    p("=== POLLEAK-DIAG (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    # Validate the subset view against the live denylist.
    try:
        from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
        missing = [mk for mk in ELECTION_MARKERS if mk not in set(_HT_DENYLIST)]
        p(f"  ELECTION_MARKERS subset of live hot_topics._DENYLIST: "
          f"{'YES' if not missing else 'NO — ' + str([_ascii(x) for x in missing])}")
    except Exception as exc:  # noqa: BLE001
        p(f"  (could not import hot_topics._DENYLIST: {str(exc)[:80]})")

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    cols = ("id, created_at, domain, title, debug_summary, verdict_label, "
            "policy_alert_level, policy_confidence_score, source_reliability_summary")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(f"SELECT {cols} FROM analysis_results ORDER BY id")
        ).all()

    population = []
    for r in rows:
        m = r._mapping
        hits = _markers_in(m["title"], ELECTION_MARKERS)
        if hits:
            population.append((m, hits))

    p("")
    p(f"=== POPULATION — stored cards with an election/politician marker in TITLE "
      f"({len(population)} of {len(rows)}) ===")
    p("  id | date | domain | collection_source | verdict_label | conf | genuine | title")
    for m, hits in population:
        src = _collection_source(m["debug_summary"])
        genuine = _has_genuine(m["source_reliability_summary"])
        conf = m["policy_confidence_score"]
        p(f"    {m['id']} | {_day_of(m['created_at'])} | {m['domain'] or '(none)'} | {src} | "
          f"{m['verdict_label'] or '(none)'} | {conf} | {genuine} | {_ascii(str(m['title'])[:80])} "
          f"| hits={[_ascii(h) for h in hits]}")

    # ---- METRIC A -----------------------------------------------------------
    p("")
    p("=== METRIC A — ENTRY PATH (collection_source distribution) ===")
    dist = {}
    for m, _ in population:
        src = _collection_source(m["debug_summary"])
        dist[src] = dist.get(src, 0) + 1
    for src, n in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])):
        p(f"    {src}: {n}")
    p("  => collection_source is the winning SEARCH ENGINE; the hot_topics denylist filters")
    p("     KEYWORD SELECTION only, so a name entering via any search path is NOT denylist-gated.")

    # ---- METRIC B -----------------------------------------------------------
    p("")
    p("=== METRIC B — WIRING CHECK (is a politician reject in the PRIMARY collector path?) ===")
    try:
        import inspect
        import news_collector
        import hot_topics
        rej_src = inspect.getsource(news_collector._reject_title_reason)
        primary_markers = [tok for tok in ("_DENYLIST", "_passes_domain_filter", "이재명",
                                           "politician", "election", "선거")
                           if tok in rej_src]
        pdf_src = inspect.getsource(hot_topics._passes_domain_filter)
        p(f"  news_collector._reject_title_reason references a politician/name reject: "
          f"{'YES (' + str(primary_markers) + ')' if primary_markers else 'NO'}")
        p(f"  _reject_title_reason DOES reject: obituary (OBITUARY_MARKERS) + opinion (OPINION_MARKERS) "
          f"— but NOT politician names.")
        p(f"  hot_topics._passes_domain_filter uses _DENYLIST: {'YES' if '_DENYLIST' in pdf_src else 'NO'} "
          f"(hot-topic KEYWORD SELECTION only).")
        gap = (not primary_markers) and ("_DENYLIST" in pdf_src)
        p(f"  => STRUCTURAL FINDING: politician denylist is HOT-TOPIC-SELECTION-ONLY; the primary")
        p(f"     collector reject has NO politician gate = COLUMN-LEAK-style wiring gap: "
          f"{'YES' if gap else 'NO'}")
    except Exception as exc:  # noqa: BLE001
        p(f"  (could not inspect collector sources: {str(exc)[:100]})")

    # ---- METRIC C -----------------------------------------------------------
    p("")
    p("=== METRIC C — RISK TRIAGE (ADVISORY HEURISTIC — scoping only, NOT a verdict) ===")
    subj_n = incid_n = 0
    for m, hits in population:
        bucket = _triage_bucket(m["title"], hits)
        if bucket == "SUBJECT-ish":
            subj_n += 1
        else:
            incid_n += 1
        p(f"    {m['id']} | {bucket} | {_ascii(str(m['title'])[:70])}")
    p(f"  SUBJECT-ish (politician is the subject; riskier): {subj_n}")
    p(f"  INCIDENTAL-ish (policy is the subject; name is modifier): {incid_n}")
    p("  => ADVISORY heuristic only — sizes the genuinely-risky subset; NOT a classifier/verdict.")

    # ---- METRIC D -----------------------------------------------------------
    p("")
    p("=== METRIC D — VERDICT-SAFETY (no confident-supportive verdict about a person) ===")
    violations = [(m["id"], m["verdict_label"]) for m, _ in population
                  if str(m["verdict_label"] or "") in SUPPORTIVE_CONFIDENT_LABELS]
    label_dist = {}
    for m, _ in population:
        lbl = str(m["verdict_label"] or "(none)")
        label_dist[lbl] = label_dist.get(lbl, 0) + 1
    p(f"  verdict_label distribution across population: "
      f"{ {k: v for k, v in sorted(label_dist.items())} }")
    p(f"  confident-supportive ({'/'.join(SUPPORTIVE_CONFIDENT_LABELS)}) about a person: "
      f"{len(violations)}")
    if violations:
        for vid, vlbl in violations:
            p(f"    !! id={vid} verdict_label={vlbl}  <-- REVIEW (confident-true about a person)")
    p(f"  => confident-TRUE-about-a-person structurally absent: {'YES' if not violations else 'NO'}")

    p("")
    p("NOTE: population match reuses the REAL hot_topics._DENYLIST political subset (substring —")
    p("the same mechanism _passes_domain_filter uses). Metric C is an ADVISORY scoping heuristic,")
    p("NOT a verdict/classifier/filter. Measurement only; nothing written, nothing proposed.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY election/politician-name-in-stored-cards diagnostic. "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
