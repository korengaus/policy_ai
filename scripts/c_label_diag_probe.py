"""C-LABEL-DIAG — READ-ONLY dissection of the draft_verified rows at
policy_confidence_score <= 20 (the one defamation-relevant Day1-2 finding).

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE /
ALTER / commit. Touches no production code, no verdict logic, no pins. Mirrors the
structure + safety guards of scripts/obit_leak_probe.py and scripts/engine_health_probe.py.
REUSES the REAL imported verification_card._verdict_label for the reconstruction step
(does NOT re-implement it).

WHY
---
The engine-health probe found stored rows with verdict_label == "draft_verified" while
policy_confidence_score <= 20. draft_verified is the STRONGEST supportive label and
both grant-gates inside _verdict_label require a HIGH confidence (>=60 snippet gate /
>=85 strong-official gate), so a <=20 draft_verified is internally inconsistent — the
one real defamation-relevant finding. Before ANY fix we must know:
  (1) which _verdict_label branch grants draft_verified,
  (2) whether these are stale-era residue (old rows only) or LIVE (recent rows too),
  (3) what the card actually displays.
MEASURE BEFORE SURGERY. Verdict-adjacent => fully READ-ONLY, then STOP.

THE GRANT MECHANISM (confirmed by reading verification_card._verdict_label)
--------------------------------------------------------------------------
_verdict_label returns "draft_verified" in exactly TWO places:
  * SNIPPET GATE  (verification_card.py:542-548): claim_count AND
    direct_support_count >= claim_count AND confidence_score >= 60 AND
    verification_strength in {medium, high}.
  * STRONG-OFFICIAL GATE (verification_card.py:558-559): confidence_score >= 85 AND
    verification_level == "strong_official_match".
BOTH require confidence >= 60. But _verdict_label runs INSIDE build_verification_card
on the PRE-clamp policy_confidence; main.py:934 THEN clamps policy_confidence_score to
<=20 on official_mismatch and does NOT recompute verdict_label. So a stored
draft_verified next to a stored score <=20 is PRE-CLAMP RESIDUE: the label was granted
with confidence>=60, the score was clamped afterwards, the label was never revised.

RECONSTRUCTION CAVEAT
---------------------
This probe re-runs the REAL _verdict_label on each row's STORED inputs. The stored
policy_confidence_score is the POST-clamp value (<=20), so reconstruction will NOT
reproduce draft_verified — that DIVERGENCE is the expected evidence of the
clamp-without-relabel residue, NOT a logic change. "reached NOW != reached THEN": the
STORED verdict_label is the display-truth; reconstruction is diagnostic only.

FIELD-NAME NOTES (confirmed by grep before writing)
---------------------------------------------------
  * TOP-LEVEL columns: id, created_at, title, policy_confidence_score,
    verification_strength, verdict_label, verdict_confidence, claims (JSON TEXT),
    evidence_snippets (JSON TEXT), contradiction_summary (JSON TEXT),
    bias_framing_summary (JSON TEXT), evidence_sources (JSON TEXT),
    source_reliability_summary (JSON TEXT), debug_summary (JSON TEXT).
  * official_mismatch + has_genuine_official_support live INSIDE
    source_reliability_summary JSON.
  * judge action lives INSIDE debug_summary: debug_summary["llm_judge"] (post-verdict)
    and debug_summary["llm_judge_prejudge"] (prejudge), each a dict with an "action"
    key (confirm/downgrade/flag_for_review). The judge can ONLY move policy_alert_level;
    it NEVER changes verdict_label — so judge_action is context, not the cause.
  * SURPRISE: evidence_comparison is NOT persisted as a column (comparison_status /
    verification_level are unavailable for reconstruction). The SNIPPET GATE (the
    plausible granter here) does not read evidence_comparison, so reconstruction of
    that gate is still faithful; the STRONG-OFFICIAL gate's verification_level is
    marked UNAVAILABLE.

SAFETY
------
  SELECT-only. postgres_storage.get_engine() + engine.connect() (never begin()), no
  commit. Lazy DB import INSIDE the live path so --selftest is fully offline. ASCII-
  guarded prints so a Korean / mojibake title can never crash the shell.

Usage (real run happens in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/c_label_diag_probe.py
    PYTHONPATH=. python scripts/c_label_diag_probe.py --selftest   # offline, no DB

Exit codes: 0 = summary printed / engine unavailable / selftest passed; 1 = selftest
failed; 2 = CLI usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# Make the project root importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Render Worker Shell is UTF-8; reconfigure defensively (mirrors the sibling probes).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Import the REAL grant logic — NOT a re-implementation. verification_card imports
# only pure agents (no DB/network at import), same class of import as news_collector
# in the sibling probes, so --selftest stays offline.
# ---------------------------------------------------------------------------
from verification_card import _verdict_label  # noqa: E402


DRAFT_VERIFIED = "draft_verified"
CONF_CEILING = 20  # the population: policy_confidence_score <= 20
OFFICIAL_SOURCE_TYPES = {"official_government", "public_institution"}
# verification_strengths that satisfy the snippet gate (verification_card._STRONG_...).
STRONG_STRENGTHS = {"medium", "high"}

# main.js DISPLAY strings for draft_verified (quoted; display-only, cannot import JS):
#   VERDICT_LABELS.draft_verified          (main.js:300) — primary card-face label
#   formatTechnicalLabel().draft_verified  (main.js:777) — technical-fallback label
MAINJS_VERDICT_LABEL = "임시 검증 완료"          # main.js:300
MAINJS_TECHNICAL_LABEL = "공식 근거 확인 필요"     # main.js:777


def p(line: str = "") -> None:
    """ASCII-guarded print — direct UTF-8, backslash-escaped fallback on encode error."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(text) -> str:
    """ensure_ascii rendering (no surrounding quotes) — no mojibake in the shell."""
    return json.dumps(str(text if text is not None else ""), ensure_ascii=True)[1:-1]


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
        s = str(created_at)
        return s[:10] if len(s) >= 10 else "(unknown)"


def _days_ago(created_at, now: datetime) -> int | None:
    """Whole days between created_at and now; None if unparseable."""
    day = _date10(created_at)
    if day == "(unknown)":
        return None
    try:
        dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - dt).days


# ---------------------------------------------------------------------------
# Pure evaluators (shared by live + self-test).
# ---------------------------------------------------------------------------
def in_population(verdict_label: str, policy_confidence_score) -> bool:
    """The C population: stored draft_verified AND policy_confidence_score <= 20."""
    return (verdict_label or "") == DRAFT_VERIFIED and _to_int(policy_confidence_score) <= CONF_CEILING


def conf_bucket(policy_confidence_score) -> str:
    """'clamp' (==20, the main.py:934 ceiling) / 'floor' (<20)."""
    return "clamp" if _to_int(policy_confidence_score) == CONF_CEILING else "floor"


def snippet_direct_support_count(evidence_snippets: list) -> int:
    return sum(
        1 for item in (evidence_snippets or [])
        if isinstance(item, dict) and item.get("evidence_type") == "direct_support"
    )


def snippet_gate_consistent(evidence_snippets: list, claim_count: int, verification_strength: str) -> bool:
    """True iff the SNIPPET GATE's non-confidence conditions hold on stored inputs
    (direct_support_count >= claim_count>=1 AND strength in {medium,high}). The
    confidence>=60 half cannot hold post-clamp — this attributes WHICH gate plausibly
    granted at build time, not whether it re-fires now."""
    if claim_count < 1:
        return False
    if snippet_direct_support_count(evidence_snippets) < claim_count:
        return False
    return (verification_strength or "") in STRONG_STRENGTHS


def official_sources_from_evidence(evidence_sources: list) -> list:
    return [
        s for s in (evidence_sources or [])
        if isinstance(s, dict) and s.get("source_type") in OFFICIAL_SOURCE_TYPES
    ]


def judge_action(debug_summary: dict) -> str:
    """Post-verdict judge action, falling back to prejudge; '(none)' if neither ran.
    Reads debug_summary['llm_judge'] / ['llm_judge_prejudge'] .action verbatim."""
    for key in ("llm_judge", "llm_judge_prejudge"):
        payload = debug_summary.get(key)
        if isinstance(payload, dict) and payload.get("action"):
            applied = payload.get("applied")
            suffix = "" if applied is None else ("/applied" if applied else "/record-only")
            return f"{payload.get('action')}{suffix}"
    return "(none)"


def reconstruct_label(row: dict) -> str:
    """Re-run the REAL _verdict_label on this row's STORED inputs. evidence_comparison
    is NOT persisted → passed empty (comparison_status/verification_level UNAVAILABLE);
    the snippet gate does not read it. policy_confidence_score is the POST-clamp stored
    value, so a divergence from the stored label is the expected residue evidence."""
    policy_confidence = {
        "policy_confidence_score": _to_int(row.get("policy_confidence_score")),
        "verification_strength": row.get("verification_strength"),
    }
    return _verdict_label(
        policy_confidence,
        {},  # evidence_comparison — UNAVAILABLE (not a stored column)
        official_sources_from_evidence(row.get("evidence_sources") or []),
        evidence_snippets=row.get("evidence_snippets") or [],
        contradiction_summary=row.get("contradiction_summary") or {},
        bias_framing_summary=row.get("bias_framing_summary") or {},
        claim_count=len(row.get("claims") or []),
    )


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST — no DB, no network.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== C-LABEL-DIAG — OFFLINE SELF-TEST (no DB) ===")
    p(f"population: verdict_label=='{DRAFT_VERIFIED}' AND policy_confidence_score <= {CONF_CEILING}")
    p("")

    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    # ---- population filter ----
    p("population filter:")
    expect("POP", "draft_verified & conf 20 -> IN",
           in_population("draft_verified", 20), True)
    expect("POP", "draft_verified & conf 90 -> OUT",
           in_population("draft_verified", 90), False)
    expect("POP", "draft_unverified & conf 10 -> OUT",
           in_population("draft_unverified", 10), False)

    # ---- clamp vs floor bucketing ----
    p("clamp-vs-floor bucketing:")
    expect("BUCKET", "conf 20 -> 'clamp'", conf_bucket(20), "clamp")
    expect("BUCKET", "conf 12 -> 'floor'", conf_bucket(12), "floor")
    expect("BUCKET", "conf 0  -> 'floor'", conf_bucket(0), "floor")

    # ---- gate attribution ----
    p("snippet-gate attribution (non-confidence half):")
    expect("GATE", "2 direct_support, 2 claims, strength high -> consistent",
           snippet_gate_consistent(
               [{"evidence_type": "direct_support"}, {"evidence_type": "direct_support"}],
               2, "high"), True)
    expect("GATE", "1 direct_support, 2 claims -> NOT consistent",
           snippet_gate_consistent([{"evidence_type": "direct_support"}], 2, "high"), False)
    expect("GATE", "2 direct_support, 2 claims, strength none -> NOT consistent",
           snippet_gate_consistent(
               [{"evidence_type": "direct_support"}, {"evidence_type": "direct_support"}],
               2, "none"), False)

    # ---- reconstruction wiring on a synthetic input ----
    p("reconstruction wiring (real _verdict_label called on stored-shape input):")
    # Clamped low-confidence row: BOTH draft_verified gates need conf>=60, so the real
    # _verdict_label must NOT return draft_verified here (divergence = residue evidence).
    recon_clamped = reconstruct_label({
        "policy_confidence_score": 20,
        "verification_strength": "high",
        "evidence_sources": [{"source_type": "official_government"}],
        "evidence_snippets": [{"evidence_type": "direct_support"}],
        "contradiction_summary": {},
        "bias_framing_summary": {},
        "claims": ["c1"],
    })
    expect("RECON", "clamped(conf20,high) real _verdict_label != draft_verified",
           recon_clamped != DRAFT_VERIFIED, True)
    p(f"         (reconstructed label on synthetic clamped row = {recon_clamped!r})")
    # Sanity that the wiring CAN yield draft_verified when confidence is high pre-clamp:
    recon_high = reconstruct_label({
        "policy_confidence_score": 88,
        "verification_strength": "high",
        "evidence_sources": [{"source_type": "official_government"}],
        "evidence_snippets": [{"evidence_type": "direct_support"}],
        "contradiction_summary": {},
        "bias_framing_summary": {},
        "claims": ["c1"],
    })
    expect("RECON", "high(conf88,1 direct_support,1 claim) -> draft_verified (snippet gate)",
           recon_high, DRAFT_VERIFIED)

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: population / bucketing / gate-attribution / reconstruction wiring proven ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH — SELECT-only.
# ---------------------------------------------------------------------------
def run_live() -> int:
    now = datetime.now(timezone.utc)
    p("=== C-LABEL-DIAG (READ-ONLY, SELECT-only) ===")
    p(f"UTC: {now.isoformat(timespec='seconds')}")
    p(f"population: verdict_label=='{DRAFT_VERIFIED}' AND policy_confidence_score <= {CONF_CEILING}")
    p("")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check that needs no DB.)")
        return 0

    # SELECT-only; parameterized population filter (no writes).
    sql = (
        "SELECT id, created_at, title, policy_confidence_score, verification_strength, "
        "verdict_label, verdict_confidence, claims, evidence_snippets, "
        "contradiction_summary, bias_framing_summary, evidence_sources, "
        "source_reliability_summary, debug_summary "
        "FROM analysis_results "
        "WHERE verdict_label = :lbl AND policy_confidence_score <= :ceil "
        "ORDER BY created_at DESC, id DESC"
    )
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(sql).bindparams(lbl=DRAFT_VERIFIED, ceil=CONF_CEILING)
        ).all()

    parsed_rows = []
    for r in rows:
        m = r._mapping
        srs = _json_obj(m["source_reliability_summary"])
        debug = _json_obj(m["debug_summary"])
        evidence_snippets = _json_list(m["evidence_snippets"])
        claims = _json_list(m["claims"])
        row = {
            "id": m["id"],
            "created_at": m["created_at"],
            "title": m["title"] or "",
            "policy_confidence_score": _to_int(m["policy_confidence_score"]),
            "verification_strength": m["verification_strength"],
            "verdict_confidence": _to_int(m["verdict_confidence"]),
            "claims": claims,
            "evidence_snippets": evidence_snippets,
            "contradiction_summary": _json_obj(m["contradiction_summary"]),
            "bias_framing_summary": _json_obj(m["bias_framing_summary"]),
            "evidence_sources": _json_list(m["evidence_sources"]),
            "official_mismatch": srs.get("official_mismatch"),
            "has_genuine": srs.get("has_genuine_official_support"),
            "direct_support_count": snippet_direct_support_count(evidence_snippets),
            "claim_count": len(claims),
            "judge_action": judge_action(debug),
        }
        row["snippet_gate"] = snippet_gate_consistent(
            evidence_snippets, row["claim_count"], row["verification_strength"]
        )
        row["reconstructed"] = reconstruct_label(row)
        row["bucket"] = conf_bucket(row["policy_confidence_score"])
        parsed_rows.append(row)

    total = len(parsed_rows)

    # ---- TABLE --------------------------------------------------------------
    p(f"=== TABLE (population size = {total}; showing up to 30) ===")
    p("id | date | conf | vconf | off_mismatch | has_genuine | direct_sup/claims | strength | judge")
    for row in parsed_rows[:30]:
        p(f"{row['id']} | {_date10(row['created_at'])} | {row['policy_confidence_score']} | "
          f"{row['verdict_confidence']} | {row['official_mismatch']!r} | {row['has_genuine']!r} | "
          f"{row['direct_support_count']}/{row['claim_count']} | "
          f"{row['verification_strength']!r} | {row['judge_action']}")
    if total > 30:
        p(f"    (+{total - 30} more rows not shown)")

    # ---- AGE SPLIT ----------------------------------------------------------
    p("")
    p("=== AGE SPLIT (the live-vs-residue test) ===")
    by_month: dict = {}
    last14 = 0
    last30 = 0
    unknown_age = 0
    for row in parsed_rows:
        month = _date10(row["created_at"])[:7]
        by_month[month] = by_month.get(month, 0) + 1
        d = _days_ago(row["created_at"], now)
        if d is None:
            unknown_age += 1
            continue
        if d <= 14:
            last14 += 1
        if d <= 30:
            last30 += 1
    for month, cnt in sorted(by_month.items()):
        p(f"    {month}: {cnt}")
    p(f"    unparseable-date rows: {unknown_age}")
    p("")
    p(f"  created in LAST 14 DAYS: {last14}   <- LIVE if > 0 (recent rows still get it)")
    p(f"  created in LAST 30 DAYS: {last30}")
    p("  (0 in the recent windows => stale-era residue; >0 => the mechanism is still live.)")

    # ---- GATE ATTRIBUTION ---------------------------------------------------
    p("")
    p("=== GATE ATTRIBUTION (which _verdict_label branch plausibly granted draft_verified) ===")
    p("SNIPPET GATE (vc.py:542) needs direct_support>=claims & strength in {medium,high}")
    p("(the confidence>=60 half cannot show post-clamp; STRONG-OFFICIAL gate vc.py:558")
    p(" needs verification_level=='strong_official_match' — UNAVAILABLE, not persisted).")
    for bucket_name, desc in (("clamp", "==20 (main.py:934 ceiling)"), ("floor", "<20")):
        bucket = [row for row in parsed_rows if row["bucket"] == bucket_name]
        snippet_yes = sum(1 for row in bucket if row["snippet_gate"])
        mism_true = sum(1 for row in bucket if row["official_mismatch"] is True)
        p(f"  bucket {bucket_name} ({desc}): total={len(bucket)}  "
          f"snippet-gate-consistent={snippet_yes}  "
          f"snippet-gate-NOT-consistent={len(bucket) - snippet_yes}  "
          f"official_mismatch=True={mism_true}")

    # ---- RECONSTRUCTION -----------------------------------------------------
    p("")
    p("=== RECONSTRUCTION (real _verdict_label on STORED inputs) ===")
    still = sum(1 for row in parsed_rows if row["reconstructed"] == DRAFT_VERIFIED)
    diverged = total - still
    p(f"  STILL draft_verified on stored inputs: {still}")
    p(f"  DIVERGED (reconstructed != stored):     {diverged}")
    recon_breakdown: dict = {}
    for row in parsed_rows:
        recon_breakdown[row["reconstructed"]] = recon_breakdown.get(row["reconstructed"], 0) + 1
    p("  reconstructed-label breakdown:")
    for lbl, cnt in sorted(recon_breakdown.items(), key=lambda kv: (-kv[1], kv[0])):
        p(f"      {lbl}: {cnt}")
    p("  CAVEAT: reconstruction uses the STORED (post-clamp) policy_confidence_score, so")
    p("  divergence is EXPECTED and confirms the clamp-without-relabel residue. The STORED")
    p("  verdict_label is the display-truth; 'reached NOW != reached THEN'. evidence_comparison")
    p("  is not persisted, so the STRONG-OFFICIAL gate cannot be reconstructed (snippet gate can).")

    # ---- DISPLAY ------------------------------------------------------------
    p("")
    p("=== DISPLAY (how main.js renders verdict_label=='draft_verified') ===")
    p(f"  VERDICT_LABELS.draft_verified (main.js:300):        {_ascii(MAINJS_VERDICT_LABEL)}")
    p(f"  formatTechnicalLabel().draft_verified (main.js:777): {_ascii(MAINJS_TECHNICAL_LABEL)}")
    p("  The card maps the STORED verdict_label directly to the Korean string above; the")
    p("  grep found NO numeric-confidence guard that softens the draft_verified label at")
    p("  display time (has_genuine gates a SEPARATE '공식 근거' box, not this label).")

    # ---- FAITHFULNESS -------------------------------------------------------
    p("")
    p("=== FAITHFULNESS NOTE ===")
    p("* TABLE / AGE / GATE read stored columns + JSON blobs VERBATIM (no re-derivation).")
    p("* RECONSTRUCTION calls the REAL imported verification_card._verdict_label.")
    p("* judge_action is context only — the judge moves policy_alert_level, NEVER verdict_label.")
    p("* Nothing written: verdict_label / has_genuine / score / every pipeline field untouched.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY dissection of draft_verified rows at confidence<=20 "
                    "(age split + gate attribution + reconstruction + display). "
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
    raise SystemExit(main())
