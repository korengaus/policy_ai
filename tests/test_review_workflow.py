"""Phase 2 M8.0: pure-helpers tests for the review workflow module.

Covers the deterministic vocabulary, status-transition matrix, ID
generation, and snapshot extraction. No database, no FastAPI, no
network, no OpenAI.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import review_workflow as rw  # noqa: E402


class VocabularyTests(unittest.TestCase):
    def test_all_status_constants_present(self):
        # Round-trip every documented status through normalize_review_status.
        for status in rw.ALL_STATUSES:
            self.assertEqual(rw.normalize_review_status(status), status)

    def test_all_decision_constants_present(self):
        for decision in rw.ALL_DECISIONS:
            self.assertEqual(rw.normalize_review_decision(decision), decision)

    def test_unknown_status_raises(self):
        with self.assertRaises(rw.ReviewWorkflowError) as cm:
            rw.normalize_review_status("not-a-status")
        self.assertEqual(cm.exception.reason, "unknown_status")

    def test_unknown_decision_raises(self):
        with self.assertRaises(rw.ReviewWorkflowError) as cm:
            rw.normalize_review_decision("nope")
        self.assertEqual(cm.exception.reason, "unknown_decision")

    def test_normalizers_lowercase_and_strip(self):
        self.assertEqual(rw.normalize_review_status("  APPROVED  "), "approved")
        self.assertEqual(rw.normalize_review_decision("Approve"), "approve")


class TransitionMatrixTests(unittest.TestCase):
    def test_pending_review_approve_to_approved(self):
        self.assertEqual(
            rw.validate_status_transition("pending_review", "approve"),
            "approved",
        )

    def test_pending_review_reject_to_rejected(self):
        self.assertEqual(
            rw.validate_status_transition("pending_review", "reject"),
            "rejected",
        )

    def test_pending_review_more_evidence(self):
        self.assertEqual(
            rw.validate_status_transition("pending_review", "needs_more_evidence"),
            "needs_more_evidence",
        )

    def test_needs_more_evidence_can_resolve_to_approved(self):
        self.assertEqual(
            rw.validate_status_transition("needs_more_evidence", "approve"),
            "approved",
        )
        self.assertEqual(
            rw.validate_status_transition("needs_more_evidence", "reject"),
            "rejected",
        )

    def test_comment_does_not_change_status(self):
        # comment leaves status unchanged at every legal status.
        for status in ("pending_review", "needs_more_evidence",
                       "approved", "rejected", "published", "corrected"):
            self.assertEqual(
                rw.validate_status_transition(status, "comment"),
                status,
                msg=f"comment should preserve status={status!r}",
            )

    def test_approved_cannot_be_re_approved(self):
        with self.assertRaises(rw.ReviewWorkflowError) as cm:
            rw.validate_status_transition("approved", "approve")
        self.assertEqual(cm.exception.reason, "transition_not_allowed")

    def test_rejected_cannot_be_re_decided(self):
        for decision in ("approve", "reject", "needs_more_evidence"):
            with self.assertRaises(rw.ReviewWorkflowError) as cm:
                rw.validate_status_transition("rejected", decision)
            self.assertEqual(cm.exception.reason, "transition_not_allowed")

    def test_published_is_not_reachable_in_m80(self):
        # No decision should be able to move a task to ``published``.
        for status in ("pending_review", "needs_more_evidence", "approved"):
            for decision in rw.ALL_DECISIONS:
                try:
                    new_status = rw.validate_status_transition(status, decision)
                except rw.ReviewWorkflowError:
                    continue
                self.assertNotEqual(new_status, "published")
                self.assertNotEqual(new_status, "corrected")


class IdGenerationTests(unittest.TestCase):
    def test_make_review_task_id_is_stable(self):
        a = rw.make_review_task_id(
            result_id="42", job_id="abc", item_index=0,
            claim_text="정부가 보조금을 신설한다",
        )
        b = rw.make_review_task_id(
            result_id="42", job_id="abc", item_index=0,
            claim_text="정부가 보조금을 신설한다",
        )
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("review_"))

    def test_make_review_task_id_changes_with_claim(self):
        a = rw.make_review_task_id(
            result_id="42", job_id="abc", item_index=0, claim_text="X",
        )
        b = rw.make_review_task_id(
            result_id="42", job_id="abc", item_index=0, claim_text="Y",
        )
        self.assertNotEqual(a, b)

    def test_make_review_decision_id_is_unique(self):
        ids = {rw.make_review_decision_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for d in ids:
            self.assertTrue(d.startswith("decision_"))

    def test_idempotency_key_matches_task_identity(self):
        # Same identifying tuple → same key.
        a = rw.make_idempotency_key(
            result_id="1", job_id="j", item_index=0, claim_text="C",
        )
        b = rw.make_idempotency_key(
            result_id="1", job_id="j", item_index=0, claim_text="C",
        )
        self.assertEqual(a, b)
        # Different identifying tuple → different key.
        c = rw.make_idempotency_key(
            result_id="2", job_id="j", item_index=0, claim_text="C",
        )
        self.assertNotEqual(a, c)


class SnapshotExtractionTests(unittest.TestCase):
    def test_handles_empty_payload(self):
        snap = rw.extract_review_snapshot_from_result({})
        self.assertEqual(snap["claim_text"], "")
        self.assertTrue(snap["human_review_required"])
        self.assertEqual(snap["item_index"], 0)

    def test_handles_none_payload(self):
        snap = rw.extract_review_snapshot_from_result(None)
        self.assertEqual(snap["claim_text"], "")
        self.assertTrue(snap["human_review_required"])

    def test_extracts_normalized_claim_first(self):
        payload = {
            "result": {
                "results": [{
                    "title": "Headline",
                    "original_url": "https://example.go.kr/x",
                    "normalized_claims": [
                        {"claim_text": "정부가 청년 보조금을 신설한다."},
                    ],
                    "policy_claims": [
                        {"sentence": "fallback policy claim sentence"},
                    ],
                    "final_decision": {"decision_label": "사실 확인 필요"},
                    "policy_confidence": {"verification_strength": "moderate"},
                    "verification_card": {"x": 1},
                    "debug_summary": {"semantic_evidence_summary": {"a": 1}},
                }],
            },
            "query": "전세사기",
        }
        snap = rw.extract_review_snapshot_from_result(payload, item_index=0)
        self.assertEqual(snap["claim_text"], "정부가 청년 보조금을 신설한다.")
        self.assertEqual(snap["title"], "Headline")
        self.assertEqual(snap["url"], "https://example.go.kr/x")
        self.assertEqual(snap["final_decision"], "사실 확인 필요")
        self.assertEqual(snap["policy_confidence"], "moderate")
        self.assertTrue(snap["has_verification_card"])
        self.assertTrue(snap["has_semantic_evidence_summary"])
        self.assertEqual(snap["query"], "전세사기")

    def test_falls_back_to_policy_claim_when_normalized_missing(self):
        payload = {
            "news_results": [{
                "title": "T",
                "policy_claims": [{"sentence": "정부가 정책을 발표했다."}],
            }],
        }
        snap = rw.extract_review_snapshot_from_result(payload)
        self.assertEqual(snap["claim_text"], "정부가 정책을 발표했다.")

    def test_tolerates_missing_fields(self):
        # Sparse item — only title.
        payload = {"results": [{"title": "OnlyTitle"}]}
        snap = rw.extract_review_snapshot_from_result(payload)
        # claim_text falls back to title via _extract_first_claim.
        self.assertEqual(snap["claim_text"], "OnlyTitle")
        self.assertEqual(snap["title"], "OnlyTitle")
        self.assertEqual(snap["url"], "")
        self.assertEqual(snap["final_decision"], "")

    def test_human_review_required_is_true_by_default(self):
        snap = rw.extract_review_snapshot_from_result(
            {"news_results": [{"normalized_claims": [{"claim_text": "x"}]}]}
        )
        self.assertTrue(snap["human_review_required"])

    def test_item_index_clamps_safely_when_out_of_range(self):
        payload = {"results": [{"normalized_claims": [{"claim_text": "a"}]}]}
        snap = rw.extract_review_snapshot_from_result(payload, item_index=5)
        # Out-of-range falls back to an empty item — no claim extracted.
        self.assertEqual(snap["claim_text"], "")
        self.assertEqual(snap["item_index"], 5)


class SummaryHelpersTests(unittest.TestCase):
    def test_summarize_review_task_strips_internal_fields(self):
        row = {
            "task_id": "t1", "result_id": "42", "job_id": "j",
            "item_index": 0, "status": "pending_review",
            "query": "Q", "claim_text": "C", "title": "T", "url": "U",
            "final_decision": "X", "policy_confidence": "Y",
            "human_review_required": 1,
            "snapshot": {"big": "blob"},  # excluded from summary
            "created_at": "2026-05-21T00:00:00+00:00",
            "updated_at": "2026-05-21T00:00:00+00:00",
        }
        out = rw.summarize_review_task(row)
        self.assertEqual(out["task_id"], "t1")
        self.assertTrue(out["human_review_required"])
        self.assertNotIn("snapshot", out)

    def test_detail_review_task_includes_decisions(self):
        row = {
            "task_id": "t1", "status": "approved",
            "snapshot": {"hint": True},
        }
        out = rw.detail_review_task(row, decisions=[{"decision_id": "d1"}])
        self.assertEqual(out["decisions"], [{"decision_id": "d1"}])
        self.assertEqual(out["snapshot"], {"hint": True})


class IsolationTests(unittest.TestCase):
    def test_review_workflow_does_not_import_verdict_modules(self):
        # The helper must stay independent of verdict-side code so the
        # reviewer layer never mutates final_decision / policy_confidence.
        text = (ROOT / "review_workflow.py").read_text(encoding="utf-8")
        self.assertNotIn("import policy_decision", text)
        self.assertNotIn("import policy_scoring", text)
        self.assertNotIn("import verification_card", text)
        self.assertNotIn("import api_server", text)
        self.assertNotIn("import database", text)


if __name__ == "__main__":
    unittest.main()
