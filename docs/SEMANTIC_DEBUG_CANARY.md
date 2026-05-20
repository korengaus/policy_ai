# Semantic Debug Canary (Phase 2 M7.2)

A monitoring + dry-run plan for **future** Render activation of semantic
matching as debug-only metadata. **This milestone does not enable
anything on Render** — it adds the tooling and runbook that an operator
will need before flipping any env var.

## A. Purpose

- Provide a repeatable smoke flow that runs the live async-job pipeline
  and surfaces semantic-canary metrics (provider availability, runtime
  percentiles, support distributions, cap ratio, overstrong-like
  detection) without changing verdicts.
- Give the operator a clear pass / warn / fail health classification so
  a debug canary is reversible the moment something drifts.
- Document the activation gate, the precise env vars to set (locally
  first, Render last), and the rollback steps.

## B. What this does NOT do

- It does **not** enable production embeddings. Render keeps
  `SEMANTIC_MATCHING_ENABLED=false` / `EMBEDDING_PROVIDER=disabled`
  unless an operator manually changes the Render dashboard.
- It does **not** modify `render.yaml`. The canary tooling is purely
  local + operator-driven.
- It does **not** change verdicts. `policy_decision`, `policy_scoring`,
  and `verification_card` do not import the new modules or read their
  output — pinned by
  `tests/test_semantic_canary_metrics.py::VerdictIsolationTests` and
  `tests/test_smoke_semantic_canary.py::VerdictIsolationTests`.
- It does **not** expose semantic labels to users. Any future UI
  exposure is a separate milestone with its own review.
- It does **not** verify claims. Every report includes the conservative
  disclaimer: *semantic match strength is metadata only*.
- It does **not** call OpenAI itself. If the target app has semantic
  matching enabled server-side, the app may call OpenAI — that is the
  canary the operator is measuring.

## C. Pre-canary gates already satisfied

Cumulative across M5.6 → M7.1:

1. Synthetic calibration fixture (36 cases) passes deterministic + live
   OpenAI with `overstrong_count = 0`. ✓ (M6.0 / M6.1)
2. Synthetic real-claim batch (72 cases) passes deterministic + live
   OpenAI with `overstrong_count = 0` after M6.6 closed the
   `policy_scope_mismatch` gap. ✓ (M6.4 / M6.5 / M6.6)
3. Generated historical batch (100 cases from real local artifacts)
   passes deterministic + live OpenAI with `overstrong_count = 0`,
   `cap_applied=0/100`, `cap_ratio=0.0`, runtime well under budget.
   ✓ (M7.0 / M7.1)
4. Render smoke (`scripts/smoke_async_job.py`) currently passes with
   semantic disabled. ✓
5. M5.7 + M6.6 guardrails proven necessary by M6.5's first overstrong
   failure and the M6.5-rerun verifying the fix. ✓

The remaining gates are operational, not data:

- per-Render-instance latency probing under realistic concurrency
- explicit operator decision on which Render service / env var to flip
- monitoring loop running against the canary

## D. Local canary dry-run

Run the entire pipeline against a local uvicorn instance with semantic
matching turned on. **The API key stays in your shell — never paste it
into chat, commit it, or write it to a file.**

PowerShell — terminal 1 (server):

```powershell
$env:SEMANTIC_MATCHING_ENABLED = "true"
$env:EMBEDDING_PROVIDER = "openai"
$env:EMBEDDING_MODEL = "text-embedding-3-small"
$env:OPENAI_API_KEY = "<your-key>"
python -m uvicorn api_server:app --reload --port 8000
```

PowerShell — terminal 2 (smoke):

```powershell
python scripts/check_semantic_canary_env.py --require-openai
python scripts/smoke_semantic_canary.py `
  --base-url http://127.0.0.1:8000 `
  --query 전세사기 `
  --max-news 1 `
  --expect-semantic-enabled `
  --expect-provider openai `
  --fail-on-semantic-unavailable `
  --fail-on-health-warn `
  --json-out reports/semantic_canary_local.json `
  --markdown-out reports/semantic_canary_local.md
```

Generated reports go under `reports/` (gitignored). Do not commit them.

Exit codes:

| code | meaning |
| --- | --- |
| 0 | smoke clean — canary health is `pass` |
| 1 | HTTP / server / result-shape failure |
| 2 | `--fail-on-semantic-unavailable` triggered (configured but unavailable) |
| 3 | `--fail-on-health-warn` triggered (`warn` or `fail` health) |

## E. Render debug canary plan

**This step requires explicit operator decision. The runbook below is a
template, not an instruction to act.**

Suggested canary scope:

- one Render service only — pick the lowest-traffic environment first
- `max_news=1` for the first smoke
- internal / manual smoke only — do **not** schedule the smoke from any
  user-facing path
- no UI copy change — verdict-side wording stays exactly as it is today
- no `final_decision` / `policy_confidence` change
- no export change

Render env vars to add **only when the operator decides** (Render
dashboard → Environment, NOT `render.yaml`):

```
SEMANTIC_MATCHING_ENABLED=true
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
OPENAI_API_KEY=<set securely in Render dashboard>
```

Render's redeploy after a env-var change is the moment to start the
monitoring loop.

## F. Render smoke after canary

```
python scripts/smoke_semantic_canary.py `
  --base-url https://policy-ai-q5ax.onrender.com `
  --query 전세사기 `
  --max-news 1 `
  --expect-semantic-enabled `
  --expect-provider openai `
  --fail-on-semantic-unavailable `
  --fail-on-health-warn `
  --json-out reports/semantic_canary_render.json `
  --markdown-out reports/semantic_canary_render.md
```

Reports stay local. Do not commit them.

## G. Pass / warn / fail rules

Computed deterministically by
`semantic_canary_metrics.classify_canary_health`:

**`pass`** when:

- semantic is available where expected
- `provider_error_count = 0`
- `overstrong_like_count = 0`
- `runtime_ms_p95 ≤ 1500` ms
- `cap_ratio ≤ 0.70`

**`warn`** when (but does not by itself force rollback):

- `cap_ratio > 0.70` — guardrails carrying high safety load on real
  traffic; investigate input drift
- `runtime_ms_p95 > 1500` ms — verify Render request budget
- semantic configured but unavailable (e.g. key rate-limited or
  revoked)
- many `limitations` entries

**`fail`** / rollback when:

- `provider_error_count > 0`
- `overstrong_like_count > 0` — a critical mismatch was detected but
  the support label remained `strong` (the M6.5-style failure mode)
- job timeout
- result endpoint failure
- old `/analyze` compatibility broken
- UI / export wording changed (this should be impossible because no
  M7.x code touches that path — but smoke verifies)

## H. Rollback

Reverse the env-var change in the Render dashboard:

```
SEMANTIC_MATCHING_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

Then re-run the pre-semantic smoke to confirm the verdict path is
unchanged:

```
python scripts/smoke_async_job.py `
  --base-url https://policy-ai-q5ax.onrender.com `
  --query 전세사기 `
  --max-news 1
```

The canary's monitoring loop should now report semantic disabled.
Capture the rollback time + the failing health signal in the local
postmortem (do not commit the reports).

## I. Monitoring checklist

Per canary run, capture:

- `provider_error_count`
- `runtime_ms_avg` and `runtime_ms_p95`
- `cap_ratio`
- `best_support_distribution`, `raw_support_distribution`
- `risk_flag_counts` — watch for any new flag the synthetic + historical
  evaluations did not surface
- `cache_hits_total` and `embedding_request_count_total` — cache should
  approach 100% hit rate on repeat runs
- `result_count` and `semantic_summary_count` agreement
- old `smoke_async_job.py` continues to pass against the same endpoint
- exports (markdown / text) continue to render the same conservative
  wording (`사람 검토 필요`, `의미 매칭 근거 부족`)

The `--json-out` / `--markdown-out` flags persist these snapshots
locally for postmortem use; treat the reports as ephemeral operational
artifacts, not part of the repo's truth.

## J. Next steps after a clean debug canary

- Keep the canary in debug-only mode for several runs across different
  queries / loads / times of day. Re-run `smoke_semantic_canary.py`
  periodically.
- Regenerate the historical batch (M7.0) every few weeks from the
  growing `reports/` corpus and re-run M7.1-style live OpenAI on it to
  detect drift.
- Compare the canary's `risk_flag_counts` to the historical-batch
  baseline (M7.1 saw zero flags fire on real data). If a new pattern
  appears, extend `semantic_fact_guardrails.py` before any user-facing
  exposure.
- Only after **multiple** clean canary runs across varied workload
  should an operator consider a small debug UI exposure (e.g. a
  reviewer-only "semantic match strength" badge). **Verdict changes
  remain off-limits.**

## K. Validation

```
python tests/test_semantic_canary_metrics.py
python tests/test_smoke_semantic_canary.py
python scripts/smoke_semantic_canary.py --help
python scripts/check_semantic_canary_env.py
```

CI runs the first two on every push. Live canary smoke is intentionally
not part of CI — it requires a running app + (for full coverage) a real
OpenAI key on the server side.
