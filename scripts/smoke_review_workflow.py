"""Phase 2 M8.3: self-contained smoke for the server-backed review workflow.

Spins up the existing FastAPI app against a temporary SQLite database and
exercises the M8.0–M8.2 review surface end-to-end with a dummy in-process
token. Never calls OpenAI, never calls Render, never makes external network
calls, never prints the token.

Usage:
    python scripts/smoke_review_workflow.py --self-contained

Intended to be wired into ``scripts/run_operational_checks.py --profile
review-local``. Verdict logic (``policy_decision`` / ``policy_scoring`` /
``verification_card``) is not imported here. The smoke only inspects the
review surface and verifies that verdict-side snapshot fields stay stable
across decision actions.

Exit codes:
    0 — every check passed
    1 — at least one check failed
    2 — bad CLI args (e.g. ``--self-contained`` missing)
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# A dummy local-only token used exclusively by this script's in-process
# smoke run. It is NOT a real review token, is NEVER logged, NEVER
# persisted, and NEVER printed to stdout/stderr or the JSON summary —
# every check below sends it as the ``X-Review-Token`` header value only.
_DUMMY_TOKEN = "smoke-dummy-token-internal-only-do-not-publish"  # noqa: S105

# Allowed review decisions (kept in lockstep with review_workflow.ALL_DECISIONS).
# Listed here defensively so the smoke fails loudly if the workflow vocabulary
# changes without an explicit M8.x migration of this script.
_EXPECTED_DECISIONS = ("approve", "reject", "needs_more_evidence", "comment")

# Reserved status names that must NOT be reachable from any decision in the
# review workflow. M8.3 pins this contract operationally.
_RESERVED_STATUSES = ("published", "corrected")


# ---------------------------------------------------------------------------
# Synthetic payload helpers (no live analysis, conservative wording)
# ---------------------------------------------------------------------------


def _conservative_synthetic_payload(*, claim: str, title: str, url: str) -> dict:
    """Return a ``/jobs/{id}/result``-shaped payload with conservative wording.

    The verdict labels deliberately mirror the conservative copy the live
    pipeline emits (``사람 검토 필요`` / ``moderate``) — they do NOT use
    ``100%`` / ``사실`` / ``거짓`` style language. The snapshot extractor
    will read these labels through; the smoke later asserts they remain
    stable across review decisions.
    """
    return {
        "status": "ok",
        "result": {
            "results": [{
                "title": title,
                "original_url": url,
                "normalized_claims": [{"claim_text": claim}],
                "final_decision": {"decision_label": "사람 검토 필요"},
                "policy_confidence": {"verification_strength": "moderate"},
                "verification_card": {
                    "summary": "공식 출처 확인 필요 — 사람 검토 대기",
                    "status": "pending_review",
                },
                "debug_summary": {"semantic_evidence_summary": {"placeholder": True}},
            }],
        },
        "query": "정책 검수 스모크",
    }


# ---------------------------------------------------------------------------
# Env / DB context managers — every one of these restores state on exit.
# ---------------------------------------------------------------------------


@contextmanager
def _temp_review_env(token: str):
    """Set REVIEW_API_ENABLED / REVIEW_API_TOKEN only for the with-block."""
    keys = ("REVIEW_API_ENABLED", "REVIEW_API_TOKEN")
    original = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["REVIEW_API_ENABLED"] = "true"
        os.environ["REVIEW_API_TOKEN"] = token
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _disabled_review_env():
    """Clear review env so the disabled-by-default behavior is exercised."""
    keys = ("REVIEW_API_ENABLED", "REVIEW_API_TOKEN")
    original = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _temp_sqlite_database():
    """Point ``database.DB_PATH`` at a fresh temp file and reload api_server.

    The api_server module reload is required because the FastAPI app may
    already have been imported by a prior test in the same process — reloading
    ensures any startup side-effects (init_db on lifespan) hit the temp DB.
    The original DB_PATH is restored on exit; the reloaded api_server module
    is left in place intentionally so subsequent operations within the same
    process see a consistent FastAPI app.
    """
    import database
    tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db_path = Path(tmp_dir.name) / "review_smoke.db"
    previous = database.DB_PATH
    database.DB_PATH = db_path
    try:
        database.init_db()
        import api_server  # noqa: F401
        importlib.reload(api_server)
        yield database, api_server, db_path
    finally:
        database.DB_PATH = previous
        try:
            tmp_dir.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Individual checks. Every check returns a small dict whose ``passed`` field
# the consolidator uses to compute the overall result.
# ---------------------------------------------------------------------------


def _check_disabled_default(api_server) -> dict:
    """A. With REVIEW_API_ENABLED unset, the API must return 503."""
    from fastapi.testclient import TestClient
    with _disabled_review_env():
        with TestClient(api_server.app) as client:
            resp = client.get("/review/tasks")
    detail = ""
    try:
        if resp.headers.get("content-type", "").startswith("application/json"):
            detail = str(resp.json().get("detail", ""))
    except Exception:
        detail = ""
    return {
        "passed": resp.status_code == 503 and "disabled" in detail.lower(),
        "status_code": resp.status_code,
        "detail_contains_disabled": "disabled" in detail.lower(),
    }


def _check_token_behavior(api_server, token: str) -> dict:
    """B. Missing/wrong tokens fail with 403, correct token gets 200."""
    from fastapi.testclient import TestClient
    results: Dict[str, Any] = {}
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            no_header = client.get("/review/tasks")
            results["missing_header_status"] = no_header.status_code
            results["missing_header_403"] = no_header.status_code == 403

            wrong = client.get(
                "/review/tasks",
                headers={"X-Review-Token": "definitely-not-the-token"},
            )
            results["wrong_token_status"] = wrong.status_code
            results["wrong_token_403"] = wrong.status_code == 403

            ok = client.get(
                "/review/tasks",
                headers={"X-Review-Token": token},
            )
            results["correct_token_status"] = ok.status_code
            results["correct_token_200"] = ok.status_code == 200
    results["passed"] = bool(
        results["missing_header_403"]
        and results["wrong_token_403"]
        and results["correct_token_200"]
    )
    return results


def _post_from_result(client, token: str, body: dict):
    return client.post(
        "/review/tasks/from-result",
        json=body,
        headers={"X-Review-Token": token},
    )


def _check_task_creation(api_server, token: str) -> Tuple[dict, dict]:
    """C. POST /review/tasks/from-result with a synthetic payload."""
    from fastapi.testclient import TestClient
    payload = _conservative_synthetic_payload(
        claim="정부는 청년 보조금 지원안을 발표했다.",
        title="청년 보조금 지원안 발표",
        url="https://example.go.kr/policy/youth-support",
    )
    original_payload = copy.deepcopy(payload)
    body = {
        "result_id": "smoke-result-1",
        "job_id": "smoke-job-1",
        "item_index": 0,
        "result_payload": payload,
    }
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            resp = _post_from_result(client, token, body)
    ok = resp.status_code == 200
    task = (resp.json().get("task") or {}) if ok else {}
    snapshot = task.get("snapshot") or {}
    final_decision_ok = snapshot.get("final_decision") == "사람 검토 필요"
    confidence_ok = snapshot.get("policy_confidence") == "moderate"
    check = {
        "passed": bool(
            ok
            and task.get("status") == "pending_review"
            and task.get("human_review_required") is True
            and bool(task.get("claim_text"))
            and final_decision_ok
            and confidence_ok
            and payload == original_payload
        ),
        "status_code": resp.status_code,
        "task_status": task.get("status"),
        "human_review_required": task.get("human_review_required"),
        "snapshot_final_decision_unchanged": final_decision_ok,
        "snapshot_policy_confidence_unchanged": confidence_ok,
        "original_payload_unchanged": payload == original_payload,
    }
    return check, task


def _check_idempotency(api_server, token: str) -> dict:
    """D. POST /review/tasks/from-result twice → same task id + idempotent flag."""
    from fastapi.testclient import TestClient
    payload = _conservative_synthetic_payload(
        claim="청년 보조금 지원안의 시행 시점은 추후 발표 예정이다.",
        title="청년 보조금 시행 시점",
        url="https://example.go.kr/policy/youth-support/schedule",
    )
    body = {
        "result_id": "smoke-result-2",
        "job_id": "smoke-job-2",
        "item_index": 0,
        "result_payload": payload,
    }
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            first = _post_from_result(client, token, body)
            second = _post_from_result(client, token, body)
    first_body = first.json() if first.status_code == 200 else {}
    second_body = second.json() if second.status_code == 200 else {}
    first_id = (first_body.get("task") or {}).get("task_id")
    second_id = (second_body.get("task") or {}).get("task_id")
    second_idempotent = bool(second_body.get("idempotent"))
    return {
        "passed": bool(
            first.status_code == 200
            and second.status_code == 200
            and first_id
            and first_id == second_id
            and second_idempotent
        ),
        "first_status_code": first.status_code,
        "second_status_code": second.status_code,
        "task_ids_match": first_id == second_id,
        "second_idempotent": second_idempotent,
    }


def _check_list_detail(api_server, token: str, expected_task_id: str) -> dict:
    """E. GET /review/tasks and GET /review/tasks/{id} surface the task."""
    from fastapi.testclient import TestClient
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            list_resp = client.get(
                "/review/tasks", headers={"X-Review-Token": token},
            )
            detail_resp = client.get(
                f"/review/tasks/{expected_task_id}",
                headers={"X-Review-Token": token},
            )
    list_ok = list_resp.status_code == 200
    list_body = list_resp.json() if list_ok else {}
    tasks = list_body.get("tasks") or []
    task_ids = {t.get("task_id") for t in tasks}
    detail_ok = detail_resp.status_code == 200
    detail_body = detail_resp.json() if detail_ok else {}
    detail_task = detail_body.get("task") or {}
    return {
        "passed": bool(
            list_ok
            and detail_ok
            and expected_task_id in task_ids
            and detail_task.get("task_id") == expected_task_id
            and detail_task.get("status") == "pending_review"
        ),
        "list_status_code": list_resp.status_code,
        "detail_status_code": detail_resp.status_code,
        "list_count": len(tasks),
        "expected_task_visible": expected_task_id in task_ids,
    }


def _check_decisions(api_server, token: str) -> dict:
    """F. Exercise every allowed decision against a fresh task.

    Phase 2 M9.0 — also asserts the audit fields appear in the POST
    decision response and in the embedded decisions list (transition,
    decision_source, audit_version, decision_id, previous/new status).
    """
    from fastapi.testclient import TestClient
    decision_to_expected_status = {
        "approve": "approved",
        "reject": "rejected",
        "needs_more_evidence": "needs_more_evidence",
        # comment-only decisions never change status.
        "comment": "pending_review",
    }
    per_decision: Dict[str, dict] = {}
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            for idx, decision in enumerate(_EXPECTED_DECISIONS):
                payload = _conservative_synthetic_payload(
                    claim=f"청년 보조금 정책 검토 항목 {idx} — 사람 검토 대기.",
                    title=f"검수 스모크 청구항 {idx}",
                    url=f"https://example.go.kr/policy/youth-support/{idx}",
                )
                body = {
                    "result_id": f"smoke-decision-{idx}",
                    "job_id": f"smoke-decision-job-{idx}",
                    "item_index": 0,
                    "result_payload": payload,
                }
                create = _post_from_result(client, token, body)
                if create.status_code != 200:
                    per_decision[decision] = {
                        "passed": False,
                        "create_status_code": create.status_code,
                    }
                    continue
                task_id = (create.json().get("task") or {}).get("task_id")
                dec_resp = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={
                        "decision": decision,
                        "reviewer_id": "smoke-local",
                        "comment": "smoke-only",
                        # M9.0 — operator-supplied audit label, not auth.
                        "decision_source": "smoke_test",
                    },
                    headers={"X-Review-Token": token},
                )
                dec_body = dec_resp.json() if dec_resp.status_code == 200 else {}
                new_status = dec_body.get("new_status")
                prev_status = dec_body.get("previous_status")
                expected_new = decision_to_expected_status[decision]
                expected_prev = "pending_review"
                expected_transition = (
                    f"{expected_prev} (unchanged)"
                    if expected_new == expected_prev
                    else f"{expected_prev} → {expected_new}"
                )
                transition_ok = dec_body.get("transition") == expected_transition
                audit_record = dec_body.get("audit_record") or {}
                source_ok = (
                    dec_body.get("decision_source") == "smoke_test"
                    and audit_record.get("decision_source") == "smoke_test"
                )
                audit_version_ok = (
                    dec_body.get("audit_version") == 1
                    and audit_record.get("audit_version") == 1
                )
                decision_id_ok = (
                    bool(dec_body.get("decision_id"))
                    and audit_record.get("decision_id") == dec_body.get("decision_id")
                )
                per_decision[decision] = {
                    "passed": (
                        dec_resp.status_code == 200
                        and new_status == expected_new
                        and prev_status == expected_prev
                        and transition_ok
                        and source_ok
                        and audit_version_ok
                        and decision_id_ok
                    ),
                    "decision_status_code": dec_resp.status_code,
                    "new_status": new_status,
                    "previous_status": prev_status,
                    "expected_status": expected_new,
                    "transition": dec_body.get("transition"),
                    "transition_matches": transition_ok,
                    "decision_source": dec_body.get("decision_source"),
                    "audit_version": dec_body.get("audit_version"),
                    "decision_id_present": decision_id_ok,
                }
    return {
        "passed": all(d.get("passed") for d in per_decision.values()),
        "allowed_decisions": list(_EXPECTED_DECISIONS),
        "decisions": per_decision,
    }


def _check_verdict_isolation(api_server, token: str) -> dict:
    """G. Review actions must not mutate verdict-side snapshot fields or payload."""
    from fastapi.testclient import TestClient
    payload = _conservative_synthetic_payload(
        claim="청년 보조금 — 검수 후 verdict 비교 청구항.",
        title="verdict-isolation 검수 스모크",
        url="https://example.go.kr/policy/youth-support/isolation",
    )
    original = copy.deepcopy(payload)
    body = {
        "result_id": "smoke-isolation-1",
        "job_id": "smoke-isolation-job",
        "item_index": 0,
        "result_payload": payload,
    }
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            create = _post_from_result(client, token, body)
            if create.status_code != 200:
                return {
                    "passed": False,
                    "reason": "could not create isolation task",
                    "status_code": create.status_code,
                }
            created_task = create.json().get("task") or {}
            task_id = created_task.get("task_id")
            snapshot_after_create = created_task.get("snapshot") or {}
            # Record a comment, then approve — neither should rewrite verdict fields.
            client.post(
                f"/review/tasks/{task_id}/decision",
                json={"decision": "comment", "comment": "isolation comment"},
                headers={"X-Review-Token": token},
            )
            client.post(
                f"/review/tasks/{task_id}/decision",
                json={"decision": "approve", "reviewer_id": "smoke-local"},
                headers={"X-Review-Token": token},
            )
            detail = client.get(
                f"/review/tasks/{task_id}",
                headers={"X-Review-Token": token},
            )
    payload_unchanged = payload == original
    detail_body = detail.json() if detail.status_code == 200 else {}
    detail_task = detail_body.get("task") or {}
    snapshot_after_decision = detail_task.get("snapshot") or {}
    final_decision_stable = (
        snapshot_after_create.get("final_decision")
        == snapshot_after_decision.get("final_decision")
        == "사람 검토 필요"
    )
    confidence_stable = (
        snapshot_after_create.get("policy_confidence")
        == snapshot_after_decision.get("policy_confidence")
        == "moderate"
    )
    vc_stable = (
        original["result"]["results"][0]["verification_card"]
        == payload["result"]["results"][0]["verification_card"]
    )
    return {
        "passed": bool(
            payload_unchanged
            and final_decision_stable
            and confidence_stable
            and vc_stable
        ),
        "payload_unchanged": payload_unchanged,
        "final_decision_label_stable": final_decision_stable,
        "policy_confidence_label_stable": confidence_stable,
        "verification_card_unchanged": vc_stable,
    }


def _check_audit_trail(api_server, token: str) -> dict:
    """I. Phase 2 M9.0 — verify decision audit trail exposes audit fields.

    Records two decisions (comment then approve) against a fresh task,
    then re-fetches the decisions list and asserts every audit field is
    present, transition labels are correct, and no token-shaped literal
    leaked into the response. ``decision_source`` defaults to
    ``review_api`` when the client omits it, and respects ``smoke_test``
    when provided.
    """
    from fastapi.testclient import TestClient
    import re as _re

    payload = _conservative_synthetic_payload(
        claim="감사 추적 검수 청구항 — M9.0 audit trail check.",
        title="audit-trail 검수 스모크",
        url="https://example.go.kr/policy/youth-support/audit",
    )
    body = {
        "result_id": "smoke-audit-1",
        "job_id": "smoke-audit-job",
        "item_index": 0,
        "result_payload": payload,
    }
    token_shaped = _re.compile(r"[0-9a-fA-F]{32,}")
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            create = _post_from_result(client, token, body)
            if create.status_code != 200:
                return {
                    "passed": False,
                    "reason": "could not create audit task",
                    "status_code": create.status_code,
                }
            task_id = (create.json().get("task") or {}).get("task_id")

            # Decision 1: comment with explicit decision_source.
            comment_resp = client.post(
                f"/review/tasks/{task_id}/decision",
                json={
                    "decision": "comment",
                    "reviewer_id": "smoke-local",
                    "comment": "audit smoke comment",
                    "decision_source": "smoke_test",
                },
                headers={"X-Review-Token": token},
            )
            # Decision 2: approve WITHOUT decision_source — must default
            # to "review_api" server-side (operator label, not auth).
            approve_resp = client.post(
                f"/review/tasks/{task_id}/decision",
                json={
                    "decision": "approve",
                    "reviewer_id": "smoke-local",
                },
                headers={"X-Review-Token": token},
            )
            list_resp = client.get(
                f"/review/tasks/{task_id}/decisions",
                headers={"X-Review-Token": token},
            )

    comment_body = comment_resp.json() if comment_resp.status_code == 200 else {}
    approve_body = approve_resp.json() if approve_resp.status_code == 200 else {}
    list_body = list_resp.json() if list_resp.status_code == 200 else {}

    comment_audit_ok = (
        comment_resp.status_code == 200
        and comment_body.get("transition") == "pending_review (unchanged)"
        and comment_body.get("decision_source") == "smoke_test"
        and comment_body.get("audit_version") == 1
        and bool(comment_body.get("decision_id"))
        and bool((comment_body.get("audit_record") or {}).get("created_at"))
    )
    approve_audit_ok = (
        approve_resp.status_code == 200
        and approve_body.get("transition") == "pending_review → approved"
        and approve_body.get("decision_source") == "review_api"
        and approve_body.get("audit_version") == 1
        and bool(approve_body.get("decision_id"))
    )
    decisions = list_body.get("decisions") or []
    list_audit_ok = (
        list_resp.status_code == 200
        and list_body.get("audit_version") == 1
        and len(decisions) == 2
        and all(d.get("audit_version") == 1 for d in decisions)
        and all(bool(d.get("transition")) for d in decisions)
        and all(bool(d.get("decision_id")) for d in decisions)
        and {d.get("decision_source") for d in decisions} == {
            "smoke_test", "review_api",
        }
    )
    serialized = json.dumps(list_body, ensure_ascii=False)
    no_token_leak = (
        token not in serialized
        and not token_shaped.search(serialized)
    )

    return {
        "passed": bool(
            comment_audit_ok and approve_audit_ok
            and list_audit_ok and no_token_leak
        ),
        "comment_audit_ok": comment_audit_ok,
        "approve_audit_ok": approve_audit_ok,
        "list_audit_ok": list_audit_ok,
        "no_token_leak_in_decision_list": no_token_leak,
        "comment_transition": comment_body.get("transition"),
        "approve_transition": approve_body.get("transition"),
        "list_count": len(decisions),
    }


def _check_audit_packet(api_server, token: str) -> dict:
    """J. Phase 2 M9.1 — internal reviewer audit packet endpoint.

    Creates a fresh task, records an approve decision, then GETs the
    audit-packet endpoint and asserts:

        * disabled-default response (503 with "disabled" detail) when
          REVIEW_API_ENABLED is unset
        * 200 + the M9.1 packet shape when enabled with a correct token
        * 404 for a missing task
        * the packet's safety_contract block has the expected values
        * the packet's review_decisions list carries M9.0 audit fields
        * verdict snapshot fields are unchanged (verdict isolation)
        * the dummy token never appears in the JSON body
    """
    from fastapi.testclient import TestClient
    import re as _re

    payload = _conservative_synthetic_payload(
        claim="감사 패킷 — M9.1 audit packet check.",
        title="audit-packet 검수 스모크",
        url="https://example.go.kr/policy/youth-support/packet",
    )
    body = {
        "result_id": "smoke-audit-packet-1",
        "job_id": "smoke-audit-packet-job",
        "item_index": 0,
        "result_payload": payload,
    }
    token_shaped = _re.compile(r"[0-9a-fA-F]{32,}")

    # Step 1 — disabled-default: 503 without enabling the gate.
    with _disabled_review_env():
        with TestClient(api_server.app) as client:
            disabled_resp = client.get(
                "/review/tasks/nonexistent/audit-packet"
            )
    disabled_detail = ""
    try:
        if disabled_resp.headers.get("content-type", "").startswith("application/json"):
            disabled_detail = str(disabled_resp.json().get("detail", ""))
    except Exception:
        disabled_detail = ""
    disabled_ok = (
        disabled_resp.status_code == 503
        and "disabled" in disabled_detail.lower()
    )

    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            # Step 2 — missing task → 404.
            missing_resp = client.get(
                "/review/tasks/definitely-missing-task/audit-packet",
                headers={"X-Review-Token": token},
            )
            missing_ok = missing_resp.status_code == 404

            # Step 3 — create a task + record a decision so the packet
            # carries a non-empty review_decisions list.
            create = _post_from_result(client, token, body)
            if create.status_code != 200:
                return {
                    "passed": False,
                    "reason": "could not create audit-packet task",
                    "status_code": create.status_code,
                }
            task_id = (create.json().get("task") or {}).get("task_id")
            client.post(
                f"/review/tasks/{task_id}/decision",
                json={
                    "decision": "approve",
                    "reviewer_id": "smoke-local",
                    "comment": "audit-packet smoke",
                    "decision_source": "smoke_test",
                },
                headers={"X-Review-Token": token},
            )

            # Step 4 — fetch the audit packet.
            packet_resp = client.get(
                f"/review/tasks/{task_id}/audit-packet",
                headers={"X-Review-Token": token},
            )
            packet_body = packet_resp.json() if packet_resp.status_code == 200 else {}

    packet_shape_ok = (
        packet_resp.status_code == 200
        and packet_body.get("packet_type") == "internal_review_audit_packet"
        and packet_body.get("audit_version") == 1
        and bool(packet_body.get("generated_at"))
        and isinstance(packet_body.get("task"), dict)
        and isinstance(packet_body.get("verdict_snapshot"), dict)
        and isinstance(packet_body.get("source_snapshot"), dict)
        and isinstance(packet_body.get("review_decisions"), list)
        and isinstance(packet_body.get("safety_contract"), dict)
    )

    safety = packet_body.get("safety_contract") or {}
    safety_ok = (
        safety.get("publication") is False
        and safety.get("mutates_original_result") is False
        and safety.get("mutates_final_decision") is False
        and safety.get("mutates_policy_confidence") is False
        and safety.get("mutates_verification_card") is False
        and safety.get("semantic_matching_debug_only") is True
        and safety.get("human_review_required") is True
    )

    verdict = packet_body.get("verdict_snapshot") or {}
    verdict_isolation_ok = (
        verdict.get("final_decision") == "사람 검토 필요"
        and verdict.get("policy_confidence") == "moderate"
        and verdict.get("verification_card_status") == "pending_review"
    )

    decisions = packet_body.get("review_decisions") or []
    decisions_ok = (
        len(decisions) == 1
        and decisions[0].get("decision") == "approve"
        and decisions[0].get("transition") == "pending_review → approved"
        and decisions[0].get("decision_source") == "smoke_test"
        and decisions[0].get("audit_version") == 1
        and bool(decisions[0].get("decision_id"))
    )

    serialized = json.dumps(packet_body, ensure_ascii=False)
    no_token_leak = (
        token not in serialized
        and not token_shaped.search(serialized)
        and "REVIEW_API_TOKEN" not in serialized
        and "X-Review-Token" not in serialized
    )

    return {
        "passed": bool(
            disabled_ok and missing_ok and packet_shape_ok
            and safety_ok and verdict_isolation_ok
            and decisions_ok and no_token_leak
        ),
        "disabled_response_ok": disabled_ok,
        "missing_task_404_ok": missing_ok,
        "packet_shape_ok": packet_shape_ok,
        "safety_contract_ok": safety_ok,
        "verdict_isolation_ok": verdict_isolation_ok,
        "decisions_in_packet_ok": decisions_ok,
        "no_token_leak_in_packet": no_token_leak,
        "packet_keys": sorted(list(packet_body.keys())),
    }


def _check_publication_absent(api_server, token: str) -> dict:
    """H. No /publish endpoint exists; reserved status names are unreachable."""
    from fastapi.testclient import TestClient
    payload = _conservative_synthetic_payload(
        claim="청년 보조금 — publication-absent 청구항.",
        title="publication 차단 확인",
        url="https://example.go.kr/policy/youth-support/no-publish",
    )
    body = {
        "result_id": "smoke-no-publish-1",
        "job_id": "smoke-no-publish-job",
        "item_index": 0,
        "result_payload": payload,
    }
    with _temp_review_env(token):
        with TestClient(api_server.app) as client:
            create = _post_from_result(client, token, body)
            if create.status_code != 200:
                return {
                    "passed": False,
                    "reason": "could not create task for publication check",
                    "status_code": create.status_code,
                }
            task_id = (create.json().get("task") or {}).get("task_id")
            publish = client.post(
                f"/review/tasks/{task_id}/publish",
                headers={"X-Review-Token": token},
            )
            reserved_attempts: Dict[str, int] = {}
            for status_name in _RESERVED_STATUSES:
                bad = client.post(
                    f"/review/tasks/{task_id}/decision",
                    json={"decision": status_name},
                    headers={"X-Review-Token": token},
                )
                reserved_attempts[status_name] = bad.status_code
    publish_blocked = publish.status_code in (404, 405)
    reserved_blocked = all(
        code in (400, 409, 422) for code in reserved_attempts.values()
    )
    return {
        "passed": bool(publish_blocked and reserved_blocked),
        "publish_status_code": publish.status_code,
        "publish_blocked": publish_blocked,
        "reserved_decision_attempts": reserved_attempts,
        "reserved_blocked": reserved_blocked,
    }


# ---------------------------------------------------------------------------
# Consolidation + entry point
# ---------------------------------------------------------------------------


CHECK_KEYS = (
    "disabled_check",
    "token_check",
    "task_creation_check",
    "idempotency_check",
    "list_detail_check",
    "decision_check",
    "verdict_isolation_check",
    "publication_absent_check",
    "audit_trail_check",   # M9.0
    "audit_packet_check",  # M9.1
)


def run_self_contained() -> dict:
    """Execute every check in order and return the consolidated summary."""
    token = _DUMMY_TOKEN
    summary: Dict[str, Any] = {
        "mode": "self-contained",
        "passed": False,
    }
    for key in CHECK_KEYS:
        summary[key] = {"passed": False, "reason": "not run"}

    with _temp_sqlite_database() as (_database, api_server, _db_path):
        summary["disabled_check"] = _check_disabled_default(api_server)
        summary["token_check"] = _check_token_behavior(api_server, token)
        creation, task = _check_task_creation(api_server, token)
        summary["task_creation_check"] = creation
        summary["idempotency_check"] = _check_idempotency(api_server, token)
        created_task_id = task.get("task_id") or ""
        if created_task_id:
            summary["list_detail_check"] = _check_list_detail(
                api_server, token, created_task_id,
            )
        else:
            summary["list_detail_check"] = {
                "passed": False,
                "reason": "task_creation_check did not return a task_id",
            }
        summary["decision_check"] = _check_decisions(api_server, token)
        summary["verdict_isolation_check"] = _check_verdict_isolation(api_server, token)
        summary["publication_absent_check"] = _check_publication_absent(api_server, token)
        summary["audit_trail_check"] = _check_audit_trail(api_server, token)
        summary["audit_packet_check"] = _check_audit_packet(api_server, token)

    summary["passed"] = all(bool(summary[k].get("passed")) for k in CHECK_KEYS)
    return summary


def _print_summary(summary: dict) -> None:
    print("[smoke-review] self-contained run")
    for key in CHECK_KEYS:
        print(f"  {key:<26}: {bool(summary[key].get('passed'))}")
    print(f"  passed={summary['passed']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Self-contained smoke for the M8.0–M8.2 server-backed review "
            "workflow. Uses a temp SQLite DB + FastAPI TestClient + a dummy "
            "in-process token. Never calls OpenAI, never calls Render, never "
            "prints the token, never modifies Render env."
        ),
    )
    parser.add_argument(
        "--self-contained", action="store_true",
        help="Run the offline smoke. Currently the only supported mode.",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None,
        help=(
            "Optional path to write the JSON summary in addition to stdout. "
            "The summary never contains the dummy token."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.self_contained:
        print(
            "[smoke-review] --self-contained is required "
            "(no live/Render mode is supported by this script).",
            file=sys.stderr,
        )
        return 2
    summary = run_self_contained()
    _print_summary(summary)
    body = json.dumps(summary, ensure_ascii=False, indent=2)
    print(body)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(body, encoding="utf-8")
        print(f"[smoke-review] JSON summary written to {args.json_out}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
