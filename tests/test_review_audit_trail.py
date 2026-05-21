"""Phase 2 M9.0: tests for the reviewer decision audit trail.

Focused on the new audit metadata: ``decision_source``, ``transition``,
``audit_version``, ``audit_record``. Existing M8.0 tests in
``tests/test_review_api.py`` cover transition rules and verdict
isolation — this file adds the M9.0 contract on top:

    A. POST decision returns the audit shape (transition, decision_source,
       audit_version, audit_record).
    B. GET decisions returns audit-rich records (legacy rows degrade to
       decision_source="unknown").
    C. Transition labels are correct for every allowed decision.
    D. decision_source normalization: unknown values fall back to
       "unknown", omitted values default to "review_api".
    E. Verdict isolation still holds — audit additions do not mutate
       original payload / final_decision / policy_confidence /
       verification_card.
    F. Token safety — no token literal leaks into any audit response,
       and reviewer_id is not derived from REVIEW_API_TOKEN.
    G. review_workflow helpers (normalize_decision_source,
       transition_label, build_decision_audit_record) — direct unit
       tests, no network.

No OpenAI, no Render, no network. Uses FastAPI TestClient + a per-test
temp SQLite DB, the same pattern as the existing review_api tests.
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

import review_workflow  # noqa: E402


TEST_TOKEN = "m9-audit-test-token-internal-only"  # noqa: S105


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@contextmanager
def _env(**values):
    """Apply env vars for the duration of the block; restore on exit."""
    original = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _temp_db():
    """Point ``database.DB_PATH`` at a fresh temp file and reload api_server."""
    import database
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = Path(tmp_dir.name) / "audit_test.db"
        previous = database.DB_PATH
        database.DB_PATH = db_path
        try:
            database.init_db()
            import api_server
            importlib.reload(api_server)
            yield database, api_server, db_path
        finally:
            database.DB_PATH = previous
    finally:
        try:
            tmp_dir.cleanup()
        except Exception:
            pass


def _synthetic_payload(claim: str, *, idx: int = 0):
    """Conservative-wording payload; mirrors what smoke_review_workflow uses."""
    return {
        "status": "ok",
        "result": {
            "results": [{
                "title": f"감사 추적 검수 청구항 {idx}",
                "original_url": f"https://example.go.kr/audit/{idx}",
                "normalized_claims": [{"claim_text": claim}],
                "final_decision": {"decision_label": "사람 검토 필요"},
                "policy_confidence": {"verification_strength": "moderate"},
                "verification_card": {
                    "summary": "공식 출처 확인 필요 — 사람 검토 대기",
                    "status": "pending_review",
                },
            }],
        },
        "query": "감사 추적 스모크",
    }


def _enabled_env():
    return {"REVIEW_API_ENABLED": "true", "REVIEW_API_TOKEN": TEST_TOKEN}


class _AuditAPIBase(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(self._api_server.app)

    def setUp(self):
        self._db_ctx = _temp_db()
        self._database, self._api_server, _ = self._db_ctx.__enter__()

    def tearDown(self):
        try:
            self._db_ctx.__exit__(None, None, None)
        except Exception:
            pass

    def _create_task(self, client, *, claim: str = "감사 추적 청구항",
                     idx: int = 0) -> str:
        payload = _synthetic_payload(claim, idx=idx)
        resp = client.post(
            "/review/tasks/from-result",
            json={
                "result_id": f"audit-{idx}",
                "job_id": f"audit-job-{idx}",
                "item_index": 0,
                "result_payload": payload,
            },
            headers={"X-Review-Token": TEST_TOKEN},
        )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        return resp.json()["task"]["task_id"]


# ---------------------------------------------------------------------------
# A — POST decision returns the audit shape
# ---------------------------------------------------------------------------


class PostDecisionAuditShapeTests(_AuditAPIBase):
    def test_post_decision_returns_transition_and_audit_fields(self):
        with _env(**_enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "approve",
                        "reviewer_id": "local_reviewer",
                        "comment": "audit shape check",
                        "decision_source": "review_ui",
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        body = resp.json()
        # Existing M8.0 fields preserved.
        self.assertIn("task", body)
        self.assertIn("decision_id", body)
        self.assertEqual(body["previous_status"], "pending_review")
        self.assertEqual(body["new_status"], "approved")
        self.assertTrue(body["status_changed"])
        # M9.0 audit additions present.
        self.assertEqual(body["transition"], "pending_review → approved")
        self.assertEqual(body["decision_source"], "review_ui")
        self.assertEqual(body["audit_version"], 1)
        audit = body.get("audit_record") or {}
        self.assertEqual(audit.get("decision_id"), body["decision_id"])
        self.assertEqual(audit.get("decision_source"), "review_ui")
        self.assertEqual(audit.get("transition"), "pending_review → approved")
        self.assertEqual(audit.get("audit_version"), 1)
        # Embedded decisions list is enriched.
        decisions = body["task"]["decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision_source"], "review_ui")
        self.assertEqual(decisions[0]["transition"], "pending_review → approved")
        self.assertEqual(decisions[0]["audit_version"], 1)

    def test_post_decision_defaults_decision_source_to_review_api(self):
        with _env(**_enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client, idx=1)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    # No decision_source — server defaults to review_api.
                    json={"decision": "comment", "comment": "audit default"},
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        body = resp.json()
        self.assertEqual(body["decision_source"], "review_api")
        self.assertEqual(body["transition"], "pending_review (unchanged)")

    def test_post_decision_unknown_source_falls_back_to_unknown(self):
        with _env(**_enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client, idx=2)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "comment",
                        "comment": "audit unknown",
                        "decision_source": "definitely-not-a-real-source",
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        body = resp.json()
        self.assertEqual(body["decision_source"], "unknown")


# ---------------------------------------------------------------------------
# B + C — GET decisions returns enriched audit records, transitions correct
# ---------------------------------------------------------------------------


class GetDecisionsAuditShapeTests(_AuditAPIBase):
    def test_get_decisions_returns_audit_rich_records(self):
        with _env(**_enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client, idx=10)
                # Record three decisions: comment, comment, approve.
                for note in ["note 1", "note 2"]:
                    client.post(
                        f"/review/tasks/{task_id}/decision",
                        json={
                            "decision": "comment", "comment": note,
                            "decision_source": "smoke_test",
                        },
                        headers={"X-Review-Token": TEST_TOKEN},
                    )
                client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "approve", "reviewer_id": "local_reviewer",
                        # No decision_source → defaults to review_api.
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                resp = client.get(
                    f"/review/tasks/{task_id}/decisions",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        self.assertEqual(resp.status_code, 200, msg=resp.text)
        body = resp.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["audit_version"], 1)
        decisions = body["decisions"]
        # Order: append-only, oldest first.
        sources = [d["decision_source"] for d in decisions]
        self.assertEqual(sources, ["smoke_test", "smoke_test", "review_api"])
        transitions = [d["transition"] for d in decisions]
        self.assertEqual(transitions, [
            "pending_review (unchanged)",
            "pending_review (unchanged)",
            "pending_review → approved",
        ])
        # decision_id is stable + non-empty per row.
        ids = [d.get("decision_id") for d in decisions]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(d.get("created_at") for d in decisions))
        # audit_version on every row.
        self.assertTrue(all(d.get("audit_version") == 1 for d in decisions))

    def test_each_allowed_decision_maps_to_correct_transition(self):
        cases = [
            ("approve", "pending_review → approved"),
            ("reject", "pending_review → rejected"),
            ("needs_more_evidence", "pending_review → needs_more_evidence"),
            ("comment", "pending_review (unchanged)"),
        ]
        with _env(**_enabled_env()):
            with self._client() as client:
                for idx, (decision, expected_transition) in enumerate(cases):
                    task_id = self._create_task(
                        client, claim=f"transition probe {decision}",
                        idx=100 + idx,
                    )
                    resp = client.post(
                        f"/review/tasks/{task_id}/decision",
                        json={"decision": decision},
                        headers={"X-Review-Token": TEST_TOKEN},
                    )
                    self.assertEqual(resp.status_code, 200, msg=resp.text)
                    self.assertEqual(
                        resp.json()["transition"], expected_transition,
                        msg=f"decision={decision}",
                    )


# ---------------------------------------------------------------------------
# E — Verdict isolation still holds with audit additions
# ---------------------------------------------------------------------------


class AuditDoesNotMutateVerdictTests(_AuditAPIBase):
    def test_audit_flow_does_not_mutate_original_payload(self):
        payload = _synthetic_payload("verdict isolation under audit", idx=50)
        original = copy.deepcopy(payload)
        with _env(**_enabled_env()):
            with self._client() as client:
                create = client.post(
                    "/review/tasks/from-result",
                    json={
                        "result_id": "audit-isolation",
                        "job_id": "audit-isolation-job",
                        "item_index": 0,
                        "result_payload": payload,
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                self.assertEqual(create.status_code, 200)
                task_id = create.json()["task"]["task_id"]
                # comment then approve via the audit-rich API.
                client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "comment", "comment": "audit isolation",
                        "decision_source": "review_ui",
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "approve",
                        "reviewer_id": "local_reviewer",
                        "decision_source": "review_ui",
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                detail = client.get(
                    f"/review/tasks/{task_id}",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        # Original payload object the caller passed in is untouched.
        self.assertEqual(payload, original)
        body = detail.json()
        task = body["task"]
        snapshot = task.get("snapshot") or {}
        # Snapshot verdict fields preserved exactly through the audit flow.
        self.assertEqual(snapshot.get("final_decision"), "사람 검토 필요")
        self.assertEqual(snapshot.get("policy_confidence"), "moderate")
        # Stored verification_card on the original payload is untouched.
        self.assertEqual(
            payload["result"]["results"][0]["verification_card"],
            original["result"]["results"][0]["verification_card"],
        )


# ---------------------------------------------------------------------------
# F — Token safety in audit responses
# ---------------------------------------------------------------------------


class TokenSafetyInAuditTests(_AuditAPIBase):
    def test_audit_responses_do_not_echo_token(self):
        with _env(**_enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client, idx=70)
                dec = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "approve",
                        "reviewer_id": "local_reviewer",
                        "decision_source": "review_ui",
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                listing = client.get(
                    f"/review/tasks/{task_id}/decisions",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
                detail = client.get(
                    f"/review/tasks/{task_id}",
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        for resp in (dec, listing, detail):
            text = resp.text
            self.assertNotIn(TEST_TOKEN, text,
                             msg=f"token leaked into {resp.url}")
            self.assertNotIn("REVIEW_API_TOKEN", text)
            self.assertNotIn("X-Review-Token", text)
            self.assertNotIn("OPENAI_API_KEY", text)

    def test_reviewer_id_is_operator_supplied_not_token(self):
        # The reviewer_id stored on the row must equal the body value,
        # never the X-Review-Token header value.
        with _env(**_enabled_env()):
            with self._client() as client:
                task_id = self._create_task(client, idx=71)
                resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": "approve",
                        "reviewer_id": "operator-jane",
                    },
                    headers={"X-Review-Token": TEST_TOKEN},
                )
        body = resp.json()
        self.assertEqual(
            (body.get("task") or {}).get("decisions", [{}])[0].get("reviewer_id"),
            "operator-jane",
        )
        # Audit record carries the same value; never the token.
        audit = body.get("audit_record") or {}
        self.assertEqual(audit.get("reviewer_id"), "operator-jane")
        self.assertNotEqual(audit.get("reviewer_id"), TEST_TOKEN)


# ---------------------------------------------------------------------------
# G — Direct unit tests on review_workflow helpers
# ---------------------------------------------------------------------------


class ReviewWorkflowHelperTests(unittest.TestCase):
    def test_normalize_decision_source_defaults(self):
        self.assertEqual(review_workflow.normalize_decision_source(None), "review_api")
        self.assertEqual(review_workflow.normalize_decision_source(""), "review_api")
        self.assertEqual(
            review_workflow.normalize_decision_source("  "), "review_api",
        )
        self.assertEqual(
            review_workflow.normalize_decision_source(None, default="smoke_test"),
            "smoke_test",
        )

    def test_normalize_decision_source_known_values_lowercased(self):
        for src in ("review_api", "REVIEW_UI", "Smoke_Test", "unknown"):
            normalized = review_workflow.normalize_decision_source(src)
            self.assertIn(normalized, review_workflow.KNOWN_DECISION_SOURCES)

    def test_normalize_decision_source_unknown_falls_back_to_unknown(self):
        # Falls back to "unknown" — never silently accepts a fake label.
        self.assertEqual(
            review_workflow.normalize_decision_source("rogue-source"),
            "unknown",
        )

    def test_normalize_decision_source_never_returns_token_literal(self):
        # Even an input that looks like a token literal is mapped to
        # "unknown", not echoed back.
        self.assertEqual(
            review_workflow.normalize_decision_source(
                "sk-abcdef1234567890" * 4
            ),
            "unknown",
        )

    def test_transition_label_basic(self):
        self.assertEqual(
            review_workflow.transition_label("pending_review", "approved"),
            "pending_review → approved",
        )
        self.assertEqual(
            review_workflow.transition_label("pending_review", "pending_review"),
            "pending_review (unchanged)",
        )

    def test_transition_label_missing_sides(self):
        self.assertEqual(
            review_workflow.transition_label(None, "pending_review"),
            "(unknown) → pending_review",
        )
        self.assertEqual(
            review_workflow.transition_label("approved", None),
            "approved → (unknown)",
        )
        self.assertEqual(review_workflow.transition_label(None, None), "(unknown)")

    def test_build_decision_audit_record_preserves_existing_fields(self):
        row = {
            "decision_id": "decision_abc",
            "task_id": "review_xyz",
            "decision": "approve",
            "reviewer_id": "operator-jane",
            "comment": "looks good",
            "public_note": None,
            "previous_status": "pending_review",
            "new_status": "approved",
            "created_at": "2026-05-21T00:00:00.000000+00:00",
            "metadata": {},
            "decision_source": "review_ui",
        }
        audit = review_workflow.build_decision_audit_record(row)
        # Every existing key preserved.
        for key in row:
            self.assertIn(key, audit)
        self.assertEqual(audit["decision_source"], "review_ui")
        self.assertEqual(audit["transition"], "pending_review → approved")
        self.assertEqual(audit["audit_version"], 1)

    def test_build_decision_audit_record_handles_legacy_null_source(self):
        row = {
            "decision_id": "decision_old",
            "previous_status": "pending_review",
            "new_status": "rejected",
            "decision_source": None,  # legacy row before M9.0
        }
        audit = review_workflow.build_decision_audit_record(row)
        self.assertEqual(audit["decision_source"], "unknown")
        self.assertEqual(audit["transition"], "pending_review → rejected")

    def test_build_decision_audit_record_handles_non_dict(self):
        # Defensive — caller might pass None / unexpected type.
        audit = review_workflow.build_decision_audit_record(None)
        self.assertEqual(audit["decision_source"], "unknown")
        self.assertEqual(audit["transition"], "(unknown)")
        self.assertEqual(audit["audit_version"], 1)

    def test_audit_helper_does_not_read_token_env(self):
        # The helper module never references REVIEW_API_TOKEN env. We
        # check this via the source — defensive against future regressions
        # that try to derive identity from the token.
        source = (ROOT / "review_workflow.py").read_text(encoding="utf-8")
        # The literal "REVIEW_API_TOKEN" must not appear in this module —
        # the safety contract is that audit / workflow code never touches
        # the token. (review_auth.py handles the gate.)
        for forbidden in ("REVIEW_API_TOKEN", "X-Review-Token"):
            self.assertNotIn(
                forbidden, source,
                f"review_workflow.py must not reference {forbidden!r}",
            )


# ---------------------------------------------------------------------------
# H — Schema additive migration smoke
# ---------------------------------------------------------------------------


class SchemaMigrationTests(unittest.TestCase):
    def test_decision_source_column_is_idempotent(self):
        import database
        with _temp_db() as (db, _api, _path):
            # Calling _ensure_review_tables a second time must not fail
            # even though decision_source already exists.
            with db.get_connection() as conn:
                db._ensure_review_tables(conn)
                db._ensure_review_tables(conn)
                cursor = conn.execute("PRAGMA table_info(review_decisions)")
                cols = {row[1] for row in cursor.fetchall()}
            self.assertIn("decision_source", cols)
            # Existing columns still present.
            for required in (
                "decision_id", "task_id", "decision", "reviewer_id",
                "comment", "public_note", "previous_status", "new_status",
                "created_at", "metadata_json",
            ):
                self.assertIn(required, cols)


if __name__ == "__main__":
    unittest.main()
