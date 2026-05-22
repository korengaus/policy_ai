# Verdict Producer Comparison

This is a **read-only** analysis tool that measures disagreement
between the three independent verdict producers currently in the
policy_ai pipeline:

1. `policy_decision.make_final_decision`
2. `policy_scoring.calibrate_final_decision`
   (alert level via `_alert_from_score`)
3. `verification_card._verdict_label`

## Why this tool exists

The Phase 1 audit identified that these three producers run
sequentially and can produce inconsistent labels for the same
inputs. The current regression suite passes because
conservative-under-weak-evidence happens to win in the existing
fixtures, not because the three producers actually agree.

Before the M11.0b milestone consolidates them into a single canonical
producer, M11.0a measures exactly when and how they disagree on
real, stored data. The output of this measurement feeds the
consolidation design.

## Critical: this tool does NOT change verdicts

- No verdict logic is modified. `policy_decision.py`,
  `policy_scoring.py`, and `verification_card.py` are untouched.
- The three producers are called as-is, with reconstructed inputs
  from stored `analysis_results` rows (or `reports/policy_analysis_*.
  json` files).
- Output is stored in a new `verdict_producer_comparisons` table for
  operator review. It is never read back into the live pipeline.
- `main.py`, `api_server.py`, and `scheduler.py` do not import the
  comparison tool.

## Usage

```
python scripts/compare_verdict_producers.py --from-sqlite --limit 50 --save
python scripts/compare_verdict_producers.py --from-reports --limit 100 --save
python scripts/compare_verdict_producers.py --summary
python scripts/compare_verdict_producers.py --list-disagreements --limit 20
python scripts/compare_verdict_producers.py --analysis-id <id>
python scripts/compare_verdict_producers.py --help
```

`--dry-run` and `--save` are mutually exclusive. Only one of
`--from-sqlite` / `--from-reports` / `--analysis-id` / `--summary` /
`--list-disagreements` may be set at once. `--db-path <path>` retargets
both reads and writes to a different SQLite file (used by tests and
by an operator working against an isolated DB).

## Output fields

See `verdict_producer_comparison.py` — `ProducerComparison`
dataclass. Each comparison row carries:

- `analysis_id`, `source` (`"sqlite"` or `"reports_json"`),
  `input_hash` (SHA-256 of the normalized inputs)
- `producer1_label`, `producer1_score`, `producer1_extra`
- `producer2_label`, `producer2_alert_level`, `producer2_score`,
  `producer2_extra`
- `producer3_label`, `producer3_extra`
- `all_three_agree`, `p1_p2_agree`, `p1_p3_agree`, `p2_p3_agree`
- `disagreement_pattern` (stable string like
  `P1=HIGH,P2=LOW,P3=draft_unverified`)
- `most_conservative_label` (the producer label that maps to the
  lowest severity rank — see the ordering rules below)
- `comparison_timestamp`, `notes`
- `truth_claim` — **always** `False`
- `operator_review_required` — **always** `True`

The three `producerN_extra` dicts carry diagnostic notes:
`missing_inputs`, `error`, `decision_reasons`, `calibration_reasons`,
`final_score`, etc. They are JSON-encoded as TEXT before persistence.

## Safety notes

- `truth_claim` is always `False`. Comparison is analysis only — it
  does not change any verdict.
- `operator_review_required` is always `True`. Disagreement patterns
  require human investigation.
- Comparison rows do not connect to or affect the verdict pipeline.
- Re-running on the same input replaces the prior comparison row
  (INSERT OR REPLACE on `input_hash`).

## How the conservative-ordering ranking works

When computing `most_conservative_label`, raw labels are mapped to a
numeric severity rank where **lower = more conservative** (less
action signal). The mapping is exhaustive and stable, and is fully
documented in the module-level docstring of
`verdict_producer_comparison.py`:

| rank | meaning | labels (P1, P2, P3) |
| --- | --- | --- |
| 0 | most conservative / no clear action | `LOW`, `draft_unverified` |
| 1 | watch / needs-review / disputed | `WATCH`, `draft_needs_context`, `draft_needs_review`, `draft_needs_official_confirmation`, `draft_disputed`, `draft_high_risk_review` |
| 2 | medium / likely-true | `MEDIUM`, `draft_likely_true` |
| 3 | high / verified | `HIGH`, `draft_verified` |

The "most conservative" producer is the one whose label maps to the
lowest rank. Ties are broken by producer order (P1 wins, then P2,
then P3). Labels unknown to the map are treated as rank `None` and
do not participate in the conservatism comparison.

**This ranking is itself a judgment that requires operator
validation before being used in M11.0b consolidation.** It does not
replace any existing verdict logic.

## Pairwise agreement

Two producers "agree" iff their labels map to the **same** rank in
the table above. A `None` (errored or unmapped) label never agrees
with anything — missing readings are never silently treated as
agreement. `all_three_agree` requires every producer to have emitted
a known label AND all three to share the same rank.

## Database table

```
CREATE TABLE IF NOT EXISTS verdict_producer_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL,
    source TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    producer1_label TEXT,
    producer1_score REAL,
    producer1_extra TEXT,
    producer2_label TEXT,
    producer2_alert_level TEXT,
    producer2_score REAL,
    producer2_extra TEXT,
    producer3_label TEXT,
    producer3_extra TEXT,
    all_three_agree INTEGER NOT NULL DEFAULT 0,
    p1_p2_agree INTEGER NOT NULL DEFAULT 0,
    p1_p3_agree INTEGER NOT NULL DEFAULT 0,
    p2_p3_agree INTEGER NOT NULL DEFAULT 0,
    disagreement_pattern TEXT,
    most_conservative_label TEXT,
    comparison_timestamp TEXT NOT NULL,
    notes TEXT,
    truth_claim INTEGER NOT NULL DEFAULT 0,
    operator_review_required INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdict_comparisons_analysis
    ON verdict_producer_comparisons(analysis_id);
CREATE INDEX IF NOT EXISTS idx_verdict_comparisons_pattern
    ON verdict_producer_comparisons(disagreement_pattern);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verdict_comparisons_input_hash
    ON verdict_producer_comparisons(input_hash);
```

Created idempotently via `_ensure_verdict_producer_comparisons_table`
from `init_db()` (or the standalone
`init_verdict_producer_comparisons_table()`). The `truth_claim`
column is forced to `0` and `operator_review_required` to `1` on
every `save_producer_comparison` call regardless of the caller's
input — defense-in-depth.

## Exit codes

- `0` — comparisons computed and processed (or summary printed)
- `1` — no data found, DB error, or every requested source failed
- `2` — CLI usage error (missing required flags, conflicting flags,
  unrecognized args)

## Operational profile

`scripts/run_operational_checks.py --profile verdict-comparison`
(M11.0a) chains three **offline** checks:

1. `scripts/compare_verdict_producers.py --help` — CLI smoke.
2. `scripts/compare_verdict_producers.py --summary` — read-only DB
   smoke that confirms the three safety notes are still surfaced.
3. `tests/test_verdict_producer_comparison.py` — full offline test
   suite (uses temp SQLite files; the real `policy_ai.db` is never
   touched, and `analyze_pipeline` is never called).

The profile never hits Render, never calls OpenAI, never starts a
server, never runs the live pipeline, and never modifies any input
table.

## What happens after this milestone

M11.0b will use the disagreement data collected here to design a
single canonical producer that preserves the
conservative-under-weak-evidence invariant. The npm regression test
(`tests/regression.test.js`) must continue to pass without
modification across both milestones.
