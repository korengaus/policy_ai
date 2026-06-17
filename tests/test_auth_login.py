"""AUTH-2b: login endpoint + session + dual-accept admin gate.

Reuses the SQLite-as-PG substitute + TestClient pattern from
tests/test_review_api.py. A test admin is seeded via database.create_account
in setUp; the FastAPI app is reloaded so SessionMiddleware + the /auth routes
are wired against the substitute engine.

SESSION_SECRET_KEY is intentionally left unset — config.session_secret_key()
returns a per-process random fallback (cached), which is consistent within the
test process so signed cookies round-trip.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


TEST_TOKEN = "auth2b-test-token-not-a-real-secret"
ADMIN_USER = "admin"
ADMIN_PASS = "correct-horse-battery-staple-123"
SESSION_COOKIE = "policy_ai_session"


@contextmanager
def _env(**overrides):
    """Temporarily set/clear env vars (None clears)."""
    snapshot = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _AuthBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp_ctx.__enter__())
        self._pg_db_path = self._tmp_dir / "pg_substitute.db"

        self._env_snapshot = {
            key: os.environ.get(key)
            for key in (
                "USE_POSTGRES_WRITE", "DATABASE_URL",
                "REVIEW_API_ENABLED", "REVIEW_API_TOKEN",
            )
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db_path}"
        # Start with the review token gate DISABLED so the dual-accept tests
        # can prove the session path works even when the token path 503s.
        os.environ.pop("REVIEW_API_ENABLED", None)
        os.environ.pop("REVIEW_API_TOKEN", None)

        import postgres_storage
        postgres_storage.reset_engine_for_tests()
        self._postgres_storage = postgres_storage

        import database
        self._database = database

        import api_server
        importlib.reload(api_server)
        self._api_server = api_server

        # Build the substitute engine (creates the accounts/review tables) and
        # seed one admin row.
        postgres_storage.get_engine()
        self._database.create_account(ADMIN_USER, ADMIN_PASS, role="admin")

    def tearDown(self) -> None:
        self._postgres_storage.reset_engine_for_tests()
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self._tmp_ctx.__exit__(None, None, None)
        except Exception:
            pass

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(self._api_server.app)


class LoginTests(_AuthBase):
    def test_login_success_sets_session_and_returns_role(self):
        c = self._client()
        r = c.post("/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertEqual(r.json(), {"ok": True, "role": "admin"})
        # A session cookie is issued.
        self.assertIn(SESSION_COOKIE, c.cookies)
        # /auth/me now reflects the authenticated session.
        me = c.get("/auth/me")
        self.assertEqual(me.json(), {"authenticated": True, "role": "admin"})

    def test_wrong_password_401_generic_no_session(self):
        c = self._client()
        r = c.post("/auth/login", json={"username": ADMIN_USER, "password": "WRONG-pw"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn(SESSION_COOKIE, c.cookies)
        self.assertEqual(c.get("/auth/me").json(), {"authenticated": False})

    def test_unknown_user_same_shape_as_wrong_password(self):
        c = self._client()
        unknown = c.post("/auth/login", json={"username": "ghost", "password": "x"})
        wrong = c.post("/auth/login", json={"username": ADMIN_USER, "password": "x"})
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        # Identical body — no user enumeration.
        self.assertEqual(unknown.json(), wrong.json())

    def test_password_never_echoed_in_response(self):
        pw = "super-secret-DO-NOT-ECHO-987"
        self._database.create_account("user2", pw)
        c = self._client()
        ok = c.post("/auth/login", json={"username": "user2", "password": pw})
        self.assertEqual(ok.status_code, 200, msg=ok.text)
        self.assertNotIn(pw, ok.text)
        bad_pw = "WRONGPW-echo-check-xyz"
        bad = c.post("/auth/login", json={"username": "user2", "password": bad_pw})
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn(bad_pw, bad.text)


class DualAcceptTests(_AuthBase):
    def test_session_grants_protected_endpoint_without_token(self):
        # Token gate DISABLED (setUp default) — only the session can authorize.
        c = self._client()
        c.post("/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        r = c.get("/review/tasks")  # no X-Review-Token at all
        self.assertEqual(r.status_code, 200, msg=r.text)

    def test_token_grants_protected_endpoint_without_session(self):
        c = self._client()  # never logs in -> no session
        with _env(REVIEW_API_ENABLED="true", REVIEW_API_TOKEN=TEST_TOKEN):
            r = c.get("/review/tasks", headers={"X-Review-Token": TEST_TOKEN})
        self.assertEqual(r.status_code, 200, msg=r.text)

    def test_token_path_disabled_returns_503(self):
        c = self._client()  # no session
        with _env(REVIEW_API_ENABLED=None, REVIEW_API_TOKEN=None):
            r = c.get("/review/tasks")  # no session, no token, gate disabled
        self.assertEqual(r.status_code, 503)

    def test_token_path_wrong_token_returns_403(self):
        c = self._client()  # no session
        with _env(REVIEW_API_ENABLED="true", REVIEW_API_TOKEN=TEST_TOKEN):
            r = c.get("/review/tasks", headers={"X-Review-Token": "wrong"})
        self.assertEqual(r.status_code, 403)


class LogoutTests(_AuthBase):
    def test_logout_clears_session_and_revokes_bypass(self):
        c = self._client()
        c.post("/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        self.assertTrue(c.get("/auth/me").json()["authenticated"])
        # While logged in, the session bypasses the (disabled) token gate.
        self.assertEqual(c.get("/review/tasks").status_code, 200)

        c.post("/auth/logout")
        self.assertEqual(c.get("/auth/me").json(), {"authenticated": False})
        # Session bypass is gone: with the token gate disabled the protected
        # endpoint now falls through to the legacy token path -> 503.
        self.assertEqual(c.get("/review/tasks").status_code, 503)
        # ...but the token path still works after logout.
        with _env(REVIEW_API_ENABLED="true", REVIEW_API_TOKEN=TEST_TOKEN):
            r = c.get("/review/tasks", headers={"X-Review-Token": TEST_TOKEN})
        self.assertEqual(r.status_code, 200, msg=r.text)


if __name__ == "__main__":
    unittest.main()
