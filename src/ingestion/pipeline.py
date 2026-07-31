"""Ingestion Pipeline for OpenRouter usage data.

Fetches usage from OpenRouter API, normalizes, stores idempotently in SQLite,
and emits usage.ingested events.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from src.api.client import OpenRouterClient, UsageRecord
from src.ingestion.database import (
    IngestionResult as DBIngestionResult,
    get_last_cursor,
    get_latest_record_time,
    init_db,
    set_cursor,
    upsert_usage_records,
)


@dataclass
class IngestionResult:
    """Result of an ingestion run."""
    records_processed: int
    records_inserted: int
    records_updated: int
    start_time: datetime
    end_time: datetime
    cursor: datetime
    errors: List[str] = field(default_factory=list)


# Event queue for pub/sub - shared across components
event_queue: asyncio.Queue = asyncio.Queue()


class IngestionPipeline:
    """Ingestion pipeline for OpenRouter usage data.
    
    Fetches usage records from OpenRouter API, stores them idempotently
    in SQLite (upsert on model, project, date_hour), and emits
    usage.ingested events.
    """
    
    def __init__(
        self,
        client: OpenRouterClient,
        event_queue: Optional[asyncio.Queue] = None,
    ):
        self.client = client
        self._event_queue = event_queue or event_queue
        init_db()
    
    def get_last_cursor(self) -> Optional[datetime]:
        """Get the last successful ingestion cursor.
        
        Returns the cursor from data/cursor.txt, or None if never run.
        """
        return get_last_cursor()
    
    async def run_incremental(self) -> IngestionResult:
        """Run incremental ingestion since last cursor.
        
        Fetches usage from the last cursor to now, upserts records,
        updates cursor, and emits usage.ingested event.
        """
        start_time = datetime.utcnow()
        last_cursor = self.get_last_cursor()
        
        # If no cursor, default to 24 hours ago
        if last_cursor is None:
            last_cursor = start_time - timedelta(hours=24)
        
        end_time = start_time
        
        # Fetch usage from API
        try:
            records = await self.client.get_usage(last_cursor, end_time)
        except Exception as e:
            return IngestionResult(
                records_processed=0,
                records_inserted=0,
                records_updated=0,
                start_time=start_time,
                end_time=datetime.utcnow(),
                cursor=last_cursor,
                errors=[f"API fetch failed: {e}"],
            )
        
        # Upsert records
        inserted, updated = upsert_usage_records(records)
        
        # Determine new cursor (end of the window we just processed)
        new_cursor = end_time
        set_cursor(new_cursor)
        
        end_time_actual = datetime.utcnow()
        
        result = IngestionResult(
            records_processed=len(records),
            records_inserted=inserted,
            records_updated=updated,
            start_time=start_time,
            end_time=end_time_actual,
            cursor=new_cursor,
            errors=[],
        )
        
        # Emit event
        await self._emit_event(result)
        
        return result
    
    async def run_full(self, days: int) -> IngestionResult:
        """Run full ingestion for the specified number of days back.
        
        Fetches usage from (now - days) to now, upserts records,
        updates cursor to now, and emits usage.ingested event.
        """
        start_time = datetime.utcnow()
        end_time = start_time
        window_start = end_time - timedelta(days=days)
        
        # Fetch usage from API
        try:
            records = await self.client.get_usage(window_start, end_time)
        except Exception as e:
            return IngestionResult(
                records_processed=0,
                records_inserted=0,
                records_updated=0,
                start_time=start_time,
                end_time=datetime.utcnow(),
                cursor=window_start,
                errors=[f"API fetch failed: {e}"],
            )
        
        # Upsert records
        inserted, updated = upsert_usage_records(records)
        
        # Update cursor to end of window
        new_cursor = end_time
        set_cursor(new_cursor)
        
        end_time_actual = datetime.utcnow()
        
        result = IngestionResult(
            records_processed=len(records),
            records_inserted=inserted,
            records_updated=updated,
            start_time=start_time,
            end_time=end_time_actual,
            cursor=new_cursor,
            errors=[],
        )
        
        # Emit event
        await self._emit_event(result)
        
        return result
    
    async def _emit_event(self, result: IngestionResult) -> None:
        """Emit usage.ingested event to the event queue."""
        try:
            self._event_queue.put_nowait(("usage.ingested", result))
        except asyncio.QueueFull:
            # Log but don't fail ingestion
            pass