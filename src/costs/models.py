"""Data models for the cost calculation engine."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional


@dataclass(frozen=True)
class UsageRecord:
    """A single usage record from OpenRouter API."""

    model: str
    project: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Decimal
    timestamp: datetime
    request_id: str
    # Optional fields for future extensibility
    latency_ms: Optional[int] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ModelPricing:
    """Pricing information for a specific model."""

    model: str
    prompt_price_per_1k: Decimal  # Price per 1K prompt tokens
    completion_price_per_1k: Decimal  # Price per 1K completion tokens
    currency: str = "USD"
    # Optional: cached/extended pricing for models with different tiers
    cached_prompt_price_per_1k: Optional[Decimal] = None
    # When this pricing was last updated from OpenRouter
    updated_at: Optional[datetime] = None

    def calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        """Calculate cost for given token usage."""
        prompt_cost = (Decimal(prompt_tokens) / Decimal(1000)) * self.prompt_price_per_1k
        completion_cost = (Decimal(completion_tokens) / Decimal(1000)) * self.completion_price_per_1k
        return (prompt_cost + completion_cost).quantize(Decimal("0.000001"))


@dataclass(frozen=True)
class CostBreakdown:
    """Detailed cost breakdown for a single usage record."""

    model: str
    project: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cost: Decimal
    completion_cost: Decimal
    total_cost: Decimal
    pricing_used: ModelPricing
    timestamp: datetime
    request_id: str


@dataclass(frozen=True)
class ProjectCost:
    """Aggregated cost for a project."""

    project: str
    total_cost: Decimal
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    model_breakdown: Dict[str, Decimal]  # model -> cost
    record_count: int
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True)
class CostEvent:
    """Event emitted when costs are calculated."""

    event_type: str = "costs.calculated"
    project_costs: List[ProjectCost] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)