"""Tests for ingestion pipeline idempotency.

Critic test: Seed 10k records, re-run → idempotent, cursor advances, 0 dupes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.client import OpenRouterClient, UsageRecord
from src.ingestion.database import (
    get_db_path,
    get_last_cursor,
    get_latest_record_time,
    init_db,
    upsert_usage_records,
)
from src.ingestion.pipeline import IngestionPipeline, IngestionResult, event_queue


@dataclass
class MockOpenRouterClient(OpenRouterClient):
    """Mock client for testing."""
    records_to_return: List[UsageRecord] = None
    
    def __init__(self, records: List[UsageRecord]):
        super().__init__(api_key="test-key")
        self.records_to_return = records
        self.call_count = 0
        self.last_start: Optional[datetime] = None
        self.last_end: Optional[datetime] = None
    
    async def get_usage(self, start: datetime, end: datetime) -> List[UsageRecord]:
        self.call_count += 1
        self.last_start = start
        self.last_end = end
        return self.records_to_return


def generate_test_records(count: int = 10000) -> List[UsageRecord]:
    """Generate test usage records."""
    records = []
    base_time = datetime(2024, 1, 15, 14, 0, 0)
    models = ["gpt-4", "gpt-3.5-turbo", "claude-3-opus", "claude-3-sonnet"]
    projects = ["project-a", "project-b", "project-c"]
    
    for i in range(count):
        model = models[i % len(models)]
        project = projects[i % len(projects)]
        date_hour = base_time + timedelta(hours=i // (len(models) * len(projects)))
        
        records.append(UsageRecord(
            model=model,
            project=project,
            date_hour=date_hour,
            prompt_tokens=1000 + (i % 5000),
            completion_tokens=500 + (i % 2000),
            total_tokens=1500 + (i % 7000),
            cost=0.01 + (i % 100) * 0.001,
            request_id=f"req-{i:06d}",
        ))
    
    return records


@pytest.fixture
def test_records():
    """Generate 10k test records."""
    return generate_test_records(10000)


@pytest.fixture
def mock_client(test_records):
    """Create mock client with test records."""
    return MockOpenRouterClient(test_records)


@pytest.fixture(autouse=True)
def setup_db():
    """Initialize database before each test."""
    # Remove existing DB and cursor
    db_path = get_db_path()
    cursor_path = db_path.parent / "cursor.txt"
    if db_path.exists():
        db_path.unlink()
    if cursor_path.exists():
        cursor_path.unlink()
    init_db()
    yield
    # Cleanup
    if db_path.exists():
        db_path.unlink()
    if cursor_path.exists():
        cursor_path.unlink()


@pytest.mark.asyncio
async def test_idempotent_upsert_10k_records(mock_client, test_records):
    """Critic test: Seed 10k records, re-run → idempotent, cursor advances, 0 dupes."""
    pipeline = IngestionPipeline(mock_client)
    
    # First run - incremental (no cursor exists, defaults to 24h ago)
    result1 = await pipeline.run_incremental()
    
    assert result1.records_processed == 10000
    assert result1.records_inserted == 10000
    assert result1.records_updated == 0
    assert len(result1.errors) == 0
    assert result1.cursor is not None
    
    # Verify cursor was set
    cursor1 = get_last_cursor()
    assert cursor1 is not None
    assert cursor1 == result1.cursor
    
    # Second run - incremental again (should be idempotent)
    result2 = await pipeline.run_incremental()
    
    # Should process 0 new records (cursor already at end)
    # But wait - the mock client returns the same 10k records every time
    # We need to verify idempotent upsert works
    assert result2.records_processed == 10000
    assert result2.records_inserted == 0  # No new inserts
    assert result2.records_updated == 10000  # All updated (idempotent)
    assert len(result2.errors) == 0
    
    # Cursor should advance
    cursor2 = get_last_cursor()
    assert cursor2 is not None
    assert cursor2 > cursor1
    assert cursor2 == result2.cursor
    
    # Verify no duplicates in database
    latest = get_latest_record_time()
    assert latest is not None
    
    # Count unique records in DB
    import sqlite3
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM usage_records")
    row = cursor.fetchone()
    conn.close()
    
    assert row[0] == 10000, f"Expected 10000 unique records, got {row[0]}"
    
    print("✓ Critic test passed: 10k records seeded, re-run idempotent, cursor advanced, 0 dupes")


@pytest.mark.asyncio
async def test_run_full_idempotent(mock_client, test_records):
    """Test run_full is also idempotent."""
    pipeline = IngestionPipeline(mock_client)
    
    # First run
    result1 = await pipeline.run_full(days=7)
    assert result1.records_inserted == 10000
    assert result1.records_updated == 0
    
    cursor1 = get_last_cursor()
    
    # Second run
    result2 = await pipeline.run_full(days=7)
    assert result2.records_inserted == 0
    assert result2.records_updated == 10000
    
    cursor2 = get_last_cursor()
    assert cursor2 > cursor1
    
    # Verify no duplicates
    import sqlite3
    conn = sqlite3.connect(str(get_db_path()))
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM usage_records")
    row = cursor.fetchone()
    conn.close()
    
    assert row[0] == 10000
    
    print("✓ run_full idempotent test passed")


@pytest.mark.asyncio
async def test_event_emitted(mock_client, test_records):
    """Test that usage.ingested event is emitted."""
    # Clear event queue
    while not event_queue.empty():
        event_queue.get_nowait()
    
    pipeline = IngestionPipeline(mock_client)
    await pipeline.run_incremental()
    
    # Check event was emitted
    assert not event_queue.empty()
    event_type, event_data = event_queue.get_nowait()
    assert event_type == "usage.ingested"
    assert isinstance(event_data, IngestionResult)
    assert event_data.records_processed == 10000


if __name__ == "__main__":
    # Run tests manually
    async def run_tests():
        test_records = generate_test_records(10000)
        mock_client = MockOpenRouterClient(test_records)
        
        # Setup
        db_path = get_db_path()
        cursor_path = db_path.parent / "cursor.txt"
        if db_path.exists():
            db_path.unlink()
        if cursor_path.exists():
            cursor_path.unlink()
        init_db()
        
        try:
            await test_idempotent_upsert_10k_records(mock_client, test_records)
            await test_run_full_idempotent(mock_client, test_records)
            await test_event_emitted(mock_client, test_records)
            print("\n✅ All critic tests passed!")
        finally:
            if db_path.exists():
                db_path.unlink()
            if cursor_path.exists():
                cursor_path.unlink()
    
    asyncio.run(run_tests())