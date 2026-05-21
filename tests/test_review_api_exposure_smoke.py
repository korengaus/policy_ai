"""Phase 2 M8.8: tests for ``scripts/smoke_review_api_exposure.py``.

Every test exercises the pure ``run_exposure_smoke`` entry point with
an injected ``fetch_fn`` so no real HTTP request fires. No network, no
Render, no OpenAI, no token.

Covers the M8.8 spec cases A–J:
    A. 503 disabled responses pass in --expect-disabled mode
    B. 403 token-required responses fail expectation in --expect-disabled
       mode but report public_access_detected=false
    C. 403 token-required responses pass in --expect-token-required mode
    D. mixed 503 + 403 pass in --allow-disabled-or-token-required mode
    E. any 2xx response without token fails hard in every mode and
       public_access_detected=true
    F. unexpected 404/405/500 responses are reported as unexpected and
       fail
    G. POST bodies contain no token and no secrets
    H. Output JSON contains no token / secret-like values (sk-, OPENAI_API_KEY,
       REVIEW_API_TOKEN, X-Review-Token value)
    I. timeout / base URL normalization
    J. CLI requires expectation mode
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.smoke_review_api_exposure as smoke  # noqa: E402


SCRIPT_PATH = ROOT / "scripts" / "smoke_review_api_exposure.py"

DISABLED_BODY = '{"detail": "Review API is disabled. Set REVIEW_API_ENABLED=true ..."}'
FORBIDDEN_BODY = '{"detail": "Missing or invalid X-Review-Token header."}'


def _all_disabled(method, url, body):
    return (503, DISABLED_BODY, None)


def _all_token_required(method, url, body):
    return (403, FORBIDDEN_BODY, None)


def _mixed_safe(method, url, body):
    # GETs disabled, POSTs token-required — both are safe gates.
    if method == "GET":
        return (503, DISABLED_BODY, None)
    return (403, FORBIDDEN_BODY, None)


def _one_public(method, url, body):
    if url.endswith("/review/tasks"):
        return (200, '{"tasks": [], "count": 0}', None)
    return (503, DISABLED_BODY, None)


def _all_404(method, url, body):
    return (404, '{"detail": "Not Found"}', None)


def _all_500(method, url, body):
    return (500, '{"detail": "Internal Server Error"}', None)


# ---------------------------------------------------------------------------
# A — 503 disabled passes in --expect-disabled
# ---------------------------------------------------------------------------


class ExpectDisabledTests(unittest.TestCase):
    def test_all_disabled_passes_in_expect_disabled(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_DISABLED,
            fetch_fn=_all_disabled,
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.public_access_detected)
        self.assertEqual(result.disabled_count, len(smoke.ENDPOINTS))
        self.assertEqual(result.token_required_count, 0)
        self.assertEqual(result.unexpected_count, 0)
        self.assertEqual(result.expectation_mismatch_count, 0)
        self.assertEqual(result.errors, [])
        self.assertIn("PASS", result.recommendation)

    def test_token_required_fails_expectation_in_expect_disabled(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_DISABLED,
            fetch_fn=_all_token_required,
        )
        # Public exposure is NOT detected — the gate works — but the
        # expectation does not match, so the run fails.
        self.assertFalse(result.passed)
        self.assertFalse(result.public_access_detected)
        self.assertEqual(result.token_required_count, len(smoke.ENDPOINTS))
        self.assertEqual(result.expectation_mismatch_count, len(smoke.ENDPOINTS))
        # Recommendation explicitly says "no public exposure" and explains
        # the mismatch.
        self.assertIn("MISMATCH", result.recommendation)
        self.assertIn("No public exposure", result.recommendation)


# ---------------------------------------------------------------------------
# C — 403 passes in --expect-token-required
# ---------------------------------------------------------------------------


class ExpectTokenRequiredTests(unittest.TestCase):
    def test_all_token_required_passes(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_TOKEN_REQUIRED,
            fetch_fn=_all_token_required,
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.public_access_detected)
        self.assertEqual(result.token_required_count, len(smoke.ENDPOINTS))
        self.assertEqual(result.expectation_mismatch_count, 0)
        self.assertIn("PASS", result.recommendation)

    def test_disabled_is_mismatch_in_expect_token_required(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_TOKEN_REQUIRED,
            fetch_fn=_all_disabled,
        )
        self.assertFalse(result.passed)
        self.assertFalse(result.public_access_detected)
        self.assertEqual(result.disabled_count, len(smoke.ENDPOINTS))
        self.assertEqual(result.expectation_mismatch_count, len(smoke.ENDPOINTS))
        self.assertIn("MISMATCH", result.recommendation)


# ---------------------------------------------------------------------------
# D — mixed 503 + 403 in --allow-disabled-or-token-required
# ---------------------------------------------------------------------------


class AllowEitherTests(unittest.TestCase):
    def test_mixed_503_and_403_passes(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.ALLOW_EITHER,
            fetch_fn=_mixed_safe,
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.public_access_detected)
        # The endpoint list has 3 GETs + 2 POSTs.
        self.assertEqual(result.disabled_count, 3)
        self.assertEqual(result.token_required_count, 2)
        self.assertEqual(result.unexpected_count, 0)
        self.assertEqual(result.expectation_mismatch_count, 0)


# ---------------------------------------------------------------------------
# E — any 2xx without token is hard fail in every mode
# ---------------------------------------------------------------------------


class PublicAccessTests(unittest.TestCase):
    def _assert_public_failure(self, mode):
        result = smoke.run_exposure_smoke(
            "https://example.invalid", mode, fetch_fn=_one_public,
        )
        self.assertFalse(
            result.passed, f"mode={mode} unexpectedly passed despite 2xx",
        )
        self.assertTrue(
            result.public_access_detected,
            f"mode={mode} failed to flag public_access_detected",
        )
        self.assertIn("FAIL", result.recommendation)
        self.assertIn("public-exposure incident", result.recommendation)

    def test_public_access_fails_in_expect_disabled(self):
        self._assert_public_failure(smoke.EXPECT_DISABLED)

    def test_public_access_fails_in_expect_token_required(self):
        self._assert_public_failure(smoke.EXPECT_TOKEN_REQUIRED)

    def test_public_access_fails_in_allow_either(self):
        self._assert_public_failure(smoke.ALLOW_EITHER)


# ---------------------------------------------------------------------------
# F — unexpected statuses fail
# ---------------------------------------------------------------------------


class UnexpectedStatusTests(unittest.TestCase):
    def test_404_is_unexpected_and_fails(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.ALLOW_EITHER,
            fetch_fn=_all_404,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.unexpected_count, len(smoke.ENDPOINTS))
        self.assertFalse(result.public_access_detected)
        self.assertIn("unexpected", result.recommendation.lower())

    def test_500_is_unexpected_and_fails(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_DISABLED,
            fetch_fn=_all_500,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.unexpected_count, len(smoke.ENDPOINTS))

    def test_503_without_disabled_marker_is_unexpected(self):
        # A 503 from an unrelated upstream (e.g. proxy) without the
        # disabled-body marker should NOT count as a safe gate.
        def fetch(method, url, body):
            return (503, "<html>Service Unavailable</html>", None)

        result = smoke.run_exposure_smoke(
            "https://example.invalid", smoke.ALLOW_EITHER, fetch_fn=fetch,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.unexpected_count, len(smoke.ENDPOINTS))
        self.assertEqual(result.disabled_count, 0)


# ---------------------------------------------------------------------------
# G — POST bodies are safe (no token, no secrets)
# ---------------------------------------------------------------------------


class PostBodyTests(unittest.TestCase):
    def test_post_bodies_contain_no_token_or_secret_fields(self):
        # The bodies intentionally include the literal phrase "no token"
        # in the decision comment for human readability of server logs.
        # The safety check is that nothing *token-shaped* (hex / base64 /
        # SDK prefix / header field) or secret-named appears.
        for body in (
            smoke.SYNTHETIC_FROM_RESULT_BODY,
            smoke.SYNTHETIC_DECISION_BODY,
        ):
            text = json.dumps(body)
            for needle in (
                "REVIEW_API_TOKEN", "X-Review-Token", "Authorization",
                "Bearer", "OPENAI_API_KEY",
            ):
                self.assertNotIn(
                    needle, text,
                    msg=f"POST body unexpectedly contains {needle!r}: {text}",
                )
            # OpenAI-key prefix sk-<alnum>
            self.assertFalse(
                __import__("re").search(r"sk-[A-Za-z0-9]{16,}", text),
                msg=f"POST body unexpectedly contains an SDK-key prefix: {text}",
            )

    def test_recorded_body_observable_via_fetch_fn(self):
        # The fetch_fn receives the body it would send — assert nothing
        # secret-shaped is injected by the run. We allow the literal word
        # "token" in the human-readable comment ("no token") because the
        # comment is for server logs, not authentication material.
        import re as _re
        seen = []

        def fetch(method, url, body):
            if body is not None:
                seen.append(json.dumps(body, ensure_ascii=False))
            return (503, DISABLED_BODY, None)

        smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_DISABLED,
            fetch_fn=fetch,
        )
        joined = "\n".join(seen)
        for needle in ("Bearer", "OPENAI_API_KEY", "REVIEW_API_TOKEN",
                       "X-Review-Token", "Authorization"):
            self.assertNotIn(
                needle, joined,
                f"POST body unexpectedly contains {needle!r}",
            )
        # No long hex / base64 literal anywhere in any body.
        self.assertFalse(_re.search(r"[0-9a-fA-F]{32,}", joined),
                         f"POST body carries a hex token literal: {joined!r}")
        self.assertFalse(_re.search(r"sk-[A-Za-z0-9]{16,}", joined),
                         f"POST body carries an SDK key prefix: {joined!r}")


# ---------------------------------------------------------------------------
# H — JSON output carries no secret-like values
# ---------------------------------------------------------------------------


class JSONOutputTests(unittest.TestCase):
    EXPECTED_KEYS = {
        "passed", "base_url", "expectation_mode", "endpoints_checked",
        "public_access_detected", "disabled_count", "token_required_count",
        "unexpected_count", "expectation_mismatch_count",
        "results", "warnings", "errors", "recommendation",
    }

    def test_json_payload_has_stable_keys(self):
        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.EXPECT_DISABLED,
            fetch_fn=_all_disabled,
        )
        payload = smoke.smoke_to_dict(result)
        self.assertEqual(set(payload.keys()), self.EXPECTED_KEYS)

    def test_json_payload_no_secret_like_values(self):
        # Synthesize a fetch that returns a hex token in the body to
        # make sure the smoke redacts/scrubs it before reporting.
        import re as _re
        leaky_body = (
            '{"detail": "internal: token=deadbeefcafebabe1234567890abcdef1234567890abcdef"}'
        )

        def fetch(method, url, body):
            return (503, leaky_body, None)

        result = smoke.run_exposure_smoke(
            "https://example.invalid",
            smoke.ALLOW_EITHER,
            fetch_fn=fetch,
        )
        body = json.dumps(smoke.smoke_to_dict(result))
        # Long hex token should be redacted.
        self.assertNotIn(
            "deadbeefcafebabe1234567890abcdef1234567890abcdef", body,
            "smoke output must not echo a long hex token literal",
        )
        # No leftover hex / base64 literals anywhere in the payload.
        self.assertFalse(_re.search(r"[0-9a-fA-F]{32,}", body),
                         "smoke output must not echo any 32+-char hex literal")
        # Standard secret-shaped literals never appear.
        for needle in ("OPENAI_API_KEY", "REVIEW_API_TOKEN"):
            self.assertNotIn(
                needle, body,
                msg=f"smoke output unexpectedly contained {needle!r}",
            )
        # OpenAI key prefix `sk-<alnum>` (the bare `sk-` substring is
        # too noisy because it matches `task-id` in URL paths).
        self.assertFalse(
            _re.search(r"sk-[A-Za-z0-9]{16,}", body),
            msg=f"smoke output unexpectedly contained an SDK key prefix: {body}",
        )


# ---------------------------------------------------------------------------
# I — base URL normalization + timeout handling
# ---------------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    def test_trailing_slash_stripped(self):
        seen_urls = []

        def fetch(method, url, body):
            seen_urls.append(url)
            return (503, DISABLED_BODY, None)

        smoke.run_exposure_smoke(
            "https://example.invalid////",
            smoke.EXPECT_DISABLED,
            fetch_fn=fetch,
        )
        # No double slash anywhere in the synthesized URL (except after https:).
        for url in seen_urls:
            self.assertNotIn(
                "invalid//review", url,
                f"double slash leaked into URL: {url}",
            )
            self.assertTrue(
                url.startswith("https://example.invalid/review/"),
                f"normalized URL prefix wrong: {url}",
            )

    def test_normalize_helper_preserves_scheme(self):
        self.assertEqual(
            smoke._normalize_base_url("https://example.invalid/"),
            "https://example.invalid",
        )
        self.assertEqual(
            smoke._normalize_base_url("http://localhost:8000//"),
            "http://localhost:8000",
        )


# ---------------------------------------------------------------------------
# J — CLI behavior: expectation mode is required
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def _run_main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        # main() calls argparse which raises SystemExit(2) on bad args.
        rc_holder = {"rc": None}
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc_holder["rc"] = smoke.main(argv)
        except SystemExit as e:
            rc_holder["rc"] = int(e.code) if e.code is not None else 0
        return rc_holder["rc"], out.getvalue(), err.getvalue()

    def test_missing_expectation_mode_rejected(self):
        rc, _stdout, stderr = self._run_main([
            "--base-url", "https://example.invalid",
        ])
        self.assertEqual(rc, 2,
                         f"expected exit 2 on missing mode; got {rc} stderr={stderr!r}")

    def test_missing_base_url_rejected(self):
        rc, _, _ = self._run_main(["--expect-disabled"])
        self.assertEqual(rc, 2)

    def test_two_modes_rejected(self):
        rc, _, _ = self._run_main([
            "--base-url", "https://example.invalid",
            "--expect-disabled",
            "--expect-token-required",
        ])
        self.assertEqual(rc, 2)

    def test_negative_timeout_rejected(self):
        rc, _, _ = self._run_main([
            "--base-url", "https://example.invalid",
            "--expect-disabled",
            "--timeout-seconds", "0",
        ])
        self.assertEqual(rc, 2)


# ---------------------------------------------------------------------------
# Static safety check — no token/secret literal in the script source
# ---------------------------------------------------------------------------


class ScriptSafetyTests(unittest.TestCase):
    def test_script_never_sends_x_review_token_header(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        # The exposure smoke intentionally NEVER builds an X-Review-Token
        # header. The class constant ``REVIEW_TOKEN_HEADER`` should not
        # appear anywhere as a key being assigned in a headers dict.
        # Allow it in prose / docstrings; disallow it as a dict key.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                '"X-Review-Token":', line,
                f"X-Review-Token must never be sent as a header; line: {line!r}",
            )

    def test_script_does_not_import_openai_or_verdict_modules(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "policy_decision", "policy_scoring", "verification_card",
            "review_workflow", "review_auth",
            "requests", "httpx",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"smoke_review_api_exposure.py must not import {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main()
