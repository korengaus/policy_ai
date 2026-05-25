# LLM Judge Activation Runbook (M13.1b)

Operator-facing dashboard runbook for enabling the M13.1b LLM judge
against the live Render deployment.

> **Hard rules — do not violate.**
>
> - `truth_claim` is **always False**. The judge can review a verdict;
>   it cannot make one true.
> - `operator_review_required` is **always True**. Every analysis
>   still goes to human review.
> - The judge **never raises** `policy_alert_level`. Only `confirm`,
>   one-tier `downgrade`, or `flag_for_review`.
> - The judge **never modifies** `verdict_label` (verification_card
>   P3 output stays byte-identical).
> - The Render **dashboard** is the source of truth for service env
>   vars. `render.yaml` is informational only — do not edit it.

---

## 0. Prerequisites

| Need | How to confirm |
|---|---|
| M13.1b merged to main | `git log -1 --oneline` shows the M13.1b activation commit |
| `OPENAI_API_KEY` available | The operator already manages this for the existing `ai_reasoner` LLM caller |
| Render dashboard access (Web + Worker) | https://dashboard.render.com — both `policy-ai-q5ax` (Web) and the Worker service |
| Local `.env` updated (optional, for spot-checks) | `OPENAI_API_KEY=...` already present from prior milestones |

---

## 1. Pre-flight smoke (local)

Before touching Render, confirm the wiring works locally:

```
# Default chain (no flag) → byte-identical pre-M13.1b behavior
python scripts/dry_run_llm_judge.py --provider stub --status

# Real OpenAI chain (key required) — does NOT call the API; just lists the chain
python scripts/dry_run_llm_judge.py --provider openai --status
```

Expected for `--provider openai --status`:

```
Provider chain (priority order):
  1. openai                 -- available: True (M13.1b real OpenAI; requires OPENAI_API_KEY)
```

If `available: False`, fix the key locally before continuing.

---

## 2. Activate on Render — Web service

1. Open the Render dashboard → `policy-ai-q5ax` (Web service) → **Environment**.
2. Confirm `OPENAI_API_KEY` is set. (Already required by the existing
   ai_reasoner caller; the judge re-uses the same key.)
3. Add a new env var:
   - Key: `LLM_JUDGE_ENABLED`
   - Value: `true`
4. **Save** — Render will trigger a restart automatically.
5. Wait for the deploy to go green in the dashboard.

---

## 3. Activate on Render — Worker service

The Worker runs the RQ-backed `/v2/analyze` pipeline path; the judge
must be enabled there too, otherwise async analyses will skip the
judge while sync analyses use it.

1. Open the dashboard → Worker service → **Environment**.
2. Confirm `OPENAI_API_KEY` is set.
3. Add `LLM_JUDGE_ENABLED=true`.
4. **Save** + wait for restart.

---

## 4. Verify activation

### Health endpoints

```
curl https://policy-ai-q5ax.onrender.com/health
```

Should return 200 OK (judge activation does not change the health
endpoint shape).

### Spot-check the pipeline

Run a sync analyze:

```
curl -X POST https://policy-ai-q5ax.onrender.com/analyze \
  -H "Content-Type: application/json" \
  -d '{"query":"전세사기","max_news":1}'
```

In the JSON response, look for:

```json
{
  "results": [
    {
      "debug_summary": {
        "llm_judge": {
          "action": "confirm",
          "model": "gpt-4o-mini",
          "input_tokens": 1247,
          "output_tokens": 89,
          "estimated_cost_usd": 0.000241,
          "latency_ms": 1820,
          "provider": "openai",
          "fell_back": false,
          "applied": false,
          "truth_claim": false,
          "operator_review_required": true
        }
      }
    }
  ]
}
```

Key fields to confirm:

- `provider == "openai"` (NOT `"openai_stub"` — that means the key is missing)
- `model == "gpt-4o-mini"`
- `truth_claim == false`
- `operator_review_required == true`
- `fell_back == false` (true would indicate a JSON-parse or upgrade-attempt fallback)
- `applied == false` for `confirm`; `applied == true` for `downgrade` or `flag_for_review`

Then verify the V2 async path too:

```
curl -X POST https://policy-ai-q5ax.onrender.com/v2/analyze \
  -H "Content-Type: application/json" \
  -d '{"query":"청년 월세","max_news":1}'
```

Follow the SSE stream until the `complete` event and check the
`debug_summary.llm_judge` field in the returned result.

### Render logs

Tail the Web + Worker logs for:

- `llm_judge.completed` — INFO event, one per news item processed
- `llm_judge.failed` — WARNING; investigate if present
- No `OPENAI_API_KEY` strings or `sk-` prefixes (defence-in-depth pin
  asserts no key fragments leak)

---

## 5. Cost monitoring

Per-call cost at `gpt-4o-mini` rates:

- Input: $0.000150 per 1K tokens
- Output: $0.000600 per 1K tokens
- Typical judge call: ~1200 input + ~90 output tokens → ~$0.0002/call

`/analyze` with `max_news=3` triggers 3 parallel judge calls + 3
sequential ai_reasoner calls (unchanged by M13.1b). Combined OpenAI
cost per `/analyze`:

- Judge: ~3 × $0.0002 = $0.0006
- ai_reasoner (existing): ~3 × $0.001–$0.002 (depends on prompt)
- **Total: ~$0.003–$0.007 per /analyze with 3 news items**

Watch the operator's OpenAI usage dashboard for the first ~24h after
activation. Daily ceiling can be configured in the OpenAI dashboard;
the judge's failure mode (rate limit → safe-confirm) is graceful.

---

## 6. Rollback

If anything looks wrong — unexpected cost, latency spike, surprising
downgrades — disable the judge instantly without redeploying code:

1. Render dashboard → Web service → Environment → `LLM_JUDGE_ENABLED` → set to `false` (or delete).
2. Save + restart.
3. Repeat for Worker service.
4. Confirm via spot-check: `debug_summary.llm_judge` should now be
   `null` in the response.

Pipeline output is byte-identical to pre-M13.1b once the flag is off.
There is no data to clean up — the judge never wrote to SQLite or
Postgres in M13.1b.

---

## 7. What to watch for in the first week

| Signal | What it means | Action |
|---|---|---|
| `llm_judge.completed` rate matches `/analyze` rate × `max_news` | Judge running correctly | None |
| `fell_back: true` rate > 5% | LLM frequently returning malformed JSON | Investigate prompt or model |
| Downgrade rate suspicious | Judge being too cautious (low) or too aggressive (high) | Sample 10 cases via dry-run CLI; if wrong, disable |
| `latency_ms` > 5000 consistently | Network or model degradation | Check OpenAI status page |
| `estimated_cost_usd` per day exceeds budget | Volume spike or prompt regression | Disable + audit |
| `llm_judge.failed` warnings | Exceptions reaching the wrapper — pipeline still safe | Investigate exception type |

---

## 8. Cross-references

- `docs/LLM_JUDGE.md` — architecture, schema, safety invariants.
- `llm_judge.py` — `OpenAIProvider`, `LLM_COST_PER_1K`,
  `llm_judge_enabled`, `run_judge`.
- `main.py` — `_apply_judge_to_final_decision`, integration point
  inside `_process_news_item_phase_a`.
- `scripts/dry_run_llm_judge.py` — `--provider {stub,openai}` for
  local probes without affecting production.
- `tests/test_m13_1b_openai_provider.py` — 40 pinned tests covering
  every safety invariant.
