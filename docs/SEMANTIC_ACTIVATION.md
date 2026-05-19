# Semantic Activation Prep (Phase 2 M5.5)

Tooling for observing and calibrating the semantic matching layer added in
M5 without changing production behavior.

## A. What this phase is

- Controlled activation prep — measure, do not enable.
- Observation and calibration only.
- No verdict effect, no confidence-label change, no UI redesign.
- A safer on-ramp for a future, intentional canary rollout.

## B. What this phase is not

- Not a production rollout. `SEMANTIC_MATCHING_ENABLED` stays `false` on
  Render and in CI.
- Not a vector database migration. No pgvector, no Qdrant.
- Not an automated verification upgrade. A high semantic score still does
  not mean "verified" — the conservative rule-based labels remain
  authoritative.
- Not a frontend change. Probe output is local stdout / JSON only.

## C. How to run a deterministic local probe

```
python scripts/probe_semantic_matching.py --provider deterministic --show-matches
```

The deterministic provider is the default. It:
- Uses hash-based pseudo-vectors — no network, no API key.
- Loads `tests/fixtures/semantic_activation_cases.json` (3 short synthetic
  Korean cases by default).
- Prints provider status, per-case `best_support_level`, score percent,
  chunk count, runtime, cache hits, and (with `--show-matches`) the top
  match snippets.

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--provider` | `deterministic` | `disabled`, `deterministic`, `openai`, `auto` |
| `--case-file` | `tests/fixtures/semantic_activation_cases.json` | swap fixtures |
| `--max-cases` | `3` | cap the number of evaluated cases |
| `--query` / `--source-text` | – | run a single ad-hoc claim/source pair instead of fixtures |
| `--source-title` / `--source-url` | – | metadata for the ad-hoc source |
| `--show-matches` | off | print top match snippets (truncated) |
| `--json-out` | – | write the full per-case summary as JSON |
| `--no-network` | off | block any live network call; with `--provider openai` reports unavailable cleanly |
| `--fail-on-unavailable` | off | exit code 2 if the resolved provider is unavailable |

## D. How to run an OpenAI probe manually

This is an opt-in path. Only run it when you intentionally want to spend
embedding tokens; it triggers real API calls.

PowerShell:

```powershell
$env:SEMANTIC_MATCHING_ENABLED = "true"
$env:EMBEDDING_PROVIDER = "openai"
$env:EMBEDDING_MODEL = "<currently-supported-embedding-model>"
$env:OPENAI_API_KEY = "<your-key>"
python scripts/probe_semantic_matching.py --provider openai --show-matches --max-cases 3
```

bash/zsh:

```bash
export SEMANTIC_MATCHING_ENABLED=true
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=<currently-supported-embedding-model>
export OPENAI_API_KEY=<your-key>
python scripts/probe_semantic_matching.py --provider openai --show-matches --max-cases 3
```

Pre-flight checklist before the first live OpenAI call:

1. `EMBEDDING_MODEL` is **required**. Missing or empty causes the provider
   to report `available=false` with reason `EMBEDDING_MODEL missing` — no
   default model is assumed (M5.5 fail-closed change).
2. `OPENAI_API_KEY` is **required**. Missing causes `available=false` with
   reason `OPENAI_API_KEY missing`.
3. The probe's first run will populate `embedding_cache` in the local
   `policy_ai.db`. Re-running the same fixtures is free after that.
4. The probe never logs your API key or full source bodies. Each
   embedding call's log line shows only text length, model name, and
   exception type if it fails.

## E. How to disable

Default state. Either drop the env vars or set:

```
SEMANTIC_MATCHING_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

This is the production default on Render. The pipeline still attaches a
`semantic_evidence_summary` to `debug_summary`, but its
`semantic_matching_available` is `false`, `best_support_level` is
`unavailable`, and no embedding call is made.

## F. Render guidance

- **Do not enable semantic embeddings on Render yet.** Production stays on
  the rule-based path until calibration is documented.
- Run local probes first (deterministic + manual OpenAI on a representative
  fixture set).
- When ready to canary, plan to:
  1. Toggle the flags on Render with `max_news=1`.
  2. Monitor `runtime_ms`, `embedding_request_count`, and `cache_hits` in
     `debug_summary.semantic_evidence_summary` for the first day.
  3. Watch cost on the OpenAI dashboard.
  4. Keep `SEMANTIC_MIN_SCORE_FOR_SUPPORT` conservative; never let
     "strong" semantic match alone change a verdict in code.
- If anything looks off, flipping `SEMANTIC_MATCHING_ENABLED=false` on
  Render reverts to the M4-equivalent path immediately — no migration, no
  rollback script.

## G. Safety

- Semantic matching does **not** verify claims. The summary is metadata
  attached to `debug_summary`; no verdict-side module reads it. Pinned by
  `tests/test_semantic_activation.py:VerdictIsolationTests`.
- A `strong` support label is a *semantic* label, not a *verification*
  label. The conservative phrasing ("사람 검토 필요", "의미 매칭 근거 부족",
  "공식 출처 확인 필요") remains the source of truth in the UI and exports.
- The probe and the agent never log:
  - API keys
  - Raw long official body text
  - Full embedding vectors
- Logging at INFO level emits one short structured line per pipeline run
  when matching is enabled and available — provider name, model, support
  level, score percent, chunk count, runtime, request count.

## H. Future path

- Build a labeled calibration set (claim ↔ supporting passage ↔ expected
  semantic strength) and run the probe against it with both providers.
  **M5.6 ships an initial scorecard tool** —
  `scripts/evaluate_semantic_calibration.py` and
  `tests/fixtures/semantic_calibration_cases.json`. See
  `docs/SEMANTIC_CALIBRATION.md`.
- Tune `SEMANTIC_MIN_SCORE_FOR_SUPPORT` / `_FOR_CONTEXT` from the
  calibration distribution.
- Decide whether to surface a small `semantic_evidence_summary` card in
  the UI — only after operators agree the score is trustworthy.
- Migrate the cache to pgvector or Qdrant once volume justifies it (see
  `docs/SEMANTIC_MATCHING.md#future-path-to-pgvector--qdrant`).

## Validation

The activation work is covered by:

```
python tests/test_semantic_activation.py
python scripts/probe_semantic_matching.py --provider deterministic --show-matches --max-cases 3
python scripts/probe_semantic_matching.py --provider openai --no-network --fail-on-unavailable
```

CI runs the first two plus an OpenAI no-network guard on every push. None
of them make a live API call.
