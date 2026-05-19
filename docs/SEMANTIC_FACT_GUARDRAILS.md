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

A semantic-only `strong` label on any of these is precisely the failure
mode that semantic calibration (M5.6) was designed to surface. M5.7 is the
mitigation: a deterministic check that runs after ranking and caps the
exposed `support_level` when a critical disagreement is detected.

## B. What it does

For every per-claim top match the agent ranked, `semantic_fact_guardrails`
extracts the critical factual elements from both the claim and the
matched chunk, then compares them. The comparator emits:

- `risk_flags` — `number_mismatch`, `date_mismatch`, `eligibility_mismatch`,
  `finality_mismatch`, `negation_mismatch`, `missing_critical_fact`.
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

## H. How to disable

The guardrails run whenever the semantic evidence agent runs. They are
deterministic, pure-stdlib, and have no external dependency, so there is
no env flag to gate them. Disabling semantic matching itself
(`SEMANTIC_MATCHING_ENABLED=false`, the default) makes the entire layer
short-circuit; the new summary fields are still present but populated
with safe defaults (`critical_mismatch_count=0`,
`support_cap_applied_count=0`, `best_raw_support_level="unavailable"`).

## I. Validation

```
python scripts/validate.py
python tests/test_semantic_fact_guardrails.py
python scripts/evaluate_semantic_calibration.py --provider deterministic --fail-on-regression --show-failures
python scripts/evaluate_semantic_calibration.py --provider openai --no-network --fail-on-unavailable
```

CI runs the first two on every push. None make a live API call.
