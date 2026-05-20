"""Phase 2 M6.2: anonymized real-claim semantic evaluation batch driver.

Thin wrapper around ``scripts/evaluate_semantic_calibration.py`` that:

* Defaults ``--case-file`` to ``tests/fixtures/semantic_real_claim_batch_sample.json``
  — the anonymized real-claim-like batch.
* Adds an explicit ``--live-confirm-token`` gate (compare_semantic_providers
  style) so any live OpenAI call requires an operator-typed confirmation
  token in addition to the configured env. Without the token, ``--provider
  openai`` exits with code 4 unless ``--no-network`` is also set.
* Preserves the underlying evaluator's verdict-isolation contract — this
  script still only reads ``semantic_evidence_summary`` and never touches
  ``policy_decision``, ``policy_scoring``, or ``verification_card``.

Exit codes:

    0 — success
    1 — script / tooling error or fixture invalid
    2 — provider reported ``available=False`` with ``--fail-on-unavailable``
    3 — calibration case failed with ``--fail-on-regression``
    4 — live OpenAI requested but ``--live-confirm-token`` mismatch

Reports go under ``reports/`` (gitignored). The API key is never logged.
Live evaluation is **opt-in and local-only** — CI never runs it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make Korean text printable on Windows cp949 consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Reuse the M5.6 evaluator's full pipeline (env propagation, provider
# resolution, scorecard print, JSON / CSV / Markdown writers, regression
# exit codes). We only add a live-confirmation gate on top of it.
from scripts import evaluate_semantic_calibration as base  # noqa: E402


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "semantic_real_claim_batch_sample.json"

# Mirror compare_semantic_providers.py so the operator token is consistent
# across milestones — no separate vocabulary for real-claim runs.
LIVE_CONFIRM_TOKEN = "LIVE_OPENAI_OK"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate semantic matching on the anonymized real-claim batch. "
            "Defaults to the deterministic provider so no network is "
            "involved; ``--provider openai`` without ``--no-network`` "
            f"requires ``--live-confirm-token {LIVE_CONFIRM_TOKEN}`` and a "
            "fully configured OpenAI env."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["disabled", "deterministic", "openai", "auto"],
        default="deterministic",
        help="Embedding provider to use (default: %(default)s).",
    )
    parser.add_argument(
        "--case-file", type=Path, default=DEFAULT_FIXTURE,
        help="Path to real-claim batch fixture (default: %(default)s).",
    )
    parser.add_argument(
        "--max-cases", type=int, default=None,
        help="Evaluate at most this many cases from the fixture.",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--csv-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument(
        "--show-failures", action="store_true",
        help="Print only failed cases with reasons.",
    )
    parser.add_argument(
        "--show-matches", action="store_true",
        help="Print top match snippets for each case.",
    )
    parser.add_argument(
        "--threshold-support", type=float, default=None,
        help="Override SEMANTIC_MIN_SCORE_FOR_SUPPORT for this run.",
    )
    parser.add_argument(
        "--threshold-context", type=float, default=None,
        help="Override SEMANTIC_MIN_SCORE_FOR_CONTEXT for this run.",
    )
    parser.add_argument(
        "--no-network", action="store_true",
        help="Block live OpenAI calls regardless of env.",
    )
    parser.add_argument(
        "--fail-on-regression", action="store_true",
        help="Exit code 3 if any case fails its expectations.",
    )
    parser.add_argument(
        "--fail-on-unavailable", action="store_true",
        help="Exit code 2 if the resolved provider reports available=False.",
    )
    parser.add_argument(
        "--live-confirm-token", default="",
        help=(
            f"Required for live OpenAI runs. Pass {LIVE_CONFIRM_TOKEN!r}. "
            "Without it, ``--provider openai`` exits with code 4 unless "
            "``--no-network`` is also set."
        ),
    )
    return parser


def _live_confirmation_blocked(args: argparse.Namespace) -> bool:
    """True when a live OpenAI call is requested without the explicit token.

    ``--no-network`` short-circuits this check because the underlying
    evaluator forces the provider offline; no live call can happen.
    ``--provider`` != ``openai`` also short-circuits.
    """
    if args.provider != "openai":
        return False
    if args.no_network:
        return False
    return args.live_confirm_token != LIVE_CONFIRM_TOKEN


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if _live_confirmation_blocked(args):
        print(
            "[evaluate-real-claim] FAILED: live OpenAI requires "
            f"--live-confirm-token {LIVE_CONFIRM_TOKEN!r}. "
            "Pass --no-network to exercise the offline path instead.",
            file=sys.stderr,
        )
        return 4

    # Delegate to the M5.6 evaluator. It handles env propagation, provider
    # resolution, scorecard / report output, and the regression /
    # unavailable exit codes (3 / 2). The Namespace we built has the same
    # attribute names as base._build_parser(), so it slots in directly.
    try:
        return base.run_evaluation(args)
    except base.EvaluatorError as error:
        print(f"[evaluate-real-claim] FAILED: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[evaluate-real-claim] aborted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
