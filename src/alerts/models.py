"""Data models for the alerting engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class AlertSeverity(str, Enum):
    """Alert severity levels."""

    P1 = "P1"  # Critical
    P2 = "P2"  # High
    P3 = "P3"  # Medium
    P4 = "P4"  # Low


class AlertCondition(str, Enum):
    """Comparison conditions for alert rules."""

    GT = "gt"      # Greater than
    GTE = "gte"    # Greater than or equal
    LT = "lt"      # Less than
    LTE = "lte"    # Less than or equal
    EQ = "eq"      # Equal
    NE = "ne"      # Not equal


class AlertChannel(str, Enum):
    """Delivery channels for alerts."""

    TELEGRAM = "telegram"
    STDOUT = "stdout"


class AlertMetric(str, Enum):
    """Metrics that can be evaluated for alerts."""

    DAILY_COST = "daily_cost"
    HOURLY_COST = "hourly_cost"
    PROJECT_COST = "project_cost"
    MODEL_COST = "model_cost"
    ANOMALY_SCORE = "anomaly_score"
    HOURLY_TOKENS = "hourly_tokens"
    COST_GROWTH_RATE = "cost_growth_rate"


@dataclass(frozen=True)
class AlertRule:
    """Configuration for a single alert rule."""

    name: str
    description: str
    metric: AlertMetric
    condition: AlertCondition
    threshold: float
    severity: AlertSeverity
    cooldown_hours: int = 4
    channels: List[AlertChannel] = field(default_factory=lambda: [AlertChannel.TELEGRAM, AlertChannel.STDOUT])
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AlertRule:
        """Create AlertRule from dictionary (e.g., loaded from YAML)."""
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            metric=AlertMetric(data["metric"]),
            condition=AlertCondition(data["condition"]),
            threshold=float(data["threshold"]),
            severity=AlertSeverity(data.get("severity", "P3")),
            cooldown_hours=int(data.get("cooldown_hours", 4)),
            channels=[AlertChannel(c) for c in data.get("channels", ["telegram", "stdout"])],
            enabled=data.get("enabled", True),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class Alert:
    """An alert that has been triggered."""

    rule_name: str
    rule_description: str
    metric: AlertMetric
    condition: AlertCondition
    threshold: float
    actual_value: float
    severity: AlertSeverity
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Delivery tracking
    delivered: bool = False
    delivery_results: Dict[AlertChannel, bool] = field(default_factory=dict)

    @property
    def event_type(self) -> str:
        """Event type for pub/sub."""
        return "alert.triggered"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "rule_name": self.rule_name,
            "rule_description": self.rule_description,
            "metric": self.metric.value,
            "condition": self.condition.value,
            "threshold": self.threshold,
            "actual_value": self.actual_value,
            "severity": self.severity.value,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "delivered": self.delivered,
            "delivery_results": {k.value: v for k, v in self.delivery_results.items()},
        }


@dataclass(frozen=True)
class DeliveryResult:
    """Result of delivering an alert through a channel."""

    channel: AlertChannel
    success: bool
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    error: Optional[str] = None


@dataclass(frozen=True)
class DailyMetrics:
    """Daily aggregated metrics for alert evaluation."""

    date: datetime
    total_cost: Decimal
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    project_costs: Dict[str, Decimal]
    model_costs: Dict[str, Decimal]
    hourly_costs: Dict[int, Decimal]  # hour -> cost
    hourly_tokens: Dict[int, int]     # hour -> tokens
    anomaly_scores: Dict[str, float]  # anomaly_id -> score
    cost_growth_rate: float = 0.0     # ratio vs previous period


@dataclass(frozen=True)
class AlertConfig:
    """Configuration for the alert engine."""

    rules: List[AlertRule]
    default_cooldown_hours: int = 4
    default_channels: List[AlertChannel] = field(default_factory=lambda: [AlertChannel.TELEGRAM, AlertChannel.STDOUT])
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_parse_mode: str = "HTML"
    telegram_disable_web_page_preview: bool = True

    @classmethod
    def from_yaml_files(
        cls,
        rules_path: str,
        settings_path: Optional[str] = None,
    ) -> AlertConfig:
        """Load configuration from YAML files."""
        import yaml
        from pathlib import Path

        # Load rules
        with open(rules_path, "r") as f:
            rules_data = yaml.safe_load(f)

        rules = [AlertRule.from_dict(rule) for rule in rules_data.get("rules", [])]
        default_cooldown = rules_data.get("default_cooldown_hours", 4)
        default_channels = [AlertChannel(c) for c in rules_data.get("default_channels", ["telegram", "stdout"])]

        # Load Telegram settings from settings.yaml if provided
        telegram_bot_token = None
        telegram_chat_id = None
        if settings_path:
            try:
                with open(settings_path, "r") as f:
                    settings = yaml.safe_load(f)
                telegram_bot_token = settings.get("telegram", {}).get("bot_token")
                telegram_chat_id = settings.get("telegram", {}).get("chat_id")
            except (FileNotFoundError, KeyError, TypeError):
                pass

        return cls(
            rules=rules,
            default_cooldown_hours=default_cooldown,
            default_channels=default_channels,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
        )


@dataclass
class AlertState:
    """Persistent state for alert deduplication."""

    last_triggered: Dict[str, datetime] = field(default_factory=dict)  # rule_name -> timestamp

    def should_trigger(self, rule_name: str, cooldown_hours: int) -> bool:
        """Check if an alert should trigger based on cooldown."""
        if rule_name not in self.last_triggered:
            return True
        elapsed = datetime.utcnow() - self.last_triggered[rule_name]
        return elapsed.total_seconds() >= cooldown_hours * 3600

    def mark_triggered(self, rule_name: str) -> None:
        """Mark a rule as triggered now."""
        self.last_triggered[rule_name] = datetime.utcnow()

    def to_dict(self) -> Dict[str, str]:
        """Serialize to dictionary."""
        return {k: v.isoformat() for k, v in self.last_triggered.items()}

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> AlertState:
        """Deserialize from dictionary."""
        return cls(last_triggered={k: datetime.fromisoformat(v) for k, v in data.items()})