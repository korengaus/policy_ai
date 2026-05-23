# Legacy Weak-Verified Enrollment Runbook (M11.3)

## Background

M11.1 built `legacy_review_enrollment.py` + `scripts/enroll_legacy_weak_verified.py`
to identify historical analyzed stories that should have been routed to the
reviewer queue but weren't (because the routing logic was added after they
were analyzed). The identifier reads SQLite rows from the
`verdict_label_attributions` table where `is_weak_evidence_verified=1` — it does
NOT scan JSON files in `reports/`.

M11.1's dry-run originally identified 21 candidate stories. The actual
`--enroll` was never executed pending a final operator review.

M11.3 adds a read-only audit script (`scripts/audit_legacy_enrollment.py`)
that produces a structured JSON report of current candidates without
modifying anything. Operators run the audit, review the JSON output, then
decide whether to run the actual `--enroll` command.

## Identifier-API deviation from the M11.3 brief

The M11.3 brief described `--reports-dir` scanning `reports/*.json`. The
M11.1 identifier reads SQLite rows from `verdict_label_attributions` — the
brief's framing did not match the actual identifier API. Per the brief's
"Do NOT bend the identifier" rule, the audit script exposes `--db-path`
instead (mirroring the M11.1 CLI's existing flag), and the audit JSON
schema retains the `report_path` / `total_reports_scanned` fields with
DB-derived values so downstream consumers don't have to special-case the
M11.3 output shape. See the script docstring for the full mapping.

## Pre-run checklist

- [ ] M13.3d cache deployed and stable
- [ ] CI green on latest main
- [ ] Render baseline smoke passing
- [ ] `policy_ai.db` (or the operator-supplied path) present and reachable
- [ ] Operator has read-only or read-write access to the DB as needed

## Step 1 — Run the audit (read-only)

```bash
python scripts/audit_legacy_enrollment.py \
    --db-path policy_ai.db \
    --output-dir ./reports
```

Optional flags:

- `--limit N` — cap the candidate list to the first N rows (audit's
  `total_reports_scanned` still reflects the full count).
- `--output-dir <path>` — defaults to `./reports`.

Expected stdout:

```
[audit] candidates=N output=./reports/legacy_enrollment_audit_<timestamp>.json
```

N may differ from M11.1's original count of 21 because:

- New analyses run since M11.1 may have added or replaced attribution rows.
- Some stories may now have stronger evidence (re-analyzed).
- Some stories may already have been enrolled via the M11.1 CLI.

## Step 2 — Review the audit JSON

Open `reports/legacy_enrollment_audit_<timestamp>.json`. For each candidate
in the `candidates` array, confirm:

- `verdict_label` is a "verified-leaning" label (typically `draft_verified`).
- `evidence_strength_class` is `"none"` / `"weak"` / `"unknown"`.
- `enrollment_reason` lists the weak-evidence signals that flagged the row
  (e.g., `no_official_sources`, `score_leq_30`, `strength_none`).

If any candidate looks wrong, do NOT proceed with `--enroll`. Investigate
the upstream M11.0b diagnostic first.

## Step 3 — Compare with M11.1's original 21

Take the `story_id` (== `analysis_id`) set from this audit and diff it
against the M11.1 dry-run output. Differences:

- New `story_id`s present here but not in M11.1 → analyses landed since
  M11.1 ran.
- M11.1 `story_id`s missing here → either already enrolled (M11.1 `--enroll`
  ran for that row) or re-analyzed past the weak-verified threshold.

Document the delta in your operator notes.

## Step 4 — Run the existing M11.1 status check

Before enrolling, confirm which candidates are already enrolled:

```bash
python scripts/enroll_legacy_weak_verified.py --check-status
```

This is the M11.1 CLI's `--check-status` mode. It reads `review_tasks` and
reports per-row enrollment state. M11.3 does NOT duplicate this — the
audit JSON does not consult `review_tasks`.

## Step 5 — Decide on `--enroll`

If the audit looks correct, run:

```bash
python scripts/enroll_legacy_weak_verified.py --enroll --yes
```

The `--yes` flag bypasses the interactive `YES` confirmation; if you
prefer the interactive flow, omit `--yes` and type `YES` when prompted.

This is the M11.1 script. It will enroll the identified candidates into
the reviewer queue with `status=pending_review`. M11.3 does NOT add new
`--apply` / `--enroll` logic — it only audits.

## Step 6 — Post-enroll verification

After `--enroll`:

- Confirm the reviewer queue contains the newly enrolled stories
  (`/jobs/...` review-API or the reviewer dashboard).
- Re-run the M11.3 audit. `candidates_found` may stay the same (the
  audit identifies rows by `is_weak_evidence_verified=1`, not by
  enrollment state). To verify enrollment state, run
  `scripts/enroll_legacy_weak_verified.py --check-status` instead.
- Save the post-enroll audit JSON for the historical record.

## Step 7 — Render verification

Run the standard smoke:

```bash
python scripts/run_operational_checks.py \
    --profile render-baseline \
    --base-url https://policy-ai-q5ax.onrender.com
```

The smoke should be unaffected. Enrollment is a database-only operation,
not a code change.

## Rollback

If `--enroll` enrolled stories that shouldn't have been enrolled:

1. The audit JSON saved in Step 2 lists exactly which stories were
   candidates at run time (story_id = analysis_id).
2. Manually un-enroll via the reviewer dashboard or by DELETE-ing the
   matching `review_tasks` rows (idempotency key:
   `legacy_review_enrollment.make_enrollment_idempotency_key`).
3. The M11.1 enroll path is atomic per-row and idempotent — re-running
   `--enroll` does NOT create duplicates (UNIQUE constraint on
   `review_tasks.idempotency_key`).
4. The audit script is purely read-only; re-running it never changes
   state.

## What's NOT in M11.3

- Auto-apply: M11.3 only audits. `--enroll` remains a manual decision
  driven by the M11.1 CLI.
- New identification rules: the M11.1 module is untouched.
- Reviewer dashboard changes: enrollment surfaces via the existing flow.
- Postgres migration: unrelated (this is SQLite-only).
- File-based scanning of `reports/*.json`: the identifier reads DB rows.

## Verification pins

- `tests/test_audit_legacy_enrollment.py` (M11.3 — 16 cases)
- `tests/test_legacy_review_enrollment.py` (M11.1 regression — 38 cases)
- `scripts/run_operational_checks.py --profile legacy-enrollment-audit`
  bundles both
- `npm test` (regression unchanged)

## Output file lifecycle

The audit JSON files (`reports/legacy_enrollment_audit_*.json`) are NOT
committed. The repo's `.gitignore` already ignores `reports/`, so a
sanity-run audit during PR validation stays untracked. Operators
running the audit in production should archive the JSON outside the
repo if a long-term audit trail is required.
