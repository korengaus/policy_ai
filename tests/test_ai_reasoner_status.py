"""Minimal smoke tests for ai_reasoner status behavior.

Run with: python tests/test_ai_reasoner_status.py
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_reasoner
from config import describe_ai_config


def _call_run_ai_reasoning():
    return ai_reasoner.run_ai_reasoning(
        news_title="t",
        news_summary="s",
        article_body="b",
        policy_claims=[],
        memory_context="",
    )


class TestAiReasonerStatus(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.get("OPENAI_API_KEY")

    def tearDown(self):
        if self._saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved_key

    def test_missing_api_key_returns_unavailable(self):
        os.environ.pop("OPENAI_API_KEY", None)
        result = _call_run_ai_reasoning()
        self.assertFalse(result["ai_available"])
        self.assertEqual(result["ai_status"], "unavailable")
        self.assertIn(
            result["ai_status_reason"],
            {"missing_api_key", "openai_package_missing"},
        )
        self.assertIn("ai_model", result)

    def test_client_exception_returns_error(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        fake_client = MagicMock()
        fake_client.responses.create.side_effect = RuntimeError("boom")
        with patch("ai_reasoner.get_openai_client", return_value=(fake_client, None)):
            result = _call_run_ai_reasoning()
        self.assertFalse(result["ai_available"])
        self.assertEqual(result["ai_status"], "error")
        self.assertEqual(result["ai_status_reason"], "api_call_failed")
        self.assertIn("ai_model", result)

    def test_invalid_json_returns_error_with_specific_reason(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_response.output_text = "not json"
        fake_client.responses.create.return_value = fake_response
        with patch("ai_reasoner.get_openai_client", return_value=(fake_client, None)):
            result = _call_run_ai_reasoning()
        self.assertFalse(result["ai_available"])
        self.assertEqual(result["ai_status"], "error")
        self.assertEqual(result["ai_status_reason"], "invalid_json_response")

    def test_successful_call_returns_ok(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_response.output_text = '{"one_line_summary": "s"}'
        fake_client.responses.create.return_value = fake_response
        with patch("ai_reasoner.get_openai_client", return_value=(fake_client, None)):
            result = _call_run_ai_reasoning()
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["ai_status"], "ok")
        self.assertEqual(result["ai_status_reason"], "ok")
        self.assertIn("ai_model", result)

    def test_describe_ai_config_reports_model_and_key_presence(self):
        snapshot = describe_ai_config()
        self.assertIn("ai_model", snapshot)
        self.assertIn("ai_api_key_present", snapshot)
        self.assertIn("ai_model_default", snapshot)
        self.assertIsInstance(snapshot["ai_api_key_present"], bool)


if __name__ == "__main__":
    unittest.main()
