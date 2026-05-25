"""Tests for the M13.1a LLM Judge module + dry-run CLI.

Run with: python tests/test_llm_judge.py

No real LLM provider is ever called. The provider chain is replaced
with in-test fakes. SQLite-backed CLI tests use temp DB files so the
real ``policy_ai.db`` is never touched.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import llm_judge  # noqa: E402


# ---------------------------------------------------------------------------
# In-test fake provider — kept out of llm_judge.py so the production
# module stays focused on production code.
# ---------------------------------------------------------------------------


class _FakeProviderForTests(llm_judge.ReasoningProvider):
    """Test-only provider with configurable response. Never imported
    by production code; lives in this test file only."""

    def __init__(
        self,
        name: str,
        response_text: str,
        available: bool = True,
        raise_on_call: bool = False,
        success: bool = True,
        error: str = "",
    ):
        self.name = name
        self.response_text = response_text
        self.available = available
        self.raise_on_call = raise_on_call
        self.success = success
        self.error = error
        self.call_count = 0

    def is_available(self):
        return self.available

    def call(self, request):
        self.call_count += 1
        if self.raise_on_call:
            raise RuntimeError("simulated provider crash")
        return llm_judge.LLMResponse(
            raw_text=self.response_text,
            model=request.model,
            provider=self.name,
            success=self.success,
            error=self.error or None,
        )


# ---------------------------------------------------------------------------
# Schema validator — the security boundary.
# ---------------------------------------------------------------------------


class SchemaValidatorTests(unittest.TestCase):
    def test_valid_confirm(self):
        text = json.dumps({
            "action": "confirm",
            "new_label": None,
            "reason_ko": "OK",
            "evidence_gaps": [],
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertIsNone(verdict.new_label)
        self.assertFalse(verdict.fell_back)

    def test_valid_downgrade(self):
        text = json.dumps({
            "action": "downgrade",
            "new_label": "draft_needs_context",
            "reason_ko": "evidence is weak",
            "evidence_gaps": ["missing_official_source"],
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_verified",
        )
        self.assertEqual(verdict.action, "downgrade")
        self.assertEqual(verdict.new_label, "draft_needs_context")
        self.assertFalse(verdict.fell_back)
        self.assertEqual(
            verdict.evidence_gaps, ["missing_official_source"],
        )

    def test_valid_flag_for_review(self):
        text = json.dumps({
            "action": "flag_for_review",
            "new_label": None,
            "reason_ko": "uncertain",
            "evidence_gaps": [],
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_likely_true",
        )
        self.assertEqual(verdict.action, "flag_for_review")
        self.assertIsNone(verdict.new_label)
        self.assertFalse(verdict.fell_back)

    def test_empty_response_falls_back(self):
        verdict = llm_judge.validate_judge_response_json(
            "", "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn("empty", (verdict.fallback_reason or "").lower())

    def test_whitespace_only_response_falls_back(self):
        verdict = llm_judge.validate_judge_response_json(
            "   \n  ", "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)

    def test_malformed_json_falls_back(self):
        verdict = llm_judge.validate_judge_response_json(
            "{ not json", "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn("json", (verdict.fallback_reason or "").lower())

    def test_json_array_falls_back(self):
        verdict = llm_judge.validate_judge_response_json(
            "[]", "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)

    def test_action_upgrade_falls_back(self):
        text = json.dumps({"action": "upgrade", "new_label": "draft_verified"})
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn("action", (verdict.fallback_reason or "").lower())

    def test_action_something_weird_falls_back(self):
        text = json.dumps({"action": "delete", "new_label": None})
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)

    def test_downgrade_missing_new_label_falls_back(self):
        text = json.dumps({"action": "downgrade", "new_label": None})
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_verified",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn("new_label", (verdict.fallback_reason or "").lower())

    def test_downgrade_unknown_label_falls_back(self):
        text = json.dumps({
            "action": "downgrade",
            "new_label": "draft_made_up_label",
        })
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_verified",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)

    def test_downgrade_attempting_upgrade_is_refused(self):
        """CRITICAL: this is the security boundary. Even if the model
        wraps an upgrade inside the ``downgrade`` action keyword, the
        validator must refuse and emit ``confirm`` with a
        ``refused upgrade attempt`` reason."""
        text = json.dumps({
            "action": "downgrade",
            "new_label": "draft_verified",
            "reason_ko": "트로얀 업그레이드",
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn(
            "refused upgrade", (verdict.fallback_reason or ""),
        )

    def test_downgrade_lateral_same_rank_is_refused(self):
        """A lateral move within the same rank (e.g. needs_review
        -> needs_context, both rank 1) is NOT a downgrade. Refuse."""
        text = json.dumps({
            "action": "downgrade",
            "new_label": "draft_needs_context",
            "reason_ko": "lateral",
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_review",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn(
            "refused upgrade", (verdict.fallback_reason or ""),
        )

    def test_reason_ko_truncated_to_200(self):
        long_reason = "가" * 500
        text = json.dumps({
            "action": "confirm",
            "reason_ko": long_reason,
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertLessEqual(len(verdict.reason_ko), 200)

    def test_evidence_gaps_truncated(self):
        gaps = [f"gap_{i}" * 50 for i in range(20)]
        text = json.dumps({
            "action": "flag_for_review",
            "evidence_gaps": gaps,
        }, ensure_ascii=False)
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_likely_true",
        )
        self.assertEqual(verdict.action, "flag_for_review")
        self.assertLessEqual(len(verdict.evidence_gaps), 5)
        for gap in verdict.evidence_gaps:
            self.assertLessEqual(len(gap), 100)

    def test_validator_never_raises_on_garbage_input(self):
        garbage_inputs = [
            None, 12345, 3.14, b"bytes", [], {},
            object(), float("nan"),
        ]
        for value in garbage_inputs:
            try:
                verdict = llm_judge.validate_judge_response_json(
                    value, "draft_needs_context",
                )
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"validate_judge_response_json raised on "
                    f"{value!r}: {exc!r}"
                )
            self.assertEqual(verdict.action, "confirm")
            self.assertTrue(verdict.fell_back)


# ---------------------------------------------------------------------------
# is_downgrade pure function
# ---------------------------------------------------------------------------


class IsDowngradeTests(unittest.TestCase):
    def test_verified_to_needs_context_is_downgrade(self):
        self.assertTrue(llm_judge.is_downgrade(
            "draft_verified", "draft_needs_context",
        ))

    def test_verified_to_likely_true_is_downgrade(self):
        self.assertTrue(llm_judge.is_downgrade(
            "draft_verified", "draft_likely_true",
        ))

    def test_likely_true_to_needs_review_is_downgrade(self):
        self.assertTrue(llm_judge.is_downgrade(
            "draft_likely_true", "draft_needs_review",
        ))

    def test_needs_review_to_unverified_is_downgrade(self):
        self.assertTrue(llm_judge.is_downgrade(
            "draft_needs_review", "draft_unverified",
        ))

    def test_needs_review_to_verified_is_not_downgrade(self):
        self.assertFalse(llm_judge.is_downgrade(
            "draft_needs_review", "draft_verified",
        ))

    def test_needs_review_to_needs_context_is_not_downgrade(self):
        """Both are rank 1 — lateral move."""
        self.assertFalse(llm_judge.is_downgrade(
            "draft_needs_review", "draft_needs_context",
        ))

    def test_unverified_to_anything_else_is_not_downgrade(self):
        for label in llm_judge.LABEL_SEVERITY_RANK:
            if label == "draft_unverified":
                continue
            self.assertFalse(
                llm_judge.is_downgrade("draft_unverified", label),
                msg=f"draft_unverified -> {label} unexpectedly is_downgrade",
            )

    def test_unknown_from_label_is_handled_gracefully(self):
        try:
            llm_judge.is_downgrade("not_a_real_label", "draft_unverified")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"is_downgrade raised: {exc!r}")

    def test_unknown_to_label_is_handled_gracefully(self):
        try:
            llm_judge.is_downgrade("draft_verified", "not_a_real_label")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"is_downgrade raised: {exc!r}")


# ---------------------------------------------------------------------------
# run_judge orchestration
# ---------------------------------------------------------------------------


SAMPLE_INPUT = llm_judge.JudgeInput(
    current_label="draft_verified",
    policy_confidence_score=10,
    verification_strength="none",
    claim_text="해당 과제에는 피해 지원 기간을 연장한다",
    official_sources_count=0,
    evidence_summary="official source not found",
    contradiction_summary="",
    bias_framing_summary="",
)


class RunJudgeTests(unittest.TestCase):
    def test_no_available_providers_returns_fallback(self):
        verdict = llm_judge.run_judge(SAMPLE_INPUT, providers=[])
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn(
            "no available", (verdict.fallback_reason or "").lower(),
        )

    def test_default_chain_is_all_stubs_unavailable(self):
        verdict = llm_judge.run_judge(SAMPLE_INPUT)
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)

    def test_crashing_provider_advances_to_next(self):
        crashing = _FakeProviderForTests(
            "crashing", "", raise_on_call=True,
        )
        good_response = json.dumps({
            "action": "downgrade",
            "new_label": "draft_needs_context",
            "reason_ko": "약한 증거",
        }, ensure_ascii=False)
        good = _FakeProviderForTests("good", good_response)
        verdict = llm_judge.run_judge(
            SAMPLE_INPUT, providers=[crashing, good],
        )
        self.assertEqual(verdict.action, "downgrade")
        self.assertEqual(verdict.new_label, "draft_needs_context")
        self.assertEqual(crashing.call_count, 1)
        self.assertEqual(good.call_count, 1)

    def test_provider_returning_success_false_advances_to_next(self):
        bad = _FakeProviderForTests(
            "bad", "", success=False, error="rate limited",
        )
        good_response = json.dumps({
            "action": "flag_for_review",
            "reason_ko": "검토 필요",
        }, ensure_ascii=False)
        good = _FakeProviderForTests("good", good_response)
        verdict = llm_judge.run_judge(
            SAMPLE_INPUT, providers=[bad, good],
        )
        self.assertEqual(verdict.action, "flag_for_review")
        self.assertEqual(bad.call_count, 1)
        self.assertEqual(good.call_count, 1)

    def test_malformed_first_provider_does_not_advance(self):
        """The model spoke -- the chain does NOT advance just because
        the content was bad. Operator sees the validator's verdict."""
        malformed = _FakeProviderForTests("malformed", "{ not json")
        wouldnt_be_called = _FakeProviderForTests(
            "second", json.dumps({"action": "confirm"}),
        )
        verdict = llm_judge.run_judge(
            SAMPLE_INPUT, providers=[malformed, wouldnt_be_called],
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertEqual(malformed.call_count, 1)
        self.assertEqual(wouldnt_be_called.call_count, 0)

    def test_valid_downgrade_returns_downgrade(self):
        response = json.dumps({
            "action": "downgrade",
            "new_label": "draft_needs_context",
            "reason_ko": "공식 출처 없음",
        }, ensure_ascii=False)
        provider = _FakeProviderForTests("good", response)
        verdict = llm_judge.run_judge(
            SAMPLE_INPUT, providers=[provider],
        )
        self.assertEqual(verdict.action, "downgrade")
        self.assertEqual(verdict.new_label, "draft_needs_context")
        self.assertEqual(verdict.provider_used, "good")
        self.assertFalse(verdict.fell_back)

    def test_first_provider_upgrade_attempt_is_refused(self):
        """If the first available provider returns a covert upgrade,
        the validator emits confirm. The chain does NOT advance."""
        attempt = json.dumps({
            "action": "downgrade",
            "new_label": "draft_verified",
            "reason_ko": "이건 업그레이드 시도",
        }, ensure_ascii=False)
        upgrade_provider = _FakeProviderForTests("attempt", attempt)
        downgrade_response = json.dumps({
            "action": "downgrade",
            "new_label": "draft_needs_context",
        }, ensure_ascii=False)
        second = _FakeProviderForTests(
            "second", downgrade_response,
        )
        verdict = llm_judge.run_judge(
            llm_judge.JudgeInput(current_label="draft_needs_context"),
            providers=[upgrade_provider, second],
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        self.assertIn(
            "refused upgrade", (verdict.fallback_reason or ""),
        )
        self.assertEqual(upgrade_provider.call_count, 1)
        # Chain must NOT advance after the upgrade refusal.
        self.assertEqual(second.call_count, 0)

    def test_run_judge_never_raises_on_pathological_input(self):
        weird_input = llm_judge.JudgeInput(
            current_label="not_a_real_label",
            policy_confidence_score=None,
            verification_strength=None,
            claim_text=None,
            official_sources_count=0,
        )
        try:
            verdict = llm_judge.run_judge(weird_input, providers=[])
        except Exception as exc:  # noqa: BLE001
            self.fail(f"run_judge raised: {exc!r}")
        self.assertEqual(verdict.action, "confirm")


# ---------------------------------------------------------------------------
# build_judge_request truncation + None handling
# ---------------------------------------------------------------------------


class BuildJudgeRequestTests(unittest.TestCase):
    def test_truncates_claim_text(self):
        long_claim = "가" * 5000
        req = llm_judge.build_judge_request(
            llm_judge.JudgeInput(
                current_label="draft_verified", claim_text=long_claim,
            ),
        )
        # The truncated claim appears inside the user prompt; the
        # original 5000-char string must NOT.
        self.assertNotIn("가" * 5000, req.user_prompt)
        self.assertIn("가" * 1000, req.user_prompt)

    def test_truncates_evidence_summary(self):
        long_summary = "근거" * 1000
        req = llm_judge.build_judge_request(
            llm_judge.JudgeInput(
                current_label="draft_verified",
                evidence_summary=long_summary,
            ),
        )
        self.assertNotIn("근거" * 1000, req.user_prompt)

    def test_none_inputs_replaced_with_placeholder(self):
        req = llm_judge.build_judge_request(
            llm_judge.JudgeInput(current_label="draft_verified"),
        )
        self.assertIn("정보 없음", req.user_prompt)

    def test_system_prompt_mentions_downgrade_only(self):
        req = llm_judge.build_judge_request(
            llm_judge.JudgeInput(current_label="draft_verified"),
        )
        # "downgrade-only" / "upgrade ... 수 없습니다" pinning
        self.assertIn("downgrade-only", req.system_prompt)
        self.assertIn("upgrade", req.system_prompt.lower())

    def test_system_prompt_mentions_rank_table(self):
        req = llm_judge.build_judge_request(
            llm_judge.JudgeInput(current_label="draft_verified"),
        )
        for label in ("draft_unverified", "draft_needs_context",
                      "draft_likely_true", "draft_verified"):
            self.assertIn(label, req.system_prompt)


# ---------------------------------------------------------------------------
# judge_verdict_to_dict safety pins
# ---------------------------------------------------------------------------


class SerializationSafetyTests(unittest.TestCase):
    def test_truth_claim_always_false(self):
        verdict = llm_judge.JudgeVerdict(
            action="confirm", truth_claim=True,  # try to sneak True
        )
        out = llm_judge.judge_verdict_to_dict(verdict)
        self.assertIs(out["truth_claim"], False)

    def test_operator_review_required_always_true(self):
        verdict = llm_judge.JudgeVerdict(
            action="confirm", operator_review_required=False,
        )
        out = llm_judge.judge_verdict_to_dict(verdict)
        self.assertIs(out["operator_review_required"], True)

    def test_all_documented_keys_present(self):
        verdict = llm_judge.JudgeVerdict(action="confirm")
        out = llm_judge.judge_verdict_to_dict(verdict)
        # M13.1b added input_tokens / output_tokens / estimated_cost_usd
        # so debug_summary["llm_judge"] can surface real token usage and
        # cost. The three new keys are stable contract; updating this
        # pin is the documented way to extend the dict shape.
        expected_keys = {
            "action", "new_label", "reason_ko", "evidence_gaps",
            "raw_response", "provider_used", "model", "latency_ms",
            "fell_back", "fallback_reason",
            "input_tokens", "output_tokens", "estimated_cost_usd",
            "truth_claim", "operator_review_required",
        }
        self.assertSetEqual(set(out.keys()), expected_keys)


# ---------------------------------------------------------------------------
# CLI behaviour — invoke main() with crafted argv.
# ---------------------------------------------------------------------------


def _seed_sqlite(db_path: Path, num_rows: int = 3):
    """Create a synthetic analysis_results row set for CLI tests."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE analysis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT, title TEXT, original_url TEXT,
                topic TEXT,
                verdict_label TEXT, verdict_confidence INTEGER,
                policy_confidence_score INTEGER,
                verification_strength TEXT,
                claim_text TEXT,
                evidence_summary TEXT,
                contradiction_summary TEXT,
                bias_framing_summary TEXT,
                source_candidates TEXT,
                created_at TEXT
            )
            """
        )
        for index in range(num_rows):
            conn.execute(
                "INSERT INTO analysis_results "
                "(verdict_label, policy_confidence_score, "
                "verification_strength, claim_text, evidence_summary, "
                "source_candidates) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "draft_verified" if index == 0 else "draft_needs_context",
                    10 if index == 0 else 70,
                    "none" if index == 0 else "medium",
                    f"주장 {index}",
                    f"근거 요약 {index}",
                    json.dumps([{"src": "a"}, {"src": "b"}]),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _load_cli_module():
    """Import scripts/dry_run_llm_judge.py as a fresh module each
    time so the test suite can rebind sys.stdout/stdin around calls."""
    spec = importlib.util.spec_from_file_location(
        "dry_run_llm_judge_cli",
        str(_PROJECT_ROOT / "scripts" / "dry_run_llm_judge.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliTests(unittest.TestCase):
    def _run_cli(self, argv):
        module = _load_cli_module()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture
            rc = module.main(argv)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        return rc, stdout_capture.getvalue(), stderr_capture.getvalue()

    def test_help_exits_zero(self):
        rc, out, _ = self._run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("dry_run_llm_judge", out)
        self.assertIn("Exit codes", out)

    def test_status_exits_zero_and_lists_chain(self):
        """M13.1b: get_default_provider_chain returns OpenAIProvider
        when OPENAI_API_KEY is set, StubOpenAIProvider otherwise.
        StubAnthropicProvider is no longer in the default chain
        (kept in the module for M13.1c). This test runs with the env
        cleared so it sees the stub chain deterministically."""
        prior = os.environ.pop("OPENAI_API_KEY", None)
        try:
            rc, out, _ = self._run_cli(["--status"])
        finally:
            if prior is not None:
                os.environ["OPENAI_API_KEY"] = prior
        self.assertEqual(rc, 0)
        self.assertIn("openai_stub", out)
        self.assertIn("M13.1a", out)

    def test_status_json(self):
        rc, out, _ = self._run_cli(["--status", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("providers", data)
        self.assertFalse(data["safety"]["connected_to_pipeline"])
        self.assertFalse(data["safety"]["truth_claim"])

    def test_simulate_confirm_against_seeded_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=1)
            rc, out, _ = self._run_cli([
                "--simulate-confirm",
                "--analysis-id", "1",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 0)
            self.assertIn("Judge action:       confirm", out)

    def test_simulate_downgrade_against_seeded_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=1)
            rc, out, _ = self._run_cli([
                "--simulate-downgrade",
                "--analysis-id", "1",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 0)
            self.assertIn("Judge action:       downgrade", out)
            self.assertIn("draft_needs_context", out)

    def test_simulate_upgrade_attempt_is_refused(self):
        """End-to-end pin: the CLI exposes that the validator refuses
        an upgrade attempt -- operator sees ``confirm`` and the
        ``refused upgrade`` reason."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=2)
            # Row 2 has verdict_label='draft_needs_context'; the fake
            # upgrade-attempt provider tries to push it to 'draft_verified'.
            rc, out, _ = self._run_cli([
                "--simulate-upgrade-attempt",
                "--analysis-id", "2",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 0)
            self.assertIn("Judge action:       confirm", out)
            self.assertIn("refused upgrade", out)

    def test_simulate_malformed_returns_confirm(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=1)
            rc, out, _ = self._run_cli([
                "--simulate-malformed",
                "--analysis-id", "1",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 0)
            self.assertIn("Judge action:       confirm", out)
            self.assertIn("Fell back:          True", out)

    def test_simulate_flag_returns_flag_for_review(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=1)
            rc, out, _ = self._run_cli([
                "--simulate-flag",
                "--analysis-id", "1",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 0)
            self.assertIn("Judge action:       flag_for_review", out)

    def test_from_sqlite_with_limit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=5)
            rc, out, _ = self._run_cli([
                "--from-sqlite",
                "--limit", "5",
                "--db-path", str(db_path),
                "--simulate-confirm",
            ])
            self.assertEqual(rc, 0)
            # Each row prints its own block; count the header lines.
            header_count = out.count("=== LLM Judge Dry-Run ===")
            self.assertEqual(header_count, 5)

    def test_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=2)
            rc, out, _ = self._run_cli([
                "--from-sqlite",
                "--limit", "2",
                "--db-path", str(db_path),
                "--simulate-confirm",
                "--json",
            ])
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual(len(data["rows"]), 2)
            self.assertFalse(data["safety"]["real_llm_calls_made"])

    def test_missing_analysis_id_returns_one(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "src.db"
            _seed_sqlite(db_path, num_rows=1)
            rc, _, err = self._run_cli([
                "--analysis-id", "9999",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 1)
            self.assertIn("9999", err)

    def test_missing_db_returns_one(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "does_not_exist.db"
            # Create a stub empty DB without the table to provoke an
            # OperationalError in the reader (the SQLAlchemy default
            # connect to a missing file would silently create one).
            sqlite3.connect(db_path).close()
            rc, _, err = self._run_cli([
                "--analysis-id", "1",
                "--db-path", str(db_path),
            ])
            self.assertEqual(rc, 1)
            self.assertIn("error", err.lower())


# ---------------------------------------------------------------------------
# Static checks — module shape contracts.
# ---------------------------------------------------------------------------


class ModuleLevelStaticChecks(unittest.TestCase):
    def setUp(self):
        self.module_path = _PROJECT_ROOT / "llm_judge.py"
        self.source = self.module_path.read_text(encoding="utf-8")

    def test_no_openai_or_anthropic_module_imports(self):
        for needle in ("openai", "anthropic"):
            pattern = re.compile(
                rf"^(?:from\s+{needle}\b|import\s+{needle}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"llm_judge.py must not import {needle} at module level",
            )

    def test_no_network_io_imports(self):
        for needle in ("requests", "httpx", "urllib.request", "socket"):
            pattern = re.compile(
                rf"^(?:from\s+{re.escape(needle)}\b|import\s+{re.escape(needle)}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"llm_judge.py must not import {needle}",
            )

    def test_imported_by_main_under_m13_1b(self):
        """M13.1b: main.py NOW imports llm_judge — that is the whole
        point of this milestone. Previously (M13.1a) main.py was
        forbidden from importing it; the contract is inverted here."""
        forbidden = re.compile(
            r"^(?:from\s+llm_judge\b|import\s+llm_judge\b)",
            re.MULTILINE,
        )
        main_source = (_PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIsNotNone(
            forbidden.search(main_source),
            msg=(
                "main.py MUST import llm_judge under M13.1b — the "
                "pipeline wiring lives in _process_news_item_phase_a"
            ),
        )

    def test_not_imported_by_other_entry_points(self):
        """api_server.py / scheduler.py / job_manager.py go through
        analyze_pipeline; they must not import llm_judge directly so
        the LLM caller surface stays inside main.py."""
        forbidden = re.compile(
            r"^(?:from\s+llm_judge\b|import\s+llm_judge\b)",
            re.MULTILINE,
        )
        for filename in ("api_server.py", "scheduler.py", "job_manager.py"):
            path = _PROJECT_ROOT / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                forbidden.search(text),
                msg=(
                    f"{filename} must not import llm_judge directly "
                    "— the judge runs inside main.analyze_pipeline"
                ),
            )

    def test_label_severity_rank_matches_documented_set(self):
        expected_labels = {
            "draft_unverified",
            "draft_needs_context",
            "draft_needs_review",
            "draft_needs_official_confirmation",
            "draft_disputed",
            "draft_high_risk_review",
            "draft_likely_true",
            "draft_verified",
        }
        self.assertSetEqual(
            set(llm_judge.LABEL_SEVERITY_RANK.keys()),
            expected_labels,
        )
        # Rank ordering pin: verified is the maximum, unverified the
        # minimum, likely_true sits between.
        ranks = llm_judge.LABEL_SEVERITY_RANK
        self.assertEqual(ranks["draft_unverified"], 0)
        self.assertEqual(ranks["draft_verified"], max(ranks.values()))
        self.assertLess(
            ranks["draft_likely_true"], ranks["draft_verified"],
        )
        self.assertLess(
            ranks["draft_needs_context"], ranks["draft_likely_true"],
        )

    def test_allowed_actions_documented(self):
        self.assertSetEqual(
            set(llm_judge.ALLOWED_JUDGE_ACTIONS),
            {"confirm", "downgrade", "flag_for_review"},
        )


# ---------------------------------------------------------------------------
# Integration smoke — JudgeInput from a real-shaped row + provider
# round-trip that produces a downgrade.
# ---------------------------------------------------------------------------


class IntegrationSmokeTests(unittest.TestCase):
    def test_judge_input_built_from_realistic_row(self):
        """Build a JudgeInput from a row shaped like the M11.0b ID=105
        weak-evidence pattern (verified label with confidence_score=10,
        verification_strength=none, no official sources). Drive it
        through the simulated downgrade provider; expect downgrade to
        draft_needs_context."""
        row = {
            "id": 105,
            "verdict_label": "draft_verified",
            "policy_confidence_score": 10,
            "verification_strength": "none",
            "claim_text": "해당 과제에는 피해 지원 기간을 연장한다",
            "evidence_summary": "official source not found",
            "contradiction_summary": "",
            "bias_framing_summary": "",
            "source_candidates": json.dumps([]),
        }
        judge_input = llm_judge.JudgeInput(
            current_label=row["verdict_label"],
            policy_confidence_score=row["policy_confidence_score"],
            verification_strength=row["verification_strength"],
            claim_text=row["claim_text"],
            official_sources_count=0,
            evidence_summary=row["evidence_summary"],
            contradiction_summary=row["contradiction_summary"],
            bias_framing_summary=row["bias_framing_summary"],
        )
        provider = _FakeProviderForTests(
            "simulated_downgrade",
            json.dumps({
                "action": "downgrade",
                "new_label": "draft_needs_context",
                "reason_ko": "공식 출처 없음, 점수 낮음",
                "evidence_gaps": ["official_source_missing"],
            }, ensure_ascii=False),
        )
        verdict = llm_judge.run_judge(
            judge_input, providers=[provider],
        )
        self.assertEqual(verdict.action, "downgrade")
        self.assertEqual(verdict.new_label, "draft_needs_context")
        self.assertEqual(
            verdict.evidence_gaps, ["official_source_missing"],
        )
        # Safety pins survive serialisation.
        out = llm_judge.judge_verdict_to_dict(verdict)
        self.assertIs(out["truth_claim"], False)
        self.assertIs(out["operator_review_required"], True)


if __name__ == "__main__":
    unittest.main()
