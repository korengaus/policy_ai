# CI Overview (M13.0)

## What runs on every PR and push to main

### `CI` workflow (`.github/workflows/ci.yml`) — BLOCKING

Runs the full validation suite. The workflow's body is intentionally
thin — every test it runs is owned by `scripts/validate.py`, so CI
mirrors local exactly. Three steps:

1. `python scripts/validate.py` — compileall + the full Python test
   suite (35 test files) + npm regression as the final step.
2. `npm test` — re-runs the regression suite as a distinct CI step so
   a regression failure stands out on the dashboard rather than being
   buried inside the validate.py log.
3. `python scripts/run_operational_checks.py --profile quick --no-default-reports`
   — offline operational smoke that also wraps validate.py and adds
   the report-parser layer used by the operator tools.

Total target runtime: under 5 minutes.

A failure of this workflow BLOCKS the PR from merging once branch
protection is enabled (see "Operator setup" below).

### `Lint` workflow (`.github/workflows/lint.yml`) — ADVISORY

Runs `ruff check` on the codebase. Issues are reported as GitHub
annotations but do NOT block the PR (the job carries
`continue-on-error: true`). Operators may promote this to blocking in
a later milestone by removing that flag and updating branch
protection.

### `Security` workflow (`.github/workflows/security.yml`) — ADVISORY

Runs `pip-audit` against `requirements.txt` on every PR/push AND on a
weekly cron (Monday 09:00 UTC). Advisory only.

### `smoke-deployed` job (manual only) — OPT-IN

Lives in the same `CI` workflow but only fires on `workflow_dispatch`
with a non-empty `smoke_base_url` input. Never runs on PR or push.
Runs `scripts/smoke_async_job.py` against the supplied URL — useful
for verifying a Render deploy without leaving the CI dashboard.

## What is explicitly NOT in CI

- Render deployment smoke (`render-baseline` / `render-canary`) — those
  require a live Render instance and OpenAI key; operator-driven
  post-deploy via `scripts/run_operational_checks.py` locally.
- Semantic canary live tests — require OpenAI calls.
- Postgres dual-write tests against a real Postgres — only
  `sqlite://` SQLAlchemy substrate is used in CI (see
  `docs/POSTGRES_MIGRATION.md`).
- Any test that calls OpenAI, makes outbound network requests, or
  relies on Render. Such tests must be marked as integration and
  skipped in CI (see "How to add new tests to CI" below).

## CI environment variables (all set to safe defaults)

| Env var                    | CI value     | Purpose                                                          |
|----------------------------|--------------|------------------------------------------------------------------|
| `CI`                       | `"true"`     | Lets scripts detect CI; informational only.                      |
| `PYTHONUTF8`               | `"1"`        | Forces UTF-8 on stdout/stderr — matches local Windows dev.       |
| `USE_POSTGRES_WRITE`       | `""` (empty) | validate.py's determinism guard requires this to be unset/empty. |
| `DATABASE_URL`             | `""` (empty) | No accidental Postgres connection.                               |
| `OPENAI_API_KEY`           | `""` (empty) | No OpenAI calls in CI.                                           |
| `SEMANTIC_MATCHING_ENABLED`| `"false"`    | Semantic matching off.                                           |
| `EMBEDDING_PROVIDER`       | `"disabled"` | No embedding provider.                                           |
| `REVIEW_API_ENABLED`       | `"false"`    | Review API stays disabled.                                       |

## How to debug a CI failure

1. Open the failed run on GitHub Actions.
2. Read the failing step's output.
3. Reproduce locally:

   ```
   python scripts/validate.py
   npm test
   python scripts/run_operational_checks.py --profile quick
   ```

   If those pass locally but CI fails, check env var differences (the
   table above) and Python / Node version differences (CI pins 3.12
   and Node 20).
4. On failure, the `python-tests` job uploads any
   `reports/operational_check_*.json` files as a
   `operational-reports-<run-id>` artifact for download.

## Operator setup (one-time, in GitHub UI)

To make CI blocking, the operator should:

1. Go to GitHub → Settings → Branches → Branch protection rules.
2. Add rule for `main`:
   - Require status checks to pass before merging.
   - Require branches to be up to date before merging.
   - Required status checks: `CI / Python Tests`.
   - Do NOT require `Lint / Ruff (advisory)` or
     `Security / pip-audit (advisory)` yet — those are advisory in
     M13.0.
3. Save.

This is a manual operator step. The workflow itself does not
configure branch protection.

## How to add new tests to CI

Tests are not added to CI directly. They are added by appending them
to `scripts/validate.py`. Once a test file is in validate.py, CI
picks it up automatically because CI runs validate.py.

## CI must remain offline

Any future change that adds network calls, OpenAI calls, or Postgres
dependencies to a CI-run test will make CI fragile. Such tests must
be marked as integration-only and skipped in the validate.py / CI
path. The pattern is:

```python
import os
import unittest


@unittest.skipUnless(
    os.environ.get("RUN_INTEGRATION_TESTS") == "true",
    "Integration test — skipped in CI",
)
class MyIntegrationTests(unittest.TestCase):
    ...
```

## Cost

GitHub Actions free tier provides 2000 minutes/month for private
repos and unlimited minutes for public repos. The full CI suite
should run in under 5 minutes per push, so 400 pushes/month fit
comfortably in the free tier.

## Coexistence with prior CI (M4)

M13.0 replaces an earlier `ci.yml` (added in Phase 2 M4) that
enumerated ~20 individual test scripts directly inside the workflow
YAML. That design duplicated logic with `scripts/validate.py` and
drifted whenever a new test was added locally but not echoed in CI.
The M13.0 design delegates the test list to `validate.py` so the two
cannot drift. The optional `workflow_dispatch` smoke against a
deployed instance is preserved as the `smoke-deployed` job in the new
workflow.
