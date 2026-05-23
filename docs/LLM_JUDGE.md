# LLM Judge (M13.1)

## Two-phase rollout

| Phase  | What it does                                          | Status     |
|--------|-------------------------------------------------------|------------|
| M13.1a | Infrastructure + dry-run CLI; NOT connected to verdict pipeline | this PR    |
| M13.1b | Connect Judge to `analyze_pipeline` behind feature flag | future     |

The split is intentional. The audit explicitly says
"LLM cannot raise confidence — only downgrade." A two-phase rollout
lets us verify that invariant against real verdict inputs (via the
M13.1a dry-run CLI) **before** any production behaviour changes.

## Why this exists

The Phase 1 audit identified that the current LLM call
(`ai_reasoner.run_ai_reasoning`) runs late in the pipeline (step 24 of
26) and does not influence any user-visible verdict. The Judge
promotes the LLM from "peripheral cosmetic" to "constrained gate" —
but with strict guardrails:

- The Judge can `confirm`, `downgrade`, or `flag_for_review`.
- The Judge CANNOT `upgrade`.
- Schema-validated. Malformed output falls back to `confirm`.
- Refused upgrade attempts are logged with the
  `refused upgrade attempt: <from> -> <to>` reason.

This preserves the "conservative under weak evidence" invariant while
letting LLM reasoning catch cases where rules emit `draft_verified`
despite weak inputs.

## What M13.1a adds

- `llm_judge.py` — Judge module, schema validator, provider abstraction
  with stubs.
- `scripts/dry_run_llm_judge.py` — CLI for observing Judge behaviour
  without affecting verdicts.
- `tests/test_llm_judge.py` — comprehensive tests with mocked providers
  (59 cases).
- `docs/LLM_JUDGE.md` — this document.

## What M13.1a does NOT do

- Does NOT connect the Judge to `analyze_pipeline`.
- Does NOT modify `verification_card.py`, `policy_decision.py`,
  `policy_scoring.py`, `policy_confidence.py`, `ai_reasoner.py`.
- Does NOT modify `main.py`, `api_server.py`, `job_manager.py`.
- Does NOT make real LLM API calls (providers are stubs that always
  report `is_available() == False`; tests use in-test fake providers).
- Does NOT add OpenAI or Anthropic to CI / validation requirements —
  the validate.py guard explicitly sets `OPENAI_API_KEY=""` and the
  Judge module has zero top-level dependencies on `openai` /
  `anthropic`.
- Does NOT change any verdict stored in `analysis_results`.

## Label severity rank

Mirrors the M11.0b descriptive ordering in
`docs/VERDICT_LABEL_DIAGNOSTIC.md`. Lower rank = more conservative.

| Rank | Labels                                                                                                  |
|------|---------------------------------------------------------------------------------------------------------|
| 0    | `draft_unverified`                                                                                      |
| 1    | `draft_needs_context`, `draft_needs_review`, `draft_needs_official_confirmation`, `draft_disputed`, `draft_high_risk_review` |
| 2    | `draft_likely_true`                                                                                     |
| 3    | `draft_verified`                                                                                        |

A downgrade is a **strict decrease** in rank. Lateral moves within
the same rank (e.g. `draft_needs_review` → `draft_needs_context`,
both rank 1) are NOT downgrades and are refused by the validator.

## Safety invariants

- `truth_claim` is always `False` in every Judge output (asserted at
  serialisation time in `judge_verdict_to_dict`).
- `operator_review_required` is always `True` in every Judge output.
- Schema validator refuses any `action` outside
  `{confirm, downgrade, flag_for_review}`.
- Schema validator refuses any downgrade whose `new_label` is not
  strictly more conservative than the input label.
- Provider crashes never escape — `run_judge` always returns a
  `JudgeVerdict`.
- A provider that succeeds but returns content the validator rejects
  does NOT advance the chain (the model spoke, it just spoke badly).
- No real network or LLM API call is made during validation or CI.
- `llm_judge.py` has no top-level imports of `openai`, `anthropic`,
  `requests`, `httpx`, `urllib.request`, or `socket`.
- `llm_judge.py` is NOT imported by any pipeline entry point — the
  contract is pinned by static tests.

## Dry-run CLI

```
python scripts/dry_run_llm_judge.py --status
python scripts/dry_run_llm_judge.py --analysis-id 105
python scripts/dry_run_llm_judge.py --from-sqlite --limit 10
python scripts/dry_run_llm_judge.py --simulate-downgrade --analysis-id 105
python scripts/dry_run_llm_judge.py --simulate-upgrade-attempt --analysis-id 105
```

The `--simulate-*` flags use built-in fake providers for exercising
the validation pipeline without real LLM calls:

| Flag                            | Fake provider response                           |
|---------------------------------|--------------------------------------------------|
| `--simulate-confirm`            | `{action: confirm}`                              |
| `--simulate-downgrade`          | `{action: downgrade, new_label: draft_needs_context}` |
| `--simulate-flag`               | `{action: flag_for_review}`                      |
| `--simulate-malformed`          | `{ not json — exercises validator fallback`     |
| `--simulate-upgrade-attempt`    | `{action: downgrade, new_label: draft_verified}` — validator refuses |

## What happens in M13.1b

M13.1b will:

1. Replace `StubAnthropicProvider` and `StubOpenAIProvider` with real
   implementations (lazy-importing `anthropic` and `openai` inside
   `call()` so the M13.1a module shape stays unchanged).
2. Add a `JUDGE_ENABLED` env var (default `false`).
3. Add a Judge step to `analyze_pipeline` AFTER the deterministic
   verdict but BEFORE the verification_card export.
4. Persist Judge outputs in a new DB table with full audit trail.
5. Surface Judge actions in the reviewer UI.

M13.1b will not happen until M13.1a's dry-run data shows the Judge
behaves correctly on real data.

## Cost guardrails (M13.1b prep, not M13.1a)

When real providers are wired in M13.1b:

- Per-call token budget.
- Daily total budget with circuit-breaker.
- Prompt caching where available.
- Failure routes to `_safe_confirm_fallback` — never blocks a verdict.
