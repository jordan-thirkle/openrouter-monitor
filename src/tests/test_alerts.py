"""
Critic tests for AlertEngine.

Tests that the alert engine correctly handles:
- Seed threshold breach → fires once, dedups 4h, delivers Telegram (mocked)
- All 8 alert rules from config/alert_rules.yaml
- Mock Telegram API, verify stdout fallback
- Cooldown persistence to data/alert_state.json
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from src.alerts.engine import (
    AlertEngine,
    AlertEngineConfig,
    create_daily_metrics_from_costs,
)
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
from src.alerts.channels import (
    ChannelManager,
    StdoutChannel,
    TelegramChannel,
)
from src.costs.models import ProjectCost


# Module-level test utilities
class MockTelegramSession:
    """Mock aiohttp.ClientSession for Telegram API."""

    def __init__(self, responses: List[Dict[str, Any]]):
        self.responses = responses
        self.call_count = 0
        self.last_payload = None
        self.last_url = None
        self.closed = False

    def post(self, url: str, json: Dict[str, Any]):
        self.call_count += 1
        self.last_url = url
        self.last_payload = json
        response_data = self.responses[min(self.call_count - 1, len(self.responses) - 1)]
        return MockTelegramResponse(response_data.get("status", 200), response_data.get("body", {"ok": True}))

    async def close(self):
        self.closed = True


class MockTelegramResponse:
    """Mock aiohttp response for Telegram API."""

    def __init__(self, status: int, data: Dict[str, Any]):
        self.status = status
        self._data = data

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def create_test_metrics(
    total_cost: float = 0.0,
    project_costs: Optional[Dict[str, float]] = None,
    model_costs: Optional[Dict[str, float]] = None,
    hourly_costs: Optional[Dict[int, float]] = None,
    hourly_tokens: Optional[Dict[int, int]] = None,
    anomaly_scores: Optional[Dict[str, float]] = None,
    cost_growth_rate: float = 0.0,
) -> DailyMetrics:
    """Create DailyMetrics for testing."""
    return DailyMetrics(
        date=datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
        total_cost=Decimal(str(total_cost)),
        total_prompt_tokens=1000,
        total_completion_tokens=500,
        total_tokens=1500,
        project_costs={k: Decimal(str(v)) for k, v in (project_costs or {}).items()},
        model_costs={k: Decimal(str(v)) for k, v in (model_costs or {}).items()},
        hourly_costs={k: Decimal(str(v)) for k, v in (hourly_costs or {}).items()},
        hourly_tokens=hourly_tokens or {},
        anomaly_scores=anomaly_scores or {},
        cost_growth_rate=cost_growth_rate,
    )


# Module-level fixtures
@pytest.fixture
def temp_dirs():
    """Create temporary directories for config and state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        config_dir.mkdir()
        data_dir.mkdir()

        # Create alert_rules.yaml
        rules_content = {
            "rules": [
                {
                    "name": "daily_cost_breach",
                    "description": "Total daily cost exceeds threshold",
                    "metric": "daily_cost",
                    "condition": "gt",
                    "threshold": 100.0,
                    "severity": "P1",
                    "cooldown_hours": 4,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                },
                {
                    "name": "hourly_cost_spike",
                    "description": "Hourly cost spike detected",
                    "metric": "hourly_cost",
                    "condition": "gt",
                    "threshold": 25.0,
                    "severity": "P2",
                    "cooldown_hours": 4,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                },
                {
                    "name": "project_cost_overrun",
                    "description": "Single project cost exceeds daily budget",
                    "metric": "project_cost",
                    "condition": "gt",
                    "threshold": 50.0,
                    "severity": "P2",
                    "cooldown_hours": 4,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                },
                {
                    "name": "model_cost_anomaly",
                    "description": "Model cost significantly higher than baseline",
                    "metric": "model_cost",
                    "condition": "gt",
                    "threshold": 30.0,
                    "severity": "P3",
                    "cooldown_hours": 4,
                    "channels": ["stdout"],
                    "enabled": True,
                },
                {
                    "name": "anomaly_detected_high",
                    "description": "High severity anomaly detected",
                    "metric": "anomaly_score",
                    "condition": "gte",
                    "threshold": 0.8,
                    "severity": "P1",
                    "cooldown_hours": 2,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                },
                {
                    "name": "anomaly_detected_medium",
                    "description": "Medium severity anomaly detected",
                    "metric": "anomaly_score",
                    "condition": "gte",
                    "threshold": 0.5,
                    "severity": "P3",
                    "cooldown_hours": 4,
                    "channels": ["stdout"],
                    "enabled": True,
                },
                {
                    "name": "token_usage_spike",
                    "description": "Token usage spike (potential runaway process)",
                    "metric": "hourly_tokens",
                    "condition": "gt",
                    "threshold": 500000,
                    "severity": "P2",
                    "cooldown_hours": 4,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                },
                {
                    "name": "cost_increase_rate",
                    "description": "Cost increasing rapidly compared to previous period",
                    "metric": "cost_growth_rate",
                    "condition": "gt",
                    "threshold": 2.0,
                    "severity": "P2",
                    "cooldown_hours": 4,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                },
            ],
            "default_cooldown_hours": 4,
            "default_channels": ["telegram", "stdout"],
        }

        rules_file = config_dir / "alert_rules.yaml"
        with open(rules_file, "w") as f:
            yaml.dump(rules_content, f)

        # Create settings.yaml with Telegram config
        settings_content = {
            "telegram": {
                "bot_token": "test-bot-token",
                "chat_id": "test-chat-id",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        }
        settings_file = config_dir / "settings.yaml"
        with open(settings_file, "w") as f:
            yaml.dump(settings_content, f)

        yield {
            "config_dir": config_dir,
            "data_dir": data_dir,
            "rules_file": rules_file,
            "settings_file": settings_file,
            "state_file": data_dir / "alert_state.json",
        }


@pytest.fixture
def engine(temp_dirs):
    """Create AlertEngine with test configuration."""
    config = AlertEngineConfig(
        rules_file=temp_dirs["rules_file"],
        settings_file=temp_dirs["settings_file"],
        state_file=temp_dirs["state_file"],
    )
    return AlertEngine(config)


@pytest.fixture
def temp_dirs_delivery():
    """Create temporary directories for delivery tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        config_dir.mkdir()
        data_dir.mkdir()

        rules_content = {
            "rules": [
                {
                    "name": "test_rule",
                    "description": "Test rule",
                    "metric": "daily_cost",
                    "condition": "gt",
                    "threshold": 10.0,
                    "severity": "P2",
                    "cooldown_hours": 4,
                    "channels": ["telegram", "stdout"],
                    "enabled": True,
                }
            ],
            "default_cooldown_hours": 4,
            "default_channels": ["telegram", "stdout"],
        }
        rules_file = config_dir / "alert_rules.yaml"
        with open(rules_file, "w") as f:
            yaml.dump(rules_content, f)

        settings_content = {
            "telegram": {
                "bot_token": "test-token",
                "chat_id": "test-chat",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        }
        settings_file = config_dir / "settings.yaml"
        with open(settings_file, "w") as f:
            yaml.dump(settings_content, f)

        yield {
            "rules_file": rules_file,
            "settings_file": settings_file,
            "state_file": data_dir / "alert_state.json",
        }


# Test classes

    def test_engine_loads_all_8_rules(self, engine):
        """Test that all 8 rules from config are loaded."""
        engine.initialize()
        rules = engine.list_rules()

        assert len(rules) == 8
        rule_names = {r.name for r in rules}
        expected = {
            "daily_cost_breach",
            "hourly_cost_spike",
            "project_cost_overrun",
            "model_cost_anomaly",
            "anomaly_detected_high",
            "anomaly_detected_medium",
            "token_usage_spike",
            "cost_increase_rate",
        }
        assert rule_names == expected

    def test_evaluate_daily_cost_breach(self, engine):
        """Test daily_cost_breach rule triggers on threshold."""
        engine.initialize()

        metrics = create_test_metrics(total_cost=150.0)  # Above 100 threshold
        alerts = engine.evaluate(metrics)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.rule_name == "daily_cost_breach"
        assert alert.severity == AlertSeverity.P1
        assert alert.actual_value == 150.0
        assert alert.threshold == 100.0

    def test_evaluate_no_alert_below_threshold(self, engine):
        """Test no alert when metric below threshold."""
        engine.initialize()

        metrics = create_test_metrics(total_cost=50.0)  # Below 100 threshold
        alerts = engine.evaluate(metrics)

        assert len(alerts) == 0

    def test_evaluate_project_cost_overrun(self, engine):
        """Test project_cost rule triggers on project threshold."""
        engine.initialize()

        metrics = create_test_metrics(
            total_cost=60.0,
            project_costs={"project-a": 75.0, "project-b": 10.0},  # project-a > 50
        )
        alerts = engine.evaluate(metrics)

        project_alerts = [a for a in alerts if a.rule_name == "project_cost_overrun"]
        assert len(project_alerts) == 1
        alert = project_alerts[0]
        assert alert.metadata.get("triggered_project") == "project-a"

    def test_evaluate_model_cost_anomaly(self, engine):
        """Test model_cost rule triggers on model threshold."""
        engine.initialize()

        metrics = create_test_metrics(
            total_cost=40.0,
            model_costs={"gpt-4": 45.0, "gpt-3.5-turbo": 5.0},  # gpt-4 > 30
        )
        alerts = engine.evaluate(metrics)

        model_alerts = [a for a in alerts if a.rule_name == "model_cost_anomaly"]
        assert len(model_alerts) == 1
        assert model_alerts[0].metadata.get("triggered_model") == "gpt-4"

    def test_evaluate_anomaly_score_high(self, engine):
        """Test anomaly_score rule triggers on high score."""
        engine.initialize()

        metrics = create_test_metrics(
            total_cost=10.0,
            anomaly_scores={"anom-1": 0.9},  # Above 0.8
        )
        alerts = engine.evaluate(metrics)

        anomaly_alerts = [a for a in alerts if a.rule_name == "anomaly_detected_high"]
        assert len(anomaly_alerts) == 1
        assert anomaly_alerts[0].severity == AlertSeverity.P1

    def test_evaluate_cost_growth_rate(self, engine):
        """Test cost_growth_rate rule triggers on rapid growth."""
        engine.initialize()

        metrics = create_test_metrics(
            total_cost=10.0,
            cost_growth_rate=3.0,  # 3x growth, above 2.0 threshold
        )
        alerts = engine.evaluate(metrics)

        growth_alerts = [a for a in alerts if a.rule_name == "cost_increase_rate"]
        assert len(growth_alerts) == 1


class TestAlertEngineDeduplication:
    """Test alert deduplication (cooldown)."""

    @pytest.fixture
    def engine(self, temp_dirs):
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        return AlertEngine(config)

    def test_same_rule_not_fired_twice_within_cooldown(self, engine):
        """Test that same rule doesn't fire twice within cooldown period."""
        engine.initialize()

        metrics = create_test_metrics(total_cost=150.0)

        # First evaluation - should trigger
        alerts1 = engine.evaluate(metrics)
        assert len(alerts1) == 1

        # Second evaluation immediately - should be deduplicated
        alerts2 = engine.evaluate(metrics)
        assert len(alerts2) == 0

    def test_cooldown_persists_to_file(self, engine, temp_dirs):
        """Test that cooldown state persists to JSON file."""
        engine.initialize()

        metrics = create_test_metrics(total_cost=150.0)
        engine.evaluate(metrics)

        # Check state file was written
        assert temp_dirs["state_file"].exists()

        with open(temp_dirs["state_file"], "r") as f:
            state_data = json.load(f)

        assert "daily_cost_breach" in state_data
        # Timestamp should be recent
        triggered_time = datetime.fromisoformat(state_data["daily_cost_breach"])
        assert (datetime.utcnow() - triggered_time).total_seconds() < 10

    def test_cooldown_respected_after_restart(self, engine, temp_dirs):
        """Test that cooldown is respected after engine restart."""
        engine.initialize()

        metrics = create_test_metrics(total_cost=150.0)
        engine.evaluate(metrics)  # Trigger and save state

        # Create new engine instance (simulates restart)
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        engine2 = AlertEngine(config)
        engine2.initialize()

        # Should not fire again due to persisted cooldown
        alerts = engine2.evaluate(metrics)
        assert len(alerts) == 0


class TestAlertDelivery:
    """Test alert delivery through channels."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_dir = tmp_path / "config"
            data_dir = tmp_path / "data"
            config_dir.mkdir()
            data_dir.mkdir()

            rules_content = {
                "rules": [
                    {
                        "name": "test_rule",
                        "description": "Test rule",
                        "metric": "daily_cost",
                        "condition": "gt",
                        "threshold": 10.0,
                        "severity": "P2",
                        "cooldown_hours": 4,
                        "channels": ["telegram", "stdout"],
                        "enabled": True,
                    }
                ],
                "default_cooldown_hours": 4,
                "default_channels": ["telegram", "stdout"],
            }
            rules_file = config_dir / "alert_rules.yaml"
            with open(rules_file, "w") as f:
                yaml.dump(rules_content, f)

            settings_content = {
                "telegram": {
                    "bot_token": "test-token",
                    "chat_id": "test-chat",
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
            }
            settings_file = config_dir / "settings.yaml"
            with open(settings_file, "w") as f:
                yaml.dump(settings_content, f)

            yield {
                "rules_file": rules_file,
                "settings_file": settings_file,
                "state_file": data_dir / "alert_state.json",
            }

    @pytest.mark.asyncio
    async def test_telegram_delivery_success(self, temp_dirs):
        """Test successful Telegram delivery."""
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        engine = AlertEngine(config)
        engine.initialize()

        # Mock aiohttp session
        mock_session = MockTelegramSession([{"status": 200, "body": {"ok": True, "result": {}}}])
        engine._channel_manager._channels[AlertChannel.TELEGRAM]._session = mock_session

        metrics = create_test_metrics(total_cost=50.0)
        alerts = engine.evaluate(metrics)
        assert len(alerts) == 1

        results = await engine.deliver(alerts[0])

        assert AlertChannel.TELEGRAM in results
        assert results[AlertChannel.TELEGRAM].success is True
        assert mock_session.call_count == 1
        assert "test-token" in mock_session.last_url
        assert mock_session.last_payload["chat_id"] == "test-chat"

    @pytest.mark.asyncio
    async def test_telegram_delivery_failure_fallbacks_to_stdout(self, temp_dirs):
        """Test stdout fallback when Telegram fails."""
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        engine = AlertEngine(config)
        engine.initialize()

        # Mock Telegram to fail
        mock_session = MockTelegramSession([{"status": 500, "body": {"ok": False, "description": "Internal error"}}])
        engine._channel_manager._channels[AlertChannel.TELEGRAM]._session = mock_session

        metrics = create_test_metrics(total_cost=50.0)
        alerts = engine.evaluate(metrics)

        results = await engine.deliver(alerts[0])

        # Telegram should fail
        assert results[AlertChannel.TELEGRAM].success is False
        # Stdout should succeed
        assert results[AlertChannel.STDOUT].success is True

    @pytest.mark.asyncio
    async def test_stdout_delivery_always_works(self, temp_dirs):
        """Test stdout channel always succeeds."""
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        engine = AlertEngine(config)
        engine.initialize()

        metrics = create_test_metrics(total_cost=50.0)
        alerts = engine.evaluate(metrics)

        # Deliver only to stdout
        alert = alerts[0]
        alert.metadata["channels"] = [AlertChannel.STDOUT]

        results = await engine.deliver(alert)

        assert AlertChannel.STDOUT in results
        assert results[AlertChannel.STDOUT].success is True

    @pytest.mark.asyncio
    async def test_evaluate_and_deliver_convenience(self, temp_dirs):
        """Test evaluate_and_deliver convenience method."""
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        engine = AlertEngine(config)
        engine.initialize()

        # Mock Telegram
        mock_session = MockTelegramSession([{"status": 200, "body": {"ok": True}}])
        engine._channel_manager._channels[AlertChannel.TELEGRAM]._session = mock_session

        metrics = create_test_metrics(total_cost=50.0)
        alerts = await engine.evaluate_and_deliver(metrics)

        assert len(alerts) == 1
        assert alerts[0].delivered is True
        assert alerts[0].delivery_results[AlertChannel.TELEGRAM].success is True


class TestAlertEngineClearCooldown:
    """Test manual cooldown clearing."""

    @pytest.fixture
    def engine(self, temp_dirs):
        config = AlertEngineConfig(
            rules_file=temp_dirs["rules_file"],
            settings_file=temp_dirs["settings_file"],
            state_file=temp_dirs["state_file"],
        )
        return AlertEngine(config)

    def test_clear_cooldown_allows_retrigger(self, engine):
        """Test that clearing cooldown allows rule to fire again."""
        engine.initialize()

        metrics = create_test_metrics(total_cost=150.0)

        # First trigger
        alerts1 = engine.evaluate(metrics)
        assert len(alerts1) == 1

        # Clear cooldown
        result = engine.clear_cooldown("daily_cost_breach")
        assert result is True

        # Should trigger again
        alerts2 = engine.evaluate(metrics)
        assert len(alerts2) == 1

    def test_clear_cooldown_unknown_rule_returns_false(self, engine):
        """Test clearing unknown rule returns False."""
        engine.initialize()
        result = engine.clear_cooldown("nonexistent_rule")
        assert result is False


class TestCreateDailyMetricsFromCosts:
    """Test helper function to bridge cost engine to alert engine."""

    def test_create_daily_metrics_from_costs(self):
        """Test conversion from ProjectCost dict to DailyMetrics."""
        project_costs = {
            "project-a": ProjectCost(
                project="project-a",
                total_cost=Decimal("10.0"),
                total_prompt_tokens=5000,
                total_completion_tokens=2500,
                total_tokens=7500,
                model_breakdown={"gpt-4": Decimal("7.0"), "gpt-3.5-turbo": Decimal("3.0")},
                record_count=10,
                period_start=datetime.utcnow(),
                period_end=datetime.utcnow(),
            ),
            "project-b": ProjectCost(
                project="project-b",
                total_cost=Decimal("5.0"),
                total_prompt_tokens=2000,
                total_completion_tokens=1000,
                total_tokens=3000,
                model_breakdown={"claude-3": Decimal("5.0")},
                record_count=5,
                period_start=datetime.utcnow(),
                period_end=datetime.utcnow(),
            ),
        }

        metrics = create_daily_metrics_from_costs(project_costs)

        assert metrics.total_cost == Decimal("15.0")
        assert metrics.total_prompt_tokens == 7000
        assert metrics.total_completion_tokens == 3500
        assert metrics.total_tokens == 10500
        assert metrics.project_costs["project-a"] == Decimal("10.0")
        assert metrics.project_costs["project-b"] == Decimal("5.0")
        assert metrics.model_costs["gpt-4"] == Decimal("7.0")
        assert metrics.model_costs["gpt-3.5-turbo"] == Decimal("3.0")
        assert metrics.model_costs["claude-3"] == Decimal("5.0")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])