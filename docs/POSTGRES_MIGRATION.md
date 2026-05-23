# Postgres Migration Plan

## Overall plan (4 sub-phases)

| Phase  | What it does                              | Status        |
|--------|-------------------------------------------|---------------|
| M12.0a | Add dual-write foundation, SQLite=truth   | this PR       |
| M12.0b | Backfill existing rows to Postgres        | next          |
| M12.0c | Switch reads to Postgres, SQLite=backup   | future        |
| M12.0d | Retire SQLite                             | future        |

Each sub-phase is independently shippable and revertable via env var.

## M12.0a — what this PR adds

- `postgres_storage.py` — top-level module with `mirror_write`,
  `mirror_upsert`, `health_check`, `ensure_schema`, and lazy
  `get_engine`. All helpers swallow Postgres failures and return
  `False`; none raise.
- Postgres schema definitions matching every SQLite table 1:1.
- Dual-write hooks inside the `save_*` / `create_*` / `record_*`
  functions in `database.py`. The mirror call is always **after** the
  SQLite write succeeds.
- `scripts/check_postgres_health.py` — read-only diagnostic CLI.
- `USE_POSTGRES_WRITE` env var (default `false`).
- `tests/test_postgres_storage.py` — feature-flag, lazy-init, safety,
  schema-parity, and SQLite-isolation tests. Uses
  `sqlite:///<tmp>` as a SQLAlchemy substitute for integration
  coverage; **no real Postgres server is required to run the tests**.
- This document.

## What stays the same

- SQLite is the source of truth. All reads come from SQLite (every
  `database.get_*` and `_row_to_*` helper is unchanged).
- `analyze_pipeline` behaviour, verdict logic, and the npm regression
  test are byte-identical to the previous milestone.
- Render env vars are NOT modified — Render continues running with
  SQLite only.
- `verification_card._verdict_label`, `policy_decision`,
  `policy_scoring`, and `verdict_producer_comparison` are all
  untouched.

## Coexistence with the M1 dual-write (`db/postgres.py`)

A normalised-schema dual-write to `stories`/`claims`/`verdicts`/
`audit_log` already exists at `db/postgres.py`, wired into
`api_server.py` and `job_manager.py`. M12.0a's mirror dual-write at
`postgres_storage.py` runs alongside it without interaction:

- Both gate on `USE_POSTGRES_WRITE=true`. Setting the flag enables
  both code paths in parallel.
- The two paths write to disjoint table sets — `stories`/`claims`/
  `verdicts`/`audit_log` (M1) vs. the 10 mirror tables here. No row
  collides.
- Either path failing leaves SQLite intact; both swallow internally.

The M1 path stays in place because the API server already depends on
it. M12.0b / M12.0c will choose which schema becomes canonical when
read switchover happens; until then, both are best-effort shadow
storage.

## How to enable dual-write locally for testing

1. Provision a Postgres database (Neon, Render Postgres, etc.).
2. Set env vars in the shell that runs `python main.py` /
   `python api_server.py`:

   ```
   USE_POSTGRES_WRITE=true
   DATABASE_URL=postgresql+psycopg://user:pass@host:port/dbname
   ```

3. Run the diagnostic:

   ```
   python scripts/check_postgres_health.py
   ```

   Expect `dual_write_enabled: True`, `can_connect: True`, and a
   "ENABLED. Postgres is being mirrored alongside SQLite." line.

4. Optionally create the mirror tables idempotently:

   ```
   python scripts/check_postgres_health.py --ensure-schema
   ```

5. Run a small analysis to trigger dual-writes via the normal pipeline.
   Inspect the mirror tables to verify rows are appearing alongside the
   SQLite rows.

**DO NOT enable on Render in M12.0a.** The Render-side env var change
is a separate operator decision, taken only after local testing is
complete and the M12.0b backfill plan is ready.

## Failure mode

If Postgres is unreachable, slow, or returns errors:

- `mirror_write` / `mirror_upsert` log a WARNING and return `False`.
- The SQLite write completes normally and returns its usual value.
- `save_analysis_result` / `save_fetch_artifact` / etc. all return
  their SQLite row id (or `{"saved": True, ...}` payload) regardless of
  the mirror outcome.
- `analyze_pipeline` continues without interruption.
- There is no user-facing impact.

This is by design. SQLite is the source of truth.

## What we cannot do yet

- Cannot read from Postgres (M12.0c).
- Cannot rely on Postgres for analysis history (until M12.0c).
- Cannot retire SQLite (until M12.0d).
- Cannot serve queries from Postgres on Render (until all sub-phases
  complete).

## Schema source of truth

- `postgres_storage.py` defines the Postgres mirror schema as
  `_metadata` (10 SQLAlchemy `Table` objects).
- `database.py` defines the SQLite schema via `_ensure_*_table`
  helpers.

Until M12.0d, both must be kept in sync **manually**: any future
SQLite schema change (new column, new table) requires a matching update
to `postgres_storage.py`. The `SchemaParityTests` in
`tests/test_postgres_storage.py` enforce column-name parity for every
mirrored table on every CI run, so a drift will surface as a test
failure before it reaches Render.

A future milestone may add an automated `schema-drift` check that
compares the SQLite `PRAGMA table_info` output against the SQLAlchemy
metadata at boot time and refuses to start dual-write when they
diverge. M12.0a relies on the test-time parity check.

## Rollback

Set `USE_POSTGRES_WRITE=false` (or unset it). The application reverts
to SQLite-only behaviour immediately on the next call. There is no
data loss — SQLite still holds the canonical copy. Restarting the
process is not required, but recommended to dispose any cached engine
state (the dual-write helpers re-check the env var on every call so
the next write attempt will respect the new value even without a
restart).

## Validation

The new infrastructure is exercised by:

- `python scripts/check_postgres_health.py --help`
- `python scripts/check_postgres_health.py`
- `python scripts/check_postgres_health.py --json`
- `python tests/test_postgres_storage.py`
- `python scripts/validate.py`
- `python scripts/run_operational_checks.py --profile postgres-dual-write`
- `npm test` (regression — must remain byte-identical)

All of these run offline. No real Postgres is required for the
validation suite to pass.

## M12.0b — Backfill Script (this PR)

M12.0b adds `postgres_backfill.py` and `scripts/run_postgres_backfill.py`
to copy existing SQLite rows into the M12.0a mirror tables. The
backfill is:

- Idempotent (re-running is safe)
- Per-table selectable (`--table <name>`)
- Dry-run by default
- SQLite-read-only (never modifies the source)
- Bounded by `--limit <N>` as an operator safety cap

### When to run backfill

After a Postgres instance is provisioned and `DATABASE_URL` +
`USE_POSTGRES_WRITE=true` are set. Backfill is a **manual operator
step**, not automatic. M12.0b does not enable dual-write on Render and
does not run backfill on Render — the operator decides when both
events happen.

### Order of operations

1. Provision Postgres (Render Postgres, Neon, etc.).
2. Set env vars in your local shell first:

   ```
   USE_POSTGRES_WRITE=true
   DATABASE_URL=postgresql+psycopg://user:pass@host:port/dbname
   ```

3. `python scripts/check_postgres_health.py` — confirm connectivity.
4. `python scripts/run_postgres_backfill.py --status` — check counts.
5. `python scripts/run_postgres_backfill.py --dry-run` — preview.
6. `python scripts/run_postgres_backfill.py --execute --yes` — run.

Only after local backfill succeeds should the Render env vars be set.

### Idempotency strategies

| Table                            | Strategy             | Conflict columns                       |
|----------------------------------|----------------------|----------------------------------------|
| analysis_results                 | skip_existing_id     | -                                      |
| jobs                             | skip_existing_unique | id (text PK, not autoincrement)        |
| embedding_cache                  | upsert_by_columns    | text_hash, provider, model             |
| review_tasks                     | upsert_by_columns    | idempotency_key                        |
| review_decisions                 | skip_existing_unique | decision_id (text PK)                  |
| source_fetch_artifacts           | skip_existing_id     | -                                      |
| artifact_text_extractions        | skip_existing_id     | -                                      |
| artifact_evidence_candidates     | skip_existing_id     | -                                      |
| verdict_producer_comparisons     | upsert_by_columns    | input_hash                             |
| verdict_label_attributions       | upsert_by_columns    | analysis_id                            |

Note: `jobs` and `review_decisions` use TEXT primary keys (not the
integer autoincrement that the `skip_existing_id` strategy targets),
so they use `skip_existing_unique` against the PK column for the same
"already there?" semantics.

### Coexistence with M1 dual-write

M1 (`db/postgres.py`) writes to a separate normalised schema
(`stories`, `claims`, `verdicts`, `audit_log`). M12.0b backfill does
**NOT** touch the M1 tables. A separate M1 backfill would be a
different milestone if/when needed. M12.0b imports nothing from
`db.postgres` and the static-source tests in
`tests/test_postgres_backfill.py` pin that contract.

### Failure modes

- Postgres unreachable → backfill exits with error 1, SQLite untouched.
- One bad row → logged, counted in `rows_errored`, the loop continues.
- Re-run safe → idempotency via primary key or UNIQUE constraint.
- Backfill never modifies SQLite under any circumstance (pinned by
  `test_backfill_never_modifies_sqlite`).

### Validation (M12.0b)

The new infrastructure is exercised by:

- `python scripts/run_postgres_backfill.py --help`
- `python scripts/run_postgres_backfill.py --status`
- `python tests/test_postgres_backfill.py`
- `python scripts/validate.py`
- `python scripts/run_operational_checks.py --profile postgres-backfill`
- `npm test` (regression — still byte-identical)

All offline. No real Postgres is required for the validation suite to
pass. Actual backfill execution is gated on operator intent
(`--execute --yes`) and a provisioned `DATABASE_URL`.

## CI integration (M13.0)

M13.0 added GitHub Actions CI that runs the full Python test suite +
npm regression on every PR. The Postgres dual-write tests (M12.0a/b)
run in CI using `sqlite://` SQLAlchemy substrate — no real Postgres
is required or used. When M12.0c provisions a real Render Postgres,
a separate opt-in integration job may be added to run against a test
Postgres instance. For now CI is fully offline. See
`docs/CI_OVERVIEW.md` for the env-var safety contract.
