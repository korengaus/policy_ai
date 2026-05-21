# Operational Automation Runner (Phase 2 M7.5)

A single CLI that bundles the standard post-change and canary-monitoring
checks so the operator stops typing the same five commands after every
milestone. The runner only orchestrates existing scripts
(`scripts/validate.py`, `scripts/smoke_async_job.py`,
`scripts/smoke_semantic_canary.py`,
`scripts/build_historical_claim_batch.py`,
`scripts/evaluate_real_claim_batch.py`); it makes no production
decisions and never modifies Render env.

## A. Purpose

- Reduce manual validation / smoke / canary repetition.
- Produce a consolidated JSON + Markdown report per run with parsed
  pass / warn / fail status per step.
- Keep the door open for future specialized AI agents (testing,
  deployment, monitoring, reviewer) to consume these reports — without
  building autonomous code-editing agents yet.

## B. What this does NOT do

- **Not autonomous coding.** It runs existing scripts; it does not edit
  source files.
- **Not multi-agent orchestration.** Single-process, single-CLI.
- **Not a Render env modifier.** Activating / rolling back the semantic
  canary still requires manual operator action in the Render dashboard.
- **Not Celery / Redis / pgvector / Qdrant.** Pure stdlib + existing
  scripts.
- **Not a verdict change.** `policy_decision`, `policy_scoring`, and
  `verification_card` are never imported. Pinned by
  `tests/test_operational_checks_runner.py::VerdictIsolationTests`.

## C. Profiles

| profile | what it runs | hits Render? | may trigger OpenAI? | typical use |
| --- | --- | --- | --- | --- |
| `quick` | `validate.py` | No | No | pre-commit local check |
| `post-commit` | `validate` + legacy Render smoke | Yes (smoke) | No (smoke script never calls OpenAI; Render server may if semantic is on) | after `git push` |
| `render-baseline` | legacy smoke + semantic canary (no `expect-enabled`) | Yes | Indirectly via Render if semantic on | inspect current Render semantic state |
| `render-canary` | semantic canary with `--expect-semantic-enabled --expect-provider openai --fail-on-semantic-unavailable` + legacy smoke | Yes | **Yes — Render will issue OpenAI requests** | monitor active semantic canary |
| `historical` | historical builder dry-run + deterministic eval (if file exists) | No | No | check builder output / regenerate batch evaluation |
| `review-local` (M8.3) | offline reviewer-workflow smoke — `scripts/smoke_review_workflow.py --self-contained` | No | No | exercise M8.0–M8.2 reviewer surface against a temp SQLite DB with a dummy in-process token |
| `review-exposure` (M8.8) | no-token public-exposure smoke — `scripts/smoke_review_api_exposure.py --expect-disabled` | Yes | No | verify `/review/*` is disabled-or-token-gated on a deploy after reviewer/admin UI or review API changes |
| `full` | `validate` + `render-canary` + `historical` | Yes | Indirectly via Render | nightly / weekly comprehensive check |

## D. Common usage

Before commit (offline, fast):

```
python scripts/run_operational_checks.py --profile quick
```

After push / Render redeploy (legacy smoke):

```
python scripts/run_operational_checks.py --profile post-commit \
  --base-url https://policy-ai-q5ax.onrender.com
```

Inspect current Render semantic state (baseline):

```
python scripts/run_operational_checks.py --profile render-baseline \
  --base-url https://policy-ai-q5ax.onrender.com
```

Monitor active Render semantic canary (primary + secondary query):

```
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query
```

Historical dry-run + deterministic eval (no network):

```
python scripts/run_operational_checks.py --profile historical
```

Reviewer-workflow smoke (offline, no Render, no OpenAI, dummy
in-process token only):

```
python scripts/run_operational_checks.py --profile review-local
```

Pass/warn/fail interpretation for `review-local`:

| smoke result | runner status | meaning |
| --- | --- | --- |
| every sub-check `passed=true`, exit 0 | `pass` | M8.0–M8.2 + M9.0 + M9.1 review surface intact: disabled-by-default, token gate, from-result, idempotency, list/detail, every allowed decision, verdict isolation, the absent publication path, the M9.0 decision audit trail (transition + decision_source + audit_version + audit_record), **and the M9.1 internal reviewer audit-packet endpoint (`GET /review/tasks/{id}/audit-packet`) with its disabled / 404 / shape / safety-contract / token-leak checks** are all working. |
| any sub-check `passed=false`, exit 1 | `fail` | At least one reviewer-workflow contract regressed. The runner summary names the failing sub-check; inspect the smoke's JSON tail in the report. Do **not** roll forward until the failing sub-check is restored. |
| CLI misuse (e.g. `--self-contained` missing), exit 2 | `fail` | Treat as a hard fail; the smoke did not run any contract check. |

`review-local` is fully local/offline: it does **not** call OpenAI, does
**not** call Render, does **not** require `REVIEW_API_TOKEN` from the
operator, and does **not** modify Render env / `render.yaml`.

Full check (validate + canary + historical):

```
python scripts/run_operational_checks.py --profile full \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query
```

Dry-run (print commands, write a dry-run report, execute nothing):

```
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query --dry-run
```

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--base-url` | `https://policy-ai-q5ax.onrender.com` | Local or Render endpoint |
| `--query` | `전세사기` | Primary query |
| `--secondary-query` | `청년 월세` | Used with `--include-secondary-query` |
| `--max-news` | `1` | Smoke parameter |
| `--timeout-seconds` | `300` | Per-job timeout for smoke calls |
| `--poll-interval` | `2` | Smoke poll interval |
| `--skip-validate` / `--skip-render` / `--skip-semantic-canary` / `--skip-historical` | off | Drop steps without changing profile |
| `--include-secondary-query` | off | Run canary smoke twice per profile |
| `--json-out` / `--markdown-out` | auto-timestamped under `reports/` | Override default report paths |
| `--no-default-reports` | off | Suppress auto reports; explicit `--*-out` still honored |
| `--fail-on-warn` | off | Exit code 2 when any step returns `warn` |
| `--dry-run` | off | Print commands + write dry-run report; execute nothing |
| `--no-openai-note` | off | Suppress the render-canary OpenAI note |

Exit codes:

| code | meaning |
| --- | --- |
| 0 | all steps passed (or warn-only when `--fail-on-warn` not set) |
| 1 | at least one step failed (run stopped at first fail) |
| 2 | at least one step warned and `--fail-on-warn` was set |
| 130 | operator interrupt (Ctrl-C) |

## E. Safety

- **May hit Render** for profiles that include smoke / canary. The
  `--base-url` flag controls the target — point it at a local
  uvicorn for offline testing if Render shouldn't be touched.
- **`render-canary` may indirectly trigger OpenAI** if Render's
  `SEMANTIC_MATCHING_ENABLED` is currently `true`. The script never
  calls OpenAI itself; the Render service does, server-side. A note
  prints before the run unless `--no-openai-note` is set.
- **Never prints the API key.** The runner never reads
  `OPENAI_API_KEY` from the environment and never writes it to disk.
- **Never modifies Render env.** Activation / rollback stays with the
  operator in the Render dashboard.
- **Generated reports are gitignored.** `reports/operational_check_*.json`
  and `.md` live under `reports/` which is in `.gitignore` at line 5.
  Do not commit them. Reports older than a few days can be deleted
  freely.

## F. Interpretation

**`pass`** when every step passes. Semantic canary steps report
`provider_errors=0`, `overstrong_like=0`, semantic available where
expected, and the legacy async smoke also passes.

**`warn`** when one or more steps return `warn`. Common causes:

- Cold-start runtime on Render's first OpenAI call pushes
  `runtime_p95` above the 1500 ms threshold.
- Small-sample math drives `cap_ratio` above 0.70 on n=1 to n=3 claim
  payloads (the historical 100-case run sat at 0.0).
- Low cache hit rate on a fresh embedding cache.

These are usually self-correcting after a few canary runs as caches
warm. If the warn pattern persists across multiple runs, that's a real
operational signal worth investigating.

**`fail`** / consider rollback when:

- Any step exits non-zero.
- Semantic was expected enabled but unavailable
  (`smoke_semantic_canary` exit 2).
- Provider errors appear (server-side OpenAI failures).
- `overstrong_like_count > 0` (the M6.5-style failure mode — a
  critical mismatch was detected but the support label remained
  strong).
- The job timed out.
- The result endpoint returned an unusable payload.
- The legacy `smoke_async_job.py` regressed against the same
  semantic-enabled server.

Rollback path (operator action in Render dashboard):

```
SEMANTIC_MATCHING_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

Then re-run `--profile post-commit` to confirm the legacy verdict path
is unchanged.

## F'. Semantic canary safety classification (M8.4)

Render canary runs historically returned `overall_status=warn` whenever
`runtime_p95_ms` exceeded the 1500 ms warn threshold, even when every
semantic safety metric was clean. That made `warn` ambiguous — was it
"runtime is slow today" or "the canary detected a semantic safety
regression"? M8.4 splits those signals so the report answers that
question without re-reading the raw scorecard.

The canary step's `metrics` block now carries:

| field | values | meaning |
| --- | --- | --- |
| `semantic_safety_status` | `pass` / `warn` / `fail` | Looks **only** at provider errors, overstrong_like detection, and semantic availability. Never at runtime / cap_ratio. |
| `semantic_runtime_status` | `pass` / `warn` | Tripped by `runtime_p95_ms > 1500` or `cap_ratio > 0.70`. Never `fail` — runtime is a soft signal. |
| `rollback_recommended` | `true` / `false` | `true` only when at least one hard safety signal fires. |
| `rollback_reasons` | list of strings | One entry per hard safety signal that fired (provider errors, overstrong_like, semantic configured-but-unavailable, smoke exit 1/2). |
| `warn_only_reasons` | list of strings | Soft signals that warrant attention but **not** rollback (runtime, cap_ratio). |
| `semantic_safety_summary` | short string | Single-line human-readable digest for reports. |

These fields are surfaced in three places:

1. The per-step `summary` string appends
   `semantic_safety_status=...`, `semantic_runtime_status=...`, and
   `rollback_recommended=...`.
2. The JSON report stores the full lists under
   `commands[i].metrics`.
3. The runner's per-run `next_actions` block prints specific
   guidance — either the rollback recipe (with reasons) or the
   "runtime-only warn — no rollback recommended" message.

### Classification rules

The classifier is deterministic. Same canary scorecard always produces
the same classification.

- `rollback_recommended=true` (and therefore `semantic_safety_status=fail`)
  when **any** of:
  - `provider_errors > 0`
  - `overstrong_like > 0`
  - smoke exit code 2 (semantic expected enabled but unavailable, with
    `--fail-on-semantic-unavailable`)
  - `semantic_enabled=1` and `semantic_available=0` in the scorecard
  - smoke exit code 1 (script / server / result-shape failure)
- `semantic_safety_status=pass` when `provider_errors=0` and
  `overstrong_like=0` and no rollback trigger fires.
- `semantic_runtime_status=warn` when `runtime_p95_ms > 1500` or
  `cap_ratio > 0.70`. Runtime warnings alone do **not** force a
  rollback.
- A scorecard with `health=warn` but a hard safety trigger (e.g.
  `semantic_enabled=1` + `semantic_available=0`) is **promoted** to
  step status `fail` and `rollback_recommended=true`. The classifier
  is intentionally more conservative than the smoke's `health` value
  here.

### Reading the runner output

- `provider_errors=0` and `overstrong_like=0` with `semantic_available=1`
  means semantic safety is clean. Even when overall status is `warn`,
  the operator should look at `semantic_safety_status` first — if it
  is `pass`, the warn is runtime-only and no rollback is recommended.
- A runtime-only warn alone is **not** a rollback reason. Re-run after
  a few minutes; warm-cache effects typically resolve it.
- The runner's printed `next_actions` block tells the operator whether
  a rollback is recommended. If it says "no rollback recommended",
  trust it — the classifier already inspected every hard safety signal.

### Exact render-canary command

```
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query
```

This is the command the operator runs after a Render redeploy. The
runner never modifies Render env; rollback is still a manual operator
action in the Render dashboard.

## F''. Operator preflight (M8.5)

`scripts/operator_preflight.py` is a small local-only operator helper that
reads `git status --porcelain` and recommends a precise `git add` command.
It **never** stages, commits, pushes, or modifies any file. It never calls
external services, never reads `OPENAI_API_KEY`, and never modifies Render
env.

The point is to avoid `git add .` and the ".claude/settings.local.json
slipped into a commit" failure mode that the operator hit during M8.0–
M8.4. The script:

- shows the current change set, split into changed / untracked / excluded
  local-only;
- accepts an `--expected <paths...>` whitelist and tells the operator
  whether every expected file is actually changed, which files are
  unexpectedly modified, and which dangerous files are present;
- prints a recommended `git add` command listing **only** the safe
  expected files;
- can emit JSON for tooling or a compact ChatGPT review summary on
  request;
- refuses to mark the change set as `commit_ready` when a dangerous file
  (e.g. `.claude/settings.local.json`) was explicitly listed in
  `--expected`.

### Exact commands

Show the current change summary:

```
python scripts/operator_preflight.py
```

Recommend a `git add` command for an intended file list:

```
python scripts/operator_preflight.py --expected web/index.html docs/REVIEW_WORKFLOW.md
```

Short ChatGPT-pasteable summary:

```
python scripts/operator_preflight.py --expected web/index.html docs/REVIEW_WORKFLOW.md --chatgpt-summary
```

JSON form (stable keys, no secrets):

```
python scripts/operator_preflight.py --expected web/index.html docs/REVIEW_WORKFLOW.md --json
```

### What it never does

- Never calls `git add`, `git commit`, `git push`, `git reset`, or
  `git checkout` — the only subprocess invocation is a read-only
  `git status --porcelain`. Verified statically and at runtime by
  `tests/test_operator_preflight.py::ScriptSafetyTests`.
- Never replaces operator review. The recommended command is a
  suggestion; the operator still inspects the diff before running it.
- Never reads or stores any API key. Never modifies Render env.
- Never calls OpenAI / network / Render.

### Always-excluded patterns

The script treats these as dangerous and always excludes them from the
recommended `git add`:

| pattern | reason |
| --- | --- |
| `.claude/settings.local.json` | per-operator local config; never staged |
| `reports/` and `reports/operational_check_*.{json,md}` | gitignored generated artifacts |
| `.env`, `.env.*` (anywhere) | secrets |
| `node_modules/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `coverage/`, `.coverage` | caches |
| `dist/`, `build/` | build outputs |
| `*.pyc` | bytecode |
| Root-level `operational_check_*.json` / `.md` | legacy report names |

If one of these is present in the working copy, it is moved to
`excluded_local_only_files` with a warning — `commit_ready` is **not**
forced to false by mere presence. If one of these is passed via
`--expected`, it moves to `forbidden_files_present` and `commit_ready`
is forced to false.

## F'''. Review bundle helper (M8.6)

`scripts/build_review_bundle.py` is a local-only operator helper that
turns the current uncommitted change set into a compact,
ChatGPT-friendly text bundle. It builds on top of
`scripts/operator_preflight.py`: it reuses the same forbidden /
excluded classification, the same recommended `git add` command, and
adds a header (project, timestamp, latest commit, mode), an optional
length-capped diff section, a copy-paste ChatGPT block, and a fixed
safety reminder.

**It never stages, commits, pushes, modifies git state, modifies Render
env, calls OpenAI, or hits any network.** The only subprocess
invocations it makes are read-only git commands:

- `git status --porcelain` (via `operator_preflight.run_git_status`)
- `git log --oneline -1`
- `git diff --no-color HEAD -- <safe expected path>` (only when
  `--include-diff` is passed, and only for paths classified as
  safe-expected)

This tool is **developer / operator tooling only.** It is not a
product feature. It complements — not replaces — operator review.

### Exact commands

Default mode (writes `reports/review_bundle_<ts>.txt`, prints the
path):

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md
```

With a milestone label (appears in the header and the ChatGPT block):

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md --milestone "Phase 2 M8.6"
```

ChatGPT-pasteable summary only (printed to stdout, no file written):

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md --chatgpt-summary
```

Full bundle to stdout (no file written):

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md --stdout
```

Stable JSON (no file written; useful for tooling):

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md --json
```

Include a length-capped diff for safe expected files only:

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md --include-diff
```

Add manual test result notes (the helper does **not** run these
commands — you paste in the result you already observed):

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md \
  --test-note "python scripts/validate.py -> PASS" \
  --test-note "python scripts/run_operational_checks.py --profile quick -> PASS"
```

### Default output location

The default file is `reports/review_bundle_<timestamp>.txt`. The
`reports/` directory is gitignored (`.gitignore` line 5) and must
**never** be committed. The helper itself refuses to ever include
`reports/...` files in its own recommended `git add` command, and the
`review_bundle_*.txt` filename pattern is treated as always-excluded
even outside `reports/`.

### CLI flags

| flag | default | purpose |
| --- | --- | --- |
| `--expected <paths...>` | none | Whitelist of files the operator intends to stage |
| `--milestone <label>` | none | Optional milestone label for header + ChatGPT block |
| `--test-note <text>` (repeatable) | none | Manually-supplied test result strings |
| `--include-diff` | off | Append a length-capped diff section (safe expected files only) |
| `--max-diff-chars <n>` | 30000 | Truncation cap for the combined diff section |
| `--stdout` | off | Print the full bundle to stdout; do not write a file |
| `--json` | off | Print stable JSON to stdout; do not write a file |
| `--chatgpt-summary` | off | Print only the ChatGPT block to stdout; do not write a file |
| `--out <path>` | `reports/review_bundle_<ts>.txt` | Custom output path; must end `.txt` and live under `reports/` |
| `--repo-root <path>` | script's parent | Override repository root (mostly for tests) |

`--stdout`, `--json`, and `--chatgpt-summary` are mutually exclusive.

### JSON payload

Stable keys (always present, in alphabetical order):

```
commit_ready, errors, excluded_local_only_files,
expected_changed_files, expected_files, expected_missing_files,
forbidden_files_present, latest_commit, milestone, output_path,
passed, recommended_git_add_command, test_notes,
unexpected_changed_files, warnings
```

No secret-like values (`sk-`, `OPENAI_API_KEY`, `REVIEW_API_TOKEN`)
ever appear in the JSON output. Pinned by
`tests/test_review_bundle.py::JSONOutputTests`.

### Diff handling (`--include-diff`)

- Diffs are gathered only for files in `expected_changed_files` that
  are **not** classified as forbidden by the bundle helper's superset
  check (preflight + the `review_bundle_*.txt` pattern). Forbidden
  expected files appear in the diff section's "skipped" list with no
  content shown.
- Diff content is concatenated and capped at `--max-diff-chars`
  (default 30000); when truncation happens, a clear
  `[truncated at N characters]` marker is appended.
- The helper never reads or includes diff for
  `.claude/settings.local.json`, `reports/`, `.env`, `.env.*`, build
  caches, or any other excluded pattern.

### Always-excluded patterns

Inherited from `scripts/operator_preflight.py` (§F'' above) and
additionally:

| pattern | reason |
| --- | --- |
| `review_bundle_*.txt` | the helper's own gitignored output |

If `.claude/settings.local.json` or `reports/...` are modified locally,
they appear under `excluded_local_only_files` with a warning; their
presence alone does not block `commit_ready`. If any always-excluded
path is explicitly passed via `--expected`, it is promoted to
`forbidden_files_present` and `commit_ready` is forced to `False`.

### When to run it

Use the helper **after Claude implementation finishes and before** you
manually run `git add` / `git commit`. The typical loop:

1. Claude implements a milestone.
2. Operator runs `python scripts/validate.py` and any milestone-specific
   smoke commands.
3. Operator runs `python scripts/build_review_bundle.py --expected ...`
   with the intended file list.
4. Operator pastes the ChatGPT block into ChatGPT for review.
5. Only after ChatGPT and the operator agree, the operator runs the
   recommended `git add` (never `git add .`) and `git commit`.

The bundle does **not** replace operator judgment. It packages the
review-ready facts so the operator and ChatGPT can both look at the
same artifacts.

### Validation

```
python tests/test_review_bundle.py
python scripts/validate.py
python scripts/run_operational_checks.py --profile quick
```

`tests/test_review_bundle.py` is invoked from `scripts/validate.py`,
so the `quick` profile covers it. None of these commands call
OpenAI, hit Render, or modify any external state.

## F''''. Review API public-exposure smoke profile (M8.8)

The `review-exposure` profile wraps `scripts/smoke_review_api_exposure.py`
so the operator can run a no-token, no-secret public-exposure check
against any deploy from the same single CLI used for the other
profiles.

### Exact command

```
python scripts/run_operational_checks.py --profile review-exposure \
  --base-url https://policy-ai-q5ax.onrender.com
```

Internally this resolves to one step:

```
python scripts/smoke_review_api_exposure.py \
  --base-url https://policy-ai-q5ax.onrender.com \
  --expect-disabled \
  --timeout-seconds 300
```

The smoke's `expect-disabled` mode matches current Render policy
(review API disabled by default). The runner classifies any
`public_access_detected=true` as a hard fail and surfaces a specific
rollback hint in `next_actions` ahead of every other recommendation.

### What this profile does NOT do

- **Does not call OpenAI.** The smoke is pure stdlib `urllib` and
  never touches OpenAI or any other external service.
- **Does not require a `REVIEW_API_TOKEN`.** By design the smoke
  cannot be tricked into sending one — the script doesn't even
  accept a token flag.
- **Does not modify Render env.** No `REVIEW_API_ENABLED` toggling.
- **Does not run any other smoke.** This profile is intentionally
  small so the operator can run it after frontend / review_* changes
  without paying the canary's OpenAI / semantic cost.
- **Is not part of `quick`.** `quick` stays offline + no-Render so
  pre-commit checks remain fast and OpenAI-free. The exposure smoke
  hits Render and belongs in the post-deploy profile family.

### Metrics surfaced in the consolidated report

The runner's `commands[i].metrics` block carries:

| field | meaning |
| --- | --- |
| `public_access_detected` | `true` iff at least one endpoint returned 2xx without a token |
| `disabled_count` | endpoints returning 503 with `disabled` body marker |
| `token_required_count` | endpoints returning 403 |
| `unexpected_count` | endpoints returning anything else (404 / 405 / 500 / network failure / 503 without `disabled` marker) |
| `expectation_mismatch_count` | endpoints whose classification did not match the operator's `--expect-*` mode (still safe from public exposure) |
| `expectation_mode` | `expect-disabled` / `expect-token-required` / `allow-disabled-or-token-required` |
| `recommendation` | short human-readable next step (PASS / MISMATCH / FAIL …) |

### When to run it

After **any** deploy that touched:

- `web/index.html` reviewer/admin UI (M8.1 / M8.2 / M8.7)
- `review_auth.py`, `review_workflow.py`, or the `/review/*`
  endpoints in `api_server.py` (M8.0)
- environment variables that toggle `REVIEW_API_ENABLED` /
  `REVIEW_API_TOKEN` on Render

The smoke is fast (a handful of HTTP requests) and runs without any
secret, so there's no operator-side cost to running it.

### Validation

```
python tests/test_review_api_exposure_smoke.py
python tests/test_operational_checks_runner.py
python scripts/smoke_review_api_exposure.py --base-url http://127.0.0.1:8000 --expect-disabled  # local
```

The local form points at a running uvicorn that has `REVIEW_API_ENABLED`
unset; both endpoints should return 503 and the smoke should pass.

## G. Relationship to future AI agents automation

This is the first automation layer. It produces structured JSON
reports that future specialized agents can consume:

- **testing agent** — interpret `validate` step output, suggest fixes
- **deployment agent** — read `post-commit` reports, gate on `pass`
- **monitoring agent** — chart `render-canary` runtime_p95 / cap_ratio
  over time
- **reviewer agent** — flag `unknown_historical` cases in the
  historical batch that need labeling

The runner deliberately stops at orchestration. **Autonomous
code-editing agents are not in scope** for this milestone — the policy
domain still requires human review for substantive changes, and the
current `claude-code` interactive flow is the right granularity.

## H. Validation

```
python tests/test_operational_checks_runner.py
python scripts/run_operational_checks.py --help
python scripts/run_operational_checks.py --profile quick --no-default-reports
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query --dry-run --no-default-reports
```

CI runs the first one on every push with a fake subprocess runner — no
real Render call, no real OpenAI call. Live `render-canary` runs are
intentionally not part of CI.
