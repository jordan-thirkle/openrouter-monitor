"""OpenRouter API Client - Interface definitions.

This module defines the interface for the OpenRouter client.
The actual implementation is built by sub-1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class UsageRecord:
    """A single usage record from OpenRouter API (normalized for ingestion).
    
    This is the normalized form used internally - hourly bucketed.
    """
    model: str
    project: str
    date_hour: datetime  # Hour bucket (e.g., 2024-01-15 14:00:00)
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    request_id: Optional[str] = None


@dataclass(frozen=True)
class RawUsageRecord:
    """Raw usage record from OpenRouter API before normalization."""
    model: str
    model_slug: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    timestamp: datetime
    project: Optional[str] = None
    user: Optional[str] = None
    generation_id: Optional[str] = None


@dataclass(frozen=True)
class ModelInfo:
    """Model information from OpenRouter."""
    id: str
    name: str
    context_length: int
    pricing_prompt: float  # per 1M tokens
    pricing_completion: float  # per 1M tokens


@dataclass(frozen=True)
class KeyInfo:
    """API key information."""
    label: str
    limit: Optional[float]
    usage: float
    is_free_tier: bool


class OpenRouterClient:
    """OpenRouter REST client with rate limiting and retry logic.
    
    Rate limit: 60 RPM / 500 RPH (configurable)
    Retry: Exponential backoff, max 3 attempts
    Auth: Bearer token from config/settings.yaml
    """
    
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1"):
        self.api_key = api_key
        self.base_url = base_url
    
    async def get_usage(self, start: datetime, end: datetime) -> List[RawUsageRecord]:
        """Fetch raw usage records between start and end (inclusive start, exclusive end)."""
        raise NotImplementedError("Implemented by sub-1")
    
    async def get_models(self) -> List[ModelInfo]:
        """Fetch available models."""
        raise NotImplementedError("Implemented by sub-1")
    
    async def get_key_info(self) -> KeyInfo:
        """Fetch API key info."""
        raise NotImplementedError("Implemented by sub-1")


def normalize_usage_records(raw_records: List[RawUsageRecord]) -> List[UsageRecord]:
    """Convert raw API records to normalized hourly-bucketed records.
    
    Groups by (model, project, date_hour) and aggregates tokens/cost.
    """
    from collections import defaultdict
    
    # Group by (model, project, date_hour)
    grouped: dict[tuple[str, str, datetime], dict] = defaultdict(lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "request_ids": [],
    })
    
    for record in raw_records:
        # Bucket timestamp to hour
        date_hour = record.timestamp.replace(minute=0, second=0, microsecond=0)
        project = record.project or "default"
        
        key = (record.model, project, date_hour)
        grouped[key]["prompt_tokens"] += record.prompt_tokens
        grouped[key]["completion_tokens"] += record.completion_tokens
        grouped[key]["total_tokens"] += record.total_tokens
        grouped[key]["cost"] += record.cost
        if record.generation_id:
            grouped[key]["request_ids"].append(record.generation_id)
    
    # Convert to normalized records
    normalized = []
    for (model, project, date_hour), agg in grouped.items():
        normalized.append(UsageRecord(
            model=model,
            project=project,
            date_hour=date_hour,
            prompt_tokens=agg["prompt_tokens"],
            completion_tokens=agg["completion_tokens"],
            total_tokens=agg["total_tokens"],
            cost=agg["cost"],
            request_id=",".join(agg["request_ids"]) if agg["request_ids"] else None,
        ))
    
    return normalized