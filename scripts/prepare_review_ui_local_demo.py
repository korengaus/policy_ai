"""Phase 2 M9.3: local reviewer UI activation dry-run helper.

A **local-only** developer / operator tool that seeds a small,
conservative-wording SQLite database operators can point the FastAPI
app at to exercise the M8.x/M9.x reviewer/admin UI surface end-to-end —
including the M9.2 audit packet viewer — without enabling the review
API on Render and without using any real ``REVIEW_API_TOKEN``.

Hard contract:
    * Never reads any shared review secret from the environment.
    * Never reads any shared OpenAI key from the environment.
    * Never modifies Render env / ``render.yaml``.
    * Never spawns a server (the operator runs uvicorn explicitly).
    * Never calls OpenAI, Render, or any external network.
    * Writes only under ``reports/`` by default; ``--db-path`` is
      refused outside ``reports/`` for blast-radius safety.
    * Does not stage / commit / push.
    * Does not import semantic / verdict / provider modules.

Usage:

    python scripts/prepare_review_ui_local_demo.py
    python scripts/prepare_review_ui_local_demo.py --reset
    python scripts/prepare_review_ui_local_demo.py --json
    python scripts/prepare_review_ui_local_demo.py --verify
    python scripts/prepare_review_ui_local_demo.py --token "my-dummy-tag" --reset

Exit codes:
    0 — demo prepared (and ``--verify`` succeeded when requested)
    1 — refused (existing DB without ``--reset``) or verify failed
    2 — bad CLI usage (empty token, unsafe path, …)
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib
import io
import json
import os
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# A clearly-labeled local dummy token. Not a real secret. Used only by
# this helper's printed runbook + ``--verify`` mode. The operator is
# free to override with ``--token``; the script never reads any real
# ``REVIEW_API_TOKEN`` from the environment.
DEFAULT_DEMO_TOKEN = "local-review-demo-token"

DEMO_DB_FILENAME = "review_ui_local_demo.sqlite"
REPORTS_DIR_NAME = "reports"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


# ---------------------------------------------------------------------------
# Demo data — conservative Korean wording only.
# ---------------------------------------------------------------------------


def _demo_payloads() -> List[Dict]:
    """One synthetic, conservative-wording review snippet.

    Mirrors the shape ``review_workflow.extract_review_snapshot_from_result``
    expects so seeding goes through the same code path the real
    pipeline uses, ensuring the audit packet works end-to-end against
    the seeded row.
    """
    return [
        {
            "result_id": "demo-result-1",
            "job_id": "demo-job-1",
            "item_index": 0,
            "query": "청년 월세 지원 정책",
            "payload": {
                "status": "ok",
                "query": "청년 월세 지원 정책",
                "result": {
                    "results": [{
                        "title": "청년 월세 지원 정책 — 사람 검토 대기 데모",
                        "original_url": (
                            "https://example.go.kr/policy/youth-rent-support"
                        ),
                        "normalized_claims": [{
                            "claim_text": (
                                "청년 월세 지원 정책은 사람 검토가 필요한 상태입니다."
                            ),
                        }],
                        # Conservative labels only — never "100% 사실", never
                        # "확정 참", never "확정 거짓".
                        "final_decision": {"decision_label": "사람 검토 필요"},
                        "policy_confidence": {
                            "verification_strength": "moderate",
                        },
                        "verification_card": {
                            "summary": "공식 출처 확인 필요 — 사람 검토 대기",
                            "status": "pending_review",
                        },
                    }],
                },
            },
            "decision": {
                "decision": "needs_more_evidence",
                "reviewer_id": "demo-local-reviewer",
                "comment": "공식 출처 확인 후 다시 검토 예정 (로컬 데모).",
                "public_note": None,
                "decision_source": "review_ui",
            },
        },
    ]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class DemoResult:
    passed: bool
    db_path: str
    token_is_dummy: bool
    token_label: str
    seeded_task_ids: List[str] = field(default_factory=list)
    expected_local_url: str = ""
    powershell_commands: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    verify: Optional[Dict[str, object]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_db_path(raw: Optional[str]) -> Path:
    """Default to ``reports/review_ui_local_demo.sqlite`` under ROOT.

    Resolved to an absolute path so the operator-facing PowerShell
    commands carry a path uvicorn (and any other tool) can use from
    any cwd.
    """
    if raw is None or not str(raw).strip():
        return (ROOT / REPORTS_DIR_NAME / DEMO_DB_FILENAME).resolve()
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    return p


def _path_is_under_reports(p: Path) -> bool:
    try:
        p.resolve().relative_to((ROOT / REPORTS_DIR_NAME).resolve())
        return True
    except ValueError:
        return False


def _powershell_commands(db_path: Path, token: str) -> List[str]:
    """The exact PowerShell snippet the operator pastes to start a
    local demo server. The launcher points ``DATABASE_URL`` at the demo
    SQLite-as-PG substitute (M12.0e-6b-3 retired the SQLite ``DB_PATH``
    override; reviews are PG-only)."""
    serve_script = ROOT / "scripts" / "serve_review_ui_local_demo.py"
    return [
        f'$env:REVIEW_API_ENABLED = "true"',
        f'$env:REVIEW_API_TOKEN = "{token}"',
        # The launcher points DATABASE_URL at the demo substitute before
        # importing api_server. Operators do NOT have to touch policy_ai.db.
        f'python "{serve_script}" --db-path "{db_path}"',
    ]


def _runbook_lines(db_path: Path, token: str, *, url: str) -> List[str]:
    cmds = _powershell_commands(db_path, token)
    return [
        "Local reviewer UI demo runbook",
        "",
        "1. Demo DB prepared at:",
        f"     {db_path}",
        "",
        "2. In PowerShell (from the repo root), start the local demo server:",
        *(f"     {c}" for c in cmds),
        "",
        f"3. Open the local app:",
        f"     {url}",
        "",
        "4. In the internal reviewer/admin panel:",
        "     - Expand '내부 검수 도구 열기 (관리자 전용)'",
        f"     - Paste the dummy token shown above ({token})",
        "     - Click '토큰 적용'",
        "     - Click '큐 새로고침'",
        "     - Select the seeded review task",
        "     - Click '감사 패킷 보기'",
        "     - Optionally click '감사 패킷 복사'",
        "",
        "Notes:",
        "  * The demo DB lives under reports/ and is gitignored. Do not commit it.",
        "  * Render env is NOT modified by this helper. Review API on Render",
        "    remains disabled by default; this dry-run is purely local.",
        "  * The token shown is a local-only dummy label, not a real secret.",
        "  * Stop the local server with Ctrl+C when finished.",
    ]


# ---------------------------------------------------------------------------
# Seeding (calls into database / review_workflow without touching Render)
# ---------------------------------------------------------------------------


@contextmanager
def _override_database_path(target: Path):
    """Point ``DATABASE_URL`` at ``target`` (the demo SQLite-as-PG
    substitute) for the duration of the block; reload ``api_server`` so
    cached imports see the new env; restore env on exit.

    review_tasks / review_decisions writes are PG-only, so SQLAlchemy
    writes from create_review_task / record_review_decision /
    update_review_task_status land in ``target`` and PG-primary reads
    resolve from it. (M12.0e-6b-3: the SQLite ``DB_PATH`` swap was
    removed with the retired SQLite machinery.)
    """
    import database
    env_snapshot = {
        key: os.environ.get(key)
        for key in ("USE_POSTGRES_WRITE", "DATABASE_URL")
    }
    os.environ["USE_POSTGRES_WRITE"] = "true"
    os.environ["DATABASE_URL"] = f"sqlite:///{target}"
    try:
        import postgres_storage
        postgres_storage.reset_engine_for_tests()
    except Exception:
        postgres_storage = None  # type: ignore
    # M12.0e-6b-3: SQLite init / DB_PATH swap removed (machinery retired);
    # the demo writes/reads go through the PG substitute at ``target``.
    try:
        try:
            import api_server  # noqa: F401
            importlib.reload(api_server)
        except Exception:
            # api_server may not have been imported yet; that's fine.
            pass
        # Build PG-substitute engine so ensure_schema creates the
        # mirror tables before any write fires.
        if postgres_storage is not None:
            try:
                postgres_storage.get_engine()
            except Exception:
                pass
        yield database
    finally:
        if postgres_storage is not None:
            try:
                postgres_storage.reset_engine_for_tests()
            except Exception:
                pass
        for key, value in env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seed_demo_db(db_path: Path) -> List[str]:
    """Initialize review tables on ``db_path`` and insert the demo rows.

    Uses only the existing public helpers (``create_review_task``,
    ``record_review_decision``) so the seeded rows match every
    invariant the rest of the codebase (and tests) rely on. The PG
    mirror schema is created by ensure_schema (M12.0e-6b-3 retired the
    SQLite init helpers).
    """
    import review_workflow

    seeded: List[str] = []
    with _override_database_path(db_path) as database:
        for case in _demo_payloads():
            snapshot = review_workflow.extract_review_snapshot_from_result(
                case["payload"], item_index=case["item_index"],
                query=case["query"],
            )
            claim_text = snapshot.get("claim_text") or ""
            task_id = review_workflow.make_review_task_id(
                result_id=case["result_id"], job_id=case["job_id"],
                item_index=case["item_index"], claim_text=claim_text,
            )
            idempotency_key = review_workflow.make_idempotency_key(
                result_id=case["result_id"], job_id=case["job_id"],
                item_index=case["item_index"], claim_text=claim_text,
            )
            now = review_workflow.now_iso()
            database.create_review_task(
                task_id=task_id,
                result_id=case["result_id"],
                job_id=case["job_id"],
                item_index=case["item_index"],
                status=review_workflow.STATUS_PENDING_REVIEW,
                query=snapshot.get("query") or "",
                claim_text=claim_text,
                title=snapshot.get("title") or "",
                url=snapshot.get("url") or "",
                final_decision=snapshot.get("final_decision") or "",
                policy_confidence=snapshot.get("policy_confidence") or "",
                human_review_required=True,
                snapshot=snapshot,
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
            # Optional decision row so the audit trail isn't empty.
            decision_spec = case.get("decision")
            if decision_spec:
                decision_id = review_workflow.make_review_decision_id()
                database.record_review_decision(
                    decision_id=decision_id,
                    task_id=task_id,
                    decision=decision_spec["decision"],
                    reviewer_id=decision_spec.get("reviewer_id"),
                    comment=decision_spec.get("comment"),
                    public_note=decision_spec.get("public_note"),
                    previous_status=review_workflow.STATUS_PENDING_REVIEW,
                    new_status=review_workflow.validate_status_transition(
                        review_workflow.STATUS_PENDING_REVIEW,
                        decision_spec["decision"],
                    ),
                    created_at=now,
                    metadata={},
                    decision_source=decision_spec.get("decision_source")
                                    or review_workflow.DECISION_SOURCE_REVIEW_UI,
                )
                # If the decision moved the status, update the task row.
                new_status = review_workflow.validate_status_transition(
                    review_workflow.STATUS_PENDING_REVIEW,
                    decision_spec["decision"],
                )
                if new_status != review_workflow.STATUS_PENDING_REVIEW:
                    database.update_review_task_status(
                        task_id, new_status=new_status, updated_at=now,
                    )
            seeded.append(task_id)
    return seeded


# ---------------------------------------------------------------------------
# Optional --verify mode (FastAPI TestClient, no real server, no network)
# ---------------------------------------------------------------------------


def _run_verify(db_path: Path, token: str) -> Dict[str, object]:
    """Exercise the seeded DB against the FastAPI app via TestClient.

    Reuses the documented review-env override + module-reload pattern
    so any module-level caches see the temp DB. Sets
    ``REVIEW_API_ENABLED`` / ``REVIEW_API_TOKEN`` only for the duration
    of the verify block and restores prior values on exit (even on
    exception).
    """
    keys = ("REVIEW_API_ENABLED", "REVIEW_API_TOKEN")
    original_env = {k: os.environ.get(k) for k in keys}
    out: Dict[str, object] = {
        "passed": False,
        "list_status": None,
        "detail_status": None,
        "audit_packet_status": None,
        "task_visible": False,
        "audit_packet_publication_false": None,
        "no_token_in_audit_packet": None,
        "errors": [],
    }
    try:
        os.environ["REVIEW_API_ENABLED"] = "true"
        os.environ["REVIEW_API_TOKEN"] = token
        with _override_database_path(db_path) as database:
            try:
                from fastapi.testclient import TestClient  # type: ignore
                import api_server  # noqa: F401
            except Exception as error:
                out["errors"].append(
                    f"failed to import FastAPI / api_server: {error}"
                )
                return out

            try:
                with TestClient(api_server.app) as client:
                    list_resp = client.get(
                        "/review/tasks",
                        headers={"X-Review-Token": token},
                    )
                    out["list_status"] = list_resp.status_code
                    body = list_resp.json() if list_resp.status_code == 200 else {}
                    tasks = body.get("tasks") or []
                    if not tasks:
                        out["errors"].append("no tasks visible in list endpoint")
                        return out
                    task_id = tasks[0].get("task_id")
                    out["task_visible"] = bool(task_id)

                    detail_resp = client.get(
                        f"/review/tasks/{task_id}",
                        headers={"X-Review-Token": token},
                    )
                    out["detail_status"] = detail_resp.status_code

                    packet_resp = client.get(
                        f"/review/tasks/{task_id}/audit-packet",
                        headers={"X-Review-Token": token},
                    )
                    out["audit_packet_status"] = packet_resp.status_code
                    packet_body = (
                        packet_resp.json() if packet_resp.status_code == 200 else {}
                    )
                    contract = (packet_body.get("safety_contract") or {})
                    out["audit_packet_publication_false"] = (
                        contract.get("publication") is False
                    )
                    serialized = json.dumps(packet_body, ensure_ascii=False)
                    out["no_token_in_audit_packet"] = token not in serialized
            except Exception as error:
                out["errors"].append(f"TestClient run failed: {error}")
                return out

        out["passed"] = bool(
            out["list_status"] == 200
            and out["detail_status"] == 200
            and out["audit_packet_status"] == 200
            and out["task_visible"]
            and out["audit_packet_publication_false"] is True
            and out["no_token_in_audit_packet"] is True
            and not out["errors"]
        )
    finally:
        # Restore env exactly as it was, including unset.
        for k, v in original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def prepare_demo(
    *,
    db_path: Optional[Path] = None,
    token: str = DEFAULT_DEMO_TOKEN,
    reset: bool = False,
    verify: bool = False,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> DemoResult:
    """Pure-ish entry point. Tests call this with a temp-dir ``db_path``
    so they never touch the project-level ``reports/`` directory."""
    target = (db_path if db_path is not None else _normalize_db_path(None))
    warnings: List[str] = []
    errors: List[str] = []

    # Refuse paths outside reports/ — the helper writes a demo DB; we
    # don't want stray writes to the repo or the user's home dir.
    if not _path_is_under_reports(target):
        errors.append(
            f"refusing to write demo DB outside reports/: {target}"
        )
        return DemoResult(
            passed=False,
            db_path=str(target),
            token_is_dummy=True,
            token_label=token,
            warnings=warnings,
            errors=errors,
        )

    # Defensive token validation.
    if not token or not str(token).strip():
        errors.append("token must be a non-empty string")
        return DemoResult(
            passed=False,
            db_path=str(target),
            token_is_dummy=True,
            token_label=token,
            warnings=warnings,
            errors=errors,
        )

    # Warn (but do not read) if REVIEW_API_TOKEN is present in the env —
    # the demo uses its own dummy token regardless.
    if os.environ.get("REVIEW_API_TOKEN"):
        warnings.append(
            "REVIEW_API_TOKEN is set in your environment. The demo "
            "ignores it and uses its own local dummy token; no value "
            "from the env is read or printed by this helper."
        )

    # Refuse to overwrite an existing DB unless --reset.
    if target.exists() and not reset:
        errors.append(
            f"demo DB already exists at {target} — pass --reset to overwrite."
        )
        return DemoResult(
            passed=False,
            db_path=str(target),
            token_is_dummy=True,
            token_label=token,
            warnings=warnings,
            errors=errors,
        )

    # Ensure parent directory exists, then (if --reset) clear the old file.
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and reset:
        # Force-collect any lingering sqlite3 connections from a prior
        # call in the same process. On Windows, a still-referenced
        # connection holds the underlying file open and ``unlink`` would
        # raise PermissionError [WinError 32]. The collection is
        # harmless on other platforms.
        import gc as _gc
        _gc.collect()
        try:
            target.unlink()
        except OSError as error:
            errors.append(f"could not reset demo DB at {target}: {error}")
            return DemoResult(
                passed=False,
                db_path=str(target),
                token_is_dummy=True,
                token_label=token,
                warnings=warnings,
                errors=errors,
            )

    try:
        seeded = _seed_demo_db(target)
    except Exception as error:
        errors.append(f"seeding failed: {type(error).__name__}: {error}")
        return DemoResult(
            passed=False,
            db_path=str(target),
            token_is_dummy=True,
            token_label=token,
            warnings=warnings,
            errors=errors,
        )

    url = f"http://{host}:{port}/"
    result = DemoResult(
        passed=True,
        db_path=str(target),
        token_is_dummy=(token == DEFAULT_DEMO_TOKEN),
        token_label=token,
        seeded_task_ids=seeded,
        expected_local_url=url,
        powershell_commands=_powershell_commands(target, token),
        warnings=warnings,
        errors=errors,
    )

    if verify:
        verify_outcome = _run_verify(target, token)
        result.verify = verify_outcome
        if not verify_outcome.get("passed"):
            result.passed = False

    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def result_to_dict(result: DemoResult) -> Dict[str, object]:
    return {
        "passed": result.passed,
        "db_path": result.db_path,
        "token_is_dummy": result.token_is_dummy,
        "token_label": result.token_label,
        "seeded_task_ids": list(result.seeded_task_ids),
        "expected_local_url": result.expected_local_url,
        "powershell_commands": list(result.powershell_commands),
        "warnings": list(result.warnings),
        "errors": list(result.errors),
        "verify": result.verify,
    }


def _print_runbook(result: DemoResult) -> None:
    if not result.passed:
        for e in result.errors:
            print(f"[demo] error: {e}", file=sys.stderr)
        for w in result.warnings:
            print(f"[demo] warn: {w}", file=sys.stderr)
        return
    db_path = Path(result.db_path)
    print(f"[demo] passed=True db_path={result.db_path}")
    print(f"[demo] seeded {len(result.seeded_task_ids)} review task(s):")
    for tid in result.seeded_task_ids:
        print(f"[demo]   - {tid}")
    for w in result.warnings:
        print(f"[demo] warn: {w}")
    if result.verify is not None:
        print(f"[demo] verify={result.verify.get('passed')!s}")
        for ek in ("list_status", "detail_status", "audit_packet_status"):
            print(f"[demo]   {ek}={result.verify.get(ek)}")
        if result.verify.get("errors"):
            for e in result.verify["errors"]:
                print(f"[demo]   verify error: {e}", file=sys.stderr)
    print()
    for line in _runbook_lines(db_path, result.token_label,
                               url=result.expected_local_url):
        print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local-only helper that prepares a small SQLite demo DB so the "
            "operator can exercise the M9.2 audit-packet viewer (and the "
            "rest of the internal reviewer UI) against a local FastAPI "
            "server. Never modifies Render env, never calls OpenAI, never "
            "reads a real REVIEW_API_TOKEN."
        ),
    )
    parser.add_argument(
        "--db-path", default=None,
        help=(
            "Path for the demo SQLite DB (must live under reports/). "
            f"Default: reports/{DEMO_DB_FILENAME}."
        ),
    )
    parser.add_argument(
        "--token", default=DEFAULT_DEMO_TOKEN,
        help=(
            "Dummy local token used in the printed PowerShell commands. "
            "This script never reads REVIEW_API_TOKEN from the environment. "
            f"Default: {DEFAULT_DEMO_TOKEN!r}."
        ),
    )
    parser.add_argument(
        "--reset", action="store_true",
        help=(
            "Delete and re-create the demo DB if it already exists. Without "
            "this flag the script refuses to overwrite the file."
        ),
    )
    parser.add_argument(
        "--verify", action="store_true",
        help=(
            "After seeding, exercise GET /review/tasks, GET /review/tasks/{id}, "
            "and GET /review/tasks/{id}/audit-packet against the seeded DB via "
            "FastAPI TestClient. Sets REVIEW_API_ENABLED / REVIEW_API_TOKEN "
            "only for the duration of the verify block. No external network."
        ),
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Host shown in the printed runbook URL (default {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port shown in the printed runbook URL (default {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help=(
            "Print a stable JSON summary instead of the human runbook. The "
            "JSON includes: passed, db_path, token_is_dummy, token_label, "
            "seeded_task_ids, expected_local_url, powershell_commands, "
            "warnings, errors, verify."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.token is None or not str(args.token).strip():
        print("[demo] --token must be a non-empty string.", file=sys.stderr)
        return 2

    db_path = _normalize_db_path(args.db_path)
    if not _path_is_under_reports(db_path):
        print(
            f"[demo] --db-path must live under reports/ (got {db_path}).",
            file=sys.stderr,
        )
        return 2

    result = prepare_demo(
        db_path=db_path,
        token=args.token,
        reset=args.reset,
        verify=args.verify,
        host=args.host,
        port=args.port,
    )

    if args.json:
        print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))
    else:
        _print_runbook(result)

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
