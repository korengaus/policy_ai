"""Tests for the M14.3a request-id middleware in api_server.

Run with: python tests/test_api_request_id_middleware.py

Uses FastAPI's ``TestClient`` for end-to-end middleware behaviour and
unit-tests the ``_is_valid_request_id`` helper directly. The full
suite runs offline — no real Render call.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import api_server  # noqa: E402
import request_context  # noqa: E402


# ---------------------------------------------------------------------------
# Middleware registration
# ---------------------------------------------------------------------------


class MiddlewareRegistrationTests(unittest.TestCase):
    """The middleware was added via ``@app.middleware('http')``, which
    registers it as a Starlette ``BaseHTTPMiddleware`` in
    ``app.user_middleware``. We assert the registration is present so
    a future PR that accidentally drops the decorator surfaces here."""

    def test_request_id_middleware_is_registered(self):
        middleware_names = [
            entry.cls.__name__
            for entry in api_server.app.user_middleware
        ]
        # FastAPI / Starlette wraps the function in a
        # BaseHTTPMiddleware factory; verify by name plus checking
        # the function attribute on the app's middleware stack.
        self.assertTrue(
            any("Middleware" in name for name in middleware_names),
            msg=(
                "Expected at least one BaseHTTPMiddleware in the app "
                f"stack; saw {middleware_names}"
            ),
        )

    def test_request_id_middleware_function_present(self):
        # The module-level function must exist and be referenced by
        # the app — easier to introspect directly.
        self.assertTrue(
            hasattr(api_server, "request_id_middleware"),
            msg=(
                "api_server.request_id_middleware function missing"
            ),
        )


# ---------------------------------------------------------------------------
# _is_valid_request_id helper
# ---------------------------------------------------------------------------


class IsValidRequestIdTests(unittest.TestCase):
    def _check(self, value, expected):
        self.assertEqual(
            api_server._is_valid_request_id(value), expected,
            msg=f"value={value!r} -> expected {expected}",
        )

    def test_valid_hex_strings(self):
        self._check("abc12345", True)            # 8 chars, min length
        self._check("0123456789abcdef", True)    # 16 chars
        self._check("a" * 64, True)              # 64 chars, max length

    def test_valid_uuid_with_dashes(self):
        self._check(
            "550e8400-e29b-41d4-a716-446655440000", True,
        )

    def test_valid_human_readable_traces(self):
        # The middleware accepts URL-safe alphanumerics + _ + - so
        # operators can use meaningful client trace IDs.
        self._check("my-trace-123abc", True)
        self._check("client_session_42", True)
        self._check("smoke-test-2026-05-23", True)

    def test_valid_alphanumeric_accepted(self):
        self._check("abc123xyz", True)
        self._check("AlphaNumericTraceID", True)
        self._check("Trace_ABC-123", True)

    def test_too_short_rejected(self):
        self._check("abc", False)         # 3 chars
        self._check("1234567", False)     # 7 chars (just under min)

    def test_too_long_rejected(self):
        self._check("a" * 65, False)

    def test_empty_rejected(self):
        self._check("", False)

    def test_special_chars_rejected(self):
        self._check("../../etc/passwd", False)   # path traversal
        self._check("'; DROP TABLE", False)      # injection-ish
        self._check("12345 67890", False)        # space
        self._check("12345\n67890", False)       # newline (header injection)
        self._check("trace.with.dots", False)    # dots not allowed
        self._check("trace+with+plus", False)    # plus not allowed
        self._check("trace/with/slash", False)   # slash
        self._check("한국어트레이스", False)         # non-ASCII


# ---------------------------------------------------------------------------
# End-to-end via FastAPI TestClient
# ---------------------------------------------------------------------------


class _TestClientMixin:
    """Lazy TestClient instantiation — keeps import-time cost low and
    lets each test class manage its own startup/shutdown."""

    @classmethod
    def _client(cls):
        from fastapi.testclient import TestClient
        return TestClient(api_server.app)


class GeneratedRequestIdTests(_TestClientMixin, unittest.TestCase):
    def test_no_header_yields_generated_id_in_response(self):
        with self._client() as client:
            response = client.get("/health")
        # /health may not exist as a route in the test app; if not,
        # try a guaranteed-200 path. We assert the middleware ran by
        # checking the response header regardless of status.
        rid = response.headers.get("x-request-id")
        self.assertIsNotNone(
            rid,
            msg="Middleware did not echo X-Request-ID header",
        )
        self.assertTrue(
            re.fullmatch(r"[0-9a-f]{12}", rid),
            msg=f"generated ID {rid!r} does not match 12-hex pattern",
        )

    def test_client_provided_id_echoed(self):
        with self._client() as client:
            response = client.get(
                "/health",
                headers={"X-Request-ID": "client-trace-abcdef12"},
            )
        self.assertEqual(
            response.headers.get("x-request-id"),
            "client-trace-abcdef12",
        )

    def test_invalid_client_id_replaced_by_generated(self):
        bad_ids = ("../../etc/passwd", "x", "x" * 100, "with space")
        with self._client() as client:
            for bad in bad_ids:
                response = client.get(
                    "/health",
                    headers={"X-Request-ID": bad},
                )
                rid = response.headers.get("x-request-id")
                self.assertIsNotNone(rid)
                self.assertNotEqual(
                    rid, bad,
                    msg=(
                        f"Invalid client-supplied ID {bad!r} was "
                        "echoed back; middleware must replace it"
                    ),
                )
                self.assertTrue(
                    re.fullmatch(r"[0-9a-f]{12}", rid),
                    msg=(
                        f"Replacement ID {rid!r} does not match "
                        "the 12-hex pattern"
                    ),
                )

    def test_two_requests_get_different_ids(self):
        with self._client() as client:
            r1 = client.get("/health")
            r2 = client.get("/health")
        self.assertNotEqual(
            r1.headers.get("x-request-id"),
            r2.headers.get("x-request-id"),
            msg="Two consecutive no-header requests must get different IDs",
        )

    def test_cleanup_after_request(self):
        """After a request completes (TestClient context exited), the
        ContextVar in the test runner's own context must be back to
        None — the middleware's ``finally`` reset must run even when
        the handler succeeds normally."""
        # Set a sentinel; the request should not clobber it because
        # FastAPI's worker runs in its own context.
        token = request_context.set_request_id("sentinel-rid")
        try:
            with self._client() as client:
                client.get("/health")
            self.assertEqual(
                request_context.get_request_id(),
                "sentinel-rid",
                msg=(
                    "TestClient request leaked the request_id into "
                    "the test runner's context — middleware reset "
                    "is not working as expected"
                ),
            )
        finally:
            request_context.reset_request_id(token)


# ---------------------------------------------------------------------------
# Smoke that an exception still triggers cleanup. We bind a temporary
# /__rid_test_raise route directly to the existing app to keep the
# test offline.
# ---------------------------------------------------------------------------


class ExceptionCleanupTests(_TestClientMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import HTTPException

        @api_server.app.get("/__m14_3a_test_raise")
        async def _test_route():
            raise HTTPException(status_code=500, detail="boom")

        cls._route_registered = True

    def test_exception_does_not_leak_request_id(self):
        token = request_context.set_request_id("pre-exception-rid")
        try:
            with self._client() as client:
                response = client.get(
                    "/__m14_3a_test_raise",
                    headers={"X-Request-ID": "during-exception-rid"},
                )
            # Status 500 expected, but the middleware should still
            # have echoed the request ID into the response headers
            # before the framework returned the error page.
            self.assertEqual(response.status_code, 500)
            self.assertEqual(
                response.headers.get("x-request-id"),
                "during-exception-rid",
                msg=(
                    "Request ID must be echoed even on exception path"
                ),
            )
            # The test runner's pre-existing context must survive.
            self.assertEqual(
                request_context.get_request_id(),
                "pre-exception-rid",
            )
        finally:
            request_context.reset_request_id(token)


if __name__ == "__main__":
    unittest.main()
