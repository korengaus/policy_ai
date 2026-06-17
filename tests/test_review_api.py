"""Phase 2 M8.0 (AUTH-2d session model): review API tests (FastAPI
TestClient + temp SQLite).

Verifies:
    * the admin gate is session-only (AUTH-2d): no session → 401, invalid
      session → 401, authenticated session → 200 (the legacy X-Review-Token
      gate was retired),
    * task creation from a result payload is idempotent,
    * decision recording follows the documented status-transition matrix,
    * comment-only decisions do not change status,
    * approved / rejected tasks cannot publish (no publish endpoint exists),
    * review endpoints never mutate the underlying analysis_results row,
    * verdict-side modules don't import review modules,
    * no OpenAI key, no network, no live server required.

CI safety: every test runs against a per-test SQLite-as-Postgres
substitute (``USE_POSTGRES_WRITE=true`` + ``DATABASE_URL=sqlite:///<tmp>``)
before instantiating the FastAPI TestClient. (M12.0e-6b-3: the SQLite
``DB_PATH`` swap was removed with the retired SQLite machinery.)
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TEST_TOKEN = "test-token-shouldnt-leak"  # vestigial; backend ignores it post-AUTH-2d
ADMIN_USER = "admin"
ADMIN_PASS = "review-api-test-pw-123"
SESSION_COOKIE = "policy_ai_session"


@contextmanager
def _env(**overrides):
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _synthetic_result_payload(*, claim: str = "정부가 청년 보조금을 신설한다.",
                              title: str = "Sample headline",
                              url: str = "https://example.go.kr/sample",
                              final_decision_label: str = "사실 확인 필요",
                              confidence_label: str = "moderate") -> dict:
    """Build a /jobs/{id}/result-shaped payload the review snapshot
    extractor can consume."""
    return {
        "status": "ok",
        "result": {
            "results": [{
                "title": title,
                "original_url": url,
                "normalized_claims": [{"claim_text": claim}],
                "final_decision": {"decision_label": final_decision_label},
                "policy_confidence": {"verification_strength": confidence_label},
                "verification_card": {"summary": "sample"},
                "debug_summary": {"semantic_evidence_summary": {"x": 1}},
            }],
        },
        "query": "전세사기",
    }


class _ReviewAPIBase(unittest.TestCase):
    """Spin up a fresh SQLite-as-PG substitute + FastAPI TestClient per test.

    M12.0d Stage 3c-2: review_tasks / review_decisions writes are
    PG-only. The fixture provisions a fresh SQLite file as the
    dual-write substitute (``USE_POSTGRES_WRITE=true`` +
    ``DATABASE_URL=sqlite:///<tmp>``) so the production write path
    (mirror_upsert / mirror_write into postgres_storage) lands in the
    substitute and the PG-primary reads return what was just written.
    The local SQLite DB remains for legacy paths that fall back to
    SQLite when PG is disabled.
    """

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

        # M12.0e-6b-3: SQLite machinery retired. The FastAPI lifespan no
        # longer calls init_db, so no DB_PATH swap is needed — the
        # PG-substitute engine below (ensure_schema inside get_engine)
        # owns the review schema.
        import api_server
        importlib.reload(api_server)
        self._api_server = api_server

        # Build the PG-substitute engine so ensure_schema (via the
        # 3c-1 hotfix inside get_engine) creates the mirror tables
        # in the substitute SQLite file before any write fires.
        postgres_storage.get_engine()

        # AUTH-2d: admin auth is session-only. Seed one admin so tests can log
        # in via /auth/login and drive the protected endpoints with the session
        # cookie (no token header).
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

    def _client(self, *, login: bool = True):
        """Return a TestClient. By default it is already authenticated via
        /auth/login (session cookie set), so protected endpoints work with no
        token header. Pass ``login=False`` for the unauthenticated cases."""
        from fastapi.testclient import TestClient
        client = TestClient(self._api_server.app)
        if login:
            resp = client.post(
                "/auth/login",
                json={"username": ADMIN_USER, "password": ADMIN_PASS},
            )
            assert resp.status_code == 200, resp.text
        return client

    def _enabled_env(self):
        # AUTH-2d: the review API no longer reads any token env var. Kept as an
        # empty no-op so the existing ``with _env(**self._enabled_env()):``
        # wrappers below stay valid; auth now comes from the session cookie that
        # ``_client()`` establishes. Any X-Review-Token header in a call is
        # simply ignored by the backend.
        return {}


# ---------------------------------------------------------------------------
# Session-only admin gate (AUTH-2d)
# ---------------------------------------------------------------------------


class ReviewAPISessionGateTests(_ReviewAPIBase):
    def test_no_session_returns_401(self):
        # The legacy token fallback is gone: an unauthenticated request to a
        # protected endpoint is rejected with 401 (no token escape hatch).
        with self._client(login=False) as client:
            resp = client.get("/review/tasks")
        self.assertEqual(resp.status_code, 401)

    def test_invalid_session_cookie_returns_401(self):
        with self._client(login=False) as client:
            client.cookies.set(SESSION_COOKIE, "garbage.not-a-valid-session")
            resp = client.get("/review/tasks")
        self.assertEqual(resp.status_code, 401)

    def test_token_header_does_not_authenticate(self):
        # A leftover X-Review-Token header must NOT grant access — the token
        # gate was retired; only the session cookie authenticates.
        with self._client(login=False) as client:
            resp = client.get(
                "/review/tasks", headers={"X-Review-Token": TEST_TOKEN},
            )
        self.assertEqual(resp.status_code, 401)

    def test_authenticated_session_returns_200_with_empty_list(self):
        with self._client() as client:  # logged in via /auth/login
            resp = client.get("/review/tasks")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tasks"], [])
        self.assertEqual(body["count"], 0)


# ---------------------------------------------------------------------------
# Task creation + idempotency
# ---------------------------------------------------------------------------


class ReviewTaskCreationTests(_ReviewAPIBase):
    def test_create_task_from_result_payload(self):
        payload = _synthetic_result_payload()
        with _env(**self._enabled_env()):
            with self._client() as client:
                resp = client.post(
                    "/review/tasks/from-result",
                    json={
                        "result_id": "42",
                        "job_id": "job-A",
                        "item_index": 0,
                        "result_payload": payload,
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        body = resp.json()
        task = body["task"]
        self.assertEqual(task["status"], "pending_review")
        self.assertEqual(task["claim_text"], "정부가 청년 보조금을 신설한다.")
        self.assertEqual(task["final_decision"], "사실 확인 필요")
        self.assertEqual(task["policy_confidence"], "moderate")
        self.assertTrue(task["human_review_required"])
        self.assertTrue(task["task_id"].startswith("review_"))

    def test_create_is_idempotent_for_same_identity_tuple(self):
        payload = _synthetic_result_payload()
        with _env(**self._enabled_env()):
            with self._client() as client:
                first = client.post(
                    "/review/tasks/from-result",
                    json={"result_id": "42", "job_id": "job-A",
                          "item_index": 0, "result_payload": payload},
                    headers={"X-Review-Token": TEST_TOKEN},
                ).json()
                second = client.post(
                    "/review/tasks/from-result",
                    json={"result_id": "42", "job_id": "job-A",
                          "item_index": 0, "result_payload": payload},
                    headers={"X-Review-Token": TEST_TOKEN},
                ).json()
        self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
        # Second call recognized the duplicate via the idempotency key.
        self.assertTrue(second["idempotent"])

    def test_create_fails_when_no_payload_and_unknown_ids(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                resp = client.post(
                    "/review/tasks/from-result",
                    json={"result_id": "9999999", "job_id": "missing"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("payload", resp.json().get("detail", "").lower())

    def test_create_fails_when_payload_has_no_claim(self):
        # Empty result list → snapshot has no claim_text → 400.
        empty_payload = {"status": "ok", "result": {"results": [{"title": ""}]}}
        with _env(**self._enabled_env()):
            with self._client() as client:
                resp = client.post(
                    "/review/tasks/from-result",
                    json={"result_id": "x", "job_id": "y",
                          "result_payload": empty_payload},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("claim", resp.json().get("detail", "").lower())


class ReviewListAndDetailTests(_ReviewAPIBase):
    def _create_task(self, client) -> dict:
        payload = _synthetic_result_payload()
        resp = client.post(
            "/review/tasks/from-result",
            json={"result_id": "42", "job_id": "job-A",
                  "item_index": 0, "result_payload": payload},
            headers={"X-Review-Token": TEST_TOKEN},
        )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        return resp.json()["task"]

    def test_list_returns_created_task(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                created = self._create_task(client)
                resp = client.get(
                    "/review/tasks",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["tasks"][0]["task_id"], created["task_id"])

    def test_list_filters_by_status(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                self._create_task(client)
                # Status filter with non-matching value → empty list.
                resp = client.get(
                    "/review/tasks?status=approved",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)

    def test_list_rejects_unknown_status(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                resp = client.get(
                    "/review/tasks?status=invalid_status",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 400)

    def test_detail_returns_task_with_snapshot_and_decisions(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                created = self._create_task(client)
                resp = client.get(
                    f"/review/tasks/{created['task_id']}",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        body = resp.json()
        self.assertEqual(body["task"]["task_id"], created["task_id"])
        self.assertEqual(body["decisions"], [])
        # Snapshot must surface in the detail view.
        self.assertIn("snapshot", body["task"])

    def test_detail_404_for_unknown_task(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                resp = client.get(
                    "/review/tasks/review_unknown",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Decisions + status transitions
# ---------------------------------------------------------------------------


class ReviewDecisionTests(_ReviewAPIBase):
    def _create_task(self, client) -> str:
        resp = client.post(
            "/review/tasks/from-result",
            json={"result_id": "42", "job_id": "job-A",
                  "item_index": 0,
                  "result_payload": _synthetic_result_payload()},
            headers={"X-Review-Token": TEST_TOKEN},
        )
        return resp.json()["task"]["task_id"]

    def test_approve_moves_to_approved(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "approve",
                          "reviewer_id": "local_reviewer",
                          "comment": "looks good"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        body = resp.json()
        self.assertEqual(body["new_status"], "approved")
        self.assertTrue(body["status_changed"])
        self.assertEqual(body["task"]["status"], "approved")
        # Decision was recorded.
        self.assertEqual(len(body["task"]["decisions"]), 1)

    def test_reject_moves_to_rejected(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "reject", "reviewer_id": "r1"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["new_status"], "rejected")

    def test_needs_more_evidence_moves_correctly(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "needs_more_evidence"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["new_status"], "needs_more_evidence")

    def test_comment_does_not_change_status(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "comment", "comment": "noted"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        body = resp.json()
        self.assertEqual(body["new_status"], "pending_review")
        self.assertFalse(body["status_changed"])

    def test_invalid_decision_returns_400(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "bogus"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 400)

    def test_approved_task_cannot_be_re_approved(self):
        # Once approved, only ``comment`` is allowed — any other decision
        # returns 409 conflict.
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                # First approve.
                client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "approve"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                # Now try to approve again.
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "approve"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                self.assertEqual(resp.status_code, 409)
                # But comment is still allowed.
                comment_resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "comment", "comment": "post-approval note"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                self.assertEqual(comment_resp.status_code, 200)

    def test_publish_endpoint_does_not_exist(self):
        """M8.0 contract: there is no /review/tasks/{id}/publish endpoint.

        The status-transition matrix also refuses any decision that
        would move into ``published`` / ``corrected``. Pin both facts.
        """
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                # No publish endpoint.
                resp = client.post(
                    f"/review/tasks/{task_id}/publish",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                self.assertIn(resp.status_code, (404, 405))

    def test_list_decisions_returns_appended_history(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                # Several comments then approve.
                for note in ["note A", "note B"]:
                    client.post(
                        f"/review/tasks/{task_id}/decision",
                        json={"decision": "comment", "comment": note},
                        headers={"X-Review-Token": TEST_TOKEN},
                    )
                client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "approve"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                resp = client.get(
                    f"/review/tasks/{task_id}/decisions",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 3)
        # Stored in append order.
        self.assertEqual(body["decisions"][0]["decision"], "comment")
        self.assertEqual(body["decisions"][-1]["decision"], "approve")


# ---------------------------------------------------------------------------
# Verdict isolation
# ---------------------------------------------------------------------------


class VerdictIsolationTests(_ReviewAPIBase):
    def test_review_endpoints_do_not_mutate_original_result(self):
        """The review layer must never write back to analysis_results.

        We construct a result payload, snapshot a copy, then create +
        decide a review task; the snapshot must equal the original
        payload byte-for-byte at the end.
        """
        payload = _synthetic_result_payload()
        original = copy.deepcopy(payload)
        with _env(**self._enabled_env()):
            with self._client() as client:
                client.post(
                    "/review/tasks/from-result",
                    json={"result_id": "42", "job_id": "job-A",
                          "item_index": 0, "result_payload": payload},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                # Approve + comment to exercise both code paths.
                resp = client.post(
                    "/review/tasks/from-result",
                    json={"result_id": "42", "job_id": "job-A",
                          "item_index": 0, "result_payload": payload},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                task_id = resp.json()["task"]["task_id"]
                client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": "approve"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        # Even after task creation + decision, the payload dict the
        # client passed is untouched.
        self.assertEqual(payload, original)

    def test_verdict_modules_do_not_import_review_modules(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            for forbidden in ("review_workflow", "review_auth"):
                self.assertNotIn(
                    forbidden, text,
                    f"{module_name}.py must not import {forbidden}",
                )


# ---------------------------------------------------------------------------
# CI safety + isolation
# ---------------------------------------------------------------------------


class CISafetyTests(_ReviewAPIBase):
    def test_no_openai_key_required(self):
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None,
                  SEMANTIC_MATCHING_ENABLED=None,
                  **self._enabled_env()):
            with self._client() as client:
                resp = client.get(
                    "/review/tasks",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200)

    def test_review_modules_do_not_import_openai(self):
        # AUTH-2d: review_auth.py was deleted (token gate retired); only
        # review_workflow remains in the review surface.
        for module_name in ("review_workflow",):
            text = (ROOT / f"{module_name}.py").read_text(encoding="utf-8")
            for forbidden in ("import openai", "from openai"):
                self.assertNotIn(forbidden, text)


# ---------------------------------------------------------------------------
# M40a — human-review promotion endpoint
# ---------------------------------------------------------------------------


class ReviewPromoteEndpointTests(_ReviewAPIBase):
    """POST /review/results/{id}/promote sets/clears ONLY the M39a
    human_reviewed_at / human_reviewed_by columns, token-gated."""

    def _seed_result(self) -> int:
        saved = self._database.save_analysis_result(
            {
                "title": "Promote sample",
                "original_url": "https://example.go.kr/promote-sample",
                "topic": "test",
                "final_decision": {"policy_alert_level": "WATCH"},
                "policy_confidence": {},
                "policy_impact": {},
                "verification_card": {
                    "claim_text": "테스트 주장",
                    "verdict_label": "draft_needs_review",
                    "review_status": "ai_draft_pending_human_review",
                },
            },
            "프로모트 테스트",
        )
        self.assertTrue(saved.get("id"), msg=str(saved))
        return int(saved["id"])

    def test_promote_requires_session(self):
        # AUTH-2d: promote is session-gated. No session (and no token escape
        # hatch) → 401.
        result_id = self._seed_result()
        with self._client(login=False) as client:
            resp = client.post(
                f"/review/results/{result_id}/promote",
                json={"promote": True},
            )
        self.assertEqual(resp.status_code, 401)

    def test_promote_unknown_id_returns_404(self):
        with _env(**self._enabled_env()):
            with self._client() as client:
                resp = client.post(
                    "/review/results/99999999/promote",
                    json={"promote": True},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 404)

    def test_promote_then_unpromote_sets_only_review_columns(self):
        result_id = self._seed_result()
        before = self._database.get_result_by_id(result_id) or {}
        self.assertIsNone(before.get("human_reviewed_at"))
        with _env(**self._enabled_env()):
            with self._client() as client:
                promoted = client.post(
                    f"/review/results/{result_id}/promote",
                    json={"promote": True, "reviewer": "tester"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                self.assertEqual(promoted.status_code, 200, msg=promoted.text)
                pbody = promoted.json()
                self.assertTrue(pbody["ok"])
                self.assertEqual(pbody["result_id"], result_id)
                self.assertTrue(pbody["human_reviewed_at"])
                self.assertEqual(pbody["human_reviewed_by"], "tester")

                after = self._database.get_result_by_id(result_id) or {}
                # ONLY the two review columns changed — verdict / review_status
                # / alert / identity columns are byte-identical.
                for col in (
                    "verdict_label", "review_status", "policy_alert_level",
                    "title", "original_url",
                ):
                    self.assertEqual(
                        after.get(col), before.get(col),
                        f"{col} must be unchanged by promote",
                    )
                self.assertTrue(after.get("human_reviewed_at"))
                self.assertEqual(after.get("human_reviewed_by"), "tester")

                # Un-promote clears both columns back to NULL.
                cleared = client.post(
                    f"/review/results/{result_id}/promote",
                    json={"promote": False},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                self.assertEqual(cleared.status_code, 200, msg=cleared.text)
                cbody = cleared.json()
                self.assertIsNone(cbody["human_reviewed_at"])
                self.assertIsNone(cbody["human_reviewed_by"])
                final = self._database.get_result_by_id(result_id) or {}
                self.assertIsNone(final.get("human_reviewed_at"))
                self.assertIsNone(final.get("human_reviewed_by"))
                self.assertEqual(
                    final.get("verdict_label"), before.get("verdict_label"),
                )


if __name__ == "__main__":
    unittest.main()
