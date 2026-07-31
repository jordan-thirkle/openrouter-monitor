"""Database module for ingestion pipeline.

Handles SQLite connections, schema, and upsert operations.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

from src.api.client import UsageRecord


@dataclass(frozen=True)
class IngestionResult:
    """Result of an ingestion run."""
    records_processed: int
    records_inserted: int
    records_updated: int
    start_time: datetime
    end_time: datetime
    cursor: datetime
    errors: List[str]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_records (
    model TEXT NOT NULL,
    project TEXT NOT NULL,
    date_hour TEXT NOT NULL,  -- ISO format: YYYY-MM-DD HH:00:00
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0.0,
    request_id TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (model, project, date_hour)
);

CREATE INDEX IF NOT EXISTS idx_usage_date_hour ON usage_records(date_hour);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_records(model);
CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_records(project);
"""


def get_db_path() -> Path:
    """Get the database file path."""
    return Path(__file__).parent.parent.parent / "data" / "monitor.db"


def get_cursor_path() -> Path:
    """Get the cursor file path."""
    return Path(__file__).parent.parent.parent / "data" / "cursor.txt"


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Get a database connection with row factory."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Initialize the database schema."""
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def upsert_usage_records(records: List[UsageRecord]) -> tuple[int, int]:
    """Upsert usage records. Returns (inserted_count, updated_count)."""
    if not records:
        return 0, 0
    
    inserted = 0
    updated = 0
    
    with get_connection() as conn:
        for record in records:
            # Check if record exists
            cursor = conn.execute(
                "SELECT 1 FROM usage_records WHERE model = ? AND project = ? AND date_hour = ?",
                (record.model, record.project, record.date_hour.strftime("%Y-%m-%d %H:00:00"))
            ).fetchone()
            
            if cursor:
                # Update existing
                conn.execute(
                    """UPDATE usage_records SET
                        prompt_tokens = ?,
                        completion_tokens = ?,
                        total_tokens = ?,
                        cost = ?,
                        request_id = ?,
                        ingested_at = datetime('now')
                       WHERE model = ? AND project = ? AND date_hour = ?""",
                    (
                        record.prompt_tokens,
                        record.completion_tokens,
                        record.total_tokens,
                        record.cost,
                        record.request_id,
                        record.model,
                        record.project,
                        record.date_hour.strftime("%Y-%m-%d %H:00:00")
                    )
                )
                updated += 1
            else:
                # Insert new
                conn.execute(
                    """INSERT INTO usage_records 
                       (model, project, date_hour, prompt_tokens, completion_tokens, total_tokens, cost, request_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.model,
                        record.project,
                        record.date_hour.strftime("%Y-%m-%d %H:00:00"),
                        record.prompt_tokens,
                        record.completion_tokens,
                        record.total_tokens,
                        record.cost,
                        record.request_id
                    )
                )
                inserted += 1
        conn.commit()
    
    return inserted, updated


def get_last_cursor() -> Optional[datetime]:
    """Get the last ingestion cursor from file."""
    cursor_path = get_cursor_path()
    if not cursor_path.exists():
        return None
    try:
        content = cursor_path.read_text().strip()
        if not content:
            return None
        return datetime.fromisoformat(content)
    except (ValueError, OSError):
        return None


def set_cursor(cursor: datetime) -> None:
    """Set the ingestion cursor to file."""
    cursor_path = get_cursor_path()
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(cursor.isoformat())


def get_latest_record_time() -> Optional[datetime]:
    """Get the latest date_hour from the database."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(date_hour) as max_date FROM usage_records"
        ).fetchone()
        if row and row["max_date"]:
            return datetime.strptime(row["max_date"], "%Y-%m-%d %H:00:00")
    return None


def count_records() -> int:
    """Count total records in database."""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM usage_records").fetchone()
        return row["cnt"] if row else 0