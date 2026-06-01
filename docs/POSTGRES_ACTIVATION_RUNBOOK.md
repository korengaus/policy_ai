# Postgres Dual-Write Activation Runbook

This runbook walks an operator through enabling the M12.0a dual-write
infrastructure against a real, provisioned Postgres instance. It is the
M12.1 deliverable.

> **NOTE (M12.0e-6b-2).** `postgres_backfill.py` /
> `scripts/run_postgres_backfill.py` are RETIRED — the migration is
> complete and SQLite is no longer written. The `run_postgres_backfill`
> steps below are historical; skip them.

The runbook assumes:

- M12.0a (`postgres_storage.py`) and M12.0b (`postgres_backfill.py`)
  are already deployed.
- The operator has shell access to the local repository checkout AND
  permission to set environment variables in that shell.
- SQLite remains the source of truth throughout activation. Postgres
  is a side-channel mirror until M12.0c.

**Hard rules:**

- `truth_claim` is **always False**. Postgres parity does not imply
  Postgres correctness; it only confirms the row sets match.
- `operator_review_required` is **always True**. No step in this
  runbook should be automated end-to-end without operator confirmation.
- The CLIs in this runbook are read-only or explicitly require
  `--execute --yes`. None will write to Postgres or modify SQLite
  without operator intent.

---

## 0. Prerequisites

| Need | How to confirm |
|---|---|
| Postgres URL with `+psycopg` prefix | `echo $DATABASE_URL` (PowerShell: `$env:DATABASE_URL`) starts with `postgresql+psycopg://` |
| Python deps installed | `python -c "import sqlalchemy, psycopg"` exits cleanly |
| SQLite source row counts known | `python scripts/run_postgres_backfill.py --status` lists per-table counts |
| Render is NOT yet dual-write | `USE_POSTGRES_WRITE` is **not** set on Render |

**Driver note (M12.0a):** The dual-write code uses psycopg v3. URLs
must begin with `postgresql+psycopg://`, not `postgresql://` and not
`postgresql+psycopg2://`. The Render "External URL" exposes a raw
`postgres://...` form — rewrite it to the `+psycopg` form when storing
in `.env`.

---

## 1. Set the environment locally

Local shell only — do not modify Render env yet.

PowerShell:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://USER:PASS@HOST:5432/DBNAME"
$env:USE_POSTGRES_WRITE = "true"
```

Bash:

```bash
export DATABASE_URL="postgresql+psycopg://USER:PASS@HOST:5432/DBNAME"
export USE_POSTGRES_WRITE=true
```

A `.env` file at the repo root is acceptable so long as the shell that
runs subsequent commands loads it before invocation. The scripts do not
auto-load `.env` — the operator does.

---

## 2. Confirm connectivity

```
python scripts/check_postgres_health.py
```

Expected output:

```
dual_write_enabled:    True
database_url_present:  True
engine_available:      True
can_connect:           True
tables_defined:        10 tables [...]

Status: dual-write is ENABLED. Postgres is being mirrored alongside SQLite.
```

If `can_connect: False`:

- Verify the URL host / port / credentials.
- Verify the DB host firewall allows your local IP.
- Verify the database actually exists (`psql ${DATABASE_URL}` from a
  separate shell).

Exit code policy: `0` when reachable, `1` when enabled but unreachable.

---

## 3. Snapshot pre-backfill state

```
python scripts/run_postgres_backfill.py --status
```

Reports per-table SQLite vs Postgres counts. On a fresh DB, every
Postgres count should be `0`. Capture this output — it is the baseline
for the dry-run delta.

---

## 4. Dry-run the backfill

```
python scripts/run_postgres_backfill.py --dry-run
```

This probes Postgres for the idempotency keys, computes "would insert"
counts, and exits 0 without writing. Confirm:

- "Would insert" matches the SQLite row count for each non-empty table.
- "Skip" is 0 on a fresh DB (will be non-zero on re-runs — expected).
- "Err" is 0.

**Stop here if any table reports errors.** Investigate the error
column before proceeding.

---

## 5. Execute the backfill

```
python scripts/run_postgres_backfill.py --execute --yes
```

`--yes` is required because the script refuses to run `--execute` on a
non-TTY stdin without explicit confirmation. The operator passing
`--yes` is the audit trail.

Expected output ends with:

```
Total: N rows inserted, 0 skipped, 0 errors.

[Safety] EXECUTE — Postgres writes performed.
[Safety] SQLite is the source of truth. SQLite is not modified.
[Safety] Re-running this command is safe — idempotent via primary key / UNIQUE checks.
```

If `Err > 0` or `errors:` is non-empty, investigate the offending
row(s). Backfill never aborts on a single bad row — it logs and
continues. Re-running is safe.

---

## 6. Confirm parity

```
python scripts/check_parity.py
```

Expected output ends with:

```
Status: parity OK — SQLite and Postgres row counts match on every mirror table.
```

Every table line should read `delta=+0`. Exit code `0`.

For deeper verification (catches the same-count-different-rows drift
case), run:

```
python scripts/check_parity.py --sample --sample-limit 500
```

This fetches identity tuples from both sides and compares the sets.
`sqlite_only` and `postgres_only` should both be `0` on every table.

JSON form for tooling:

```
python scripts/check_parity.py --json > reports/parity_$(date +%Y%m%d).json
```

---

## 7. Validate the full local suite

```
python scripts/validate.py
```

The full validation suite runs with dual-write **disabled** (the script
refuses to start if `USE_POSTGRES_WRITE` is anything other than
unset/`false`). To run the suite cleanly while keeping the activation
session alive:

PowerShell:

```powershell
$saved = $env:USE_POSTGRES_WRITE
$env:USE_POSTGRES_WRITE = "false"
python scripts/validate.py
$env:USE_POSTGRES_WRITE = $saved
```

Bash:

```bash
saved=$USE_POSTGRES_WRITE
USE_POSTGRES_WRITE=false python scripts/validate.py
export USE_POSTGRES_WRITE=$saved
```

Then run the operational checks profile with the env restored:

```
python scripts/run_operational_checks.py --profile postgres-dual-write
```

All steps must report `PASS`.

---

## 8. (Out of scope for M12.1) Activate on Render

M12.1 stops at local activation. Activating on Render is a separate
operator decision that requires:

- A real Render Postgres add-on (paid tier for production traffic;
  Singapore region to match the existing app).
- Render env vars `USE_POSTGRES_WRITE=true` + `DATABASE_URL=...`
  added via the Render dashboard (NOT committed to `render.yaml`).
- A staged rollout: first deploy with the env vars set and watch the
  app logs for `mirror_write` warnings before declaring the change
  safe.
- A planned `M12.0c` follow-on that switches reads to Postgres.

Until that decision is made, Render continues running SQLite-only
exactly as before.

---

## 9. Rollback

At any point, set `USE_POSTGRES_WRITE=false` (or unset it) in the
local shell. The next call into `database.save_*` / `mirror_*` will
respect the new value immediately. There is no data loss — SQLite
still holds the canonical copy and Postgres rows remain in place for
the next activation attempt.

To purge the Postgres mirror entirely (rare, e.g. before re-running a
clean backfill against a corrupted source), drop the mirror tables
manually via `psql` and re-run `python scripts/check_postgres_health.py
--ensure-schema` to recreate them. The CLIs in this runbook never
truncate or drop tables on the operator's behalf.

---

## Cross-references

- `docs/POSTGRES_MIGRATION.md` — overall 4-phase plan + M12.0a / M12.0b
  / M12.1 detail.
- `scripts/check_postgres_health.py` — read-only connectivity probe.
- `scripts/run_postgres_backfill.py` — row mover (dry-run / execute /
  status).
- `scripts/check_parity.py` — read-only parity diagnostic (this PR).
- `tests/test_check_parity.py` — pinned parity test suite (this PR).
