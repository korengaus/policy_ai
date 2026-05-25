# LLM Judge (M13.1)

## Three-phase rollout

| Phase  | What it does                                          | Status     |
|--------|-------------------------------------------------------|------------|
| M13.1a | Infrastructure + dry-run CLI; NOT connected to verdict pipeline | landed     |
| M13.1b | Real OpenAI provider + `analyze_pipeline` wiring behind `LLM_JUDGE_ENABLED` feature flag | **this PR** |
| M13.1c | Anthropic provider activation (Claude as primary, OpenAI as fallback) | future     |

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

## M13.1b — what ships in this PR

### OpenAIProvider (real provider)

`llm_judge.OpenAIProvider` calls the OpenAI Chat Completions API
(`client.chat.completions.create`) with the model from `config.AI_MODEL`
(currently `gpt-4o-mini`). The `openai` SDK is lazy-imported inside
`call()` so `llm_judge.py` keeps zero top-level LLM dependencies — the
M13.1a static-import test still passes.

Failure paths (all return a failure-shaped `LLMResponse` and let
`run_judge` fall through to safe-confirm):

- `openai` SDK not importable → `openai_sdk_missing`.
- `OPENAI_API_KEY` unset / blank → `missing_api_key`.
- Network / rate-limit / auth / timeout → `openai_call_failed:
  <ExceptionType>`. The exception **type** is logged but the full
  message (which can quote prompt fragments) is not, reducing the
  chance of leaking prompt PII into logs.
- Response shape unexpected (missing `.choices`, `.usage`, etc.) →
  `openai_response_shape_unexpected`.

Timeout: 15 seconds, passed to the `OpenAI(timeout=15.0)` constructor.

### `get_default_provider_chain()` shape under M13.1b

| Environment | Chain |
|---|---|
| `OPENAI_API_KEY` set (non-empty after strip) | `[OpenAIProvider()]` |
| Otherwise | `[StubOpenAIProvider()]` (offline-safe; `is_available → False`; drives `run_judge` to safe-confirm) |

`StubAnthropicProvider` remains in the module file but is NOT in the
default chain. M13.1c will revive it as a real `AnthropicProvider`.

### `LLM_JUDGE_ENABLED` feature flag

- Default: `"false"` (unset = disabled).
- Truthy: `"true"` (case-insensitive, stripped). Any other value —
  including `"1"`, `"yes"`, `"on"` — stays False. The stricter
  parsing is intentional and avoids accidentally enabling the judge
  via shell habits.
- Read lazily at the pipeline call site on every analyze invocation;
  toggling the env var does NOT require a process restart (matches
  the `is_postgres_dual_write_enabled` pattern).
- Independent of `OPENAI_API_KEY`. Both conditions are required for
  the judge to make a real call; either being unset reverts to safe
  fallback behaviour.

### Pipeline integration point

The judge is invoked inside `main._process_news_item_phase_a`,
between `print_final_decision(...)` and the
`_build_disagreement_signal(...)` call. The block:

1. Captures `p2_alert_pre_judge = final_decision.get("policy_alert_level")`
   BEFORE the judge runs.
2. If `llm_judge_enabled()`, builds a `JudgeInput`, calls `run_judge`,
   and applies the verdict via `_apply_judge_to_final_decision`.
3. Wraps the whole block in `try/except Exception` with a
   `log.warning("llm_judge.failed", extra={"error_type": ...})` —
   the pipeline NEVER fails because of the judge.
4. Records the verdict (or `None` when disabled) in
   `debug_summary["llm_judge"]`.
5. Passes `p2_alert_pre_judge` (NOT the post-judge value) to
   `_build_disagreement_signal` so the M11.0d-1 fixtures stay
   byte-identical even when a judge downgrade fires.

### Application-site invariants

| Field | Modifiable by judge? | How |
|---|---|---|
| `final_decision["policy_alert_level"]` | YES — downgrade only | Drops exactly one tier via `_ALERT_TIER_DOWNGRADE = {"HIGH"→"WATCH", "WATCH"→"LOW", "LOW"→"LOW"}` |
| `final_decision["llm_judge_flagged_for_review"]` | Set to `True` on `flag_for_review` action | Never overwritten elsewhere |
| `verification_card["verdict_label"]` | **NO** | Byte-identical pre/post judge — the judge reads it as input only |
| `final_decision["action_recommendation"]` / `["decision_summary"]` | **NO** | Prose realignment already ran; judge never rewrites |
| `final_decision["market_signal"]` / `["decision_reasons"]` | **NO** | Label-independent |
| `disagreement_signal` | **NO** | Computed from `p2_alert_pre_judge`, not the post-judge value |
| `operator_review_required` | **NO** | ALWAYS True elsewhere; judge never sets / reads / writes |
| `truth_claim` | **NO** | ALWAYS False elsewhere; judge never sets / reads / writes |

Schema-layer enforcement: `validate_judge_response_json` refuses any
upgrade attempt at the source. The application site
(`_apply_judge_to_final_decision`) is a second guard — the alert
tier can only move down. Two-layer defence.

**Schema tolerance for extra keys (M13.1b decision):** the validator
intentionally tolerates unknown keys in the LLM response. LLMs
occasionally append explanatory keys, and rejecting them would force
unnecessary safe-confirm fallbacks. The action + new_label invariants
are still enforced strictly.

### Cost tracking

A single `log.info("llm_judge.completed", extra={...})` is emitted
inside `llm_judge.run_judge` on the success path. The structured
fields:

```
model               = "gpt-4o-mini"
action              = "confirm" | "downgrade" | "flag_for_review"
input_tokens        = int
output_tokens       = int
estimated_cost_usd  = float | null  (null when model not in LLM_COST_PER_1K)
latency_ms          = int
provider            = "openai" | "openai_stub"
fell_back           = bool
```

Pricing dict (top of `llm_judge.py`):

```python
LLM_COST_PER_1K = {
    "gpt-4o-mini": {"input": 0.000150, "output": 0.000600},
}
```

`OPENAI_API_KEY` is never logged. The exception path in `main.py`
emits `log.warning("llm_judge.failed", extra={"error_type": <name>})`
— the exception **type** only, not the full message.

### Pin impact on `EXPECTED_TOTAL_LOG_CALLS`

`tests/test_log_level_reclassification.py` pin bumped from `265` →
`266`. Only the new `main.py` warning counts; the `llm_judge.py`
INFO emission does not, because `llm_judge.py` is not in
`MIGRATED_FILES`.

### Default behaviour without the flag

With `LLM_JUDGE_ENABLED` unset (the default), the entire judge block
is skipped — `debug_summary["llm_judge"]` is set to `None`, no
log emissions, no API calls, and the pipeline output is byte-identical
to pre-M13.1b. This is the rollback path.

### Operator activation

See `docs/LLM_JUDGE_ACTIVATION_RUNBOOK.md` for the Render dashboard
steps.

### Cost guardrails

When real providers are wired:

- The per-call cap is the `max_tokens=800` ceiling in
  `build_judge_request` (kept tight on purpose — the judge reads
  summaries, not full articles).
- `gpt-4o-mini` at `~$0.0002–$0.0005` per call; daily budget is
  whatever the operator's OpenAI account allows.
- No prompt caching in M13.1b; prompts are short enough that caching
  isn't load-bearing yet.
- Failure routes to `_safe_confirm_fallback` — never blocks a verdict.

### Rollback

Set `LLM_JUDGE_ENABLED=false` (or unset) on both Render services and
restart. The pipeline reverts to byte-identical pre-M13.1b behaviour
on the next call.

## What happens in M13.1c

M13.1c will:

1. Replace `StubAnthropicProvider` with a real implementation
   (Claude as primary; OpenAI as fallback).
2. Add Anthropic-specific cost rates to `LLM_COST_PER_1K`.
3. Persist judge outputs in a new DB table with full audit trail.
4. Surface judge actions in the reviewer UI.

M13.1c will not happen until M13.1b's production cost / latency /
correctness data is reviewed.
