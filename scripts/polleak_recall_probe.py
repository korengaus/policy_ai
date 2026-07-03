"""POLLEAK-RECALL — READ-ONLY, SELECT-only go/no-go gate for the political_subject
intake reject (POLLEAK-FIX Phase 2).

Replays the REAL predicate (news_collector._is_political_subject / _reject_title_reason
-> "political_subject") over ALL stored titles and proves it drops the election/
politician-SUBJECT cards WITHOUT dropping any policy card.

ACCEPTANCE BAR (go/no-go before commit):
  * POLICY cards dropped == 0  (a dropped card is POLICY if has_genuine_official_support
    is True OR it carries a policy-domain _ALLOWLIST head — the latter must be 0 by
    construction, since the master recall guard keeps any _ALLOWLIST title).
  * The POLLEAK population (titles carrying an election/politician marker) splits
    6 drop / 8 keep.

MEASUREMENT ONLY. Every DB statement is a SELECT (engine.connect(); no commit). Imports
the REAL predicate from news_collector — does NOT re-implement it. Reuses the SAME
_ALLOWLIST the predicate uses (news_collector._pol_policy_head_allowlist) for the policy
check, and the ELECTION/POLITICIAN marker subset (validated ⊆ hot_topics._DENYLIST) to
identify the POLLEAK population. has_genuine_official_support is read from
source_reliability_summary JSON (the official-status box's predicate).

SAFETY: SELECT-only; engine.connect(); no commit; lazy DB import inside the live path so
--selftest is fully offline. ASCII-guarded prints.

Usage:
    PYTHONPATH=. python scripts/polleak_recall_probe.py
    PYTHONPATH=. python scripts/polleak_recall_probe.py --selftest   # offline, no DB

Exit codes: 0 = dump printed / engine unavailable / selftest passed;
            1 = selftest failed OR acceptance bar VIOLATED (policy dropped > 0).
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

# The REAL predicate under test + the SAME allowlist it uses (no re-implementation).
import news_collector  # noqa: E402

# ELECTION/POLITICIAN marker subset (identical to POLLEAK-DIAG; validated ⊆ live
# hot_topics._DENYLIST at --selftest) — used ONLY to identify the POLLEAK population.
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


def _markers_in(text: str, markers) -> list[str]:
    t = str(text or "")
    return [mk for mk in markers if mk in t]


def _would_drop(title: str) -> bool:
    """The REAL production decision: does _reject_title_reason tag this title
    'political_subject'? (Imported predicate; not re-implemented.)"""
    return news_collector._reject_title_reason(str(title or "")) == "political_subject"


def _has_genuine(srs_text) -> bool:
    """has_genuine_official_support boolean inside source_reliability_summary JSON."""
    if isinstance(srs_text, dict):
        parsed = srs_text
    elif isinstance(srs_text, str) and srs_text:
        try:
            parsed = json.loads(srs_text)
        except Exception:  # noqa: BLE001
            return False
    else:
        return False
    if not isinstance(parsed, dict):
        return False
    val = parsed.get("has_genuine_official_support")
    return val if isinstance(val, bool) else False


def _has_policy_head(title: str) -> bool:
    """Any policy-domain _ALLOWLIST term (the SAME set the predicate's master recall
    guard uses). A dropped card with a policy head would be a recall failure — must be 0."""
    allow = news_collector._pol_policy_head_allowlist()
    t = str(title or "")
    return any(term in t for term in allow)


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== POLLEAK-RECALL --selftest (offline; no DB, no network) ===")
    failures = []

    # 1. ELECTION_MARKERS ⊆ live denylist (population identifier is a real subset).
    try:
        from hot_topics import _DENYLIST as _HT_DENYLIST  # type: ignore
        missing = [mk for mk in ELECTION_MARKERS if mk not in set(_HT_DENYLIST)]
        if missing:
            failures.append(f"ELECTION_MARKERS not in live _DENYLIST: {[_ascii(x) for x in missing]}")
        else:
            p(f"  [ok] ELECTION_MARKERS ({len(ELECTION_MARKERS)}) ⊆ live hot_topics._DENYLIST.")
    except Exception as exc:  # noqa: BLE001
        p(f"  [note] could not import hot_topics._DENYLIST offline ({str(exc)[:80]}).")

    # 2. The REAL predicate: SUBJECT drops, INCIDENTAL keeps (recall-safe).
    cases = [
        ("특검 통한 이재명 대통령 공소취소", True,  "name + action, no policy head"),
        ("6·3 지방선거 개표 결과 여당 당선", True,  "election-event, no policy head"),
        ("윤석열 前대통령 구속영장 청구",     True,  "name-at-head + action"),
        ("이재명 정부 1년 부동산 성적표",     False, "부동산 policy head -> KEEP"),
        ("한동훈, 복지 예산 확대 촉구",        False, "복지/예산 policy head -> KEEP"),
        ("이재명 취임 100일 경제정책 점검",   False, "정책 policy head -> KEEP"),
        ("정부 전세 공급 대책 발표",           False, "no political marker -> KEEP"),
    ]
    for title, exp, why in cases:
        got = _would_drop(title)
        tag = "ok" if got == exp else "xx"
        if got != exp:
            failures.append(f"predicate: {why!r} expected drop={exp} got {got}")
        p(f"  [{tag}] drop={got!s:5} exp={exp!s:5} | {why}")

    # 3. Policy-head + genuine readers.
    if not _has_policy_head("부동산 대책 발표") or _has_policy_head("이재명 공소취소"):
        failures.append("_has_policy_head misfire")
    if _has_genuine('{"has_genuine_official_support": true}') is not True:
        failures.append("_has_genuine read wrong")
    if _has_genuine("garbage") is not False:
        failures.append("_has_genuine did not fail soft")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (real predicate drops SUBJECT / keeps INCIDENTAL + policy-head + genuine readers)")
    return 0


# ---------------------------------------------------------------------------
# LIVE RUN (SELECT-only) — THE GO/NO-GO GATE
# ---------------------------------------------------------------------------
def run_live() -> int:
    p("=== POLLEAK-RECALL (READ-ONLY, SELECT-only) — go/no-go gate ===")

    import postgres_storage
    import sqlalchemy as sa

    # Confirm the master recall guard (_ALLOWLIST) actually loaded — else the predicate
    # fails OPEN (drops nothing) and the gate would be meaningless.
    allow = news_collector._pol_policy_head_allowlist()
    p(f"  _ALLOWLIST (master recall guard) loaded: "
      f"{'YES' if news_collector._POL_SIGNAL_CACHE.get('ok') else 'NO — predicate fail-open, reject disabled'} "
      f"({len(allow)} terms)")

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check. The live gate runs in the Worker Shell.)")
        return 0

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, title, domain, source_reliability_summary "
                    "FROM analysis_results ORDER BY id")
        ).all()

    total = len(rows)
    dropped = []
    policy_dropped = []
    for r in rows:
        m = r._mapping
        title = m["title"] or ""
        if _would_drop(title):
            genuine = _has_genuine(m["source_reliability_summary"])
            policy_head = _has_policy_head(title)
            dropped.append((m["id"], title, genuine, policy_head))
            if genuine or policy_head:
                policy_dropped.append((m["id"], title, genuine, policy_head))

    p("")
    p(f"=== RESULT — replay over ALL {total} stored titles ===")
    p(f"  total WOULD-DROP (political_subject): {len(dropped)}")
    for cid, title, genuine, ph in dropped:
        p(f"    id={cid} genuine={genuine} policy_head={ph} | {_ascii(str(title)[:80])}")

    # ---- ACCEPTANCE BAR: POLICY dropped must be 0 --------------------------------
    p("")
    p("=== ACCEPTANCE BAR — POLICY cards dropped MUST be 0 ===")
    p(f"  POLICY dropped (has_genuine True OR policy-domain _ALLOWLIST head): {len(policy_dropped)}")
    for cid, title, genuine, ph in policy_dropped:
        p(f"    !! id={cid} genuine={genuine} policy_head={ph} | {_ascii(str(title)[:80])}  <-- RECALL FAILURE")
    bar_ok = len(policy_dropped) == 0

    # ---- POLLEAK 14 population split (expect 6 drop / 8 keep) --------------------
    p("")
    p("=== POLLEAK-14 population (titles with an election/politician marker) ===")
    population = [(r._mapping["id"], r._mapping["title"]) for r in rows
                 if _markers_in(r._mapping["title"], ELECTION_MARKERS)]
    pop_drop = pop_keep = 0
    for cid, title in population:
        drop = _would_drop(title)
        pop_drop += 1 if drop else 0
        pop_keep += 0 if drop else 1
        p(f"    id={cid} | {'DROP' if drop else 'KEEP'} | {_ascii(str(title)[:74])}")
    p(f"  population={len(population)}  DROP={pop_drop}  KEEP={pop_keep}  (expected 6 drop / 8 keep)")
    split_ok = (pop_drop == 6 and pop_keep == 8)

    p("")
    p("=== GATE VERDICT ===")
    p(f"  POLICY-dropped == 0 : {'PASS' if bar_ok else 'FAIL'}")
    p(f"  POLLEAK split 6/8   : {'PASS' if split_ok else f'REVIEW (got {pop_drop}/{pop_keep})'}")
    if not bar_ok:
        p("  => GATE FAILED — recall violation. STOP: do NOT validate/commit; tune the boundary.")
    elif not split_ok:
        p("  => acceptance bar (0 policy dropped) HOLDS, but the 6/8 calibration differs — review the")
        p("     population outcomes above with the strategist before commit (not necessarily a recall bug).")
    else:
        p("  => GATE PASSED — 0 policy dropped and 6 drop / 8 keep. Proceed to validate.")

    p("")
    p("NOTE: predicate imported from news_collector (production code under test); not re-implemented.")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0 if bar_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY recall gate for the political_subject intake reject. "
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
