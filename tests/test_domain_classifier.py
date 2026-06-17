"""CLASSIFY-2a: tests for domain_classifier.classify_domain.

Fully offline — the tool-free Anthropic call (``_call_anthropic_tool_free``) is
monkeypatched with canned message objects, so no real API call fires. Verifies:
    * a clean single-label reply -> that label,
    * a stray-text reply ("Label: welfare") -> the parsed label,
    * junk / none-fit -> 기타-미분류 (fallback),
    * a raised exception inside the call -> 기타-미분류 (NEVER raises),
    * empty title -> 기타-미분류 with NO API call,
    * missing ANTHROPIC_API_KEY -> 기타-미분류 with NO API call.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import domain_classifier as dc  # noqa: E402


class _Usage:
    input_tokens = 120
    output_tokens = 3


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Msg:
    """Minimal stand-in for the Anthropic SDK message object."""
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()


class DomainClassifierTests(unittest.TestCase):
    def setUp(self):
        self._orig_call = dc._call_anthropic_tool_free
        self._orig_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-a-real-secret"

    def tearDown(self):
        dc._call_anthropic_tool_free = self._orig_call
        if self._orig_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._orig_key

    def _patch(self, fn):
        dc._call_anthropic_tool_free = fn

    def test_clean_label(self):
        self._patch(lambda prompt, model, key: _Msg("finance"))
        self.assertEqual(
            dc.classify_domain("어떤 금융 정책", "대출 금리 인하"), "finance",
        )

    def test_realestate_recovered(self):
        # The label the keyword hint missed in CLASSIFY-PROBE.
        self._patch(lambda prompt, model, key: _Msg("realestate"))
        self.assertEqual(dc.classify_domain("전세 대책 발표", None), "realestate")

    def test_stray_text_label(self):
        self._patch(lambda prompt, model, key: _Msg("Label: welfare"))
        self.assertEqual(dc.classify_domain("복지 지원금 확대", None), "welfare")

    def test_junk_reply_returns_fallback(self):
        self._patch(lambda prompt, model, key: _Msg("I cannot determine that."))
        self.assertEqual(dc.classify_domain("some title", None), "기타-미분류")

    def test_korean_fallback_reply(self):
        self._patch(lambda prompt, model, key: _Msg("기타-미분류"))
        self.assertEqual(dc.classify_domain("애매한 제목", None), "기타-미분류")

    def test_exception_never_raises(self):
        def boom(prompt, model, key):
            raise RuntimeError("anthropic api down")
        self._patch(boom)
        # Must FAIL-SOFT to the fallback, never propagate.
        self.assertEqual(dc.classify_domain("title", "claim"), "기타-미분류")

    def test_empty_title_makes_no_call(self):
        def boom(prompt, model, key):
            raise AssertionError("classify_domain must not call the API on empty title")
        self._patch(boom)
        self.assertEqual(dc.classify_domain("", None), "기타-미분류")
        self.assertEqual(dc.classify_domain("   ", None), "기타-미분류")

    def test_missing_api_key_makes_no_call(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

        def boom(prompt, model, key):
            raise AssertionError("classify_domain must not call the API without a key")
        self._patch(boom)
        self.assertEqual(dc.classify_domain("title", None), "기타-미분류")

    def test_label_set_is_the_ten_taxonomy(self):
        self.assertEqual(len(dc.LABELS), 10)
        for expected in ("finance", "realestate", "기타-미분류"):
            self.assertIn(expected, dc.LABELS)

    def test_parse_label_unit(self):
        self.assertEqual(dc._parse_label("finance"), "finance")
        self.assertEqual(dc._parse_label('"welfare"'), "welfare")
        self.assertEqual(dc._parse_label("Label: SMB"), "SMB")
        self.assertEqual(dc._parse_label("nothing here"), "기타-미분류")


if __name__ == "__main__":
    unittest.main()
