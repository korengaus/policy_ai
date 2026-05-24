# Verdict Producer Disagreement Map (M11.0d-1)

**Status:** Diagnosis only. Zero production code changes. This document is
the operator-readable reference for **M11.0d-2** (design review) and
**M11.0d-3** (consolidation implementation).

**Latest commit before this milestone:** M11.7b — narrow Playwright excepts.

---

## Why this document exists

`claude_audit_phase1.md` §1.5 #1 identifies three verdict producers in the
analysis pipeline that operate on overlapping inputs with different
thresholds and **disagree by design**:

1. `policy_decision.make_final_decision` — alert-level producer
2. `policy_scoring.calibrate_final_decision` (via `_alert_from_score`) — re-derives the alert from a weighted final score
3. `verification_card._verdict_label` — emits a workflow disposition label in a different vocabulary

The M11.0d milestone series resolves this in three steps:

- **M11.0d-1 (this milestone):** diagnose. Build a synthetic input matrix, run each producer, document the disagreement.
- **M11.0d-2:** design review. Operator picks the consolidation strategy from the options in Section F.
- **M11.0d-3:** implementation. Re-baseline all snapshots and downstream regression tests.

This document and the pinned diagnostic test (`tests/test_verdict_producer_disagreement_diagnostic.py`) lock the **current disagreement state** so M11.0d-3 starts from a known baseline.

---

## Section A — Producer Input/Output Contracts

### Producer 1 — `policy_decision.make_final_decision`

- **File:** `policy_decision.py`, lines L177-222 (entry) + L90-124 (`_policy_alert_level`).
- **Signature:** `make_final_decision(policy_confidence: dict, policy_impact: dict) -> dict`.
- **Output vocabulary:** `{HIGH, MEDIUM, WATCH, LOW}` on `policy_alert_level`.
- **Inputs (dotted access paths):**
  - `policy_confidence["policy_confidence_score"]` (int, 0-100)
  - `policy_confidence["verification_strength"]` (str: `"none" | "low" | "medium" | "high"`)
  - `policy_confidence["risk_level"]` (str: `"low" | "medium" | "high"`)
  - `policy_confidence["confidence_evidence_grade"]` (str: `"A" | "B" | "C" | "D" | "F" | None`)
  - `policy_confidence["confidence_reasons"]` (list[str])
  - `policy_impact["impact_level"]` (str: `"low" | "medium" | "high"`)
  - `policy_impact["impact_direction"]` (str)
  - `policy_impact["consumer_sensitivity"]` (int)
  - `policy_impact["business_sensitivity"]` (int)
  - `policy_impact["affected_sectors"]` / `affected_groups` / `impact_reasons` (lists)
- **Decision tree (top-to-bottom, first match wins):**
  1. `impact_level == "high" AND (risk_level == "high" OR consumer_sensitivity >= 80)`:
     - if `verification_strength == "none"` → **WATCH**
     - else → **HIGH**
  2. `confidence_score >= 60 AND impact_level in {"high", "medium"}` → **MEDIUM**
  3. `verification_strength == "none" AND risk_level == "high"` → **WATCH**
  4. `impact_level == "high" AND verification_strength in {"none", "low"}` → **WATCH**
  5. `confidence_score < 25 AND impact_level == "low"` → **LOW**
  6. (fallback) → **LOW**
- **Other fields populated:** `market_signal` (list), `action_recommendation` (Korean prose), `decision_summary` (Korean prose), `decision_reasons` (list).
- **Side effects:** none — pure function.

### Producer 2 — `policy_scoring.calibrate_final_decision` (helper: `_alert_from_score`)

- **File:** `policy_scoring.py`, lines L151-229 (entry) + L116-148 (`_alert_from_score`).
- **Signature:** `calibrate_final_decision(*, final_decision, policy_confidence, policy_impact, verification_card, source_candidates, evidence_snippets, debug_summary) -> tuple[dict, dict]`.
- **Output vocabulary:** `{HIGH, WATCH, LOW}` on `policy_alert_level`. **Never emits MEDIUM** — this is itself a structural disagreement with P1.
- **Inputs:**
  - From `policy_confidence`: `policy_confidence_score` (int), `risk_level` (str)
  - From `policy_impact`: `impact_level` (str)
  - From `verification_card`: `official_mismatch` (bool), `source_reliability_summary` (dict), `contradiction_summary` (dict), `evidence_quality_summary` (dict — `average_evidence_quality_score`)
  - From `debug_summary`: `evidence_strength_summary` (strong/medium/weak counts), `evidence_quality_summary`, `approved_boost` (bool), `rejected_penalty` (bool), `review_feedback_status` (str)
  - From `source_candidates`: list (for source-trust computation)
- **`base_score` formula:**
  ```
  base_score = 0.25*strength_component + 0.25*evidence_quality_score
             + 0.25*source_trust       + 0.15*confidence_score
             + 0.10*impact_component
  final_score = clamp(base_score + human_adjustment + contradiction_adjustment, 0, 100)
  ```
- **`_alert_from_score` decision tree:**
  1. `human_feedback_adjustment >= 15 AND final_score >= 65`:
     - if `impact_level == "high"` → **HIGH**
     - else → **WATCH**
  2. `contradiction_adjustment <= -35` → **WATCH**
  3. `official_mismatch AND source_trust_score < 45`:
     - if `impact_level == "high" OR risk_level == "high"` → **WATCH**
     - else → **LOW**
  4. ALL of `final_score >= 75 AND evidence_quality_score >= 65 AND source_trust_score >= 55 AND strength_score >= 55 AND contradiction_adjustment == 0 AND impact_level == "high"` → **HIGH**
  5. `final_score >= 45 OR impact_level == "high" OR risk_level == "high"` → **WATCH**
  6. (fallback) → **LOW**
- **Other fields written to `calibrated`:** `final_score`, `source_trust_score`, `human_feedback_adjustment`, `contradiction_adjustment`, `evidence_weighted_score`, `evidence_quality_score`, `calibration_reasons`.
- **Side effects:** returns a NEW `calibrated` dict (does not mutate input); `debug_summary` is copied and extended.

### Producer 3 — `verification_card._verdict_label`

- **File:** `verification_card.py`, lines L391-469.
- **Signature:** `_verdict_label(policy_confidence, evidence_comparison, official_sources, evidence_snippets=None, contradiction_summary=None, bias_framing_summary=None, claim_count=0) -> str`.
- **Output vocabulary (DISJOINT from P1/P2):**
  - `draft_disputed`, `draft_high_risk_review`, `draft_needs_review`,
  - `draft_needs_official_confirmation`, `draft_needs_context`,
  - `draft_verified`, `draft_likely_true`, `draft_unverified`
- **Inputs:**
  - `policy_confidence["policy_confidence_score"]`, `policy_confidence["verification_strength"]`
  - `evidence_comparison["comparison_status"]`, `evidence_comparison["verification_level"]`, `evidence_comparison["conflict_signals"]`, `evidence_comparison["semantic_conflict_signals"]`
  - `official_sources` (list)
  - `evidence_snippets` (list with `evidence_type` ∈ `{"direct_support", "official_reference", "insufficient_evidence", ...}`)
  - `contradiction_summary["possible_contradiction_count"]`, `["confirmed_contradiction_count"]` (or `["likely_contradiction_count"]`), `["needs_official_confirmation_count"]`, `["insufficient_evidence_count"]`
  - `bias_framing_summary["high_framing_count"]`
  - `claim_count` (int)
- **Decision tree (top-to-bottom, first match wins):**
  1. `conflict_signals OR comparison_status == "official_conflict_possible"` → **draft_disputed**
  2. `high_framing_count > 0 AND confirmed_count > 0` → **draft_high_risk_review**
  3. `high_framing_count > 0` → **draft_needs_review**
  4. `confirmed_count > 0` → **draft_disputed**
  5. `possible_count > 0` → **draft_needs_review**
  6. `claim_count > 0 AND needs_official_confirmation_count >= max(1, claim_count // 2)` → **draft_needs_official_confirmation**
  7. `claim_count > 0 AND insufficient_evidence_count >= max(1, claim_count // 2)` → **draft_needs_context**
  8. **(B08, M11.0c-gated)** `claim_count > 0 AND direct_support_count >= claim_count AND confidence_score >= 60 AND verification_strength in {"medium", "high"}` → **draft_verified**
  9. `official_reference_count > 0 AND direct_support_count == 0` → **draft_needs_official_confirmation**
  10. `insufficient_count > 0` → **draft_needs_context**
  11. `comparison_status == "official_evidence_missing" AND verification_level == "excluded_non_policy_page"` → **draft_needs_context**
  12. `NOT official_sources OR verification_strength == "none"` → **draft_unverified**
  13. `confidence_score >= 85 AND verification_level == "strong_official_match"` → **draft_verified**
  14. `confidence_score >= 60 AND verification_level in {"strong_official_match", "medium_official_match"}` → **draft_likely_true**
  15. `confidence_score >= 35` → **draft_needs_context**
  16. (fallback) → **draft_unverified**
- **Side effects:** none — pure function returning `str`.

### Pipeline ordering (`main.analyze_pipeline`)

Verified in `main.py:570-680`:

```
1. policy_confidence = calculate_policy_confidence(...)              ← INPUT
2. policy_impact     = analyze_policy_impact(...)                    ← INPUT
3. final_decision    = make_final_decision(policy_confidence,        ← Producer 1
                                            policy_impact)
4. verification_card = build_verification_card(...)                  ← Producer 3 (via _verdict_label)
5. (main.py L633-666) IF verification_card.official_mismatch:        ← Implicit 4th producer
     policy_confidence["policy_confidence_score"] = min(.., 20)        - main.py rewrites
     policy_confidence["verification_strength"]   = "none"             - both policy_confidence
     final_decision["policy_alert_level"]          = "WATCH" if         and final_decision
                                                     impact == high
                                                     else previous
6. final_decision, debug_summary = calibrate_final_decision(...)     ← Producer 2 (OVERWRITES P1)
```

### User-facing visibility map

| Field consumed by API/UI | Set by |
| --- | --- |
| `result["final_decision"]["policy_alert_level"]` | **Producer 2** (final overwrite). P1's verdict is silently discarded. |
| `result["final_decision"]["final_score"]` | **Producer 2** (P1 doesn't compute this). |
| `result["verification_card"]["verdict_label"]` | **Producer 3** (independent vocabulary, never reconciled with P1/P2). |
| `result["verification_card"]["verdict_confidence"]` | `policy_confidence["policy_confidence_score"]` (possibly capped at 20 by main.py mid-pipeline). |
| `result["final_decision"]["decision_summary"]` (Korean prose) | **Producer 1** (P2 doesn't regenerate). |
| `result["final_decision"]["action_recommendation"]` (Korean prose) | **Producer 1** (P2 doesn't regenerate). |
| `result["final_decision"]["calibration_reasons"]` | **Producer 2**. |
| `result["final_decision"]["market_signal"]` | **Producer 1**. |

A user sees: **P2's alert label + P1's Korean prose + P3's draft disposition** — three independent artifacts. Their narrative consistency is by construction not guaranteed.

---

## Section B — Input Surface and Synthetic Matrix

The synthetic matrix has **42 rows** across **7 scenario families**:

| Family | Row count | Purpose |
| --- | --- | --- |
| `happy_path` | 6 | Strong-evidence verified rows hitting different P1 branches |
| `boundary` | 8 | Score values at 24/25/59/60/74/75/84/85 — exact P1/P2/P3 thresholds |
| `conflict` | 6 | Various combinations of conflict signals + contradictions |
| `framing` | 4 | `high_framing_count` variations |
| `mismatch` | 8 | `official_mismatch` + `verification_strength="none"` cases |
| `calibration` | 4 | `approved_boost` / `rejected_penalty` overrides |
| `mid_confidence` | 6 | `score in {35, 45, 50, 65, 70}` — exposes P1↔P2 MEDIUM-vs-WATCH divergence |

The matrix is pinned in `tests/fixtures/m11_0d_1_synthetic_matrix.json`.

---

## Section C — Disagreement Map (computed against the synthetic matrix)

| Metric | Count | % of 42 |
| --- | --- | --- |
| **All three agree (strict — same label string)** | **0** | **0%** |
| All three agree (P3 normalized to alert tier) | 9 | 21% |
| P1 ↔ P2 agree (same vocabulary, direct compare) | 16 | 38% |
| P1 ↔ P3 agree (P3 normalized) | 13 | 31% |
| P2 ↔ P3 agree (P3 normalized) | 17 | 40% |
| **All three disagree** (after P3 normalization) | **14** | **33%** |

The "strict" zero is structural: P3's vocabulary is disjoint from P1/P2's. Even when all three "intend" the same outcome, the raw label strings differ.

P3 normalization (heuristic — used only for the disagreement count, not in production):

| P3 label | Mapped tier |
| --- | --- |
| `draft_verified` | HIGH |
| `draft_likely_true` | MEDIUM |
| `draft_disputed`, `draft_high_risk_review`, `draft_needs_review`, `draft_needs_official_confirmation`, `draft_needs_context` | WATCH |
| `draft_unverified` | LOW |

### High-impact disagreement examples (selected from the 42-row matrix)

| Row id | P1 | P2 | P3 | P3 tier | Why |
| --- | --- | --- | --- | --- | --- |
| `happy_strong_medium_impact` | MEDIUM | WATCH | draft_verified | HIGH | All three different vocabularies firing different conclusions on a textbook strong-evidence case |
| `happy_verified_85_strong_match` | MEDIUM | WATCH | draft_verified | HIGH | Score=85 hits P3 strong-match gate but P1/P2 don't promote |
| `boundary_score_75_high_impact` | MEDIUM | HIGH | draft_verified | HIGH | P2 promotes to HIGH at score=75; P1 stays MEDIUM (no high-risk gate) |
| `contradiction_confirmed` | HIGH | WATCH | draft_disputed | WATCH | P1 invisible to contradictions; P2+P3 both demote |
| `mismatch_high_impact_high_risk` | HIGH | WATCH | draft_needs_context | WATCH | P1 fires "high impact + high risk" before main.py's official_mismatch capping rewrites the input |
| `approved_boost_high_impact` | MEDIUM | HIGH | draft_verified | HIGH | P2 sees the human-feedback boost; P1 doesn't |
| `rejected_penalty_baseline` | MEDIUM | LOW | draft_verified | HIGH | P2 sees the rejection penalty; P1 and P3 don't |
| `high_framing_alone` | MEDIUM | LOW | draft_needs_review | WATCH | High framing visible only to P3 |
| `excluded_non_policy` | LOW | LOW | draft_needs_context | WATCH | P3 has a dedicated branch for `excluded_non_policy_page`; P1/P2 don't reach it |
| `mid_official_reference_only` | LOW | LOW | draft_needs_official_confirmation | WATCH | "Has official-reference but no direct support" is a P3-only branch |

---

## Section D — Existing Regression Fixture Snapshot

Three named fixtures from `tests/regression.test.js` were also run through all three producers (pinned in `tests/fixtures/m11_0d_1_regression_fixtures_snapshot.json`).

| Fixture | Input shape | P1 | P2 | P3 | Agreement state |
| --- | --- | --- | --- | --- | --- |
| `regression_fixture_geumyungwi_strong` (`strongOfficialFixture` at `regression.test.js:205-307`) | score=85, strength=high, impact=high, evidence_grade=A, strong_official_match | **MEDIUM** | **HIGH** | **draft_verified** | **P1 ≠ P2 ≠ P3** (P3 normalizes to HIGH so P2 and P3 align) |
| `regression_fixture_geumyungwi_weak` (`weakOfficialFixture("금융위")` at `regression.test.js:130`) | score=18, strength=none, impact=medium, official_mismatch=true | LOW | LOW | draft_needs_context | P1=P2 ✓; P3 differs in vocabulary (normalizes to WATCH, not LOW) |
| `regression_fixture_jeonse_fraud` (`weakOfficialFixture("전세사기")`) | score=12, strength=none, impact=high, risk=high, official_mismatch=true | WATCH | WATCH | draft_unverified | P1=P2 ✓; P3 normalizes to LOW (mild downward divergence) |

**The strong-evidence fixture (강한 공식근거 ELS) shows the cleanest disagreement: P1 says MEDIUM, P2 says HIGH, P3 says draft_verified.** This is the case the operator would expect to be unambiguous — and it isn't.

---

## Section E — Risk Surface for M11.0d-3 Consolidation

Any consolidation strategy in M11.0d-3 MUST respect these constraints, each pinned by an existing test:

| # | Constraint | Pinned by |
| --- | --- | --- |
| 1 | B08 weak-evidence lockdown — `direct_support_count >= claim_count` alone must NOT produce `draft_verified`. M11.0c added `confidence_score>=60` + `verification_strength in {medium, high}` gates. | `tests/test_verdict_label_b08_fix.py` (24 cases) |
| 2 | Conservative wording — methodology HTML must not contain "100%" certainty. Korean phrases `공식 후보만 있음`, `공식기관 후보는 있으나 상세 본문 미확인`, `의미 매칭 근거 부족`, `사람 검토 필요` must remain. | `tests/regression.test.js:15-23` |
| 3 | B08 conservative-fix preservation — 21 weak-evidence rows must NOT label `draft_verified`; 7 strong-evidence rows MUST label `draft_verified`. | `tests/test_verdict_label_b08_fix.py::WeakPatternMustNotVerify*` and `::StrongPatternMustVerify*` |
| 4 | Calibration debug_summary fields — `final_score`, `source_trust_score`, `evidence_weighted_score`, `calibrated_policy_alert_level` must remain populated for ops visibility. | `tests/test_verdict_producer_comparison.py` (37 cases) |
| 5 | Verdict diagnostic catalog parity — every documented B-branch in `verdict_label_diagnostic.py` must remain emitable. | `tests/test_verdict_label_diagnostic.py` (42 cases) |
| 6 | Verification-card duplicate prevention — `_missing_context_specific` exists exactly once (M11.4b). | `tests/test_verification_card_dedup.py` (11 cases) |
| 7 | M11.0d-1 producer snapshots — every per-row label must match the M11.0d-1 baseline until M11.0d-3 explicitly re-baselines. | `tests/test_verdict_producer_disagreement_diagnostic.py` (this milestone — 9 cases) |
| 8 | P2 emits only `{HIGH, WATCH, LOW}` — no MEDIUM. | `tests/test_verdict_producer_disagreement_diagnostic.py::Producer2SnapshotTests::test_producer_2_only_emits_documented_vocabulary` |
| 9 | P3 vocabulary is the documented 8-label set. | `tests/test_verdict_producer_disagreement_diagnostic.py::Producer3SnapshotTests::test_producer_3_only_emits_documented_vocabulary` |
| 10 | Official mismatch capping (main.py:633) — when `official_mismatch=True`, `policy_confidence_score` is capped at 20 and `verification_strength` forced to "none". | Indirectly pinned by `tests/test_verdict_label_diagnostic.py` end-to-end fixtures. |
| 11 | Reviewer-required invariant — `operator_review_required` always True regardless of producer outcome. | **Not currently pinned by any test.** M11.0d-3 should add an explicit pin. |
| 12 | LLM-cannot-raise-verdict — LLM input cannot bypass the deterministic calibration. | **Pinned by absence** — no test currently asserts this property. M11.0d-3 must preserve. |

---

## Section F — Recommendation for M11.0d-2 (design review)

These are the realistic consolidation strategies. **M11.0d-1 does NOT pick a winner.** The operator decides in M11.0d-2.

### Strategy A — Producer 2 (`calibrate_final_decision`) authoritative

P1 demoted to a Korean-prose generator; P3 kept as orthogonal workflow disposition.

**Pros:**
- P2 already runs LAST in the pipeline; user-facing `policy_alert_level` is already P2's output.
- P2's input is the richest (it sees calibration + contradiction + source trust + evidence quality + strength all at once).
- `{HIGH, WATCH, LOW}` is the production-correct vocabulary.

**Cons:**
- P1's Korean prose (`decision_summary`, `action_recommendation`) is currently generated from P1's alert level, which differs from P2's. Demoting P1 means the prose must be regenerated against P2's label — that **changes user-facing wording**.
- P2 does not emit MEDIUM — adopting it eliminates the MEDIUM tier from the operator dashboard, which is a UX decision (not just a code change).

**Migration risk:** **MEDIUM-HIGH.** Prose-regeneration touches multiple Korean string templates; verdict regression suites need re-baselining.

### Strategy B — Producer 1 (`make_final_decision`) authoritative

P2 demoted to a "score-only contributor" that doesn't re-derive the label.

**Pros:**
- P1's logic is simpler and human-readable.
- P1 emits MEDIUM (preserves the existing 4-tier dashboard).
- P1's prose generation stays consistent with its own label.

**Cons:**
- P2's calibration is more sophisticated (source trust + evidence quality + strength + contradiction adjustment all in one score). Throwing away P2's label means throwing away that signal.
- The `final_score` computed by P2 would need a separate label-mapping function.

**Migration risk:** **MEDIUM.** Verdict regression suites shift (about 30% of cases per the disagreement map).

### Strategy C — Make the disagreement explicit; no producer demoted

Add a `disagreement_signal` field to `debug_summary` that records `{p1_label, p2_label, p3_label, p3_implied_tier, agreed: bool}`. Zero behavior change; visibility upgrade only.

**Pros:**
- Zero behavior change. Backwards-compatible with all existing tests.
- Operator dashboard can render "P1 says X, P2 says Y, P3 says Z" and let humans resolve disagreements during review (which is the conservative-review posture the system advertises).
- Easy to ship as a logging-only PR following the M11.7a pattern.

**Cons:**
- Doesn't actually resolve the audit finding — the three producers continue to disagree by construction; we just label the disagreement.
- Operator dashboard complexity increases.

**Migration risk:** **LOW.**

---

## Snapshot files (committed, immutable until M11.0d-3)

| File | Contents |
| --- | --- |
| `tests/fixtures/m11_0d_1_synthetic_matrix.json` | 42 input rows with per-row scenario metadata |
| `tests/fixtures/m11_0d_1_p1_snapshot.json` | P1 label per row |
| `tests/fixtures/m11_0d_1_p2_snapshot.json` | P2 label per row |
| `tests/fixtures/m11_0d_1_p3_snapshot.json` | P3 label per row |
| `tests/fixtures/m11_0d_1_disagreement_summary.json` | Aggregate counts + per-row full label triples |
| `tests/fixtures/m11_0d_1_regression_fixtures_snapshot.json` | Named regression fixtures (금융위 strong / weak, 전세사기) |

**All six files are pinned by `tests/test_verdict_producer_disagreement_diagnostic.py`.** Any future producer change that drifts these labels without an explicit M11.0d-3 re-baselining will fail the test.

---

## What this milestone does NOT do

- Does NOT touch any production `.py` file. Read-only diagnosis.
- Does NOT propose a winning producer — that is **M11.0d-2**'s job.
- Does NOT change any threshold or label vocabulary.
- Does NOT add a `disagreement_signal` field to `debug_summary` — that would be Strategy C from Section F, which requires operator approval.
- Does NOT modify `tests/regression.test.js`.
- Does NOT bump `EXPECTED_TOTAL_LOG_CALLS` (no new log calls were added).

---

## M11.0d-3a status — Strategy C SHIPPED (logging only)

**Shipped:** the disagreement is now visible in `debug_summary["disagreement_signal"]` and as a `verdict.disagreement_signal` JSON log event on Render.

- `main.analyze_pipeline` captures P1's raw label (before the mid-pipeline `official_mismatch` rewrite and P2's overwrite) and assembles a `{p1_label, p2_label, p3_label, p3_implied_tier, agreed, disagreement_description}` payload after `calibrate_final_decision` returns.
- Zero behavior change — `policy_alert_level` and `verdict_label` byte-identical (proven by the 9-case M11.0d-1 snapshot pin and the 17 M11.0d-3a tests).
- `EXPECTED_TOTAL_LOG_CALLS` bumped 262 → 263.

## M11.0d-3b status — NARROW Strategy A SHIPPED (codification + invariants only; prose alignment DEFERRED to M11.0d-3b-2)

**Shipped:**

- **Codification of P2 authority** in docstrings of `policy_decision.make_final_decision` (now "prose-only") and `policy_scoring.calibrate_final_decision` (now "AUTHORITATIVE"), plus a contract comment at `main.py:668`. No logic change.
- **Constraint #11 pin** (`operator_review_required` ALWAYS True): structural pin on `database.py` schemas (3 CREATE TABLEs with `NOT NULL DEFAULT 1`) + behavioral pin on `artifact_evidence_linker.candidate_to_dict` (forces True even when dataclass passes False).
- **Constraint #12 pin** (LLM cannot raise verdict): structural pin asserting `llm_judge.py`, `ai_reasoner.py`, and `scripts/dry_run_llm_judge.py` contain no subscript/attribute assignment to `policy_alert_level` / `policy_confidence_score` / `verification_strength`; plus a repo-wide AST scan asserting `policy_alert_level` writers are confined to `main.py`, `policy_decision.py`, `policy_scoring.py`, `policy_confidence.py`, `verdict_label_diagnostic.py` (diagnostic), and `verdict_producer_comparison.py` (diagnostic).
- **P2-authority behavioral pin** that runs P1+P2 on the strong-evidence ELS scenario (P1=MEDIUM, P2=HIGH) and asserts the final `policy_alert_level` is P2's HIGH.
- **disagreement_signal preservation pin** confirming M11.0d-3a's wiring still captures P1's raw label after the docstring changes.

**Deferred to M11.0d-3b-2 (prose alignment):**

P1's prose generators `_decision_summary` and `_action_recommendation` branch on P1's own label, so when P1 says MEDIUM and P2 says HIGH the user sees `"정책 신뢰도와 영향도가 중간 이상으로 확인되어…"` (MEDIUM prose) next to the HIGH alert — a long-standing UX inconsistency for ~30% of production analyses.

**Why M11.0d-3b did NOT realign the prose:**

- M11.0d-3b's Phase 1 diagnosis confirmed prose alignment is **technically safe from a test-pin standpoint** — `tests/regression.test.js` runs against hardcoded JSON fixtures (never invokes Python), and zero Python tests pin the `decision_summary` / `action_recommendation` / `market_signal` fields. So no current test would catch a prose change.
- BUT the prose change would alter user-visible Korean strings on Render for ~30% of analyses. That is a deliberate UX change that deserves its own milestone with explicit operator awareness — not a hidden side-effect inside a "codification" PR.
- The NARROW path delivers the operator-visible benefit of Strategy A (formal P2 authority + Constraints #11 and #12 invariants pinned) at **zero behavior risk**, leaves M11.0d-1's snapshot pin byte-identical, and lets the operator review the exact Korean prose changes BEFORE shipping them in M11.0d-3b-2.

**M11.0d-3b-2 plan (when operator decides to ship prose alignment):**

1. Modify `policy_decision._decision_summary` and `policy_decision._action_recommendation` to accept the P2-derived label rather than P1's local label. Simplest implementation: change `make_final_decision` to take an optional `authoritative_alert_level` kwarg that, when provided, is used for prose; otherwise falls back to P1's own label (preserves existing call sites that don't pass P2's label).
2. In `main.analyze_pipeline`, after `calibrate_final_decision` returns, re-derive prose via `make_final_decision(..., authoritative_alert_level=final_decision["policy_alert_level"])` OR call a new dedicated prose-only entry point.
3. Update `tests/fixtures/m11_0d_1_regression_fixtures_snapshot.json` prose fields (label fields untouched) with an explicit M11.0d-3b-2 lineage comment.
4. Render verification REQUIRED — Korean UI strings change for ~30% of analyses.
5. Spot-check the strong-evidence ELS fixture: prose should switch from "정책 신뢰도와 영향도가 중간 이상…" (MEDIUM) to "공식 검증과 high 영향이 결합되어…" (HIGH).
