# Semantic Critical-Fact Guardrails (Phase 2 M5.7)

A small, pure-stdlib layer that runs alongside the semantic evidence agent
to catch a category of false positives that pure cosine similarity cannot:
**claims and sources whose surface text is similar but whose critical
facts disagree** — numbers, dates, eligibility, finality, and negation.

## A. Why this layer exists

Embedding similarity captures paraphrase / topical overlap. It does not
care that the claim says "100만원" and the official source says "50만원" —
their cosine similarity is still extremely high because almost every other
token overlaps. The same is true for:

- "2026년 시행" vs "2025년 시범 운영"
- "누구나 신청 가능" vs "소득 요건 충족자만"
- "최종 확정" vs "검토 중 / 아직 확정되지 않"
- A source whose body explicitly contains a refutation ("사실이 아닙니다")
- **(M6.6)** "청년 주거 대출 한도 확대" vs "청년 주거 바우처 시행 정책" —
  same topic, different policy instrument (loan vs voucher)
- **(M6.6)** "정부가 전국 시행" vs "서울시 자체 사업" — central claim
  matched against a local-only source

A semantic-only `strong` label on any of these is precisely the failure
mode that semantic calibration (M5.6) was designed to surface. M5.7 is the
mitigation: a deterministic check that runs after ranking and caps the
exposed `support_level` when a critical disagreement is detected. **M6.6
extends the check with two more flags — `policy_scope_mismatch` and
`actor_scope_mismatch` / `local_vs_central` — after the M6.5 live OpenAI
run on the 72-case real-claim batch surfaced the first overstrong
result** (`real_wrong_policy_housing_loan_vs_voucher`, OpenAI cosine 0.87,
no existing flag fired because numbers / dates / eligibility / finality /
negation / missing_critical_fact were all clean).

## B. What it does

For every per-claim top match the agent ranked, `semantic_fact_guardrails`
extracts the critical factual elements from both the claim and the
matched chunk, then compares them. The comparator emits:

- `risk_flags` — `number_mismatch`, `date_mismatch`, `eligibility_mismatch`,
  `finality_mismatch`, `negation_mismatch`, `missing_critical_fact`,
  `policy_scope_mismatch` (M6.6), `actor_scope_mismatch` (M6.6),
  `local_vs_central` (M6.6).
- `mismatches` — detailed dicts with `claim_value`, `source_value`, and a
  human-readable reason.
- `support_cap` — `strong` (no cap), `contextual`, or `weak`.

The semantic evidence agent then takes the tightest cap across all top
matches for a claim and applies it to the per-claim `support_level`:

| raw label | cap | exposed `support_level` |
| --- | --- | --- |
| strong | strong | strong |
| strong | contextual | contextual |
| strong | weak | weak |
| contextual | weak | weak |

The raw value is preserved on every claim as `raw_support_level` and on
the summary as `best_raw_support_level` for diagnostics and threshold
tuning. A new aggregate is published on the summary:

```text
semantic_evidence_summary = {
    ...,
    "semantic_guardrails_enabled": True,
    "best_support_level": "weak",            # guardrail-adjusted
    "best_raw_support_level": "strong",      # pre-cap
    "semantic_risk_flags": ["number_mismatch", ...],
    "critical_mismatch_count": 1,
    "support_cap_applied_count": 1,
    "claim_matches": [{
        "support_level": "weak",                       # adjusted
        "raw_support_level": "strong",                 # pre-cap
        "guardrail_adjusted_support_level": "weak",
        "support_cap_applied": True,
        "support_cap_reason": "capped to weak by guardrails",
        "semantic_risk_flags": [...],
        "critical_mismatches": [...],
        "top_matches": [{
            ...,
            "critical_fact_check": { ... }   # per-match guardrail report
        }],
    }],
}
```

## C. What it does NOT do

- It does **not** decide a verdict. `policy_decision`, `policy_scoring`,
  and `verification_card` do not import or read it — pinned by
  `tests/test_semantic_fact_guardrails.py:VerdictIsolationTests`.
- It does **not** call any external service. Pure regex over Korean text.
- It does **not** fabricate quotes — `mismatches` only reference the raw
  tokens already in the claim / source.
- It does **not** require enabling embeddings. The guardrails run inside
  the semantic evidence agent; when matching is disabled the agent
  short-circuits and the new fields default to safe values.
- It does **not** assert truth. A `weak` cap means "do not treat the
  semantic match as supporting evidence here." The conservative rule-based
  labels remain authoritative.

## D. Extraction rules (summary)

| signal | pattern |
| --- | --- |
| numbers | digit group (with optional thousands separator) + Korean unit (`만원`, `억원`, `%`, `명`, `건`, etc.). Years (`YYYY년`) and months (`[M]월`) are excluded so 2026 isn't read as an amount. |
| dates | `YYYY년 [M월]`, `YYYY[-./]M`, or bare `[M]월`. |
| eligibility | universal-claim words (`누구나`, `모두`, `전 국민`, ...) vs restriction words (`소득 기준`, `요건 충족`, `대상자`, `한해`, ...). |
| finality | finality (`확정`, `시행`, `발표`, `공포`, ...) vs tentative (`검토 중`, `시범 운영`, `예정`, `미정`, `확정되지 않`, ...). The negated-finality token (`확정되지`, `결정되지 않`, ...) neutralises a parallel positive term. |
| negation | refutation phrases in the source (`사실이 아`, `허위`, `오보`, `정정`, ...). |
| policy instruments (M6.6) | Mutually-exclusive policy-instrument groups: `transfer_type` (`신용보증`, `대출`, `바우처`, `보조금`, `지원금`, `보증`), `tax_adjustment` (`최종 확정`, `면제`, `감면`, `인하`, `인상`, `폐지`, `신설`), `program_kind` (`R&D 지원`, `시범 사업`, `보조 사업`, `등록제`, `인턴십`). Within each group, instruments are mutually exclusive; longest-match wins so `신용보증` doesn't double-count as `보증`. |
| actor / authority scope (M6.6) | National authorities (`정부`, `중앙정부`, 16 ministries / commissions) + national scope tokens (`전국`, `전면 시행`, `전국적으로`); local authorities (17 시·도, `시도교육청`, `지자체`, `동주민센터`) + local-scope tokens (`자체적으로`, `자체 예산`, `선정 지역`, `일부 지역`, `시범 사업`). |

Unit semantics are conservative — comparison only triggers a mismatch
when units agree, so "5천만원" never collides with "30%".

## E. Cap semantics

`compare_critical_facts` returns the tightest cap among all triggered
flags. Cap ranking (lowest first): `unavailable` < `weak` < `contextual`
< `strong`.

| flag | cap |
| --- | --- |
| `number_mismatch` | weak |
| `date_mismatch` | weak |
| `eligibility_mismatch` | weak |
| `finality_mismatch` | weak |
| `negation_mismatch` | weak |
| `policy_scope_mismatch` (M6.6) | weak |
| `actor_scope_mismatch` / `local_vs_central` (M6.6) | weak |
| `missing_critical_fact` (claim mentions an amount/date the source lacks) | contextual |
| no flag | strong (no cap) |

`missing_critical_fact` is intentionally softer — the source didn't
*contradict* the claim, it just couldn't confirm the specific number or
date. Pairing this with the conservative `support_level` makes "the claim
says ₩100만원 but the source body never references that figure" a
*contextual*, not a *weak*, semantic match.

## F. Pipeline integration

The guardrails are applied inside
`semantic_evidence_agent.compute_semantic_evidence_summary`. The full
order is:

```
build_verification_card
  └─ debug_summary = build_pipeline_debug_summary(...)
  └─ debug_summary.update(official_body_debug)
  └─ debug_summary.update(official_resolution_debug)
  └─ debug_summary["semantic_evidence_summary"]
       └─ rank chunks via semantic_similarity
       └─ apply critical-fact guardrails (M5.7)         ← NEW
       └─ expose adjusted support_level (+ raw)
  └─ official_mismatch shaping
  └─ calibrate_final_decision(debug_summary=...)        ← unchanged
  └─ verification_card["debug_summary"] = debug_summary
```

`calibrate_final_decision` does not read the M5.7 fields any more than it
read the M5 ones — semantic match (raw or adjusted) is still metadata
only.

## G. Calibration evaluator updates

`scripts/evaluate_semantic_calibration.py` now prints a `guardrails:`
line in its scorecard:

```
[evaluate] scorecard: cases=8 pass=8 fail=0 ... overstrong=0 ...
  support_level_distribution={'weak': 7, 'unavailable': 1}
  guardrails: cap_applied=4/8 critical_mismatches=5
    raw_distribution={'strong': 3, 'contextual': 1, 'weak': 3, 'unavailable': 1}
    risk_flags={'number_mismatch': 1, 'date_mismatch': 1, ...}
```

Per-case stdout includes:

- `support=<adjusted>*` — the `*` marks a cap was applied.
- `raw=<raw_support_level>` — pre-cap level.
- `cm=<count>` — critical mismatches detected for the case.
- `guardrail_risk_flags: [...]` — flags emitted by the comparator.

CSV adds columns `raw_support_level`, `support_cap_applied`,
`critical_mismatch_count`, `semantic_risk_flags`. The Markdown report
gains a per-case `cap_applied` / `critical_mismatches` /
`guardrail_risk_flags` column block, plus a scorecard summary block.

## H1. Policy-scope and actor-scope guardrails (M6.6)

The M6.5 live OpenAI run on the 72-case real-claim batch produced the
first overstrong result across all OpenAI runs:
`real_wrong_policy_housing_loan_vs_voucher` scored cosine **0.87**
because claim "정부가 청년 주거 **대출** 한도를 확대한다" and source
"정부는 청년 주거 **바우처** 시행 정책을 안내했다" share `정부 + 청년
+ 주거` topic tokens. None of the M5.7 flags fired because no
number/date/eligibility/finality/negation/missing-critical-fact pattern
matched — both texts are perfectly fluent and topically related, just
about **different policy instruments**.

M6.6 closes that gap with two additional deterministic checks:

**`policy_scope_mismatch`** — claim and source both mention an
instrument from the same mutually-exclusive group (e.g. `transfer_type`:
대출 / 바우처 / 보조금 / 지원금 / 신용보증 / 보증) but the instruments
differ. Caps to `weak`. Within-group overlap (both have `지원금`)
explicitly does **not** fire, so number/date mismatches on aligned
instruments still take the appropriate softer cap.

**`actor_scope_mismatch` + `local_vs_central`** — claim is clearly
national (mentions central-government authority or `전국`/`전면 시행`
scope token) AND source is clearly local-only (mentions 17 시·도, 시도
교육청, 지자체, 동주민센터, 자체적으로, 시범 사업, 일부 지역) with
**no** national-authority or national-scope reference. Multi-tier
policies (source mentions both 정부 and 서울시) intentionally do NOT
fire so genuinely coordinated programs aren't over-capped.

Both new flags follow the same "tightest cap wins" semantics as the
M5.7 flags. Verified on the M6.4 deterministic 72-case fixture:

- 4 cases fire `policy_scope_mismatch` (3 wrong-policy + 1 tax-direction
  variant), all correctly capped from `strong`/`contextual` → `weak`.
- 4 cases fire `actor_scope_mismatch` + `local_vs_central` (all 4
  local-vs-central authority cases), all correctly capped to `weak`.
- 9 legitimate `direct_support` final-strong cases remain unchanged —
  no false-positive caps.
- `overstrong_count` stays at **0**.

Behavioral verification on the M6.5 failing case is pinned by
`PolicyScopeMismatchTests::test_m65_failing_case_now_caps_to_weak`.

## H. Coverage on the expanded calibration fixture (M6.0)

The M6.0 calibration fixture (36 cases, see
`docs/SEMANTIC_CALIBRATION.md`) exercises every guardrail flag in this
module across realistic policy domains. On the deterministic provider,
the M6.0 baseline measured:

- `number_mismatch`: 4 cases — caps each to `weak` (subsidy 100 vs 50만원,
  loan limit 8 000 vs 3 000만원, disaster subsidy 300 vs 100만원, VAT cut
  5% vs 1%).
- `date_mismatch`: 4 cases — caps each to `weak` (year mismatch,
  application-period month mismatch, announce-vs-launch).
- `eligibility_mismatch`: 3 cases — caps each to `weak` (universal vs
  income cap, universal vs age band, universal vs household income).
- `finality_mismatch`: 8 cases — caps each to `weak`. More cases trigger
  this flag than the `finality_mismatch` category alone because several
  `date_mismatch` and `local_vs_central_authority` cases naturally carry
  a parallel finality disagreement (`시행` vs `시범 운영`, `확정` vs
  `검토 진행 중`).
- `negation_mismatch`: 2 cases — caps each to `weak`. Source explicitly
  refutes the claim (`사실이 아닙니다`, 보류, 정정).
- `missing_critical_fact`: 16 cases — caps each to `contextual`. This
  flag is the softer "claim mentions a number / date the source body
  never references" signal; it covers the `contextual_only`,
  `partial_support`, and many `number_mismatch` / `date_mismatch` cases
  where the source acknowledges the program but lacks the specific fact.

Across the 36 cases the guardrails capped 10 (raw `strong:7` →
adjusted `strong:1`). `overstrong_count` remained 0. No new guardrail
flag was added in M6.0 — the existing flag vocabulary already covers the
fixture honestly. Two M6.0 categories (`same_topic_wrong_policy`,
`actor_mismatch`) do not map to a guardrail flag; the deterministic
provider's raw score was already below the `strong` threshold for those
cases, so no cap was needed. If a future OpenAI run shows raw `strong`
on either of those categories, that is the signal to consider a small
actor-mismatch or policy-scope-mismatch extractor here.

## I. How to disable

The guardrails run whenever the semantic evidence agent runs. They are
deterministic, pure-stdlib, and have no external dependency, so there is
no env flag to gate them. Disabling semantic matching itself
(`SEMANTIC_MATCHING_ENABLED=false`, the default) makes the entire layer
short-circuit; the new summary fields are still present but populated
with safe defaults (`critical_mismatch_count=0`,
`support_cap_applied_count=0`, `best_raw_support_level="unavailable"`).

## J. Validation

```
python scripts/validate.py
python tests/test_semantic_fact_guardrails.py
python scripts/evaluate_semantic_calibration.py --provider deterministic --fail-on-regression --show-failures
python scripts/evaluate_semantic_calibration.py --provider openai --no-network --fail-on-unavailable
```

CI runs the first two on every push. None make a live API call.
