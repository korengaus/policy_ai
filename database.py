import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


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
        connection.commit()


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
    }

    for column, column_type in desired_columns.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE analysis_results ADD COLUMN {column} {column_type}"
            )


def _serialize_market_signal(value) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _serialize_json_value(value) -> str:
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


def save_analysis_result(result: dict, query: str):
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
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
