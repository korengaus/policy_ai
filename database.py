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
        _ensure_source_fetch_artifacts_table(connection)
        _ensure_artifact_text_extractions_table(connection)
        _ensure_artifact_evidence_candidates_table(connection)
        _ensure_verdict_producer_comparisons_table(connection)
        _ensure_verdict_label_attributions_table(connection)
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


# ---------------------------------------------------------------------------
# Phase 2 M10.2: source-fetch-artifact persistence.
#
# Read-only catalog of operator-triggered static fetches against
# registry-candidate sources. The pipeline (analyze_pipeline /
# main.py) never reads or writes this table. ``truth_claim`` is
# stored as 0 on every row — the registry contract is that fetch
# artifacts never assert truth.
# ---------------------------------------------------------------------------


def _ensure_source_fetch_artifacts_table(connection):
    """Idempotent. Safe to call repeatedly."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_fetch_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            url TEXT NOT NULL,
            fetch_timestamp TEXT NOT NULL,
            status_code INTEGER,
            content_type TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            text_content TEXT,
            raw_html TEXT,
            fetch_duration_ms INTEGER,
            truth_claim INTEGER NOT NULL DEFAULT 0,
            official_source_candidate INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_fetch_artifacts_source "
        "ON source_fetch_artifacts(source_id, fetch_timestamp)"
    )


def init_source_fetch_artifacts_table():
    """Public idempotent initializer. Independent of init_db() so tests
    and the operator CLI can call it without touching analysis_results."""
    with get_connection() as connection:
        _ensure_source_fetch_artifacts_table(connection)
        connection.commit()


def _row_to_fetch_artifact(row) -> dict:
    """SQLite row → fetch-artifact dict. Maps the integer success /
    truth_claim / official_source_candidate columns back to booleans
    so callers don't have to remember the SQLite-boolean convention."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    out["success"] = bool(out.get("success", 0))
    # truth_claim is stored as 0 and surfaced as a bool. The registry
    # contract is that this field is always False; we re-assert here
    # as a defensive measure against any future row corruption.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["official_source_candidate"] = bool(
        out.get("official_source_candidate", 0)
    )
    return out


def save_fetch_artifact(fetch_result: dict) -> int:
    """Persist one fetch artifact and return the inserted row id.

    ``fetch_result`` matches the shape returned by
    ``source_crawler.fetch_result_to_dict``. Missing fields default
    safely. ``truth_claim`` is always stored as 0 regardless of the
    input (defensive against future regressions in the crawler that
    might try to set it true)."""
    if not isinstance(fetch_result, dict):
        raise ValueError("fetch_result must be a dict")
    if not fetch_result.get("source_id"):
        raise ValueError("fetch_result.source_id is required")
    if not fetch_result.get("url"):
        raise ValueError("fetch_result.url is required")
    if not fetch_result.get("fetch_timestamp"):
        raise ValueError("fetch_result.fetch_timestamp is required")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as connection:
        _ensure_source_fetch_artifacts_table(connection)
        cursor = connection.execute(
            """
            INSERT INTO source_fetch_artifacts (
                source_id, url, fetch_timestamp,
                status_code, content_type, success, error,
                text_content, raw_html, fetch_duration_ms,
                truth_claim, official_source_candidate,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(fetch_result.get("source_id")),
                str(fetch_result.get("url")),
                str(fetch_result.get("fetch_timestamp")),
                fetch_result.get("status_code"),
                fetch_result.get("content_type"),
                1 if fetch_result.get("success") else 0,
                fetch_result.get("error"),
                fetch_result.get("text_content"),
                fetch_result.get("raw_html"),
                fetch_result.get("fetch_duration_ms"),
                # truth_claim is forced to 0 — the registry contract.
                0,
                1 if fetch_result.get("official_source_candidate") else 0,
                created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_fetch_artifacts(source_id: str = None, limit: int = 50) -> list:
    """Return fetch artifacts (newest first), optionally filtered
    by ``source_id``. ``limit`` is clamped to ``[1, 500]`` so a single
    call cannot pull the whole table."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    with get_connection() as connection:
        _ensure_source_fetch_artifacts_table(connection)
        if source_id:
            rows = connection.execute(
                """
                SELECT * FROM source_fetch_artifacts
                WHERE source_id = ?
                ORDER BY fetch_timestamp DESC, id DESC
                LIMIT ?
                """,
                (str(source_id), capped_limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM source_fetch_artifacts
                ORDER BY fetch_timestamp DESC, id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
    return [_row_to_fetch_artifact(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 2 M10.4 — artifact_text_extractions table.
#
# Stores the cleaned title / main_text / sections produced by
# ``artifact_extractor.extract_text_from_artifact`` against rows in
# ``source_fetch_artifacts``. The registry contract still holds:
# ``truth_claim`` is forced to 0 on every persisted row regardless of
# the caller's input. Extraction results never feed the verdict path —
# they exist purely as raw, reviewable artifacts.
# ---------------------------------------------------------------------------


def _ensure_artifact_text_extractions_table(connection):
    """Idempotent. Safe to call repeatedly."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_text_extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            url TEXT NOT NULL,
            extraction_timestamp TEXT NOT NULL,
            extraction_duration_ms INTEGER,
            success INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            title TEXT,
            main_text TEXT,
            sections TEXT,
            word_count INTEGER,
            language_hint TEXT,
            truth_claim INTEGER NOT NULL DEFAULT 0,
            official_source_candidate INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_text_extractions_artifact "
        "ON artifact_text_extractions(artifact_id)"
    )


def init_artifact_text_extractions_table():
    """Public idempotent initializer. Independent of init_db() so tests
    and the operator CLI can call it without touching analysis_results."""
    with get_connection() as connection:
        _ensure_artifact_text_extractions_table(connection)
        connection.commit()


def _row_to_extraction_result(row) -> dict:
    """SQLite row → extraction-result dict. Maps the integer success /
    truth_claim / official_source_candidate columns back to booleans
    so callers don't have to remember the SQLite-boolean convention."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    out["success"] = bool(out.get("success", 0))
    # truth_claim is stored as 0 and surfaced as a bool. The registry
    # contract is that this field is always False; we re-assert here
    # as a defensive measure against any future row corruption.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["official_source_candidate"] = bool(
        out.get("official_source_candidate", 0)
    )
    return out


def save_extraction_result(result_dict: dict, db_path: str = None) -> int:
    """Persist one extraction artifact and return the inserted row id.

    ``result_dict`` matches the shape returned by
    ``artifact_extractor.extraction_result_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 regardless
    of the input (defensive against future regressions in the
    extractor that might try to set it true).

    When ``db_path`` is provided the write goes to that SQLite file
    directly (used by the CLI's ``--db-path`` flag and by tests that
    want isolated DBs without monkey-patching the module-level
    ``DB_PATH``).
    """
    if not isinstance(result_dict, dict):
        raise ValueError("result_dict must be a dict")
    if result_dict.get("artifact_id") is None:
        raise ValueError("result_dict.artifact_id is required")
    if not result_dict.get("source_id"):
        raise ValueError("result_dict.source_id is required")
    if not result_dict.get("url"):
        raise ValueError("result_dict.url is required")
    if not result_dict.get("extraction_timestamp"):
        raise ValueError("result_dict.extraction_timestamp is required")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = _open_connection(db_path)
    try:
        _ensure_artifact_text_extractions_table(connection)
        cursor = connection.execute(
            """
            INSERT INTO artifact_text_extractions (
                artifact_id, source_id, url, extraction_timestamp,
                extraction_duration_ms, success, error,
                title, main_text, sections, word_count, language_hint,
                truth_claim, official_source_candidate, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(result_dict.get("artifact_id") or 0),
                str(result_dict.get("source_id")),
                str(result_dict.get("url")),
                str(result_dict.get("extraction_timestamp")),
                result_dict.get("extraction_duration_ms"),
                1 if result_dict.get("success") else 0,
                result_dict.get("error"),
                result_dict.get("title"),
                result_dict.get("main_text"),
                result_dict.get("sections"),
                result_dict.get("word_count"),
                result_dict.get("language_hint"),
                # truth_claim is forced to 0 — the registry contract.
                0,
                1 if result_dict.get("official_source_candidate") else 0,
                created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def get_extraction_results(source_id: str = None, artifact_id: int = None,
                           db_path: str = None, limit: int = 50) -> list:
    """Return extraction artifacts (newest first), optionally filtered
    by ``source_id`` and/or ``artifact_id``. ``limit`` is clamped to
    ``[1, 500]``.

    When ``db_path`` is provided the read goes to that SQLite file
    directly (used by the CLI's ``--db-path`` flag and by tests)."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    connection = _open_connection(db_path)
    try:
        _ensure_artifact_text_extractions_table(connection)
        clauses = []
        params: list = []
        if source_id:
            clauses.append("source_id = ?")
            params.append(str(source_id))
        if artifact_id is not None:
            try:
                clauses.append("artifact_id = ?")
                params.append(int(artifact_id))
            except (TypeError, ValueError):
                pass
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(capped_limit)
        rows = connection.execute(
            f"""
            SELECT * FROM artifact_text_extractions{where}
            ORDER BY extraction_timestamp DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        connection.close()
    return [_row_to_extraction_result(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 2 M10.5 — artifact_evidence_candidates table.
#
# Stores keyword-overlap evidence candidates produced by
# ``artifact_evidence_linker.find_evidence_candidates`` against rows
# in ``artifact_text_extractions`` and ``analysis_results``. The
# registry contract still holds: ``truth_claim`` is forced to 0 and
# ``operator_review_required`` is forced to 1 on every persisted row,
# regardless of the caller's input. Candidates never feed the verdict
# path — they exist purely as raw, reviewable artifacts for operators.
# ---------------------------------------------------------------------------


def _ensure_artifact_evidence_candidates_table(connection):
    """Idempotent. Safe to call repeatedly."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_evidence_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            url TEXT NOT NULL,
            analysis_id TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            match_score REAL NOT NULL DEFAULT 0.0,
            matched_tokens TEXT,
            supporting_passage TEXT,
            candidate_timestamp TEXT NOT NULL,
            truth_claim INTEGER NOT NULL DEFAULT 0,
            official_source_candidate INTEGER NOT NULL DEFAULT 0,
            operator_review_required INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_evidence_candidates_analysis "
        "ON artifact_evidence_candidates(analysis_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_evidence_candidates_extraction "
        "ON artifact_evidence_candidates(extraction_id)"
    )


def init_artifact_evidence_candidates_table(db_path: str = None):
    """Public idempotent initializer. Independent of init_db() so tests
    and the operator CLI can call it without touching analysis_results."""
    connection = _open_connection(db_path)
    try:
        _ensure_artifact_evidence_candidates_table(connection)
        connection.commit()
    finally:
        connection.close()


def _row_to_evidence_candidate(row) -> dict:
    """SQLite row → evidence-candidate dict. Maps the integer
    truth_claim / official_source_candidate / operator_review_required
    columns back to booleans so callers don't have to remember the
    SQLite-boolean convention. ``matched_tokens`` stays as the stored
    JSON string — callers decide whether to decode it."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    # truth_claim is stored as 0 and surfaced as a bool. The registry
    # contract is that this field is always False; re-assert here as a
    # defensive measure against any future row corruption.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["official_source_candidate"] = bool(
        out.get("official_source_candidate", 0)
    )
    # operator_review_required is stored as 1 and surfaced as a bool.
    # The contract is that candidates always require review — defensive
    # re-assertion the same way.
    out["operator_review_required"] = bool(
        out.get("operator_review_required", 1)
    )
    try:
        out["match_score"] = float(out.get("match_score") or 0.0)
    except (TypeError, ValueError):
        out["match_score"] = 0.0
    return out


def save_evidence_candidate(candidate_dict: dict, db_path: str = None) -> int:
    """Persist one evidence candidate and return the inserted row id.

    ``candidate_dict`` matches the shape returned by
    ``artifact_evidence_linker.candidate_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 and
    ``operator_review_required`` as 1 regardless of the input
    (defensive against future regressions in the linker that might
    try to flip either flag).
    """
    if not isinstance(candidate_dict, dict):
        raise ValueError("candidate_dict must be a dict")
    if candidate_dict.get("extraction_id") is None:
        raise ValueError("candidate_dict.extraction_id is required")
    if not candidate_dict.get("source_id"):
        raise ValueError("candidate_dict.source_id is required")
    if not candidate_dict.get("url"):
        raise ValueError("candidate_dict.url is required")
    if not candidate_dict.get("analysis_id"):
        raise ValueError("candidate_dict.analysis_id is required")
    if not candidate_dict.get("claim_text"):
        raise ValueError("candidate_dict.claim_text is required")
    if not candidate_dict.get("candidate_timestamp"):
        raise ValueError("candidate_dict.candidate_timestamp is required")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    matched_tokens_raw = candidate_dict.get("matched_tokens")
    if isinstance(matched_tokens_raw, (list, tuple)):
        matched_tokens_text = json.dumps(
            list(matched_tokens_raw), ensure_ascii=False,
        )
    elif matched_tokens_raw is None:
        matched_tokens_text = json.dumps([], ensure_ascii=False)
    else:
        matched_tokens_text = str(matched_tokens_raw)
    try:
        match_score = float(candidate_dict.get("match_score") or 0.0)
    except (TypeError, ValueError):
        match_score = 0.0
    connection = _open_connection(db_path)
    try:
        _ensure_artifact_evidence_candidates_table(connection)
        cursor = connection.execute(
            """
            INSERT INTO artifact_evidence_candidates (
                extraction_id, source_id, url, analysis_id,
                claim_text, match_score, matched_tokens,
                supporting_passage, candidate_timestamp,
                truth_claim, official_source_candidate,
                operator_review_required, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate_dict.get("extraction_id") or 0),
                str(candidate_dict.get("source_id")),
                str(candidate_dict.get("url")),
                str(candidate_dict.get("analysis_id")),
                str(candidate_dict.get("claim_text")),
                match_score,
                matched_tokens_text,
                candidate_dict.get("supporting_passage"),
                str(candidate_dict.get("candidate_timestamp")),
                # truth_claim is forced to 0 — the registry contract.
                0,
                1 if candidate_dict.get("official_source_candidate") else 0,
                # operator_review_required is forced to 1 — the
                # candidate contract.
                1,
                candidate_dict.get("notes"),
                created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def get_evidence_candidates(
    analysis_id: str = None,
    source_id: str = None,
    extraction_id: int = None,
    db_path: str = None,
    limit: int = 50,
) -> list:
    """Return evidence candidates (newest first), optionally filtered
    by any combination of ``analysis_id``, ``source_id``, and
    ``extraction_id``. ``limit`` is clamped to ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    connection = _open_connection(db_path)
    try:
        _ensure_artifact_evidence_candidates_table(connection)
        clauses: list = []
        params: list = []
        if analysis_id is not None and str(analysis_id):
            clauses.append("analysis_id = ?")
            params.append(str(analysis_id))
        if source_id:
            clauses.append("source_id = ?")
            params.append(str(source_id))
        if extraction_id is not None:
            try:
                clauses.append("extraction_id = ?")
                params.append(int(extraction_id))
            except (TypeError, ValueError):
                pass
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(capped_limit)
        rows = connection.execute(
            f"""
            SELECT * FROM artifact_evidence_candidates{where}
            ORDER BY candidate_timestamp DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        connection.close()
    return [_row_to_evidence_candidate(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 2 M11.0a — verdict_producer_comparisons table.
#
# Read-only measurement layer for the three current verdict producers
# (policy_decision.make_final_decision,
# policy_scoring.calibrate_final_decision via _alert_from_score, and
# verification_card._verdict_label). The registry-style invariants
# still hold: ``truth_claim`` is forced to 0 and
# ``operator_review_required`` is forced to 1 on every persisted row.
# Comparison rows never feed the verdict path — they exist purely as
# operator-reviewable measurement.
# ---------------------------------------------------------------------------


def _ensure_verdict_producer_comparisons_table(connection):
    """Idempotent. Safe to call repeatedly."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS verdict_producer_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT NOT NULL,
            source TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            producer1_label TEXT,
            producer1_score REAL,
            producer1_extra TEXT,
            producer2_label TEXT,
            producer2_alert_level TEXT,
            producer2_score REAL,
            producer2_extra TEXT,
            producer3_label TEXT,
            producer3_extra TEXT,
            all_three_agree INTEGER NOT NULL DEFAULT 0,
            p1_p2_agree INTEGER NOT NULL DEFAULT 0,
            p1_p3_agree INTEGER NOT NULL DEFAULT 0,
            p2_p3_agree INTEGER NOT NULL DEFAULT 0,
            disagreement_pattern TEXT,
            most_conservative_label TEXT,
            comparison_timestamp TEXT NOT NULL,
            notes TEXT,
            truth_claim INTEGER NOT NULL DEFAULT 0,
            operator_review_required INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdict_comparisons_analysis "
        "ON verdict_producer_comparisons(analysis_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdict_comparisons_pattern "
        "ON verdict_producer_comparisons(disagreement_pattern)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_verdict_comparisons_input_hash "
        "ON verdict_producer_comparisons(input_hash)"
    )


def init_verdict_producer_comparisons_table(db_path: str = None):
    """Public idempotent initializer. Independent of init_db() so tests
    and the operator CLI can call it without touching analysis_results."""
    connection = _open_connection(db_path)
    try:
        _ensure_verdict_producer_comparisons_table(connection)
        connection.commit()
    finally:
        connection.close()


def _row_to_producer_comparison(row) -> dict:
    """SQLite row → comparison dict. Maps the integer flag columns
    back to booleans and leaves the JSON-encoded ``producerN_extra``
    columns as strings (the caller decides whether to decode them)."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    for flag in (
        "all_three_agree", "p1_p2_agree", "p1_p3_agree", "p2_p3_agree",
    ):
        out[flag] = bool(out.get(flag, 0))
    # truth_claim must always read back False; operator_review_required
    # must always read back True. Defensive re-assertion mirrors the
    # registry contract pattern.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["operator_review_required"] = bool(
        out.get("operator_review_required", 1)
    )
    for score_field in ("producer1_score", "producer2_score"):
        if out.get(score_field) is not None:
            try:
                out[score_field] = float(out[score_field])
            except (TypeError, ValueError):
                out[score_field] = None
    return out


def save_producer_comparison(comparison_dict: dict, db_path: str = None) -> int:
    """Persist (or replace, on input_hash collision) one comparison
    row and return the resulting row id.

    ``comparison_dict`` matches
    ``verdict_producer_comparison.comparison_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 and
    ``operator_review_required`` as 1 regardless of input (defensive
    against future regressions in the comparison module).

    Re-saving the same ``input_hash`` overwrites the prior row via
    the UNIQUE index on ``input_hash`` (INSERT OR REPLACE)."""
    if not isinstance(comparison_dict, dict):
        raise ValueError("comparison_dict must be a dict")
    if not comparison_dict.get("analysis_id"):
        raise ValueError("comparison_dict.analysis_id is required")
    if not comparison_dict.get("source"):
        raise ValueError("comparison_dict.source is required")
    if not comparison_dict.get("input_hash"):
        raise ValueError("comparison_dict.input_hash is required")
    if not comparison_dict.get("comparison_timestamp"):
        raise ValueError("comparison_dict.comparison_timestamp is required")

    def _ensure_extra_text(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = _open_connection(db_path)
    try:
        _ensure_verdict_producer_comparisons_table(connection)
        cursor = connection.execute(
            """
            INSERT OR REPLACE INTO verdict_producer_comparisons (
                analysis_id, source, input_hash,
                producer1_label, producer1_score, producer1_extra,
                producer2_label, producer2_alert_level,
                producer2_score, producer2_extra,
                producer3_label, producer3_extra,
                all_three_agree, p1_p2_agree, p1_p3_agree, p2_p3_agree,
                disagreement_pattern, most_conservative_label,
                comparison_timestamp, notes,
                truth_claim, operator_review_required, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(comparison_dict.get("analysis_id")),
                str(comparison_dict.get("source")),
                str(comparison_dict.get("input_hash")),
                comparison_dict.get("producer1_label"),
                comparison_dict.get("producer1_score"),
                _ensure_extra_text(comparison_dict.get("producer1_extra")),
                comparison_dict.get("producer2_label"),
                comparison_dict.get("producer2_alert_level"),
                comparison_dict.get("producer2_score"),
                _ensure_extra_text(comparison_dict.get("producer2_extra")),
                comparison_dict.get("producer3_label"),
                _ensure_extra_text(comparison_dict.get("producer3_extra")),
                1 if comparison_dict.get("all_three_agree") else 0,
                1 if comparison_dict.get("p1_p2_agree") else 0,
                1 if comparison_dict.get("p1_p3_agree") else 0,
                1 if comparison_dict.get("p2_p3_agree") else 0,
                comparison_dict.get("disagreement_pattern"),
                comparison_dict.get("most_conservative_label"),
                str(comparison_dict.get("comparison_timestamp")),
                comparison_dict.get("notes"),
                # truth_claim is forced to 0 — the registry contract.
                0,
                # operator_review_required is forced to 1 — the
                # measurement contract.
                1,
                created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def get_producer_comparisons(
    analysis_id: str = None,
    disagreement_pattern: str = None,
    only_disagreements: bool = False,
    db_path: str = None,
    limit: int = 50,
) -> list:
    """Return verdict-producer comparisons (newest first), optionally
    filtered by ``analysis_id``, ``disagreement_pattern``, or the
    ``only_disagreements`` flag (rows where ``all_three_agree=0``).
    ``limit`` is clamped to ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    connection = _open_connection(db_path)
    try:
        _ensure_verdict_producer_comparisons_table(connection)
        clauses: list = []
        params: list = []
        if analysis_id is not None and str(analysis_id):
            clauses.append("analysis_id = ?")
            params.append(str(analysis_id))
        if disagreement_pattern:
            clauses.append("disagreement_pattern = ?")
            params.append(str(disagreement_pattern))
        if only_disagreements:
            clauses.append("all_three_agree = 0")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(capped_limit)
        rows = connection.execute(
            f"""
            SELECT * FROM verdict_producer_comparisons{where}
            ORDER BY comparison_timestamp DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        connection.close()
    return [_row_to_producer_comparison(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 2 M11.0b — verdict_label_attributions table.
#
# Read-only diagnostic layer for ``verification_card._verdict_label``.
# Each row records which documented branch most likely produced the
# stored ``analysis_results.verdict_label`` value AND whether the
# stored label is a weak-evidence "draft_verified" candidate that the
# operator should investigate (the line 465-466 bug surface uncovered
# by M11.0a). The registry-style invariants still hold: ``truth_claim``
# is forced to 0 and ``operator_review_required`` is forced to 1 on
# every persisted row. Attribution rows never feed the verdict path.
# ---------------------------------------------------------------------------


def _ensure_verdict_label_attributions_table(connection):
    """Idempotent. Safe to call repeatedly."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS verdict_label_attributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT NOT NULL,
            stored_verdict_label TEXT,
            stored_verdict_confidence INTEGER,
            stored_policy_alert_level TEXT,
            stored_policy_confidence_score INTEGER,
            stored_verification_strength TEXT,
            stored_claim_text TEXT,
            stored_evidence_summary TEXT,
            reconstructed_inputs TEXT,
            attributed_branch_id TEXT,
            attribution_confidence TEXT,
            attribution_reason TEXT,
            is_weak_evidence_verified INTEGER NOT NULL DEFAULT 0,
            weak_evidence_signals TEXT,
            diagnostic_timestamp TEXT NOT NULL,
            notes TEXT,
            truth_claim INTEGER NOT NULL DEFAULT 0,
            operator_review_required INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdict_label_attr_analysis "
        "ON verdict_label_attributions(analysis_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdict_label_attr_branch "
        "ON verdict_label_attributions(attributed_branch_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verdict_label_attr_weak "
        "ON verdict_label_attributions(is_weak_evidence_verified)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_verdict_label_attr_analysis_unique "
        "ON verdict_label_attributions(analysis_id)"
    )


def init_verdict_label_attributions_table(db_path: str = None):
    """Public idempotent initializer. Independent of init_db() so tests
    and the operator CLI can call it without touching analysis_results."""
    connection = _open_connection(db_path)
    try:
        _ensure_verdict_label_attributions_table(connection)
        connection.commit()
    finally:
        connection.close()


def _row_to_verdict_label_attribution(row) -> dict:
    """SQLite row → attribution dict. Surfaces booleans as Python
    bools and leaves the JSON-encoded ``reconstructed_inputs`` /
    ``weak_evidence_signals`` columns as strings (callers decide
    whether to decode)."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    out["is_weak_evidence_verified"] = bool(
        out.get("is_weak_evidence_verified", 0)
    )
    # truth_claim must always read back False; operator_review_required
    # must always read back True. Defensive re-assertion.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["operator_review_required"] = bool(
        out.get("operator_review_required", 1)
    )
    return out


def save_verdict_label_attribution(
    attribution_dict: dict, db_path: str = None,
) -> int:
    """Persist (or replace, on analysis_id collision) one attribution
    row and return the resulting row id.

    ``attribution_dict`` matches
    ``verdict_label_diagnostic.attribution_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 and
    ``operator_review_required`` as 1 regardless of input (defensive
    against future regressions).

    Re-saving the same ``analysis_id`` overwrites the prior row via
    the UNIQUE index on ``analysis_id`` (INSERT OR REPLACE)."""
    if not isinstance(attribution_dict, dict):
        raise ValueError("attribution_dict must be a dict")
    if not attribution_dict.get("analysis_id"):
        raise ValueError("attribution_dict.analysis_id is required")
    if not attribution_dict.get("diagnostic_timestamp"):
        raise ValueError(
            "attribution_dict.diagnostic_timestamp is required"
        )

    def _ensure_text(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection = _open_connection(db_path)
    try:
        _ensure_verdict_label_attributions_table(connection)
        cursor = connection.execute(
            """
            INSERT OR REPLACE INTO verdict_label_attributions (
                analysis_id, stored_verdict_label,
                stored_verdict_confidence, stored_policy_alert_level,
                stored_policy_confidence_score,
                stored_verification_strength,
                stored_claim_text, stored_evidence_summary,
                reconstructed_inputs,
                attributed_branch_id, attribution_confidence,
                attribution_reason,
                is_weak_evidence_verified, weak_evidence_signals,
                diagnostic_timestamp, notes,
                truth_claim, operator_review_required, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(attribution_dict.get("analysis_id")),
                attribution_dict.get("stored_verdict_label"),
                attribution_dict.get("stored_verdict_confidence"),
                attribution_dict.get("stored_policy_alert_level"),
                attribution_dict.get("stored_policy_confidence_score"),
                attribution_dict.get("stored_verification_strength"),
                attribution_dict.get("stored_claim_text"),
                attribution_dict.get("stored_evidence_summary"),
                _ensure_text(attribution_dict.get("reconstructed_inputs")),
                attribution_dict.get("attributed_branch_id"),
                attribution_dict.get("attribution_confidence"),
                attribution_dict.get("attribution_reason"),
                1 if attribution_dict.get("is_weak_evidence_verified") else 0,
                _ensure_text(attribution_dict.get("weak_evidence_signals")),
                str(attribution_dict.get("diagnostic_timestamp")),
                attribution_dict.get("notes"),
                # truth_claim is forced to 0 — the registry contract.
                0,
                # operator_review_required is forced to 1 — the
                # diagnostic contract.
                1,
                created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def get_verdict_label_attributions(
    analysis_id: str = None,
    attributed_branch_id: str = None,
    only_weak_evidence_verified: bool = False,
    db_path: str = None,
    limit: int = 100,
) -> list:
    """Return verdict-label attribution rows (newest first), filtered
    by any combination of ``analysis_id``, ``attributed_branch_id``,
    and the ``only_weak_evidence_verified`` flag. ``limit`` is
    clamped to ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        capped_limit = 100
    connection = _open_connection(db_path)
    try:
        _ensure_verdict_label_attributions_table(connection)
        clauses: list = []
        params: list = []
        if analysis_id is not None and str(analysis_id):
            clauses.append("analysis_id = ?")
            params.append(str(analysis_id))
        if attributed_branch_id:
            clauses.append("attributed_branch_id = ?")
            params.append(str(attributed_branch_id))
        if only_weak_evidence_verified:
            clauses.append("is_weak_evidence_verified = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(capped_limit)
        rows = connection.execute(
            f"""
            SELECT * FROM verdict_label_attributions{where}
            ORDER BY diagnostic_timestamp DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        connection.close()
    return [_row_to_verdict_label_attribution(r) for r in rows]


def _open_connection(db_path):
    """Internal helper: open a sqlite3 connection at ``db_path`` (or
    the module-level ``DB_PATH`` when ``db_path`` is None). Returns a
    plain ``sqlite3.Connection`` with the standard row_factory; the
    caller is responsible for closing it."""
    if db_path is None:
        connection = sqlite3.connect(DB_PATH)
    else:
        connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


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
