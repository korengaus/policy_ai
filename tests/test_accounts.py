"""AUTH-2a: tests for bcrypt hashing (accounts.py) and the account
data layer (database.create_account / get_account_by_username).

The DB tests reuse the SQLite-as-PG substitute pattern from
tests/test_review_api.py: set USE_POSTGRES_WRITE=true + DATABASE_URL to a
temp SQLite file, reset the cached engine, and let ensure_schema (inside
get_engine) create the mirror tables — including the new ``accounts`` table.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import accounts  # noqa: E402


# ---------------------------------------------------------------------------
# Pure bcrypt hashing (no DB)
# ---------------------------------------------------------------------------


class HashingTests(unittest.TestCase):
    def test_hash_then_verify_true(self):
        h = accounts.hash_password("test-pw-123")
        self.assertTrue(accounts.verify_password("test-pw-123", h))

    def test_verify_wrong_password_false(self):
        h = accounts.hash_password("test-pw-123")
        self.assertFalse(accounts.verify_password("wrong", h))

    def test_verify_malformed_hash_returns_false_not_raise(self):
        # Must NOT raise on a non-bcrypt stored hash.
        self.assertFalse(accounts.verify_password("x", "not-a-hash"))

    def test_verify_empty_inputs_false(self):
        self.assertFalse(accounts.verify_password("", ""))
        self.assertFalse(accounts.verify_password("pw", ""))
        self.assertFalse(accounts.verify_password("", "irrelevant"))

    def test_hash_is_not_plaintext(self):
        h = accounts.hash_password("secret-pw")
        self.assertNotIn("secret-pw", h)
        self.assertTrue(h.startswith("$2"))  # bcrypt prefix

    def test_hash_empty_raises(self):
        with self.assertRaises(ValueError):
            accounts.hash_password("")


# ---------------------------------------------------------------------------
# Account data layer (database.py) over the SQLite-as-PG substitute
# ---------------------------------------------------------------------------


class _AccountDBBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._tmp_dir = Path(self._tmp_ctx.__enter__())
        self._pg_db_path = self._tmp_dir / "pg_substitute.db"

        self._env_snapshot = {
            key: os.environ.get(key)
            for key in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db_path}"

        import postgres_storage
        postgres_storage.reset_engine_for_tests()
        self._postgres_storage = postgres_storage

        import database
        self._database = database

        # Build the substitute engine so ensure_schema creates the mirror
        # tables (incl. accounts) before any write fires.
        postgres_storage.get_engine()

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


class AccountTableTests(_AccountDBBase):
    def test_accounts_table_registered_in_metadata(self):
        self.assertIn(
            "accounts", self._postgres_storage._metadata.tables.keys()
        )

    def test_create_then_get_returns_hash_never_plaintext(self):
        self._database.create_account("admin", "pw-abc-123", role="admin")
        row = self._database.get_account_by_username("admin")
        self.assertIsNotNone(row)
        self.assertEqual(row["username"], "admin")
        self.assertEqual(row["role"], "admin")
        self.assertIn("password_hash", row)
        # The stored value is the hash, NOT the plaintext.
        self.assertNotEqual(row["password_hash"], "pw-abc-123")
        self.assertTrue(
            accounts.verify_password("pw-abc-123", row["password_hash"])
        )

    def test_role_defaults_to_admin(self):
        self._database.create_account("someone", "pw-xyz-789")
        row = self._database.get_account_by_username("someone")
        self.assertEqual(row["role"], "admin")

    def test_duplicate_username_raises(self):
        self._database.create_account("dup", "pw-1")
        with self.assertRaises(self._database.AccountExistsError):
            self._database.create_account("dup", "pw-2")

    def test_get_unknown_username_returns_none(self):
        self.assertIsNone(self._database.get_account_by_username("nobody"))

    def test_create_empty_username_or_password_raises(self):
        with self.assertRaises(ValueError):
            self._database.create_account("", "pw")
        with self.assertRaises(ValueError):
            self._database.create_account("user", "")


if __name__ == "__main__":
    unittest.main()
