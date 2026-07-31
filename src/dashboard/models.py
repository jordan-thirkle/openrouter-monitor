"""Data models for the dashboard API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class Granularity(str, Enum):
    """Time granularity for usage queries."""
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class GroupBy(str, Enum):
    """Grouping options for cost queries."""
    MODEL = "model"
    PROJECT = "project"
    DAY = "day"


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AnomalySeverity(str, Enum):
    """Anomaly severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class UsageRecord:
    """Usage record for dashboard display."""
    model: str
    project: str
    date_hour: datetime
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    request_id: Optional[str] = None


@dataclass(frozen=True)
class UsageResponse:
    """Response for /api/usage endpoint."""
    records: List[UsageRecord]
    total_records: int
    from_date: datetime
    to_date: datetime
    granularity: Granularity


@dataclass(frozen=True)
class CostBreakdown:
    """Cost breakdown for a group."""
    group: str  # model, project, or date
    total_cost: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    record_count: int
    model_breakdown: Dict[str, float] = field(default_factory=dict)  # model -> cost


@dataclass(frozen=True)
class CostsResponse:
    """Response for /api/costs endpoint."""
    breakdowns: List[CostBreakdown]
    group_by: GroupBy
    total_cost: float
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True)
class Alert:
    """Alert model for dashboard."""
    id: str
    rule_name: str
    severity: AlertSeverity
    message: str
    project: Optional[str]
    model: Optional[str]
    value: float
    threshold: float
    acknowledged: bool
    created_at: datetime
    acknowledged_at: Optional[datetime] = None


@dataclass(frozen=True)
class AlertsResponse:
    """Response for /api/alerts endpoint."""
    alerts: List[Alert]
    total: int
    unacknowledged: int


@dataclass(frozen=True)
class Anomaly:
    """Anomaly model for dashboard."""
    id: str
    model: str
    project: Optional[str]
    metric: str  # e.g., "cost", "tokens", "latency"
    severity: AnomalySeverity
    score: float
    expected_value: float
    actual_value: float
    deviation_pct: float
    detected_at: datetime
    description: str


@dataclass(frozen=True)
class AnomaliesResponse:
    """Response for /api/anomalies endpoint."""
    anomalies: List[Anomaly]
    total: int
    by_severity: Dict[str, int]


@dataclass(frozen=True)
class HealthResponse:
    """Response for /api/health endpoint."""
    status: str  # "healthy", "degraded", "unhealthy"
    version: str
    uptime_seconds: float
    database_connected: bool
    event_queue_size: int
    active_websockets: int
    last_ingestion: Optional[datetime] = None
    last_cost_calculation: Optional[datetime] = None
    last_anomaly_scan: Optional[datetime] = None


@dataclass(frozen=True)
class DashboardSnapshot:
    """Real-time dashboard snapshot for WebSocket updates."""
    timestamp: datetime
    total_cost_24h: float
    total_tokens_24h: int
    active_models: int
    active_projects: int
    unacknowledged_alerts: int
    recent_anomalies: int
    usage_by_model: Dict[str, float]  # model -> cost
    usage_by_project: Dict[str, float]  # project -> cost
    cost_trend_24h: List[Dict[str, Any]]  # [{"hour": "14:00", "cost": 1.23}, ...]


@dataclass(frozen=True)
class WebSocketMessage:
    """WebSocket message format."""
    type: str  # "snapshot", "alert", "anomaly", "usage_update", "cost_update", "health"
    payload: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


# Event types that dashboard subscribes to
class DashboardEventType(str, Enum):
    USAGE_INGESTED = "usage.ingested"
    COSTS_CALCULATED = "costs.calculated"
    ALERT_TRIGGERED = "alert.triggered"
    ANOMALY_DETECTED = "anomaly.detected"


@dataclass(frozen=True)
class DashboardEvent:
    """Event received by dashboard from other components."""
    event_type: DashboardEventType
    payload: Any
    timestamp: datetime = field(default_factory=datetime.utcnow)


__all__ = [
    "Granularity",
    "GroupBy",
    "AlertSeverity",
    "AnomalySeverity",
    "UsageRecord",
    "UsageResponse",
    "CostBreakdown",
    "CostsResponse",
    "Alert",
    "AlertsResponse",
    "Anomaly",
    "AnomaliesResponse",
    "HealthResponse",
    "DashboardSnapshot",
    "WebSocketMessage",
    "DashboardEventType",
    "DashboardEvent",
]