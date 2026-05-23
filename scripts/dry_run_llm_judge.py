"""LLM Judge dry-run CLI (M13.1a).

Observes what the M13.1a Judge would do against stored verdicts in
``analysis_results``. Never writes to the database, never connects to
``analyze_pipeline``, never makes a real LLM API call.

Usage:
    python scripts/dry_run_llm_judge.py --help
    python scripts/dry_run_llm_judge.py --status
    python scripts/dry_run_llm_judge.py --analysis-id 105
    python scripts/dry_run_llm_judge.py --from-sqlite --limit 10
    python scripts/dry_run_llm_judge.py --simulate-downgrade --analysis-id 105
    python scripts/dry_run_llm_judge.py --simulate-upgrade-attempt --analysis-id 105
    python scripts/dry_run_llm_judge.py --simulate-malformed --analysis-id 105

The ``--simulate-*`` flags swap in built-in fake providers that produce
deterministic responses (downgrade JSON, malformed text, an upgrade
attempt the validator must refuse, etc.). They exist so operators can
exercise the validation pipeline without standing up a real LLM, and
so the M13.1a tests can drive every code path through the CLI.

Exit codes:
    0 — dry-run completed (including the "no available provider" path
        because that is the documented M13.1a outcome)
    1 — DB error / no data found for the requested analysis_id
    2 — CLI usage error
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


import llm_judge  # noqa: E402 — import after sys.path manipulation


# ---------------------------------------------------------------------------
# Built-in fake providers for --simulate-* flags.
#
# These live in the CLI (not in llm_judge.py) so the Judge module
# stays focused on production code. Each provider returns a single
# pre-canned response shape; the validator's behaviour on each shape
# is what we're exercising.
# ---------------------------------------------------------------------------


class _SimulatedProvider(llm_judge.ReasoningProvider):
    """Test-only provider with a hard-coded response. Never raises."""

    def __init__(self, name: str, raw_text: str):
        self.name = name
        self._raw_text = raw_text

    def is_available(self) -> bool:
        return True

    def call(self, request: llm_judge.LLMRequest) -> llm_judge.LLMResponse:
        return llm_judge.LLMResponse(
            raw_text=self._raw_text,
            model=request.model,
            provider=self.name,
            success=True,
        )


_SIM_CONFIRM = json.dumps({
    "action": "confirm",
    "new_label": None,
    "reason_ko": "시뮬레이션 확인",
    "evidence_gaps": [],
}, ensure_ascii=False)

_SIM_DOWNGRADE = json.dumps({
    "action": "downgrade",
    "new_label": "draft_needs_context",
    "reason_ko": "시뮬레이션 다운그레이드",
    "evidence_gaps": ["official_source_missing"],
}, ensure_ascii=False)

_SIM_FLAG = json.dumps({
    "action": "flag_for_review",
    "new_label": None,
    "reason_ko": "시뮬레이션 검토 요청",
    "evidence_gaps": ["contradiction_detected"],
}, ensure_ascii=False)

_SIM_MALFORMED = "{ this is not valid json"

# Upgrade attempt: tells the validator the action is "downgrade" but
# names a label that is strictly MORE confident than the input. The
# validator must refuse and emit confirm.
_SIM_UPGRADE_ATTEMPT = json.dumps({
    "action": "downgrade",
    "new_label": "draft_verified",
    "reason_ko": "이 응답은 사실상 업그레이드 시도입니다",
    "evidence_gaps": [],
}, ensure_ascii=False)


def _simulated_provider(flag: str):
    if flag == "confirm":
        return _SimulatedProvider("simulated_confirm", _SIM_CONFIRM)
    if flag == "downgrade":
        return _SimulatedProvider("simulated_downgrade", _SIM_DOWNGRADE)
    if flag == "flag":
        return _SimulatedProvider("simulated_flag", _SIM_FLAG)
    if flag == "malformed":
        return _SimulatedProvider("simulated_malformed", _SIM_MALFORMED)
    if flag == "upgrade_attempt":
        return _SimulatedProvider(
            "simulated_upgrade_attempt", _SIM_UPGRADE_ATTEMPT,
        )
    return None


# ---------------------------------------------------------------------------
# DB helpers — read-only.
# ---------------------------------------------------------------------------


def _open_db(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _row_to_judge_input(row) -> llm_judge.JudgeInput:
    """Translate one ``analysis_results`` row to a :class:`JudgeInput`.

    ``official_sources_count`` is derived from the JSON-encoded
    ``source_candidates`` column when present; falls back to 0 if the
    column is missing or unparseable. The Judge only needs a count —
    we deliberately do not pass the full candidate list because the
    Judge runs on summary signals, not raw evidence chunks.
    """
    def _safe_int(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _candidates_count(raw):
        if not raw:
            return 0
        if isinstance(raw, (list, tuple)):
            return len(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return 0
            return len(parsed) if isinstance(parsed, (list, tuple)) else 0
        return 0

    columns = row.keys()
    source_candidates = (
        row["source_candidates"] if "source_candidates" in columns else None
    )
    return llm_judge.JudgeInput(
        current_label=row["verdict_label"] if "verdict_label" in columns else "",
        policy_confidence_score=_safe_int(
            row["policy_confidence_score"]
            if "policy_confidence_score" in columns else None
        ),
        verification_strength=(
            row["verification_strength"]
            if "verification_strength" in columns else None
        ),
        claim_text=row["claim_text"] if "claim_text" in columns else None,
        official_sources_count=_candidates_count(source_candidates),
        evidence_summary=(
            row["evidence_summary"] if "evidence_summary" in columns else None
        ),
        contradiction_summary=(
            row["contradiction_summary"]
            if "contradiction_summary" in columns else None
        ),
        bias_framing_summary=(
            row["bias_framing_summary"]
            if "bias_framing_summary" in columns else None
        ),
    )


def _load_one_row(db_path: str, analysis_id: int):
    try:
        with _open_db(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM analysis_results WHERE id = ?",
                (int(analysis_id),),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"SQLite read failed: {exc}") from exc
    return row


def _load_rows(db_path: str, limit: int):
    try:
        with _open_db(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_results "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"SQLite read failed: {exc}") from exc
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_status_human(providers, model: str) -> str:
    lines = ["=== LLM Judge Provider Status ==="]
    lines.append("")
    lines.append("Provider chain (priority order):")
    for index, provider in enumerate(providers, start=1):
        availability = "True" if provider.is_available() else "False"
        suffix = ""
        if isinstance(provider, llm_judge.StubAnthropicProvider):
            suffix = " (M13.1a stub)"
        elif isinstance(provider, llm_judge.StubOpenAIProvider):
            suffix = " (M13.1a stub)"
        elif isinstance(provider, _SimulatedProvider):
            suffix = " (simulated)"
        lines.append(
            f"  {index}. {provider.name:<22} -- available: {availability}{suffix}"
        )
    lines.append("")
    lines.append(f"Default model: {model}")
    lines.append("")
    lines.append(
        "[Safety] M13.1a uses stub providers. All Judge runs fall "
        "back to \"confirm\"."
    )
    lines.append("[Safety] Real LLM calls will be wired in M13.1b.")
    lines.append(
        "[Safety] The Judge is NOT connected to analyze_pipeline in M13.1a."
    )
    return "\n".join(lines)


def _render_one_row_human(
    analysis_id, judge_input: llm_judge.JudgeInput,
    verdict: llm_judge.JudgeVerdict,
) -> str:
    claim_preview = (judge_input.claim_text or "(no claim text)")[:100]
    lines = ["=== LLM Judge Dry-Run ==="]
    lines.append(f"analysis_id: {analysis_id}")
    lines.append(f"current_label: {judge_input.current_label}")
    lines.append(
        f"policy_confidence_score: {judge_input.policy_confidence_score}"
    )
    lines.append(
        f"verification_strength: {judge_input.verification_strength}"
    )
    lines.append(f"claim_text (first 100 chars): {claim_preview}")
    lines.append("")
    lines.append(f"Judge action:       {verdict.action}")
    lines.append(f"Judge new_label:    {verdict.new_label}")
    lines.append(f"Judge reason:       {verdict.reason_ko}")
    provider_label = (
        verdict.provider_used
        if verdict.provider_used
        else "None (fell back)"
    )
    lines.append(f"Provider used:      {provider_label}")
    lines.append(f"Fell back:          {verdict.fell_back}")
    lines.append(f"Fallback reason:    {verdict.fallback_reason}")
    if verdict.evidence_gaps:
        lines.append("Evidence gaps:")
        for gap in verdict.evidence_gaps:
            lines.append(f"  - {gap}")
    lines.append("")
    lines.append(
        "[Safety] truth_claim=False -- Judge output is advisory only."
    )
    lines.append(
        "[Safety] operator_review_required=True -- every action "
        "requires human review."
    )
    lines.append(
        "[Safety] This is M13.1a infrastructure only. The Judge is "
        "NOT connected to the live pipeline."
    )
    lines.append("[Safety] No actual LLM API calls were made.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dry_run_llm_judge",
        description=(
            "Dry-run the M13.1a LLM Judge against stored verdicts. "
            "Read-only -- no DB writes, no pipeline connection, no "
            "real LLM API calls."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- dry-run completed (incl. \"no available provider\")\n"
            "  1 -- DB error or analysis_id not found\n"
            "  2 -- CLI usage error\n\n"
            "Safety: M13.1a is infrastructure only. The Judge is not "
            "connected to analyze_pipeline. truth_claim is always "
            "False and operator_review_required is always True in "
            "every Judge output."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--analysis-id", type=int, default=None,
        help="Process a single analysis_results row by id.",
    )
    target.add_argument(
        "--from-sqlite", action="store_true",
        help="Process multiple rows (newest first; respects --limit).",
    )
    target.add_argument(
        "--status", action="store_true",
        help="Print provider availability summary and exit.",
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Max rows when --from-sqlite is set. Default: 10. Cap: 100.",
    )
    parser.add_argument(
        "--db-path", default="policy_ai.db",
        help="SQLite path (default: %(default)s).",
    )
    sim = parser.add_mutually_exclusive_group()
    sim.add_argument(
        "--simulate-confirm", action="store_true",
        help="Use a fake provider that always confirms.",
    )
    sim.add_argument(
        "--simulate-downgrade", action="store_true",
        help="Use a fake provider that downgrades to draft_needs_context.",
    )
    sim.add_argument(
        "--simulate-flag", action="store_true",
        help="Use a fake provider that flags for review.",
    )
    sim.add_argument(
        "--simulate-malformed", action="store_true",
        help=(
            "Use a fake provider that returns invalid JSON "
            "-- exercises the validator fallback."
        ),
    )
    sim.add_argument(
        "--simulate-upgrade-attempt", action="store_true",
        help=(
            "Use a fake provider that tries to upgrade the label "
            "-- the validator must refuse."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    return parser


def _resolve_providers(args):
    """Return the provider chain implied by the --simulate-* flags
    (or the default stub chain when no flag is given)."""
    if args.simulate_confirm:
        return [_simulated_provider("confirm")]
    if args.simulate_downgrade:
        return [_simulated_provider("downgrade")]
    if args.simulate_flag:
        return [_simulated_provider("flag")]
    if args.simulate_malformed:
        return [_simulated_provider("malformed")]
    if args.simulate_upgrade_attempt:
        return [_simulated_provider("upgrade_attempt")]
    return llm_judge.get_default_provider_chain()


def _emit_payload(args, payload):
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(payload["_human"])


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # Resolve providers up front so --status output reflects what the
    # actual run would use.
    providers = _resolve_providers(args)

    if args.status:
        human = _render_status_human(providers, llm_judge.DEFAULT_JUDGE_MODEL)
        if args.json:
            payload = {
                "providers": [
                    {
                        "name": p.name,
                        "available": p.is_available(),
                    }
                    for p in providers
                ],
                "default_model": llm_judge.DEFAULT_JUDGE_MODEL,
                "safety": {
                    "milestone": "M13.1a",
                    "truth_claim": False,
                    "operator_review_required": True,
                    "connected_to_pipeline": False,
                },
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(human)
        return 0

    if args.analysis_id is None and not args.from_sqlite:
        # Default to --from-sqlite for friendlier "just run it" UX,
        # but only when no simulation flag has been set; with a
        # simulation flag we still need a target to read from, so we
        # also default to --from-sqlite (the operator can override).
        args.from_sqlite = True

    limit = max(1, min(int(args.limit or 10), 100))

    rows_payload = []
    try:
        if args.analysis_id is not None:
            row = _load_one_row(args.db_path, args.analysis_id)
            if row is None:
                msg = (
                    f"No analysis_results row with id={args.analysis_id}"
                )
                if args.json:
                    print(json.dumps({"error": msg}, indent=2))
                else:
                    print(msg, file=sys.stderr)
                return 1
            rows = [row]
        else:
            rows = _load_rows(args.db_path, limit)
    except RuntimeError as exc:
        msg = str(exc)
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1

    if not rows:
        if args.json:
            print(json.dumps({"rows": [], "note": "no data"}, indent=2))
        else:
            print(
                "(no analysis_results rows found in "
                f"{args.db_path}; nothing to dry-run)"
            )
        return 0

    for row in rows:
        analysis_id = row["id"] if "id" in row.keys() else None
        judge_input = _row_to_judge_input(row)
        verdict = llm_judge.run_judge(
            judge_input,
            providers=providers,
            model=llm_judge.DEFAULT_JUDGE_MODEL,
        )
        human = _render_one_row_human(analysis_id, judge_input, verdict)
        rows_payload.append({
            "analysis_id": analysis_id,
            "judge_input": {
                "current_label": judge_input.current_label,
                "policy_confidence_score":
                    judge_input.policy_confidence_score,
                "verification_strength":
                    judge_input.verification_strength,
                "official_sources_count":
                    judge_input.official_sources_count,
                "claim_text_preview":
                    (judge_input.claim_text or "")[:100],
            },
            "judge_verdict": llm_judge.judge_verdict_to_dict(verdict),
            "_human": human,
        })

    if args.json:
        # Strip the _human render from JSON payloads -- it would
        # duplicate fields and bloat operator scripts.
        for payload in rows_payload:
            payload.pop("_human", None)
        print(json.dumps(
            {"rows": rows_payload,
             "safety": {
                 "milestone": "M13.1a",
                 "truth_claim": False,
                 "operator_review_required": True,
                 "connected_to_pipeline": False,
                 "real_llm_calls_made": False,
             }},
            indent=2, ensure_ascii=False, sort_keys=True,
        ))
    else:
        for index, payload in enumerate(rows_payload):
            if index > 0:
                print()
            print(payload["_human"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
