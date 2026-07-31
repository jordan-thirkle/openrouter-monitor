"""Tests for ingestion pipeline idempotency.

Critic test: Seed 10k records, re-run → idempotent, cursor advances, 0 dupes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

import pytest

from src.api.client import OpenRouterClient, RawUsageRecord, normalize_usage_records
from src.ingestion.database import (
    count_records,
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
    records_to_return: List[RawUsageRecord] = None
    
    def __init__(self, records: List[RawUsageRecord]):
        super().__init__(api_key="test-key")
        self.records_to_return = records
        self.call_count = 0
        self.last_start: Optional[datetime] = None
        self.last_end: Optional[datetime] = None
    
    async def get_usage(self, start: datetime, end: datetime) -> List[RawUsageRecord]:
        self.call_count += 1
        self.last_start = start
        self.last_end = end
        return self.records_to_return


def generate_test_raw_records(count: int = 10000) -> List[RawUsageRecord]:
    """Generate test raw usage records (as would come from API)."""
    records = []
    base_time = datetime(2024, 1, 15, 14, 0, 0)
    models = ["gpt-4", "gpt-3.5-turbo", "claude-3-opus", "claude-3-sonnet"]
    projects = ["project-a", "project-b", "project-c"]
    
    for i in range(count):
        model = models[i % len(models)]
        project = projects[i % len(projects)]
        # Create timestamps within the same hour for some records to test aggregation
        timestamp = base_time + timedelta(minutes=i % 60, hours=i // 60)
        
        records.append(RawUsageRecord(
            model=model,
            model_slug=model.lower().replace(".", "-"),
            prompt_tokens=1000 + (i % 5000),
            completion_tokens=500 + (i % 2000),
            total_tokens=1500 + (i % 7000),
            cost=0.01 + (i % 100) * 0.001,
            timestamp=timestamp,
            project=project,
            user=f"user-{i % 10}",
            generation_id=f"gen-{i:06d}",
        ))
    
    return records


@pytest.fixture
def test_raw_records():
    """Generate 10k test raw records."""
    return generate_test_raw_records(10000)


@pytest.fixture
def mock_client(test_raw_records):
    """Create mock client with test records."""
    return MockOpenRouterClient(test_raw_records)


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
async def test_idempotent_upsert_10k_records(mock_client, test_raw_records):
    """Critic test: Seed 10k records, re-run → idempotent, cursor advances, 0 dupes."""
    pipeline = IngestionPipeline(mock_client)
    
    # First run - incremental (no cursor exists, defaults to 24h ago)
    result1 = await pipeline.run_incremental()
    
    assert result1.records_processed > 0  # After normalization, may be fewer due to hourly bucketing
    assert result1.records_inserted > 0
    assert result1.records_updated == 0
    assert len(result1.errors) == 0
    assert result1.cursor is not None
    
    # Verify cursor was set
    cursor1 = get_last_cursor()
    assert cursor1 is not None
    assert cursor1 == result1.cursor
    
    # Count records after first run
    count_after_first = count_records()
    
    # Second run - incremental again (should be idempotent)
    result2 = await pipeline.run_incremental()
    
    # Should process same number of normalized records
    assert result2.records_processed == result1.records_processed
    assert result2.records_inserted == 0  # No new inserts
    assert result2.records_updated == result1.records_processed  # All updated (idempotent)
    assert len(result2.errors) == 0
    
    # Cursor should advance
    cursor2 = get_last_cursor()
    assert cursor2 is not None
    assert cursor2 > cursor1
    assert cursor2 == result2.cursor
    
    # Verify no duplicates in database - count should be same
    count_after_second = count_records()
    assert count_after_second == count_after_first, f"Duplicate records! Expected {count_after_first}, got {count_after_second}"
    
    print(f"✓ Critic test passed: {count_after_first} normalized records, re-run idempotent, cursor advanced, 0 dupes")


@pytest.mark.asyncio
async def test_run_full_idempotent(mock_client, test_raw_records):
    """Test run_full is also idempotent."""
    pipeline = IngestionPipeline(mock_client)
    
    # First run
    result1 = await pipeline.run_full(days=7)
    assert result1.records_inserted > 0
    assert result1.records_updated == 0
    
    cursor1 = get_last_cursor()
    count_after_first = count_records()
    
    # Second run
    result2 = await pipeline.run_full(days=7)
    assert result2.records_inserted == 0
    assert result2.records_updated == result1.records_processed
    
    cursor2 = get_last_cursor()
    assert cursor2 > cursor1
    
    # Verify no duplicates
    count_after_second = count_records()
    assert count_after_second == count_after_first
    
    print("✓ run_full idempotent test passed")


@pytest.mark.asyncio
async def test_event_emitted(mock_client, test_raw_records):
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
    assert event_data.records_processed > 0


@pytest.mark.asyncio
async def test_normalize_aggregates_hourly():
    """Test that normalize_usage_records aggregates records within the same hour."""
    base_time = datetime(2024, 1, 15, 14, 0, 0)
    
    raw_records = [
        RawUsageRecord(
            model="gpt-4",
            model_slug="gpt-4",
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            cost=0.01,
            timestamp=base_time + timedelta(minutes=10),
            project="project-a",
            generation_id="gen-1",
        ),
        RawUsageRecord(
            model="gpt-4",
            model_slug="gpt-4",
            prompt_tokens=2000,
            completion_tokens=1000,
            total_tokens=3000,
            cost=0.02,
            timestamp=base_time + timedelta(minutes=30),
            project="project-a",
            generation_id="gen-2",
        ),
        RawUsageRecord(
            model="gpt-4",
            model_slug="gpt-4",
            prompt_tokens=500,
            completion_tokens=250,
            total_tokens=750,
            cost=0.005,
            timestamp=base_time + timedelta(hours=1, minutes=5),
            project="project-a",
            generation_id="gen-3",
        ),
    ]
    
    normalized = normalize_usage_records(raw_records)
    
    # Should have 2 records (2 hours)
    assert len(normalized) == 2
    
    # First hour should have aggregated tokens
    hour1 = [r for r in normalized if r.date_hour == base_time.replace(minute=0, second=0, microsecond=0)][0]
    assert hour1.prompt_tokens == 3000
    assert hour1.completion_tokens == 1500
    assert hour1.total_tokens == 4500
    assert hour1.cost == 0.03
    assert "gen-1" in hour1.request_id
    assert "gen-2" in hour1.request_id
    
    # Second hour
    hour2 = [r for r in normalized if r.date_hour == (base_time + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)][0]
    assert hour2.prompt_tokens == 500
    assert hour2.completion_tokens == 250
    assert hour2.total_tokens == 750
    assert hour2.cost == 0.005
    
    print("✓ Normalization test passed")


if __name__ == "__main__":
    # Run tests manually
    async def run_tests():
        test_raw_records = generate_test_raw_records(10000)
        mock_client = MockOpenRouterClient(test_raw_records)
        
        # Setup
        db_path = get_db_path()
        cursor_path = db_path.parent / "cursor.txt"
        if db_path.exists():
            db_path.unlink()
        if cursor_path.exists():
            cursor_path.unlink()
        init_db()
        
        try:
            await test_idempotent_upsert_10k_records(mock_client, test_raw_records)
            await test_run_full_idempotent(mock_client, test_raw_records)
            await test_event_emitted(mock_client, test_raw_records)
            await test_normalize_aggregates_hourly()
            print("\n✅ All critic tests passed!")
        finally:
            if db_path.exists():
                db_path.unlink()
            if cursor_path.exists():
                cursor_path.unlink()
    
    asyncio.run(run_tests())