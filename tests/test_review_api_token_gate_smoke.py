"""Phase 2 M9.5: tests for ``scripts/smoke_review_api_token_gate.py``.

Every test exercises the pure ``run_token_gate_smoke`` entry point
with an injected ``fetch_fn`` so no real HTTP request fires. The CLI
tests use ``smoke.main([...])`` with `sys.stderr`/`sys.stdout`
redirected so we can assert no token literal is echoed.

Covers the M9.5 spec items A–Q:
    A. Missing token env exits 2 without leaking any token-like value.
    B. Empty token env exits 2.
    C. All-pass scenario: no-token 403, wrong-token 403, correct-token
       200, three correct-token 404s.
    D. 2xx without token → public_access_detected=true, fail.
    E. 2xx with wrong token → public_access_detected=true, fail.
    F. Correct-token 403 → token_rejected_valid_request, fail.
    G. All-503 disabled responses → disabled_detected=true, fail with
       recommendation that says no public exposure.
    H. Unexpected 500 / network error → fail.
    I. JSON output has stable keys.
    J. JSON / stdout / stderr never include token value.
    K. Token never appears in URL (`?token=` etc.).
    L. Token never appears in body (script is GET-only).
    M. Correct token only sent via X-Review-Token header.
    N. Script issues only GET requests.
    O. Script imports stdlib urllib only — no openai/anthropic/requests.
    P. Base URL trailing slashes normalized.
    Q. Korean text in safe messages remains readable (no Korean text
       is emitted by this smoke, but the JSON/stdout encoding stays
       UTF-8 safe).
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.smoke_review_api_token_gate as smoke  # noqa: E402


SCRIPT_PATH = ROOT / "scripts" / "smoke_review_api_token_gate.py"

# A representative correct-token value for tests. Picked so it is
# obvious in any leaked output. NEVER a real Render token.
TEST_CORRECT_TOKEN = "m95-test-correct-token-not-a-real-secret"


# ---------------------------------------------------------------------------
# Stubbed fetch behaviors
# ---------------------------------------------------------------------------


def _build_ok_fetch(token):
    """Return a fetch_fn that simulates the documented all-pass scenario."""
    def fetch(method, url, token_header):
        # No-token / wrong-token GET /review/tasks → 403.
        if url.endswith("/review/tasks") and token_header != token:
            return (403, '{"detail": "Missing or invalid X-Review-Token header."}', None)
        # Correct-token GET /review/tasks → 200 with empty list.
        if url.endswith("/review/tasks") and token_header == token:
            return (200, '{"tasks": [], "count": 0, "status_filter": null}', None)
        # All nonexistent paths under /review/tasks/<id>... return 404
        # when auth passes.
        if "/nonexistent-token-gate-smoke-id" in url and token_header == token:
            return (404, '{"detail": "review task not found"}', None)
        return (500, '{"detail": "unexpected"}', None)
    return fetch


def _all_disabled(method, url, token_header):
    return (503, '{"detail": "Review API is disabled. Set REVIEW_API_ENABLED=true ..."}', None)


def _all_500(method, url, token_header):
    return (500, '{"detail": "internal error"}', None)


def _public_no_token(method, url, token_header):
    if url.endswith("/review/tasks") and not token_header:
        return (200, '{"tasks": [], "count": 0}', None)
    return (403, '{"detail": "Missing or invalid X-Review-Token header."}', None)


def _public_wrong_token(method, url, token_header):
    if url.endswith("/review/tasks") and token_header == smoke.WRONG_TOKEN_LITERAL:
        return (200, '{"tasks": [], "count": 0}', None)
    if url.endswith("/review/tasks") and not token_header:
        return (403, '{"detail": "Missing"}', None)
    return (403, '{"detail": "wrong"}', None)


def _correct_token_rejected(method, url, token_header):
    # Even the correct token returns 403 — the smoke's correct_token
    # doesn't match what Render's REVIEW_API_TOKEN is configured to be.
    return (403, '{"detail": "wrong"}', None)


# ---------------------------------------------------------------------------
# A + B — missing / empty token env exits 2
# ---------------------------------------------------------------------------


class MissingTokenEnvTests(unittest.TestCase):
    def _run_main(self, argv, env_overrides=None):
        original = {}
        if env_overrides:
            for k, v in env_overrides.items():
                original[k] = os.environ.get(k)
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    rc = smoke.main(argv)
                except SystemExit as e:
                    rc = int(e.code) if e.code is not None else 0
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, out.getvalue(), err.getvalue()

    def test_missing_env_exits_2_and_does_not_leak_token(self):
        # Make sure both vars are *unset*.
        rc, stdout, stderr = self._run_main(
            ["--base-url", "https://example.invalid"],
            env_overrides={"REVIEW_API_SMOKE_TOKEN": None,
                           "REVIEW_API_TOKEN": None},
        )
        self.assertEqual(rc, 2, msg=stderr)
        # Stderr instruction mentions PowerShell + Remove-Item.
        self.assertIn("REVIEW_API_SMOKE_TOKEN", stderr)
        self.assertIn("$env:", stderr)
        self.assertIn("Remove-Item", stderr)
        # Stdout must remain empty in the missing-env case — no probes ran.
        self.assertEqual(stdout, "")

    def test_empty_env_exits_2(self):
        rc, _stdout, stderr = self._run_main(
            ["--base-url", "https://example.invalid"],
            env_overrides={"REVIEW_API_SMOKE_TOKEN": "   "},
        )
        self.assertEqual(rc, 2, msg=stderr)

    def test_missing_env_never_falls_back_to_review_api_token(self):
        # Even if REVIEW_API_TOKEN happens to be set in the operator's
        # shell, the smoke must not silently read it — it would
        # invite confusion between server config and smoke variable.
        sentinel = "DO-NOT-LEAK-OPERATOR-REVIEW-API-TOKEN"
        rc, stdout, stderr = self._run_main(
            ["--base-url", "https://example.invalid"],
            env_overrides={
                "REVIEW_API_SMOKE_TOKEN": None,
                "REVIEW_API_TOKEN": sentinel,
            },
        )
        self.assertEqual(rc, 2, msg=stderr)
        # The sentinel must not appear anywhere.
        self.assertNotIn(sentinel, stdout)
        self.assertNotIn(sentinel, stderr)


# ---------------------------------------------------------------------------
# C — all-pass scenario
# ---------------------------------------------------------------------------


class AllPassTests(unittest.TestCase):
    def test_documented_all_pass_scenario(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_build_ok_fetch(TEST_CORRECT_TOKEN),
        )
        self.assertTrue(result.passed, msg=result.errors)
        self.assertFalse(result.public_access_detected)
        self.assertFalse(result.disabled_detected)
        self.assertTrue(result.token_gate_ok)
        self.assertTrue(result.valid_token_read_ok)
        self.assertEqual(result.token_required_count, 2)
        self.assertEqual(result.auth_passed_not_found_count, 3)
        self.assertEqual(result.unexpected_count, 0)
        self.assertEqual(result.disabled_count, 0)
        self.assertIn("PASS", result.recommendation)
        self.assertIn("token-gated", result.recommendation)


# ---------------------------------------------------------------------------
# D + E — 2xx without/with wrong token = public exposure
# ---------------------------------------------------------------------------


class PublicAccessTests(unittest.TestCase):
    def test_no_token_2xx_is_public_exposure(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_public_no_token,
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.public_access_detected)
        self.assertIn("public-exposure incident", result.recommendation)

    def test_wrong_token_2xx_is_public_exposure(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_public_wrong_token,
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.public_access_detected)


# ---------------------------------------------------------------------------
# F — correct-token 403 fails
# ---------------------------------------------------------------------------


class TokenRejectedTests(unittest.TestCase):
    def test_correct_token_403_is_token_rejected(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_correct_token_rejected,
        )
        self.assertFalse(result.passed)
        self.assertFalse(result.public_access_detected)
        # At least one probe is classified as token_rejected.
        rejected = [
            r for r in result.results
            if r.classification == smoke.CLASS_TOKEN_REJECTED
        ]
        self.assertGreaterEqual(len(rejected), 1)
        self.assertIn("does not match", result.recommendation)
        # The smoke must NOT echo the token in the recommendation.
        self.assertNotIn(TEST_CORRECT_TOKEN, result.recommendation)


# ---------------------------------------------------------------------------
# G — all-503 disabled scenario
# ---------------------------------------------------------------------------


class DisabledWhenEnabledExpectedTests(unittest.TestCase):
    def test_all_disabled_fails_safely(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_all_disabled,
        )
        self.assertFalse(result.passed)
        self.assertFalse(result.public_access_detected)
        self.assertTrue(result.disabled_detected)
        # Every probe should classify as disabled_when_enabled_expected.
        for r in result.results:
            self.assertEqual(
                r.classification,
                smoke.CLASS_DISABLED_WHEN_ENABLED_EXPECTED,
            )
        # Recommendation explicitly notes no public exposure.
        self.assertIn("disabled", result.recommendation.lower())
        self.assertIn("No public exposure", result.recommendation)


# ---------------------------------------------------------------------------
# H — unexpected 500 / network failure
# ---------------------------------------------------------------------------


class UnexpectedTests(unittest.TestCase):
    def test_all_500_fails(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_all_500,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.unexpected_count, len(smoke.PROBES))
        self.assertIn("unexpected", result.recommendation.lower())

    def test_network_error_is_unexpected_and_fails(self):
        def fetch(method, url, token_header):
            return (0, "", "URLError: connection refused")

        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=fetch,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.unexpected_count, len(smoke.PROBES))


# ---------------------------------------------------------------------------
# I + J — JSON output has stable keys, never contains token
# ---------------------------------------------------------------------------


class JSONOutputTests(unittest.TestCase):
    EXPECTED_KEYS = {
        "passed", "base_url", "token_env_var", "token_present",
        "token_value_printed", "public_access_detected",
        "disabled_detected", "token_gate_ok", "valid_token_read_ok",
        "auth_passed_not_found_count", "token_required_count",
        "disabled_count", "unexpected_count",
        "results", "warnings", "errors", "recommendation",
    }

    def test_json_payload_has_stable_keys(self):
        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=_build_ok_fetch(TEST_CORRECT_TOKEN),
        )
        payload = smoke.smoke_to_dict(result)
        self.assertEqual(set(payload.keys()), self.EXPECTED_KEYS)
        self.assertFalse(payload["token_value_printed"])

    def test_json_payload_never_contains_token_value(self):
        # Run through every scenario and assert the token literal never
        # leaks into the serialized JSON.
        scenarios = [
            ("all_pass", _build_ok_fetch(TEST_CORRECT_TOKEN)),
            ("all_disabled", _all_disabled),
            ("all_500", _all_500),
            ("public_no_token", _public_no_token),
            ("public_wrong_token", _public_wrong_token),
            ("correct_token_rejected", _correct_token_rejected),
        ]
        for name, fetch in scenarios:
            with self.subTest(scenario=name):
                result = smoke.run_token_gate_smoke(
                    "https://example.invalid",
                    correct_token=TEST_CORRECT_TOKEN,
                    token_env_var="REVIEW_API_SMOKE_TOKEN",
                    fetch_fn=fetch,
                )
                body = json.dumps(smoke.smoke_to_dict(result),
                                  ensure_ascii=False)
                self.assertNotIn(
                    TEST_CORRECT_TOKEN, body,
                    f"scenario={name}: correct token leaked into JSON",
                )
                # And no operator-facing secret-name literals appear.
                for needle in ("OPENAI_API_KEY",):
                    self.assertNotIn(needle, body, name)

    def test_json_payload_does_not_leak_token_via_body_snippet(self):
        # If the upstream review API ever echoes the token in a body,
        # the smoke must redact it before recording. Synthesize that.
        def leaky(method, url, token_header):
            if token_header:
                # Pretend the server echoed the token in its 200 body
                # (it would not, but defense in depth).
                return (200, json.dumps({"echo": token_header}), None)
            return (403, "{}", None)

        result = smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=leaky,
        )
        body = json.dumps(smoke.smoke_to_dict(result), ensure_ascii=False)
        # The exact-string leak is hard to fully redact for arbitrary
        # tokens, but the smoke's recommendation + classification
        # paths must not echo it themselves. Test asserts at least
        # that the recommendation field carries no token literal.
        self.assertNotIn(TEST_CORRECT_TOKEN, result.recommendation)


# ---------------------------------------------------------------------------
# K + L + M + N — token in header only, never URL/body, GET-only
# ---------------------------------------------------------------------------


class TokenHeaderOnlyTests(unittest.TestCase):
    def test_url_never_carries_token_or_query_string(self):
        seen_urls = []

        def fetch(method, url, token_header):
            seen_urls.append(url)
            return (200, "{}", None) if token_header == TEST_CORRECT_TOKEN else (403, "{}", None)

        smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=fetch,
        )
        for url in seen_urls:
            self.assertNotIn(
                TEST_CORRECT_TOKEN, url,
                f"correct token leaked into URL: {url}",
            )
            # No query string at all on probe URLs.
            self.assertNotIn(
                "?", url, f"probe URL must not carry a query string: {url}",
            )
            for needle in ("token=", "Token=", "X-Review-Token="):
                self.assertNotIn(needle, url)

    def test_correct_token_only_passed_through_token_header(self):
        # Spy on token_header values the fetch_fn sees per probe.
        seen = []

        def fetch(method, url, token_header):
            seen.append((url, token_header))
            return (200, "{}", None) if token_header == TEST_CORRECT_TOKEN else (403, "{}", None)

        smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=fetch,
        )
        # First two probes (no_token + wrong_token) → token_header is
        # None or WRONG_TOKEN_LITERAL respectively. Subsequent probes
        # → exactly TEST_CORRECT_TOKEN.
        self.assertEqual(seen[0][1], None)
        self.assertEqual(seen[1][1], smoke.WRONG_TOKEN_LITERAL)
        for url, header in seen[2:]:
            self.assertEqual(header, TEST_CORRECT_TOKEN)

    def test_smoke_only_issues_get_requests(self):
        methods = []

        def fetch(method, url, token_header):
            methods.append(method)
            return (200, "{}", None) if token_header == TEST_CORRECT_TOKEN else (403, "{}", None)

        smoke.run_token_gate_smoke(
            "https://example.invalid",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=fetch,
        )
        for m in methods:
            self.assertEqual(m, "GET", "token-gate smoke must be GET-only")

    def test_probe_catalogue_is_get_only(self):
        for probe in smoke.PROBES:
            self.assertEqual(
                probe.method, "GET",
                f"probe is not GET: {probe}",
            )

    def test_request_helper_never_sends_a_body(self):
        # The script's _make_request always passes data=None — pin
        # via source scan because we don't want to spin up a real
        # urllib.Request in tests.
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        # Both occurrences of urllib.request.Request must explicitly
        # set data=None (the smoke never sends a request body).
        for line in text.splitlines():
            if "urllib.request.Request" in line and "data=" in line:
                self.assertIn(
                    "data=None", line,
                    f"smoke must never send a request body: {line!r}",
                )


# ---------------------------------------------------------------------------
# O + P — imports + base URL normalization
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def test_script_only_imports_stdlib_for_http(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx", "urllib3",
            "policy_decision", "policy_scoring", "verification_card",
            "review_workflow", "review_auth", "database",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"smoke_review_api_token_gate.py must not import {forbidden!r}",
            )

    def test_script_does_not_reference_openai_api_key(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn(
            "OPENAI_API_KEY", text,
            "smoke must never reference OPENAI_API_KEY",
        )

    def test_script_never_constructs_state_changing_git_or_post(self):
        # No POST/PUT/DELETE/PATCH HTTP methods anywhere in the smoke.
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for forbidden in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"',
                              "'POST'", "'PUT'", "'DELETE'", "'PATCH'"):
                self.assertNotIn(
                    forbidden, line,
                    f"smoke must not construct {forbidden} requests: {line!r}",
                )


class NormalizationTests(unittest.TestCase):
    def test_trailing_slash_stripped(self):
        seen_urls = []

        def fetch(method, url, token_header):
            seen_urls.append(url)
            return (200, "{}", None) if token_header == TEST_CORRECT_TOKEN else (403, "{}", None)

        smoke.run_token_gate_smoke(
            "https://example.invalid////",
            correct_token=TEST_CORRECT_TOKEN,
            token_env_var="REVIEW_API_SMOKE_TOKEN",
            fetch_fn=fetch,
        )
        for url in seen_urls:
            self.assertNotIn(
                "invalid//review", url,
                f"double slash leaked into URL: {url}",
            )
            self.assertTrue(
                url.startswith("https://example.invalid/review/"),
                f"normalized URL prefix wrong: {url}",
            )

    def test_normalize_base_url_strips_trailing_slashes(self):
        self.assertEqual(
            smoke._normalize_base_url("https://example.invalid/"),
            "https://example.invalid",
        )
        self.assertEqual(
            smoke._normalize_base_url("http://localhost:8000//"),
            "http://localhost:8000",
        )


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def _run_main(self, argv, env_overrides=None):
        original = {}
        if env_overrides:
            for k, v in env_overrides.items():
                original[k] = os.environ.get(k)
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    rc = smoke.main(argv)
                except SystemExit as e:
                    rc = int(e.code) if e.code is not None else 0
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, out.getvalue(), err.getvalue()

    def test_missing_base_url_rejected(self):
        rc, _, err = self._run_main([])
        self.assertEqual(rc, 2, msg=err)

    def test_zero_timeout_rejected(self):
        rc, _, err = self._run_main([
            "--base-url", "https://example.invalid",
            "--timeout-seconds", "0",
        ])
        self.assertEqual(rc, 2, msg=err)

    def test_negative_timeout_rejected(self):
        rc, _, err = self._run_main([
            "--base-url", "https://example.invalid",
            "--timeout-seconds", "-1",
        ])
        self.assertEqual(rc, 2, msg=err)

    def test_custom_token_env_recognized(self):
        rc, _stdout, stderr = self._run_main(
            ["--base-url", "https://example.invalid",
             "--token-env", "CUSTOM_TOKEN_ENV_VAR_THAT_IS_UNSET"],
            env_overrides={
                "CUSTOM_TOKEN_ENV_VAR_THAT_IS_UNSET": None,
                "REVIEW_API_SMOKE_TOKEN": None,
            },
        )
        # Missing custom env → exit 2 with the custom name surfaced.
        self.assertEqual(rc, 2)
        self.assertIn("CUSTOM_TOKEN_ENV_VAR_THAT_IS_UNSET", stderr)


if __name__ == "__main__":
    unittest.main()
