import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from text_utils import sanitize_data, sanitize_text


DB_PATH = Path("policy_ai.db")


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                title TEXT,
                original_url TEXT,
                topic TEXT,
                policy_alert_level TEXT,
                market_signal TEXT,
                policy_confidence_score INTEGER,
                verification_strength TEXT,
                risk_level TEXT,
                action_priority TEXT,
                impact_level TEXT,
                impact_direction TEXT,
                market_sensitivity INTEGER,
                consumer_sensitivity INTEGER,
                business_sensitivity INTEGER,
                created_at TEXT
            )
            """
        )
        _ensure_columns(connection)
        _ensure_jobs_table(connection)
        _ensure_embedding_cache_table(connection)
        _ensure_review_tables(connection)
        connection.commit()


def _ensure_embedding_cache_table(connection):
    """Phase 2 M5: idempotent embedding cache. Safe to call repeatedly.

    The cache is best-effort — a corrupted row or schema mismatch should never
    block a pipeline run. Callers go through ``get_cached_embedding`` /
    ``save_cached_embedding`` which swallow errors and log a warning.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text_hash TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            dimensions INTEGER,
            vector_json TEXT NOT NULL,
            text_preview TEXT,
            created_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_embedding_cache_lookup
        ON embedding_cache(text_hash, provider, model)
        """
    )


def _ensure_jobs_table(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            query TEXT,
            max_news INTEGER,
            progress_percent INTEGER DEFAULT 0,
            current_stage TEXT,
            result_id INTEGER,
            error_message TEXT,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            pipeline_version TEXT
        )
        """
    )
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    desired = {
        "query": "TEXT",
        "max_news": "INTEGER",
        "progress_percent": "INTEGER DEFAULT 0",
        "current_stage": "TEXT",
        "result_id": "INTEGER",
        "error_message": "TEXT",
        "created_at": "TEXT",
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "pipeline_version": "TEXT",
    }
    for column, column_type in desired.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE jobs ADD COLUMN {column} {column_type}"
            )
    connection.execute("CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_jobs_created_at ON jobs(created_at)")


def _ensure_columns(connection):
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(analysis_results)").fetchall()
    }
    desired_columns = {
        "claim_text": "TEXT",
        "verdict_label": "TEXT",
        "verdict_confidence": "INTEGER",
        "evidence_sources": "TEXT",
        "source_reliability_score": "INTEGER",
        "source_reliability_reason": "TEXT",
        "evidence_summary": "TEXT",
        "missing_context": "TEXT",
        "last_checked_at": "TEXT",
        "review_status": "TEXT",
        "claims": "TEXT",
        "normalized_claims": "TEXT",
        "source_candidates": "TEXT",
        "source_queries": "TEXT",
        "source_reliability_summary": "TEXT",
        "evidence_snippets": "TEXT",
        "claim_evidence_map": "TEXT",
        "evidence_extraction_summary": "TEXT",
        "contradiction_checks": "TEXT",
        "contradiction_summary": "TEXT",
        "bias_framing_analysis": "TEXT",
        "bias_framing_summary": "TEXT",
        "debug_summary": "TEXT",
    }

    for column, column_type in desired_columns.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE analysis_results ADD COLUMN {column} {column_type}"
            )


def _serialize_market_signal(value) -> str:
    value = sanitize_data(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _serialize_json_value(value) -> str:
    value = sanitize_data(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def result_exists_by_url(original_url: str) -> bool:
    if not original_url:
        return False

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id
            FROM analysis_results
            WHERE original_url = ?
            LIMIT 1
            """,
            (original_url,),
        ).fetchone()

    return row is not None


def get_result_id_by_url(original_url: str):
    """Return the most recent analysis_results.id for the given URL, or None.

    Used when a duplicate save is skipped but the caller still needs to link
    a job row to the persisted result for durability after restart.
    """
    if not original_url:
        return None
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id
            FROM analysis_results
            WHERE original_url = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (original_url,),
        ).fetchone()
    if row is None:
        return None
    return row["id"]


def save_analysis_result(result: dict, query: str):
    result = sanitize_data(result)
    query = sanitize_text(query)
    original_url = result.get("original_url")
    if result_exists_by_url(original_url):
        return {"saved": False, "duplicate": True, "id": None}

    final_decision = result.get("final_decision") or {}
    policy_confidence = result.get("policy_confidence") or {}
    policy_impact = result.get("policy_impact") or {}
    verification_card = result.get("verification_card") or {}
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO analysis_results (
                query,
                title,
                original_url,
                topic,
                policy_alert_level,
                market_signal,
                policy_confidence_score,
                verification_strength,
                risk_level,
                action_priority,
                impact_level,
                impact_direction,
                market_sensitivity,
                consumer_sensitivity,
                business_sensitivity,
                claim_text,
                verdict_label,
                verdict_confidence,
                evidence_sources,
                source_reliability_score,
                source_reliability_reason,
                evidence_summary,
                missing_context,
                last_checked_at,
                review_status,
                claims,
                normalized_claims,
                source_candidates,
                source_queries,
                source_reliability_summary,
                evidence_snippets,
                claim_evidence_map,
                evidence_extraction_summary,
                contradiction_checks,
                contradiction_summary,
                bias_framing_analysis,
                bias_framing_summary,
                debug_summary,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query,
                result.get("title"),
                original_url,
                result.get("topic"),
                final_decision.get("policy_alert_level"),
                _serialize_market_signal(final_decision.get("market_signal")),
                policy_confidence.get("policy_confidence_score"),
                policy_confidence.get("verification_strength"),
                policy_confidence.get("risk_level"),
                policy_confidence.get("action_priority"),
                policy_impact.get("impact_level"),
                policy_impact.get("impact_direction"),
                policy_impact.get("market_sensitivity"),
                policy_impact.get("consumer_sensitivity"),
                policy_impact.get("business_sensitivity"),
                verification_card.get("claim_text") or result.get("claim_text"),
                verification_card.get("verdict_label") or result.get("verdict_label"),
                verification_card.get("verdict_confidence") or result.get("verdict_confidence"),
                _serialize_json_value(
                    verification_card.get("evidence_sources")
                    or result.get("evidence_sources")
                ),
                verification_card.get("source_reliability_score")
                or result.get("source_reliability_score"),
                verification_card.get("source_reliability_reason")
                or result.get("source_reliability_reason"),
                verification_card.get("evidence_summary") or result.get("evidence_summary"),
                _serialize_json_value(
                    verification_card.get("missing_context")
                    or result.get("missing_context")
                ),
                verification_card.get("last_checked_at") or result.get("last_checked_at"),
                verification_card.get("review_status") or result.get("review_status"),
                _serialize_json_value(
                    verification_card.get("claims")
                    or result.get("claims")
                ),
                _serialize_json_value(
                    verification_card.get("normalized_claims")
                    or result.get("normalized_claims")
                ),
                _serialize_json_value(
                    verification_card.get("source_candidates")
                    or result.get("source_candidates")
                ),
                _serialize_json_value(
                    verification_card.get("source_queries")
                    or result.get("source_queries")
                ),
                _serialize_json_value(
                    verification_card.get("source_reliability_summary")
                    or result.get("source_reliability_summary")
                ),
                _serialize_json_value(
                    verification_card.get("evidence_snippets")
                    or result.get("evidence_snippets")
                ),
                _serialize_json_value(
                    verification_card.get("claim_evidence_map")
                    or result.get("claim_evidence_map")
                ),
                _serialize_json_value(
                    verification_card.get("evidence_extraction_summary")
                    or result.get("evidence_extraction_summary")
                ),
                _serialize_json_value(
                    verification_card.get("contradiction_checks")
                    or result.get("contradiction_checks")
                ),
                _serialize_json_value(
                    verification_card.get("contradiction_summary")
                    or result.get("contradiction_summary")
                ),
                _serialize_json_value(
                    verification_card.get("bias_framing_analysis")
                    or result.get("bias_framing_analysis")
                ),
                _serialize_json_value(
                    verification_card.get("bias_framing_summary")
                    or result.get("bias_framing_summary")
                ),
                _serialize_json_value(
                    verification_card.get("debug_summary")
                    or result.get("debug_summary")
                ),
                created_at,
            ),
        )
        connection.commit()

    return {"saved": True, "duplicate": False, "id": cursor.lastrowid}


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def get_recent_results(limit: int = 20):
    safe_limit = max(1, min(int(limit or 20), 100))
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM analysis_results
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_result_by_id(result_id: int):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM analysis_results
            WHERE id = ?
            """,
            (result_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Phase 2 M5: embedding cache helpers
# ---------------------------------------------------------------------------
# These intentionally swallow exceptions and return ``None`` / ``False`` on
# any error: the cache is a performance optimisation, not a source of truth.
# A semantic ranking run must never fail because the cache table is locked,
# corrupted, or missing — the worst case is a recomputed embedding.

import logging as _embedding_logging  # local alias avoids top-of-file churn

_embedding_logger = _embedding_logging.getLogger(__name__)


def get_cached_embedding(text_hash: str, provider: str, model: str):
    """Return a previously stored vector, or ``None`` if absent/unusable."""
    if not text_hash or not provider:
        return None
    try:
        with get_connection() as connection:
            row = connection.execute(
                """
                SELECT vector_json, dimensions
                FROM embedding_cache
                WHERE text_hash = ? AND provider = ? AND model = ?
                LIMIT 1
                """,
                (text_hash, provider, model or ""),
            ).fetchone()
    except sqlite3.Error as error:
        _embedding_logger.warning("embedding_cache read failed: %s", error)
        return None
    if row is None:
        return None
    try:
        vector = json.loads(row["vector_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        _embedding_logger.warning("embedding_cache row had unreadable vector: %s", error)
        return None
    if not isinstance(vector, list) or not all(isinstance(v, (int, float)) for v in vector):
        return None
    return [float(v) for v in vector]


def save_cached_embedding(
    text_hash: str,
    provider: str,
    model: str,
    vector,
    text_preview: str = "",
):
    """Persist a vector. Returns True on success, False on any failure."""
    if not text_hash or not provider or not isinstance(vector, (list, tuple)):
        return False
    if not vector:
        return False
    try:
        vector_json = json.dumps(list(vector), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        _embedding_logger.warning("embedding_cache could not serialize vector: %s", error)
        return False
    preview = (text_preview or "")[:200]
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO embedding_cache (
                    text_hash, provider, model, dimensions,
                    vector_json, text_preview, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text_hash,
                    provider,
                    model or "",
                    len(vector),
                    vector_json,
                    preview,
                    created_at,
                ),
            )
            connection.commit()
    except sqlite3.Error as error:
        _embedding_logger.warning("embedding_cache write failed: %s", error)
        return False
    return True


# ---------------------------------------------------------------------------
# Phase 2 M8.0: server-backed reviewer workflow persistence.
#
# Two tables — review_tasks (one row per (result_id, job_id, item_index,
# claim_text) tuple via the idempotency key) and review_decisions (one
# row per recorded reviewer action, append-only). The verdict tables
# (analysis_results) are NEVER mutated by anything in this section; the
# review layer is strictly additive.
# ---------------------------------------------------------------------------


def _ensure_review_tables(connection):
    """Idempotent. Safe to call repeatedly."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS review_tasks (
            task_id TEXT PRIMARY KEY,
            result_id TEXT,
            job_id TEXT,
            item_index INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            query TEXT,
            claim_text TEXT,
            title TEXT,
            url TEXT,
            final_decision TEXT,
            policy_confidence TEXT,
            human_review_required INTEGER DEFAULT 1,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            idempotency_key TEXT UNIQUE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS review_decisions (
            decision_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewer_id TEXT,
            comment TEXT,
            public_note TEXT,
            previous_status TEXT,
            new_status TEXT,
            created_at TEXT NOT NULL,
            metadata_json TEXT,
            decision_source TEXT
        )
        """
    )
    # Phase 2 M9.0 — additive migration for installs that created
    # review_decisions before the decision_source column existed. SQLite
    # has no IF NOT EXISTS for ADD COLUMN, so we catch the OperationalError.
    try:
        connection.execute(
            "ALTER TABLE review_decisions ADD COLUMN decision_source TEXT"
        )
    except sqlite3.OperationalError:
        # Column already present — older table that included it, or a
        # concurrent migration win. Either case is fine.
        pass
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_result ON review_tasks(result_id, job_id, item_index)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_decisions_task ON review_decisions(task_id, created_at)"
    )


def init_review_tables():
    """Public idempotent initializer. Independent of init_db() so tests
    and the API server can call it without touching analysis_results."""
    with get_connection() as connection:
        _ensure_review_tables(connection)
        connection.commit()


def _row_to_review_task(row) -> dict:
    """SQLite row → review_task dict. Inflates snapshot_json to a dict."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    snapshot_raw = out.pop("snapshot_json", "")
    try:
        out["snapshot"] = json.loads(snapshot_raw) if snapshot_raw else {}
    except (TypeError, ValueError):
        out["snapshot"] = {}
    out["human_review_required"] = bool(out.get("human_review_required", 1))
    out["item_index"] = int(out.get("item_index") or 0)
    return out


def _row_to_review_decision(row) -> dict:
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    metadata_raw = out.pop("metadata_json", "")
    try:
        out["metadata"] = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError):
        out["metadata"] = {}
    return out


def get_review_task_by_idempotency_key(idempotency_key: str):
    """Return the existing task for an idempotency key, or None."""
    if not idempotency_key:
        return None
    with get_connection() as connection:
        _ensure_review_tables(connection)
        row = connection.execute(
            "SELECT * FROM review_tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    return _row_to_review_task(row) if row else None


def create_review_task(*, task_id: str, result_id, job_id, item_index: int,
                       status: str, query: str, claim_text: str, title: str,
                       url: str, final_decision: str, policy_confidence: str,
                       human_review_required: bool, snapshot: dict,
                       idempotency_key: str, created_at: str,
                       updated_at: str):
    """Insert a new review task (or return the existing row when the
    idempotency_key conflicts). Returns ``(task, was_existing)`` —
    callers use the second value to set the API's ``idempotent`` flag
    rather than relying on timestamp comparison (which fails when two
    calls land within the same second-precision ``now_iso()``)."""
    existing = get_review_task_by_idempotency_key(idempotency_key)
    if existing:
        return existing, True
    snapshot_json = json.dumps(snapshot or {}, ensure_ascii=False)
    was_existing = False
    with get_connection() as connection:
        _ensure_review_tables(connection)
        try:
            connection.execute(
                """
                INSERT INTO review_tasks (
                    task_id, result_id, job_id, item_index, status,
                    query, claim_text, title, url,
                    final_decision, policy_confidence,
                    human_review_required, snapshot_json,
                    created_at, updated_at, idempotency_key
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    task_id,
                    str(result_id) if result_id is not None else None,
                    str(job_id) if job_id is not None else None,
                    int(item_index or 0),
                    status,
                    query, claim_text, title, url,
                    final_decision, policy_confidence,
                    1 if human_review_required else 0,
                    snapshot_json,
                    created_at, updated_at, idempotency_key,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            # A concurrent writer beat us to it — fetch the canonical row.
            existing = get_review_task_by_idempotency_key(idempotency_key)
            if existing:
                return existing, True
            raise
    return (get_review_task(task_id) or {}), was_existing


def get_review_task(task_id: str):
    """Return a single review_task dict, or None when not found."""
    if not task_id:
        return None
    with get_connection() as connection:
        _ensure_review_tables(connection)
        row = connection.execute(
            "SELECT * FROM review_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _row_to_review_task(row) if row else None


def list_review_tasks(*, status=None, limit: int = 50, offset: int = 0) -> list:
    """List review tasks (newest first). ``limit`` is clamped to [1, 100]
    so a single API call cannot pull the whole table."""
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    with get_connection() as connection:
        _ensure_review_tables(connection)
        if status:
            rows = connection.execute(
                """
                SELECT * FROM review_tasks
                WHERE status = ?
                ORDER BY created_at DESC, task_id DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM review_tasks
                ORDER BY created_at DESC, task_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
    return [_row_to_review_task(r) for r in rows]


def update_review_task_status(task_id: str, *, new_status: str,
                              updated_at: str) -> dict:
    """Update a task's status row. Caller is responsible for
    transition validation via review_workflow.validate_status_transition."""
    with get_connection() as connection:
        _ensure_review_tables(connection)
        connection.execute(
            "UPDATE review_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (new_status, updated_at, task_id),
        )
        connection.commit()
    return get_review_task(task_id) or {}


def record_review_decision(*, decision_id: str, task_id: str, decision: str,
                           reviewer_id=None, comment=None, public_note=None,
                           previous_status=None, new_status=None,
                           created_at: str, metadata: dict = None,
                           decision_source: str = None) -> dict:
    """Append a decision row. Append-only — no UPDATE / DELETE path.

    ``decision_source`` (Phase 2 M9.0) is an operator-supplied audit
    label like ``review_api`` / ``review_ui`` / ``smoke_test``. It is
    NOT identity / auth and is never derived from ``REVIEW_API_TOKEN``.
    A None value is stored as SQL NULL; the audit-record builder maps
    that to ``unknown`` at the wire layer.
    """
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with get_connection() as connection:
        _ensure_review_tables(connection)
        connection.execute(
            """
            INSERT INTO review_decisions (
                decision_id, task_id, decision, reviewer_id,
                comment, public_note, previous_status, new_status,
                created_at, metadata_json, decision_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id, task_id, decision,
                reviewer_id, comment, public_note,
                previous_status, new_status,
                created_at, metadata_json,
                decision_source,
            ),
        )
        connection.commit()
    return get_review_decision(decision_id) or {}


def get_review_decision(decision_id: str):
    if not decision_id:
        return None
    with get_connection() as connection:
        _ensure_review_tables(connection)
        row = connection.execute(
            "SELECT * FROM review_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
    return _row_to_review_decision(row) if row else None


def list_review_decisions(task_id: str) -> list:
    if not task_id:
        return []
    with get_connection() as connection:
        _ensure_review_tables(connection)
        rows = connection.execute(
            """
            SELECT * FROM review_decisions
            WHERE task_id = ?
            ORDER BY created_at ASC, decision_id ASC
            """,
            (task_id,),
        ).fetchall()
    return [_row_to_review_decision(r) for r in rows]


def embedding_cache_stats() -> dict:
    """Optional diagnostic — total rows + per-provider counts."""
    try:
        with get_connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM embedding_cache"
            ).fetchone()["n"]
            per_provider = connection.execute(
                """
                SELECT provider, COUNT(*) AS n
                FROM embedding_cache
                GROUP BY provider
                ORDER BY n DESC
                """
            ).fetchall()
    except sqlite3.Error as error:
        return {"available": False, "error": str(error)}
    return {
        "available": True,
        "total": int(total or 0),
        "per_provider": {row["provider"]: int(row["n"]) for row in per_provider},
    }
