"""Alert Engine for evaluating rules and delivering alerts."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.alerts.channels import ChannelManager
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
from src.costs.models import ProjectCost

logger = logging.getLogger(__name__)


@dataclass
class AlertEngineConfig:
    """Configuration for the AlertEngine."""

    rules_file: Path = Path("config/alert_rules.yaml")
    settings_file: Optional[Path] = Path("config/settings.yaml")
    state_file: Path = Path("data/alert_state.json")
    event_queue: Optional[asyncio.Queue] = None


class AlertEngine:
    """
    Alert evaluation and delivery engine.

    Evaluates alert rules against daily metrics, handles deduplication
    (1 alert per rule per 4 hours), and delivers via Telegram (primary)
    and stdout (fallback). Emits alert.triggered events.
    """

    def __init__(self, config: Optional[AlertEngineConfig] = None):
        self.config = config or AlertEngineConfig()
        self._alert_config: Optional[AlertConfig] = None
        self._state = AlertState()
        self._channel_manager: Optional[ChannelManager] = None
        self._rules_by_name: Dict[str, AlertRule] = {}
        self._initialized = False

    def _resolve_path(self, path: Path) -> Path:
        """Resolve relative path to project root."""
        if path.is_absolute():
            return path
        project_root = Path(__file__).parent.parent.parent
        return project_root / path

    def initialize(self) -> None:
        """Load configuration and state."""
        if self._initialized:
            return

        # Load alert configuration
        rules_path = self._resolve_path(self.config.rules_file)
        settings_path = self._resolve_path(self.config.settings_file) if self.config.settings_file else None

        self._alert_config = AlertConfig.from_yaml_files(
            rules_path=str(rules_path),
            settings_path=str(settings_path) if settings_path else None,
        )

        # Build rules lookup
        self._rules_by_name = {rule.name: rule for rule in self._alert_config.rules}

        # Load state
        state_path = self._resolve_path(self.config.state_file)
        if state_path.exists():
            try:
                with open(state_path, "r") as f:
                    state_data = json.load(f)
                self._state = AlertState.from_dict(state_data)
                logger.info(f"Loaded alert state with {len(self._state.last_triggered)} rules")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load alert state: {e}, starting fresh")

        # Initialize channel manager
        self._channel_manager = ChannelManager(self._alert_config)

        self._initialized = True
        logger.info(f"AlertEngine initialized with {len(self._rules_by_name)} rules")

    def _save_state(self) -> None:
        """Persist alert state to disk."""
        state_path = self._resolve_path(self.config.state_file)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(state_path, "w") as f:
                json.dump(self._state.to_dict(), f, indent=2)
        except OSError as e:
            logger.error(f"Failed to save alert state: {e}")

    def _evaluate_condition(self, actual: float, condition: AlertCondition, threshold: float) -> bool:
        """Evaluate a condition against actual value and threshold."""
        if condition == AlertCondition.GT:
            return actual > threshold
        elif condition == AlertCondition.GTE:
            return actual >= threshold
        elif condition == AlertCondition.LT:
            return actual < threshold
        elif condition == AlertCondition.LTE:
            return actual <= threshold
        elif condition == AlertCondition.EQ:
            return actual == threshold
        elif condition == AlertCondition.NE:
            return actual != threshold
        return False

    def _get_metric_value(self, metrics: DailyMetrics, rule: AlertRule) -> float:
        """Extract the metric value from DailyMetrics for a given rule."""
        metric = rule.metric

        if metric == AlertMetric.DAILY_COST:
            return float(metrics.total_cost)
        elif metric == AlertMetric.HOURLY_COST:
            # Return max hourly cost
            return float(max(metrics.hourly_costs.values())) if metrics.hourly_costs else 0.0
        elif metric == AlertMetric.PROJECT_COST:
            # Return max project cost
            return float(max(metrics.project_costs.values())) if metrics.project_costs else 0.0
        elif metric == AlertMetric.MODEL_COST:
            # Return max model cost
            return float(max(metrics.model_costs.values())) if metrics.model_costs else 0.0
        elif metric == AlertMetric.ANOMALY_SCORE:
            # Return max anomaly score
            return max(metrics.anomaly_scores.values()) if metrics.anomaly_scores else 0.0
        elif metric == AlertMetric.HOURLY_TOKENS:
            # Return max hourly tokens
            return float(max(metrics.hourly_tokens.values())) if metrics.hourly_tokens else 0.0
        elif metric == AlertMetric.COST_GROWTH_RATE:
            return metrics.cost_growth_rate
        else:
            logger.warning(f"Unknown metric: {metric}")
            return 0.0

    def _build_alert_metadata(self, metrics: DailyMetrics, rule: AlertRule, actual_value: float) -> Dict[str, Any]:
        """Build metadata dictionary for the alert."""
        metadata: Dict[str, Any] = {
            "channels": [c.value for c in rule.channels],
        }

        # Add relevant context based on metric
        if rule.metric == AlertMetric.PROJECT_COST and metrics.project_costs:
            # Find the project that triggered the alert
            for project, cost in metrics.project_costs.items():
                if float(cost) >= rule.threshold:
                    metadata["triggered_project"] = project
                    metadata["project_cost"] = float(cost)
                    break
        elif rule.metric == AlertMetric.MODEL_COST and metrics.model_costs:
            for model, cost in metrics.model_costs.items():
                if float(cost) >= rule.threshold:
                    metadata["triggered_model"] = model
                    metadata["model_cost"] = float(cost)
                    break
        elif rule.metric == AlertMetric.HOURLY_COST and metrics.hourly_costs:
            for hour, cost in metrics.hourly_costs.items():
                if float(cost) >= rule.threshold:
                    metadata["triggered_hour"] = hour
                    metadata["hourly_cost"] = float(cost)
                    break
        elif rule.metric == AlertMetric.ANOMALY_SCORE and metrics.anomaly_scores:
            for anomaly_id, score in metrics.anomaly_scores.items():
                if score >= rule.threshold:
                    metadata["triggered_anomaly"] = anomaly_id
                    metadata["anomaly_score"] = score
                    break

        return metadata

    def evaluate(self, metrics: DailyMetrics) -> List[Alert]:
        """
        Evaluate all enabled alert rules against daily metrics.

        Returns list of triggered alerts (after deduplication check).
        """
        if not self._initialized:
            self.initialize()

        assert self._alert_config is not None, "Alert config not initialized"
        triggered_alerts = []

        for rule in self._alert_config.rules:
            if not rule.enabled:
                continue

            # Get metric value
            actual_value = self._get_metric_value(metrics, rule)

            # Evaluate condition
            if not self._evaluate_condition(actual_value, rule.condition, rule.threshold):
                continue

            # Check deduplication (cooldown)
            if not self._state.should_trigger(rule.name, rule.cooldown_hours):
                logger.debug(f"Alert rule '{rule.name}' in cooldown, skipping")
                continue

            # Build alert
            metadata = self._build_alert_metadata(metrics, rule, actual_value)
            alert = Alert(
                rule_name=rule.name,
                rule_description=rule.description,
                metric=rule.metric,
                condition=rule.condition,
                threshold=rule.threshold,
                actual_value=actual_value,
                severity=rule.severity,
                timestamp=datetime.utcnow(),
                metadata=metadata,
            )

            triggered_alerts.append(alert)
            logger.info(f"Alert triggered: {rule.name} (actual={actual_value}, threshold={rule.threshold})")

            # Mark as triggered for deduplication
            self._state.mark_triggered(rule.name)

        # Persist state after evaluation
        if triggered_alerts:
            self._save_state()

        return triggered_alerts

    async def deliver(self, alert: Alert) -> Dict[AlertChannel, DeliveryResult]:
        """
        Deliver an alert through configured channels.

        Returns delivery results per channel.
        """
        if not self._initialized:
            self.initialize()

        if self._channel_manager is None:
            raise RuntimeError("Channel manager not initialized")

        # Deliver through channels
        results = await self._channel_manager.deliver(alert)

        # Update alert delivery status
        alert.delivered = any(r.success for r in results.values())
        alert.delivery_results = results

        # Emit event if any channel succeeded
        if alert.delivered and self.config.event_queue is not None:
            await self._emit_alert_event(alert)

        return results

    async def evaluate_and_deliver(self, metrics: DailyMetrics) -> List[Alert]:
        """
        Convenience method: evaluate rules and deliver all triggered alerts.

        Returns list of alerts that were triggered and delivered.
        """
        alerts = self.evaluate(metrics)

        for alert in alerts:
            await self.deliver(alert)

        return alerts

    async def _emit_alert_event(self, alert: Alert) -> None:
        """Emit alert.triggered event to the event queue."""
        event_queue = self.config.event_queue
        if event_queue is None:
            return
        try:
            event_queue.put_nowait(("alert.triggered", alert))
            logger.debug(f"Emitted alert.triggered event for rule: {alert.rule_name}")
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping alert.triggered event")

    async def close(self) -> None:
        """Clean up resources."""
        if self._channel_manager:
            await self._channel_manager.close()
        self._initialized = False

    def get_rule(self, name: str) -> Optional[AlertRule]:
        """Get a rule by name."""
        return self._rules_by_name.get(name)

    def list_rules(self) -> List[AlertRule]:
        """List all configured rules."""
        return list(self._rules_by_name.values())

    def get_state(self) -> AlertState:
        """Get current alert state (for inspection)."""
        return self._state

    def clear_cooldown(self, rule_name: str) -> bool:
        """Manually clear cooldown for a rule (for testing)."""
        if rule_name in self._state.last_triggered:
            del self._state.last_triggered[rule_name]
            self._save_state()
            return True
        return False


def create_daily_metrics_from_costs(
    project_costs: Dict[str, ProjectCost],
    date: Optional[datetime] = None,
) -> DailyMetrics:
    """
    Create DailyMetrics from cost engine output.

    This is a helper to bridge costs engine output to alert engine input.
    """
    if date is None:
        date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    total_cost = Decimal("0")
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    project_cost_dict = {}
    model_cost_dict = {}

    # Aggregate from project costs
    for project, pc in project_costs.items():
        project_cost_dict[project] = pc.total_cost
        total_cost += pc.total_cost
        total_prompt_tokens += pc.total_prompt_tokens
        total_completion_tokens += pc.total_completion_tokens
        total_tokens += pc.total_tokens

        for model, cost in pc.model_breakdown.items():
            model_cost_dict[model] = model_cost_dict.get(model, Decimal("0")) + cost

    # For now, hourly data is not available from costs engine
    # This would need to be populated from ingestion data
    hourly_costs = {}
    hourly_tokens = {}
    anomaly_scores = {}

    return DailyMetrics(
        date=date,
        total_cost=total_cost,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        total_tokens=total_tokens,
        project_costs=project_cost_dict,
        model_costs=model_cost_dict,
        hourly_costs=hourly_costs,
        hourly_tokens=hourly_tokens,
        anomaly_scores=anomaly_scores,
    )


# Backwards compatibility alias
AlertEngineConfig.__doc__ = "Configuration for AlertEngine"