# Magic Thresholds — Verdict-Pipeline Catalog

## Purpose

`claude_audit_phase1.md` §1.5 #5 called out "magic thresholds everywhere"
in the verdict pipeline. This file is the single-source catalog of
every numeric threshold that materially affects pipeline verdict
output (`policy_alert_level`, `verdict_label`, `disagreement_signal`,
`final_score`, etc.).

Each entry below records:

- **Location** — `file:line` (or function name when inline)
- **Value** — current numeric literal
- **Controls** — what verdict-side decision the threshold gates
- **Calibration source** — commit / milestone / docs link, when discoverable
- **Consequence if changed** — which downstream output shifts
- **Re-evaluation trigger** — when the operator should revisit

**Hard contract: this file is documentation only.** Changing any
threshold value requires its own milestone with verdict regression
proof. The values in this catalog should match the values in source
exactly — `tests/test_magic_thresholds_documented.py` enforces this
for the most verdict-critical thresholds.

Scope: ~30 thresholds covering audit-named cases + the
verdict-output-critical inline literals in `policy_scoring.py`,
`source_reliability_agent.py`, `evidence_extraction_agent.py`,
`evidence_comparator.py`, and `policy_confidence.py`. This is the
**Option N (Narrow)** catalog approved in audit §1.5 #5 Phase 1. A
future Option F (Full) milestone would expand to every numeric
literal in the verdict path (~100+ entries) — out of scope here.

---

## §1 — Document fetch gates (`official_crawler.py`)

### `MIN_DOCUMENT_SCORE = 25`
- **Location:** `official_crawler.py:40`
- **Controls:** Pre-evaluation gate — candidate documents with `score < 25` are skipped from relevance scoring entirely (set `relevance_score=0`, `relevance_level="unrelated"`, continue). See `fetch_best_official_document` per-candidate loop.
- **Calibration source:** Audit §1.5 #5 originally flagged this. Calibrated during early official-crawler work; no specific commit comment. Verdict regression suites (`tests/test_verdict_label_b08_fix.py`, `tests/test_verdict_producer_comparison.py`) pin downstream outputs but not this threshold directly.
- **Consequence if changed:**
  - **Raise (e.g., 30):** fewer candidates evaluated → fewer official docs make it into the verification card → more "official_mismatch" verdicts.
  - **Lower (e.g., 15):** more low-quality candidates scored → potential noise in `document_relevance_score`.
- **Re-evaluation trigger:** A Render-log audit showing a high rate of `relevance_level="unrelated"` early-skips → consider lowering. A high rate of low-quality "C" / "D" / "F" evidence_grade docs → consider raising.

### `WEAK_DOCUMENT_RELEVANCE_THRESHOLD = 35`
- **Location:** `official_crawler.py:41`
- **Controls:** "Weakly usable" gate. If best-evaluated doc has `relevance_score < 35`, the document is marked `usable=False, weakly_usable=False` and the source is excluded from verification.
- **Calibration source:** M11.0c B08 gating work — see `docs/VERDICT_LABEL_DIAGNOSTIC.md`.
- **Consequence if changed:** Shifts the `weakly_usable` / not-usable boundary. Directly affects `evidence_comparator._is_comparable_evidence` (which gates on `weakly_usable + relevance_score >= 35` for inclusion).
- **Re-evaluation trigger:** B08 false-positive rate change in M11.0b-style diagnostic data.

### `DOCUMENT_RELEVANCE_THRESHOLD = 40`
- **Location:** `official_crawler.py:42`
- **Controls:** "Strongly usable" gate. Combined with `evidence_grade in {A, B, C}`, gates `result["usable"] = True` (vs. `weakly_usable`).
- **Calibration source:** Same as `WEAK_*` above — M11.0c.
- **Consequence if changed:** Strong/weak boundary for official-evidence inclusion. Materially affects P2 source_trust score (via `official_body_match_count`).
- **Re-evaluation trigger:** Render-log distribution of `document_relevance_score` for usable vs. weakly_usable docs.

---

## §2 — Reliability tier scores (`source_reliability_agent.py`)

### Tier-base scores in `evaluate_source_candidate`

- **Location:** `source_reliability_agent.py:165-180`
- **Values:**
  | Tier | Base score | Trigger |
  | --- | --- | --- |
  | very_high | `95` | `source_type == "official_government"` OR domain in `VERY_HIGH_DOMAINS` |
  | high | `85` | `source_type == "public_institution"` OR domain in `HIGH_DOMAINS` |
  | established_news | `68` | `source_type == "established_news"` OR domain in `NEWS_DOMAINS` |
  | search_fallback_news | `52` | `source_type == "search_fallback_news"` |
  | unknown | `30` | default fallthrough |
- **Controls:** `reliability_score` per source, which feeds:
  1. `_level()` mapping → `reliability_level` (`very_high/high/medium/low/unknown`) at boundaries `90/75/45/25`.
  2. `policy_scoring._source_trust_score` arithmetic (average reliability → `+ min(15, avg // 5)`).
  3. `verification_card`'s top-source selection.
- **Calibration source:** No commit comment found; predates M11. Used by the M11.0c B08 fix indirectly via `policy_confidence_score` and trust score. Verdict regression suites pin the END-TO-END output but not these tier values directly.
- **Consequence if changed:**
  - **Lowering very_high (e.g., 95 → 80):** primary-evidence official docs would lose ~3 points from the trust score's `+ min(15, avg // 5)` term. Could shift HIGH alerts to WATCH.
  - **Raising unknown (e.g., 30 → 50):** unknown-publisher sources would graduate from "low" to "medium" reliability level; their `verification_role` would change from `not_reliable_enough` to `supporting_evidence`.
- **Re-evaluation trigger:** Production data on how often each tier is selected as `selected_primary_source`; calibration paper or external benchmark for Korean media reliability.

### `_level` boundaries
- **Location:** `source_reliability_agent.py:77-86`
- **Values:** `90` → very_high, `75` → high, `45` → medium, `25` → low, else unknown
- **Controls:** `reliability_level` field in source candidate dict (text label for UI).
- **Consequence if changed:** UI labels shift; downstream conditional `verification_role` mapping in `_role()` may change.

---

## §3 — Evidence quality additive weights (`evidence_extraction_agent.py`)

### `_quality_score` weights
- **Location:** `evidence_extraction_agent.py:182-263` (full body of `_quality_score`)
- **Magic adds (in order of accumulation):**
  | Step | Weight | Trigger |
  | --- | --- | --- |
  | Base | `20` | every snippet starts at 20 |
  | Source type — official | `+28` | `source_type in {official_government, public_institution}` |
  | Source type — established_news | `+16` | `source_type == "established_news"` |
  | Source type — search_fallback | `+8` | `source_type == "search_fallback_news"` |
  | Verification role — primary | `+14` | `verification_role == "primary_evidence"` |
  | Verification role — supporting | `+8` | `verification_role == "supporting_evidence"` |
  | Evidence type — direct_support | `+28` | `evidence_type == "direct_support"` |
  | Evidence type — indirect_support | `+18` | `evidence_type == "indirect_support"` |
  | Evidence type — background_context | `+6` | `evidence_type == "background_context"` |
  | Evidence type — official_reference | `+8` | `evidence_type == "official_reference"` |
  | Evidence type — insufficient | `-25` | `evidence_type == "insufficient_evidence"` |
  | Extraction method — article_body | `+16` | sentence-overlap extraction (highest-quality method) |
  | Extraction method — metadata_overlap | `+5` | metadata-only fallback |
  | Extraction method — official_no_body | `-8` | candidate without body text |
  | Extraction method — no_match | `-30` | `extraction_method == "no_relevant_sentence_found"` |
  | Relevance bonus | `min(20, relevance_score // 5)` | proportional to relevance_score |
  | Source confidence bonus | `min(12, source_confidence // 10)` | per-source reliability |
  | Length bonuses | `+5` (≥80 chars), `-10` (<20 chars) | evidence text length |
  | Various clamps | `min(score, 35)` or `min(score, 60)` | for not-fetched / official_reference / topic_mismatch / mismatched-body / search_fallback |
  | Floors for official classification | `max(score, 78)` (strong_direct) / `max(score, 55)` (medium_contextual) | when official_evidence_classification is set |
  | Final clamp | `max(0, min(100, score))` | bound to [0, 100] |
- **Controls:** `evidence_quality_score` per snippet → averaged into `quality_summary["average_evidence_quality_score"]` → fed to P2's `_alert_from_score` (`evidence_quality_score >= 65` gate for HIGH).
- **Calibration source:** Audit §1.5 #5 cites L178-259 — this is the central calibration of the evidence-quality dimension. Pinned indirectly by `tests/test_verdict_label_diagnostic.py` (42 cases) and `tests/test_verdict_label_b08_fix.py` (24 cases).
- **Consequence if changed:** EACH ADDITIVE shifts the bell curve of evidence_quality_score. The +28 (official source) is the largest single contribution; weakening it could collapse the HIGH-alert path.
- **Re-evaluation trigger:** Distribution histogram of `evidence_quality_score` per source_type / evidence_type combination in production data.

---

## §4 — Policy confidence clamp (`policy_confidence.py`)

### Confidence clamp when no usable official doc
- **Location:** `policy_confidence.py:140`
- **Value:** `policy_confidence_score = min(20, policy_confidence_score)` when `not official_usable`
- **Controls:** Forces confidence_score ≤ 20 when no usable official evidence — drives `verification_strength = "none"` in the next line (which gates many downstream P2 paths).
- **Calibration source:** Predates M11. Cited as audit §1.5 #5 magic-threshold example. The "20" is also the `unknown` tier value in `_source_confidence_score` mapping (line 153) — likely intentional symmetry.
- **Consequence if changed:**
  - **Raise (e.g., 30):** confidence_score floor for no-official docs goes up; some "none" verifications become "low" → more MEDIUM alerts.
  - **Lower (e.g., 10):** tighter no-official clamp; more "none" → more WATCH/LOW alerts.
- **Re-evaluation trigger:** Render data showing how often `verification_strength == "none"` is the gating reason for WATCH/LOW alerts.

### `_verification_strength` boundaries
- **Location:** `policy_confidence.py:97-104`
- **Values:** `>= 75` → high, `>= 50` → medium, `>= 25` → low, else none
- **Controls:** `verification_strength` field — feeds P1 + P2 alert-level computation.
- **Consequence if changed:** Recalibrates the discrete strength tiers used in `policy_decision._policy_alert_level` and `policy_scoring._alert_from_score`.

---

## §5 — P2 base-score weights (`policy_scoring.calibrate_final_decision`)

### Weighted-average coefficients
- **Location:** `policy_scoring.py:199-205`
- **Values:**
  | Component | Weight | Source |
  | --- | --- | --- |
  | strength_component | `0.25` | from `_strength_score` |
  | evidence_quality_score | `0.25` | from `quality_summary` |
  | source_trust | `0.25` | from `_source_trust_score` |
  | confidence_score | `0.15` | from `policy_confidence` |
  | impact_component | `0.10` | from `_impact_gate` |
- **Controls:** Computes the P2 `base_score` that's then adjusted by `human_adjustment + contradiction_adjustment` and clamped → `final_score`. The verdict label is then derived from `final_score` by `_alert_from_score`.
- **Calibration source:** M11.0d (calibrated P2 producer). See `docs/M11.0d_VERDICT_PRODUCER_DISAGREEMENT_MAP.md`. The 0.25/0.25/0.25/0.15/0.10 split was chosen so the three "evidence dimensions" each get equal voice, with confidence and impact playing secondary roles.
- **Consequence if changed:** Reweights the entire P2 score. Even small changes (e.g., 0.25 → 0.30 for evidence_quality) can flip borderline HIGH/WATCH cases. Verdict regression suite covers a 24+42+37 = 103 case matrix; any weight change must produce byte-identical output across all 103.
- **Re-evaluation trigger:** Sustained operator disagreement with P2 verdicts; M11.0d-3a `disagreement_signal` field aggregating across many runs.

---

## §6 — P2 alert-level cutoffs (`policy_scoring._alert_from_score`)

### HIGH alert criteria
- **Location:** `policy_scoring.py:137-145`
- **Conditions (ALL must hold):**
  - `final_score >= 75`
  - `evidence_quality_score >= 65`
  - `source_trust_score >= 55`
  - `strength_score >= 55`
  - `contradiction_adjustment == 0`
  - `impact_level == "high"`
- **Controls:** Returns `"HIGH"`. The strictest path — five separate gates plus the impact-level constraint.
- **Calibration source:** M11.0c B08 gating fix — see `docs/VERDICT_LABEL_DIAGNOSTIC.md`. The five-gate AND structure was the resolution to the M11.0b false-positive rate.
- **Consequence if changed:** Adjusting ANY gate value shifts the HIGH/WATCH boundary directly. The 75/65/55/55 quadruple is THE most behavior-critical magic-number cluster in the codebase.

### WATCH alert criteria
- **Location:** `policy_scoring.py:131-147`
- **Conditions (any one triggers WATCH):**
  - `human_feedback_adjustment >= 15 and final_score >= 65` (positive human boost + decent score, but `impact_level != "high"`)
  - `contradiction_adjustment <= -35` (confirmed contradiction)
  - `official_mismatch and source_trust_score < 45` and `(impact_level == "high" or risk_level == "high")`
  - `final_score >= 45 or impact_level == "high" or risk_level == "high"` (fallback)
- **Controls:** Returns `"WATCH"` (the soft-alert state). Default when HIGH gate not satisfied but some signal is present.
- **Calibration source:** Same M11.0c B08 work. The 65 / 45 / 45 / -35 cluster controls how leakily a WATCH fires.

### Implicit LOW
- **Location:** `policy_scoring.py:148`
- **Value:** Returns `"LOW"` if no HIGH or WATCH condition fires.

---

## §7 — P2 source trust arithmetic (`policy_scoring._source_trust_score`)

- **Location:** `policy_scoring.py:31-64`
- **Magic accumulation:**
  | Step | Weight | Trigger |
  | --- | --- | --- |
  | Base | `20` | always |
  | official_detail bonus | `+25` | `official_detail_available` |
  | official_body_matches | `+ min(30, matches * 15)` | per match, capped at 30 |
  | official_usable fallback | `+12` | partial credit when matches absent |
  | official_candidates floor | `+5` | partial credit |
  | official_resolution_direct | `+ min(25, direct * 18)` | per direct match |
  | official_resolution_contextual | `+ min(15, contextual * 10)` | per contextual match |
  | top_score ≥ 75 | `+10` | strong official resolution |
  | top_score ≥ 55 | `+6` | medium official resolution |
  | fetched_official | `+10` | per-source flag |
  | average_reliability | `+ min(15, avg // 5)` | proportional to source-set reliability |
  | fallback_only clamp | `min(score, 45)` | when ALL sources are search_fallback_news |
  | no-match clamp | `min(score, 35)` | when official_mismatch AND no official_body_matches |
- **Controls:** `source_trust_score` (0-100), one of the three 0.25-weighted components in `base_score`.
- **Calibration source:** M11.0d producer-2 calibration. The chain of accumulators was specifically tuned to give official-body matches the largest single bump (`+30`) while still allowing partial credit for lesser official evidence.

---

## §8 — P2 impact gate (`policy_scoring._impact_gate`)

- **Location:** `policy_scoring.py:96-113`
- **Values:**
  | Component | Weight | Trigger |
  | --- | --- | --- |
  | impact_level high | `+18` |  |
  | impact_level medium | `+10` |  |
  | risk_level high | `+12` |  |
  | risk_level medium | `+6` |  |
  | sensitivity bonus | `+ min(10, max(consumer, market, business) // 10)` | best of 3 sensitivities |
  | Output clamp | `[0, 35]` | tight upper bound |
- **Controls:** `impact_component`, the 0.10-weighted final term in `base_score`. Caps at 35 (smaller than other components) by design — impact alone cannot drive verdict above WATCH.

---

## §9 — P2 adjustments

### `_human_feedback_adjustment`
- **Location:** `policy_scoring.py:67-74`
- **Values:** `+15` (approved_boost), `-30` (rejected_penalty), `-10` (needs_more_info)
- **Controls:** Operator-feedback adjustment applied to `final_score` after weighted average.
- **Calibration source:** Operator preference: rejection penalty (`-30`) is intentionally heavier than approval boost (`+15`) — false-positive avoidance is preferred.

### `_contradiction_adjustment`
- **Location:** `policy_scoring.py:77-93`
- **Values:** `-35` (confirmed contradiction), `-12` (possible contradiction)
- **Controls:** Adjustment applied to `final_score`. `-35` matches the WATCH-trigger threshold in `_alert_from_score`.

---

## §10 — Other module-level pins

### `contradiction_agent.SOURCE_SCORE_MINIMUM = 45`
- **Location:** `contradiction_agent.py:35`
- **Controls:** Minimum source reliability_score for a snippet to be considered as a contradiction candidate.
- **Calibration source:** Predates M11.

### `verdict_label_diagnostic.WEAK_EVIDENCE_SCORE_THRESHOLD = 30`
- **Location:** `verdict_label_diagnostic.py:372`
- **Controls:** B08 weak-evidence-score gating threshold used by the diagnostic script (NOT live pipeline; used for retrospective analysis).
- **Calibration source:** M11.0b diagnostic work — see `docs/VERDICT_LABEL_DIAGNOSTIC.md`.

---

## §11 — Evidence comparator support-score formula

- **Location:** `evidence_comparator.py:367-372`
- **Formula:**
  ```python
  support_score = min(100, int(round((matched/total) * 70)) + min(30, doc_success * 10 + search_success * 5))
  ```
- **Magic weights:** `* 70` (keyword-overlap component, primary), `min(30, ...)` (corpus-access bonus), `* 10` (per successful document fetch), `* 5` (per successful search), capped at `100`.
- **Controls:** `support_score` field on evidence-comparison result; cited by `_make_summary` prose and downstream verification levels.
- **Calibration source:** Predates M11. The 70/30 split intentionally weights keyword overlap over access count.

---

## Re-evaluation triggers (summary)

- **HIGH-alert false-positive rate up:** revisit §1 (`MIN_DOCUMENT_SCORE` / `WEAK_*` / `DOCUMENT_RELEVANCE_THRESHOLD`) + §6 HIGH gate cluster.
- **WATCH-alert noise complaints:** revisit §6 WATCH conditions, especially the `final_score >= 45` fallback.
- **Operator-disagreement-with-P2 sustained:** revisit §5 base-score weights using `disagreement_signal` aggregated data.
- **Source distribution shift (more fallback / fewer official):** revisit §2 reliability tier scores or §7 source-trust arithmetic.
- **New evidence-source type added (e.g., new official body):** revisit §3 quality-weight additives.

## Out-of-scope for this catalog

- Cache TTLs and bytes caps (not verdict-affecting; per-site self-explanatory).
- Sentence-length bounds (`>= 25`, `<= 350`, etc. in `_split_sentences`) — operational tokenization choices, not verdict thresholds.
- HTTP timeouts, request retry counts — connectivity tuning, not verdict-affecting.
- Logger level thresholds — observability tuning.
- A future M-something milestone could fully document these too (Option F per the audit Phase 1).

## Maintenance

- When a value listed here changes in source, this catalog must update in the same PR.
- `tests/test_magic_thresholds_documented.py` enforces drift detection on the most-critical entries (the §1 document-fetch gates + §6 alert-cutoff cluster).
- A new threshold added to the verdict path should be appended to the right section here and pinned by an extension to the drift-detection test if it sits on a verdict boundary.
