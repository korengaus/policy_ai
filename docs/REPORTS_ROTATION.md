# Reports directory rotation (M12.2)

`scripts/rotate_reports.py` moves stale `reports/*.json` artifacts into
`reports/archive/YYYY-MM/` where `YYYY-MM` is derived from each file's
mtime (UTC). The script never deletes by default — deletion is an
opt-in second pass via `--delete-after-days`.

## What it does

* Scans the **top level** of `reports/` for `*.json` files (subdirectories,
  including `reports/archive/`, are skipped).
* Excludes `operational_check_*.json` by default (operational-check
  artifacts have their own retention policy).
* Moves files older than `--days N` (default 30) into
  `reports/archive/YYYY-MM/`.
* Idempotent: re-running on the same state moves nothing. Destination
  collisions are reported and skipped.
* Per-file errors do not halt the run; the exit code is 1 if any
  error occurred, 0 otherwise.
* Never queries SQLite or Postgres (`analysis_results` does not
  reference report filenames).

## When to run

Manual trigger only. M12.2 does **not** install a cron or scheduler
hook — the operator runs the script when it makes sense (suggested
cadence: monthly, or when `reports/` becomes large enough to matter).

## Preview before moving anything

```bash
python scripts/rotate_reports.py --dry-run
```

Sample output:

```
[rotate_reports] summary {"scanned": 559, "excluded": 61, "eligible": 0, "moved": 0, "would_move": 0, "already_archived": 0, "errors": 0, "bytes_moved": 0, "dry_run": true, "ts": "..."}
```

At the M12.2 install time all 559 reports were younger than 30 days,
so the default-threshold dry-run reports zero eligible files — that
is correct.

## Execute the rotation

```bash
python scripts/rotate_reports.py
```

Aggressive rotation (recent example: clear everything older than
2 weeks):

```bash
python scripts/rotate_reports.py --days 14
```

Compress while moving (saves ~70 % on JSON):

```bash
python scripts/rotate_reports.py --compress
```

## Custom exclude patterns

The default exclude is `operational_check_*.json`. To override (the
custom list replaces the default — pass `operational_check_*.json`
yourself if you want to keep that protection):

```bash
python scripts/rotate_reports.py \
  --exclude-pattern 'operational_check_*.json' \
  --exclude-pattern 'cache_measurement_*.json'
```

## Permanent deletion (opt-in)

```bash
python scripts/rotate_reports.py --delete-after-days 180
```

* Only touches files inside `reports/archive/`.
* Never touches top-level `reports/*.json`.
* Combine with `--dry-run` to preview first.
* Recommended: keep at least six months of archive; back up the
  archive directory before running with a smaller threshold.

## Structured JSON logs

```bash
python scripts/rotate_reports.py --json-log
```

Emits one JSON line per file action plus a final `summary` line. Useful
for piping into log collectors or capturing a record of the rotation.

## Restore a file from the archive

Plain move (no compression):

```bash
mv reports/archive/2026-04/policy_analysis_xyz.json reports/
```

With compression:

```bash
gunzip -c reports/archive/2026-04/policy_analysis_xyz.json.gz \
  > reports/policy_analysis_xyz.json
rm reports/archive/2026-04/policy_analysis_xyz.json.gz
```

## What it does NOT do

* Does **not** delete files by default. Only `--delete-after-days N`
  removes anything, and even then only inside `reports/archive/`.
* Does **not** touch `operational_check_*.json` (unless you override
  `--exclude-pattern`).
* Does **not** modify file contents or filenames (only adds a `.gz`
  suffix when `--compress` is set).
* Does **not** query SQLite or Postgres.
* Does **not** install itself on a cron. Operator-triggered only.

## Gitignore note

`reports/` is gitignored at the repo root (`.gitignore:5`), so the
new `reports/archive/` subtree is automatically excluded from commits.
No `.gitignore` change is required by M12.2.
