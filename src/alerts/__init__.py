"""Alert Engine Package for OpenRouter Monitor.

This package provides:
- Alert rule evaluation against metrics
- Deduplication (1 alert per rule per 4 hours)
- Multi-channel delivery (Telegram primary, stdout fallback)
- Event emission (alert.triggered)
"""

from src.alerts.models import (
    Alert,
    AlertChannel,
    AlertCondition,
    AlertConfig,
    AlertMetric,
    AlertRule,
    AlertSeverity,
    AlertState,
    DailyMetrics,
    DeliveryResult,
)
from src.alerts.engine import (
    AlertEngine,
    AlertEngineConfig,
    create_daily_metrics_from_costs,
)
from src.alerts.channels import (
    AlertChannelBase,
    ChannelManager,
    StdoutChannel,
    TelegramChannel,
)

__all__ = [
    # Models
    "Alert",
    "AlertChannel",
    "AlertCondition",
    "AlertConfig",
    "AlertMetric",
    "AlertRule",
    "AlertSeverity",
    "AlertState",
    "DailyMetrics",
    "DeliveryResult",
    # Engine
    "AlertEngine",
    "AlertEngineConfig",
    "create_daily_metrics_from_costs",
    # Channels
    "AlertChannelBase",
    "ChannelManager",
    "StdoutChannel",
    "TelegramChannel",
]

__version__ = "1.0.0"