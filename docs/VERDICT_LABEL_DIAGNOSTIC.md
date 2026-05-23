# Verdict Label Diagnostic

Read-only audit of the `verification_card._verdict_label` function and the
`verdict_label` values currently stored in `analysis_results`.

## Why this tool exists

M11.0a (verdict producer comparison) measured three-producer
disagreement. While running it on real DB data, an even more urgent
pattern surfaced:

The DB contains stored `verdict_label='draft_verified'` rows where
`policy_confidence_score=10`, `verification_strength='none'`, and
there are zero official sources. The `evidence_summary` literally
says the official search failed. This directly violates the
project's conservative-under-weak-evidence invariant. At least eight
confirmed rows match the pattern (IDs 58, 65, 82, 83, 87, 95, 104,
105).

**Suspected root cause:** a branch in `_verdict_label`
(approximately `verification_card.py` lines 465–466) returns
`"draft_verified"` based **solely** on counting `evidence_snippets`
whose `evidence_type == "direct_support"`, without checking
`official_sources`, `policy_confidence_score`, or
`verification_strength`. The strict `draft_verified` branch at lines
476–477 requires `confidence_score >= 85` AND
`verification_level == "strong_official_match"`; the line 465 path
silently bypasses those gates.

## Critical: this tool does NOT change verdicts

- `verification_card.py` is not modified.
- `_verdict_label` is not modified.
- The diagnostic correlates stored outputs with reconstructed inputs.
- All stored analyses remain pending human review.
- `main.py`, `api_server.py`, and `scheduler.py` do not import the
  diagnostic module or its CLI.

## Usage

```
python scripts/diagnose_verdict_labels.py --branch-table
python scripts/diagnose_verdict_labels.py --from-sqlite --limit 100 --save
python scripts/diagnose_verdict_labels.py --summary
python scripts/diagnose_verdict_labels.py --list-weak-verified --limit 20
python scripts/diagnose_verdict_labels.py --analysis-id 105 --json
```

`--dry-run` and `--save` are mutually exclusive. Only one of the five
mode flags may be set at once. `--db-path <path>` retargets both
reads and writes to a different SQLite file. `--branch-table` needs
no DB at all and is the safest first run.

## Branch classification

Every branch in `_verdict_label` is documented in
`verdict_label_diagnostic.VERDICT_LABEL_BRANCHES`. The 16 branches
catalogued match the 16 `return "draft_*"` statements in the
function body (this parity is enforced by
`tests/test_verdict_label_diagnostic.py::BranchCatalogueParityTests`).
Each branch carries a `risk_classification` value taken from one of
five buckets:

- **`conservative_safe`** — output is a non-verified or cautious
  label (e.g. `draft_disputed`, `draft_needs_review`,
  `draft_needs_context`). These are not suspected of producing false
  positives.
- **`verified_with_strict_checks`** — output is `draft_verified` and
  the branch's triggers include the strong-evidence gates
  (`confidence_score >= 85` AND
  `verification_level == "strong_official_match"`, lines 476–477).
- **`verified_without_strict_checks`** — output is `draft_verified`
  WITHOUT the strong-evidence gates. As of M11.0b only one branch
  (B08, lines 465–466) falls in this bucket. **This is the suspected
  bug surface.**
- **`likely_true`** — output is `draft_likely_true` (lines 478–479).
  Less risky than the loose-verified branch but still worth tracking.
- **`fallback_unverified`** — terminal conservative fallbacks
  (lines 475 and 482, both returning `draft_unverified`).

These buckets are descriptive, not graded — operator validation is
required before using them in M11.0c bug-fix design.

## Weak-evidence signals

A stored `verdict_label='draft_verified'` row is flagged
`is_weak_evidence_verified=True` when any of these signals fire:

- **`no_official_sources`** — the reconstructed `official_sources`
  list is empty. The strict branch at lines 474–475 should have
  returned `draft_unverified` here; if we still see
  `draft_verified`, an earlier branch (likely B08) short-circuited
  it.
- **`score_leq_30`** — `policy_confidence_score <= 30`. The strict
  `draft_verified` branch needs `>= 85`; anything in the LOW band
  has no business being labeled verified.
- **`strength_none`** — `verification_strength == "none"`. Exactly
  the case the conservative invariant is supposed to protect.
- **`evidence_summary_says_failure`** — the stored
  `evidence_summary` text contains failure phrases such as
  `비교할 수 없` (cannot compare), `접근이 실패` (access failed),
  `공식 검색`, `공식 상세문서`, or `정보가 부족`. These are
  emitted by the pipeline when the official search/document fetch
  failed; a verified label on such a row is contradictory on its face.

These flags are heuristics — none of them, in isolation, proves the
label is wrong, but together they make a row a high-priority
operator-review candidate. The exact phrase list lives at
`verdict_label_diagnostic.WEAK_EVIDENCE_SUMMARY_PHRASES`; operators
may extend it.

## Attribution confidence

For each stored row the diagnostic walks `VERDICT_LABEL_BRANCHES` in
source order and picks the branch that BOTH (a) has its trigger
predicate satisfied by the reconstructed inputs AND (b) emits the
same label as the stored `verdict_label`. The result carries an
`attribution_confidence` value:

- **`high`** — exactly one such branch matched.
- **`medium`** — multiple branches could explain the stored label;
  the first source-order match wins (mirrors `_verdict_label`'s own
  short-circuit semantics).
- **`low`** — no triggered branch emits the stored label, but the
  catalogue has at least one branch that does; the diagnostic
  surfaces it for the operator. Usually means the reconstructed
  inputs are incomplete.
- **`unknown`** — the stored label is not produced by any branch in
  the catalogue, or `verdict_label` is `NULL`.

## Safety notes

- `truth_claim` is always `False`.
- `operator_review_required` is always `True`.
- Diagnostic rows do not feed the verdict pipeline.
- Re-running on the same `analysis_id` replaces the prior diagnostic
  row (INSERT OR REPLACE on the UNIQUE `analysis_id` index).

## Database table

```
CREATE TABLE IF NOT EXISTS verdict_label_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL,
    stored_verdict_label TEXT,
    stored_verdict_confidence INTEGER,
    stored_policy_alert_level TEXT,
    stored_policy_confidence_score INTEGER,
    stored_verification_strength TEXT,
    stored_claim_text TEXT,
    stored_evidence_summary TEXT,
    reconstructed_inputs TEXT,                   -- JSON of every reconstructed_* field
    attributed_branch_id TEXT,
    attribution_confidence TEXT,
    attribution_reason TEXT,
    is_weak_evidence_verified INTEGER NOT NULL DEFAULT 0,
    weak_evidence_signals TEXT,                  -- JSON list
    diagnostic_timestamp TEXT NOT NULL,
    notes TEXT,
    truth_claim INTEGER NOT NULL DEFAULT 0,
    operator_review_required INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdict_label_attr_analysis
    ON verdict_label_attributions(analysis_id);
CREATE INDEX IF NOT EXISTS idx_verdict_label_attr_branch
    ON verdict_label_attributions(attributed_branch_id);
CREATE INDEX IF NOT EXISTS idx_verdict_label_attr_weak
    ON verdict_label_attributions(is_weak_evidence_verified);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verdict_label_attr_analysis_unique
    ON verdict_label_attributions(analysis_id);
```

Created idempotently via `_ensure_verdict_label_attributions_table`
from `init_db()` (or the standalone
`init_verdict_label_attributions_table()`). The `truth_claim`
column is forced to `0` and `operator_review_required` to `1` on
every `save_verdict_label_attribution` call regardless of the
caller's input.

## Exit codes

- `0` — diagnostics computed (or `--summary` / `--branch-table`
  printed)
- `1` — no data found, DB error
- `2` — CLI usage error (missing required flags, conflicting flags,
  unrecognized args)

## Operational profile

`scripts/run_operational_checks.py --profile verdict-label-diagnostic`
(M11.0b) chains four **offline** checks:

1. `scripts/diagnose_verdict_labels.py --help` — CLI smoke.
2. `scripts/diagnose_verdict_labels.py --branch-table` — no-DB smoke
   that confirms B08 is still in the catalogue and the
   `verified_without_strict_checks` risk bucket is still surfaced.
3. `scripts/diagnose_verdict_labels.py --summary` — read-only DB
   smoke; ok if empty.
4. `tests/test_verdict_label_diagnostic.py` — full offline test
   suite (uses temp SQLite files; the real `policy_ai.db` is never
   touched, and `analyze_pipeline` is never called).

The profile never hits Render, never calls OpenAI, never starts a
server, and never runs the live pipeline.

## What happens after this milestone

M11.0c will design a minimal, conservative fix for the suspected
B08 branch using the data collected here. The fix must:

- Preserve every existing regression test (`tests/regression.test.js`
  unchanged).
- Strengthen the line 465–466 branch with `official_sources` +
  `confidence_score` + `verification_strength` gates.
- Never upgrade a label — only downgrade or keep.
- Apply only to new analyses; legacy stored rows are not
  retro-rewritten.
- Be backed by new unit tests that pin the weak-evidence-verified
  patterns identified here as `draft_unverified` or
  `draft_needs_context`.

## M11.0c — B08 Conservative Fix Applied

Following M11.0b diagnostic findings (21 weak-evidence verified rows
attributed to B08 in 100 sampled rows), B08 was modified to enforce
score and `verification_strength` gates. **No other branch in
`_verdict_label` was modified.**

### Change

Before:
```python
if claim_count and direct_support_count >= claim_count:
    return "draft_verified"
```

After:
```python
if (claim_count
    and direct_support_count >= claim_count
    and confidence_score >= 60
    and verification_strength in _STRONG_VERIFICATION_STRENGTHS):
    return "draft_verified"
```

The new module-level constant lives at the top of
`verification_card.py`:

```python
_STRONG_VERIFICATION_STRENGTHS = frozenset({"medium", "high"})
```

The exact strings `"medium"` and `"high"` come from
`policy_confidence._verification_strength`, which emits one of
`"high"` / `"medium"` / `"low"` / `"none"` based on
`policy_confidence_score` bands (`>=75` → high, `>=50` → medium,
`>=25` → low, else `"none"`; with an additional override forcing
`"none"` when no official document is usable).

### Effect on M11.0b sample (100 rows)

- 21 weak-evidence rows that were B08-verified now fall through to
  conservative branches (most commonly B12 → `draft_unverified`,
  because B12's trigger
  `not official_sources or verification_strength == "none"` matches
  exactly the bad-pattern shape).
- 7 good-evidence rows (score ≥ 61, strength ∈ {medium, high})
  continue to receive `draft_verified` via the gated B08.
- No other branch's behaviour changes.
- The npm regression test (`tests/regression.test.js`) was not
  modified and still passes.

### Legacy data

Existing `analysis_results` rows are **NOT** retroactively rewritten.
Their stored `verdict_label` values remain as originally written.
The fix applies only to new analyses. Operator review of the 21
affected rows is the appropriate next step (out of scope for M11.0c).

After re-running the diagnostic against the current DB, the catalog
will show B08 as `verified_with_strict_checks` and any new attribution
of the bad-pattern rows will surface `attribution_confidence="low"`
(the stored label `draft_verified` cannot be produced by any branch
given those inputs under the post-M11.0c source).

### Why "medium" and "high" only

M11.0b data showed 21 bad-pattern rows with `strength="none"` and 0
bad rows with `strength in {medium, high}`. The 7 good rows all had
`strength in {medium, high}`. The threshold is empirical, not
arbitrary. Update `_STRONG_VERIFICATION_STRENGTHS` only after
re-running M11.0b diagnostic with new data.

### Diagnostic catalog parity after M11.0c

The catalog entry for B08 in `verdict_label_diagnostic.py` was
updated to match:

- `risk_classification`: `verified_without_strict_checks` →
  `verified_with_strict_checks`
- `trigger_summary`: now includes the score-gate and
  strong-strength clause verbatim from the source
- `line_range`: updated to `"478-484"` to reflect the new lines
  occupied by the gated `if`-block

Additionally `_branch_trigger_matches` was updated to evaluate the
new gated predicate, so the diagnostic's branch attribution stays
consistent with the post-M11.0c source.

The `RISK_VERIFIED_LOOSE = "verified_without_strict_checks"`
constant remains exposed even though no branch currently maps to
it. This is intentional: any future regression that drops the gates
will be visible as a branch entering the loose bucket again, which
the existing operational profile (`verdict-label-diagnostic`) will
surface.

### Operational profile

`scripts/run_operational_checks.py --profile verdict-label-diagnostic`
now also runs `tests/test_verdict_label_b08_fix.py`, which pins:

- Bad-pattern lockdown (8 representative
  `(score, strength)` tuples)
- Good-pattern preservation (7 representative tuples)
- Boundary cases at score=59/60/100
- The exact ID-105 regression
- Other branches unaffected (B01, B02, B04, B13, B14)
- Constant integrity (frozenset, contains medium/high, excludes
  weak/none/low)
- Catalog parity (B08 risk is strict, no branch is loose)
- Static safety (no network/OpenAI imports in
  `verification_card.py`, signature unchanged)

## M11.1 — Legacy Weak-Verified Enrollment

M11.0c fixed the B08 branch for future analyses. The 21 legacy rows
identified by M11.0b still carry their original `draft_verified`
labels in `analysis_results`. M11.1 enrolls those rows into the
existing `review_tasks` queue for operator-driven correction.

### What enrollment does

For each `verdict_label_attributions` row where
`is_weak_evidence_verified=1`:

- Creates a `review_tasks` entry with the dedicated idempotency key
  `sha256(analysis_id|legacy_weak_verified_m11_0c|legacy_review_enrollment)[:24]`
- Status: `review_workflow.STATUS_PENDING_REVIEW`
  (`"pending_review"`) — same status the production review API uses
  for fresh tasks. **Never approved, published, rejected, or
  corrected.**
- `human_review_required=True`
- `snapshot_json` carries the enrollment metadata under
  `legacy_enrollment` (reason, attribution_id, weak_evidence_signals,
  source_milestone, operator_review_required, truth_claim=False)
- Idempotent: re-running does not duplicate (UNIQUE
  `idempotency_key`)

### What enrollment does NOT do

- It does NOT modify `analysis_results` (any column).
- It does NOT rewrite `verdict_label`.
- It does NOT auto-approve, auto-correct, auto-publish, or
  auto-finalize.
- It does NOT touch the live verdict pipeline.
- It does NOT change `render.yaml` or Render env.
- It does NOT extend the `review_tasks` schema — enrollment metadata
  lives inside the existing `snapshot_json` TEXT column.

### Usage

```
python scripts/enroll_legacy_weak_verified.py --list
python scripts/enroll_legacy_weak_verified.py --check-status
python scripts/enroll_legacy_weak_verified.py --dry-run
python scripts/enroll_legacy_weak_verified.py --enroll --yes
python scripts/enroll_legacy_weak_verified.py --summary
```

The `--enroll` flag is required to actually write `review_tasks`.
Without it the script is read-only. `--enroll` further requires
either `--yes` (scripted use) or an interactive `YES` confirmation on
a TTY; non-TTY callers without `--yes` are refused with exit 1.

### After enrollment

Operators can review the enrolled rows via the existing reviewer
workflow (M8.0+ API + UI, gated by `REVIEW_API_ENABLED` and the
`X-Review-Token` header, never bypassed by this script). The
reviewer decision flow (M9.0+) records each decision with full audit
metadata. Until a decision is recorded, the enrolled task stays
`pending_review`.

### Safety

- `truth_claim` is always `False` in every output (CLI and
  `LegacyEnrollmentRecord`).
- `operator_review_required` is always `True`.
- `review_tasks` created by this script never carry an
  auto-finalized status (`approved` / `rejected` / `published` /
  `corrected`).
- The script never modifies any DB row outside `review_tasks`. The
  test suite pins `analysis_results` byte-equality before/after
  enrollment.
- The `legacy-review-enroll` operational profile never invokes
  `--enroll`; it only exercises `--help`, `--check-status`,
  `--list`, `--dry-run`, and the offline test suite.

## CI integration (M13.0)

M13.0 added GitHub Actions CI. The verdict label diagnostic tests
(M11.0a/b/c) and the B08 fix tests run in CI on every PR via
`scripts/validate.py`. Any future change to `_verdict_label` or
`VERDICT_LABEL_BRANCHES` will be caught by the existing pinning
tests before merge.

## M13.1a — LLM Judge Infrastructure (this PR)

M13.1a adds LLM Judge infrastructure as a standalone module
(`llm_judge.py`) plus a dry-run CLI
(`scripts/dry_run_llm_judge.py`). The Judge is NOT connected to
`analyze_pipeline` in M13.1a. It mirrors the descriptive M11.0b label
ordering as a concrete `LABEL_SEVERITY_RANK` table so any
LLM-proposed downgrade can be mechanically validated.

The Judge cannot upgrade a label — the validator refuses any
`new_label` whose rank is not strictly lower than the current
label's rank. Equality (a lateral move within the same rank) is
also refused.

See `docs/LLM_JUDGE.md` for full details.
