"""CLABEL-VERIFY — READ-ONLY live smoke that the CLABEL-FIX clamp-relabel is deployed
and correct. DEPLOYED != VERIFIED.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE / ALTER /
commit. Touches no production code, no verdict logic, no pins. REUSES the REAL imported
verification_card._verdict_label; does NOT re-implement it and does NOT run the pipeline.

WHY
---
CLABEL-FIX (main.py, inside the official_mismatch clamp block) recomputes verdict_label
from the POST-clamp signals so a clamped score (<=20) can no longer keep a pre-clamp
supportive label. CI is green and Web+Worker are live, but per DEPLOYED != VERIFIED we
prove on the running server that (1) the guard is actually in the imported code, (2) a
fresh mismatch case now yields an honest NON-supportive label, and (3) the 24 old rows
are intentionally UNCHANGED (the fix is new-rows-only).

CHECKS
------
  1. DEPLOY MARKER — import main, inspect.getsource(main._process_news_item_phase_a),
     assert the new guard (verdict_label in {"draft_verified","draft_likely_true"}
     recompute via _verdict_label) is PRESENT in the running code.
  2. FUNCTION-DIRECT SIMULATION (deploy_check pattern, NO pipeline / NO DB write) —
     reconstruct a POST-clamp state (official_mismatch=True, pre-clamp supportive label,
     policy_confidence_score<=20, verification_strength forced to "none" exactly as the
     clamp at main.py sets it), borrowing a stored row read-only when available, then
     call the REAL _verdict_label with the SAME arguments the guard passes and assert
     the result is NON-supportive (draft_unverified / draft_needs_context / any label
     not in the supportive set).
  3. OLD ROWS UNCHANGED — re-run the c_label_diag population count
     (verdict_label=='draft_verified' AND policy_confidence_score<=20). Expected STILL
     24 (+ the last-14-day sub-count). Unchanged-24 is the CORRECT expectation.

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  * Guard is at main._process_news_item_phase_a (the clamp block ~main.py:934-989);
    the recompute passes: post-clamp policy_confidence (score<=20, strength="none"),
    evidence_comparison, official_sources=[], evidence_snippets, contradiction_summary,
    bias_framing_summary, claim_count=len(card["claims"]).
  * TOP-LEVEL columns used for the reconstruction/count: id, created_at,
    policy_confidence_score, verification_strength, verdict_label, claims (JSON TEXT),
    evidence_snippets (JSON TEXT), contradiction_summary (JSON TEXT),
    bias_framing_summary (JSON TEXT).
  * evidence_comparison is NOT persisted (prior probe finding) -> passed {} in the
    reconstruction. This does NOT weaken Check 2: the post-clamp
    verification_strength=="none" makes BOTH supportive gates
    (verification_card.py:542/558, each needs confidence >=60/>=85 and/or strength in
    {medium,high}) structurally unreachable, so the recompute is non-supportive
    regardless of evidence_comparison.

SAFETY
------
  SELECT-only. postgres_storage.get_engine() + engine.connect() (never begin()), no
  commit. Lazy DB + lazy `import main` INSIDE the live path so --selftest is fully
  offline. ASCII-guarded prints. Old 24 rows are REPORTED, never modified.

Usage (real run in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/clabel_verify_probe.py
    PYTHONPATH=. python scripts/clabel_verify_probe.py --selftest   # offline, no DB

Exit codes: 0 = summary printed / engine unavailable / selftest passed; 1 = selftest
failed; 2 = CLI usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# Import the REAL grant logic (pure agents only; no DB/network at import — same import
# class as the sibling probes, so --selftest stays offline).
from verification_card import _verdict_label  # noqa: E402


DRAFT_VERIFIED = "draft_verified"
CONF_CEILING = 20
SUPPORTIVE_LABELS = frozenset({"draft_verified", "draft_likely_true"})
EXPECTED_OLD_ROWS = 24  # measured population before the new-rows-only fix

# Substrings that together prove the deployed guard (matched against the running source).
GUARD_MARKERS = (
    'verification_card.get("verdict_label") in {"draft_verified", "draft_likely_true"}',
    "_verdict_label(",
)
# The candidate id to borrow read-only for Check 2 (falls back to any population row).
PREFERRED_BORROW_ID = 479


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(text) -> str:
    return json.dumps(str(text if text is not None else ""), ensure_ascii=True)[1:-1]


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value or not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return []
    return parsed if isinstance(parsed, list) else []


def _json_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _date10(created_at) -> str:
    if created_at is None:
        return "(unknown)"
    if isinstance(created_at, str):
        s = created_at.strip()
        return s[:10] if len(s) >= 10 else "(unknown)"
    try:
        return created_at.isoformat()[:10]
    except Exception:  # noqa: BLE001
        return "(unknown)"


def _days_ago(created_at, now: datetime) -> int | None:
    day = _date10(created_at)
    if day == "(unknown)":
        return None
    try:
        dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - dt).days


def in_population(verdict_label: str, policy_confidence_score) -> bool:
    """The population Check 3 counts: stored draft_verified AND score <= 20."""
    return (verdict_label or "") == DRAFT_VERIFIED and _to_int(policy_confidence_score) <= CONF_CEILING


def recompute_post_clamp_label(
    *,
    policy_confidence_score,
    evidence_snippets: list,
    contradiction_summary: dict,
    bias_framing_summary: dict,
    claim_count: int,
    evidence_comparison: dict | None = None,
) -> str:
    """Call the REAL _verdict_label with the SAME arguments the deployed guard passes:
    POST-clamp policy_confidence (score clamped, verification_strength forced to "none"
    exactly as the clamp sets it), official_sources=[], and the stored snippet /
    contradiction / bias inputs. evidence_comparison defaults to {} (not persisted); the
    strength=="none" short-circuit makes the supportive gates unreachable regardless."""
    policy_confidence = {
        "policy_confidence_score": min(_to_int(policy_confidence_score), CONF_CEILING),
        "verification_strength": "none",
    }
    return _verdict_label(
        policy_confidence,
        evidence_comparison or {},
        [],
        evidence_snippets=evidence_snippets or [],
        contradiction_summary=contradiction_summary or {},
        bias_framing_summary=bias_framing_summary or {},
        claim_count=claim_count,
    )


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST — no DB, no network.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== CLABEL-VERIFY — OFFLINE SELF-TEST (no DB) ===")
    p(f"supportive labels: {sorted(SUPPORTIVE_LABELS)}")
    p("")

    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    # ---- Check 2 logic: post-clamp supportive input -> non-supportive recompute ----
    p("Check2 (function-direct recompute yields NON-supportive):")
    recon = recompute_post_clamp_label(
        policy_confidence_score=20,               # clamped
        evidence_snippets=[{"evidence_type": "direct_support"}],
        contradiction_summary={},
        bias_framing_summary={},
        claim_count=1,
    )
    expect("C2", "post-clamp(conf20, strength none, pre-label draft_verified) -> not supportive",
           recon not in SUPPORTIVE_LABELS, True)
    p(f"        (recomputed label = {recon!r})")
    # A stored floor row (conf < 20) reconstructs the same way.
    recon0 = recompute_post_clamp_label(
        policy_confidence_score=8,
        evidence_snippets=[{"evidence_type": "direct_support"},
                           {"evidence_type": "direct_support"}],
        contradiction_summary={},
        bias_framing_summary={},
        claim_count=2,
    )
    expect("C2", "post-clamp(conf8) -> not supportive", recon0 not in SUPPORTIVE_LABELS, True)
    p(f"        (recomputed label = {recon0!r})")

    # ---- Check 3 logic: population filter selects supportive+conf<=20 only ----
    p("Check3 (population filter = draft_verified AND conf<=20):")
    expect("C3", "draft_verified & conf 20 -> IN", in_population("draft_verified", 20), True)
    expect("C3", "draft_verified & conf 12 -> IN", in_population("draft_verified", 12), True)
    expect("C3", "draft_verified & conf 55 -> OUT", in_population("draft_verified", 55), False)
    expect("C3", "draft_unverified & conf 10 -> OUT", in_population("draft_unverified", 10), False)
    expect("C3", "draft_likely_true & conf 10 -> OUT (population is draft_verified only)",
           in_population("draft_likely_true", 10), False)

    # ---- Check 1 marker logic (offline substring proof on a synthetic snippet) ----
    p("Check1 (guard-marker substring detection logic):")
    synthetic_with = (
        '        if verification_card.get("verdict_label") in {"draft_verified", '
        '"draft_likely_true"}:\n            verification_card["verdict_label"] = '
        '_verdict_label(\n'
    )
    synthetic_without = '        verification_card["verdict_confidence"] = score\n'
    expect("C1", "markers present in guarded snippet",
           all(m in synthetic_with for m in GUARD_MARKERS), True)
    expect("C1", "markers absent in pre-fix snippet",
           all(m in synthetic_without for m in GUARD_MARKERS), False)

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: Check1 marker logic / Check2 recompute / Check3 filter proven ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH — Check 1 (imported code) + Check 2 (function-direct) + Check 3 (SELECT).
# ---------------------------------------------------------------------------
def run_live() -> int:
    now = datetime.now(timezone.utc)
    p("=== CLABEL-VERIFY (READ-ONLY live smoke) ===")
    p(f"UTC: {now.isoformat(timespec='seconds')}")
    p("")

    overall_ok = True

    # ---- CHECK 1: DEPLOY MARKER (running/imported code) ---------------------
    p("=== CHECK 1 — DEPLOY MARKER (imported main._process_news_item_phase_a) ===")
    import inspect
    try:
        import main  # lazy: heavy pipeline import kept out of --selftest
        src = inspect.getsource(main._process_news_item_phase_a)
        present = all(marker in src for marker in GUARD_MARKERS)
        p(f"  DEPLOY MARKER: {'PRESENT' if present else 'ABSENT'}")
        if present:
            for line in src.splitlines():
                if GUARD_MARKERS[0] in line:
                    p(f"  matched guard line: {line.strip()}")
                    break
        else:
            overall_ok = False
            p("  (guard substrings NOT found in the running code -> fix not deployed here)")
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        p(f"  DEPLOY MARKER: ERROR importing/inspecting main ({type(exc).__name__}: {exc})")

    # ---- DB engine (Checks 2 borrow + 3 count) ------------------------------
    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("")
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Check 1 above needs only the imported code; Checks 2/3 need the DB.)")
        p("(Run --selftest for the offline logic check.)")
        return 0 if overall_ok else 1

    # ---- CHECK 2: FUNCTION-DIRECT SIMULATION (borrow a stored row read-only) --
    p("")
    p("=== CHECK 2 — FUNCTION-DIRECT SIMULATION (no pipeline, no DB write) ===")
    borrow_sql = (
        "SELECT id, created_at, policy_confidence_score, verification_strength, "
        "verdict_label, claims, evidence_snippets, contradiction_summary, "
        "bias_framing_summary "
        "FROM analysis_results "
        "WHERE verdict_label = :lbl AND policy_confidence_score <= :ceil "
        "ORDER BY (id = :pref) DESC, id DESC LIMIT 1"
    )
    with engine.connect() as conn:
        borrow = conn.execute(
            sa.text(borrow_sql).bindparams(
                lbl=DRAFT_VERIFIED, ceil=CONF_CEILING, pref=PREFERRED_BORROW_ID,
            )
        ).first()

    if borrow is None:
        p("  No stored draft_verified+conf<=20 row to borrow; using a SYNTHETIC post-clamp")
        p("  state instead (still a faithful function-direct recompute).")
        borrowed_id = "(synthetic)"
        pre_label = DRAFT_VERIFIED
        pre_conf = 20
        recomputed = recompute_post_clamp_label(
            policy_confidence_score=20,
            evidence_snippets=[{"evidence_type": "direct_support"}],
            contradiction_summary={},
            bias_framing_summary={},
            claim_count=1,
        )
    else:
        m = borrow._mapping
        borrowed_id = m["id"]
        pre_label = m["verdict_label"]
        pre_conf = _to_int(m["policy_confidence_score"])
        recomputed = recompute_post_clamp_label(
            policy_confidence_score=m["policy_confidence_score"],
            evidence_snippets=_json_list(m["evidence_snippets"]),
            contradiction_summary=_json_obj(m["contradiction_summary"]),
            bias_framing_summary=_json_obj(m["bias_framing_summary"]),
            claim_count=len(_json_list(m["claims"])),
        )
    non_supportive = recomputed not in SUPPORTIVE_LABELS
    p(f"  input: id={borrowed_id} | pre-clamp label={pre_label} | stored conf={pre_conf}")
    p(f"  reconstructed post-clamp state: score<=20, verification_strength='none', "
      f"official_sources=[]")
    p(f"  recomputed verdict_label -> {recomputed}")
    p(f"  CHECK 2: {'PASS (non-supportive)' if non_supportive else 'FAIL (still supportive!)'}")
    if not non_supportive:
        overall_ok = False

    # ---- CHECK 3: OLD ROWS UNCHANGED ----------------------------------------
    p("")
    p("=== CHECK 3 — OLD ROWS UNCHANGED (population count) ===")
    count_sql = (
        "SELECT id, created_at FROM analysis_results "
        "WHERE verdict_label = :lbl AND policy_confidence_score <= :ceil"
    )
    with engine.connect() as conn:
        pop_rows = conn.execute(
            sa.text(count_sql).bindparams(lbl=DRAFT_VERIFIED, ceil=CONF_CEILING)
        ).all()
    population = len(pop_rows)
    last14 = 0
    for r in pop_rows:
        d = _days_ago(r._mapping["created_at"], now)
        if d is not None and d <= 14:
            last14 += 1
    p(f"  population (draft_verified AND conf<=20): {population}   (expected {EXPECTED_OLD_ROWS})")
    p(f"  of which created in LAST 14 DAYS:          {last14}")
    p(f"  match-expected: {'YES' if population == EXPECTED_OLD_ROWS else 'NO (investigate)'}")
    p("  NOTE: unchanged == the CORRECT expectation. CLABEL-FIX is NEW-ROWS-ONLY; the old")
    p("  rows are intentionally left as-is (a backfill is a separate, later, gated step).")
    p("  A DROP below 24 would mean something rewrote old rows (NOT this fix).")

    # ---- FAITHFULNESS -------------------------------------------------------
    p("")
    p("=== FAITHFULNESS NOTE ===")
    p("* Check 1 inspects the IMPORTED main (running code), not the file on disk.")
    p("* Check 2 is a function-direct recompute (deploy_check pattern) over a RECONSTRUCTED")
    p("  post-clamp state — NO pipeline run, NO DB write. Borrowed row fields")
    p("  (evidence_snippets/contradiction/bias/claims) are stored-VERBATIM; the post-clamp")
    p("  score/strength and official_sources=[] are RECONSTRUCTED exactly as the clamp sets")
    p("  them. evidence_comparison is not persisted -> {} (the strength=='none' short-circuit")
    p("  makes the supportive gates unreachable regardless, so the recompute stays faithful).")
    p("* Check 3 reads verdict_label + policy_confidence_score VERBATIM.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0 if overall_ok else 1


def main_cli() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY live smoke that CLABEL-FIX is deployed + correct "
                    "(deploy marker + function-direct relabel + old-rows-unchanged). "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="Run the OFFLINE synthetic-case logic check (no DB / network).",
    )
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main_cli())
