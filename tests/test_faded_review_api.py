"""FADED-CLAIMS Slice 2 — tests for the semi-auto review layer:
admin-gated /review/faded-candidates* + public /api/faded-claims.

Offline: the DB seams (_fetch_faded_rows / _set_faded_status) are
monkeypatched and the admin session gate is exercised both ways
(no session -> 401; require_admin dependency-overridden -> allowed).
No Postgres, no live DB.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402

PENDING_ROWS = [
    {"id": 1, "cluster_stable_id": "aaa", "representative_analysis_id": 101,
     "title": "청년 지원금 도입 검토", "outlet_count": 8,
     "first_at": "2026-05-20T00:00:00+00:00", "last_at": "2026-06-01T00:00:00+00:00",
     "silence_days": 40, "marker_hit": True, "score": 36.9,
     "status": "pending", "reviewed_at": None, "generated_at": "g",
     "ai_recommendation": "approve", "ai_reason": "도입 예고 후 후속 없음",
     "ai_confidence": 0.85, "ai_judged_at": "2026-07-11T00:00:00+00:00"},
]
APPROVED_ROWS = [
    {"id": 2, "cluster_stable_id": "bbb", "representative_analysis_id": 202,
     "title": "전세 대출 대책 발표", "outlet_count": 12,
     "first_at": "2026-05-01T00:00:00+00:00", "last_at": "2026-05-15T00:00:00+00:00",
     "silence_days": 57, "marker_hit": True, "score": 48.5,
     "status": "approved", "reviewed_at": "2026-07-10T00:00:00+00:00",
     "generated_at": "g",
     "ai_recommendation": "approve", "ai_reason": "대책 예고 후 후속 없음",
     "ai_confidence": 0.9, "ai_judged_at": "2026-07-11T00:00:00+00:00"},
]


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)


class _AdminOverrideMixin(_ClientMixin):
    """Simulate an authenticated admin session via dependency override —
    the same require_admin object the routes depend on."""

    def setUp(self):
        api_server.app.dependency_overrides[api_server.require_admin] = (
            lambda: None
        )

    def tearDown(self):
        api_server.app.dependency_overrides.pop(api_server.require_admin, None)


class AdminGatingTests(_ClientMixin, unittest.TestCase):
    def test_review_list_requires_admin_session(self):
        response = self.client.get("/review/faded-candidates")
        self.assertEqual(response.status_code, 401)

    def test_status_post_requires_admin_session(self):
        response = self.client.post(
            "/review/faded-candidates/1/status", json={"status": "approved"},
        )
        self.assertEqual(response.status_code, 401)

    def test_public_endpoint_needs_no_auth(self):
        with patch.object(api_server, "_fetch_faded_rows", return_value=[]):
            response = self.client.get("/api/faded-claims")
        self.assertEqual(response.status_code, 200)


class ReviewListTests(_AdminOverrideMixin, unittest.TestCase):
    def test_pending_list_default(self):
        with patch.object(api_server, "_fetch_faded_admin_rows",
                          return_value=list(PENDING_ROWS)) as seam:
            response = self.client.get("/review/faded-candidates")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["requested_status"], "pending")
        self.assertEqual(body["candidates"][0]["title"], "청년 지원금 도입 검토")
        self.assertEqual(body["candidates"][0]["score"], 36.9)
        seam.assert_called_once_with("pending")

    def test_admin_payload_includes_ai_fields(self):
        # Slice 4a: the ADMIN list carries the AI recommendation (review aid).
        with patch.object(api_server, "_fetch_faded_admin_rows",
                          return_value=list(PENDING_ROWS)):
            body = self.client.get("/review/faded-candidates").json()
        candidate = body["candidates"][0]
        self.assertEqual(candidate["ai_recommendation"], "approve")
        self.assertEqual(candidate["ai_reason"], "도입 예고 후 후속 없음")
        self.assertEqual(candidate["ai_confidence"], 0.85)

    def test_status_param_for_auditing(self):
        with patch.object(api_server, "_fetch_faded_admin_rows",
                          return_value=list(APPROVED_ROWS)) as seam:
            response = self.client.get("/review/faded-candidates",
                                       params={"status": "approved"})
        self.assertEqual(response.json()["requested_status"], "approved")
        seam.assert_called_once_with("approved")

    def test_invalid_status_param_rejected(self):
        response = self.client.get("/review/faded-candidates",
                                   params={"status": "everything"})
        self.assertEqual(response.status_code, 400)

    def test_db_error_returns_empty_not_500(self):
        with patch.object(api_server, "_fetch_faded_admin_rows",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/review/faded-candidates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidates"], [])


class StatusUpdateTests(_AdminOverrideMixin, unittest.TestCase):
    def test_approve_sets_status_and_reviewed_at(self):
        with patch.object(api_server, "_set_faded_status",
                          return_value=True) as seam:
            response = self.client.post(
                "/review/faded-candidates/7/status",
                json={"status": "approved"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["new_status"], "approved")
        self.assertTrue(body["reviewed_at"])
        args = seam.call_args[0]
        self.assertEqual(args[0], 7)
        self.assertEqual(args[1], "approved")

    def test_dismiss_allowed(self):
        with patch.object(api_server, "_set_faded_status", return_value=True):
            response = self.client.post(
                "/review/faded-candidates/7/status",
                json={"status": "dismissed"},
            )
        self.assertEqual(response.json()["new_status"], "dismissed")

    def test_other_values_rejected(self):
        for bad in ("pending", "published", "verified", "", "APPROVED!"):
            with patch.object(api_server, "_set_faded_status",
                              return_value=True) as seam:
                response = self.client.post(
                    "/review/faded-candidates/7/status",
                    json={"status": bad},
                )
            self.assertEqual(response.status_code, 400, bad)
            seam.assert_not_called()

    def test_unknown_id_404(self):
        with patch.object(api_server, "_set_faded_status", return_value=False):
            response = self.client.post(
                "/review/faded-candidates/999/status",
                json={"status": "approved"},
            )
        self.assertEqual(response.status_code, 404)


class PublicFadedClaimsTests(_ClientMixin, unittest.TestCase):
    def test_serves_approved_only_via_bound_status(self):
        with patch.object(api_server, "_fetch_faded_rows",
                          return_value=list(APPROVED_ROWS)) as seam:
            response = self.client.get("/api/faded-claims")
        # The ONLY query the public route can make is status='approved'.
        seam.assert_called_once_with("approved")
        body = response.json()
        self.assertEqual(len(body["claims"]), 1)
        claim = body["claims"][0]
        self.assertEqual(claim["representative_analysis_id"], 202)
        self.assertEqual(claim["outlet_count"], 12)
        self.assertEqual(claim["silence_days"], 57)
        # Slim public shape: no curation/internal fields leak — including the
        # Slice-4a AI recommendation fields (operator-side review aid ONLY;
        # the fixture row carries them, the public payload must not).
        self.assertNotIn("status", claim)
        self.assertNotIn("reviewed_at", claim)
        self.assertNotIn("score", claim)
        self.assertNotIn("ai_recommendation", claim)
        self.assertNotIn("ai_reason", claim)
        self.assertNotIn("ai_confidence", claim)
        self.assertNotIn("ai_judged_at", claim)

    def test_framing_always_present(self):
        with patch.object(api_server, "_fetch_faded_rows", return_value=[]):
            body = self.client.get("/api/faded-claims").json()
        self.assertEqual(body["claims"], [])
        self.assertIn("후속 보도가 끊긴 사실만", body["framing"])
        self.assertIn("진위", body["framing"])
        self.assertIn("수집망 밖", body["framing"])

    def test_error_returns_empty_not_500(self):
        with patch.object(api_server, "_fetch_faded_rows",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/faded-claims")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["claims"], [])
        self.assertTrue(body["framing"])

    def test_cache_control(self):
        with patch.object(api_server, "_fetch_faded_rows", return_value=[]):
            response = self.client.get("/api/faded-claims")
        self.assertEqual(response.headers.get("cache-control"), "max-age=120")


if __name__ == "__main__":
    unittest.main()
