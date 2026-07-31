"""Delivery channels for alerts: Telegram and stdout."""

from __future__ import annotations

import asyncio
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp

from src.alerts.models import Alert, AlertChannel, DeliveryResult, AlertConfig

logger = logging.getLogger(__name__)


class AlertChannelBase(ABC):
    """Abstract base class for alert delivery channels."""

    @property
    @abstractmethod
    def channel_type(self) -> AlertChannel:
        """Return the channel type."""
        pass

    @abstractmethod
    async def deliver(self, alert: Alert, config: AlertConfig) -> DeliveryResult:
        """Deliver an alert through this channel."""
        pass


class StdoutChannel(AlertChannelBase):
    """Stdout delivery channel (fallback)."""

    @property
    def channel_type(self) -> AlertChannel:
        return AlertChannel.STDOUT

    async def deliver(self, alert: Alert, config: AlertConfig) -> DeliveryResult:
        """Print alert to stdout with formatting."""
        try:
            severity_markers = {
                "P1": "🔴 CRITICAL",
                "P2": "🟠 HIGH",
                "P3": "🟡 MEDIUM",
                "P4": "🔵 LOW",
            }
            marker = severity_markers.get(alert.severity.value, alert.severity.value)

            lines = [
                f"\n{'=' * 60}",
                f"ALERT TRIGGERED: {marker}",
                f"{'=' * 60}",
                f"Rule:       {alert.rule_name}",
                f"Description: {alert.rule_description}",
                f"Metric:     {alert.metric.value}",
                f"Condition:  {alert.condition.value} {alert.threshold}",
                f"Actual:     {alert.actual_value}",
                f"Severity:   {alert.severity.value}",
                f"Time:       {alert.timestamp.isoformat()}",
                f"{'=' * 60}\n",
            ]

            message = "\n".join(lines)
            sys.stdout.write(message)
            sys.stdout.flush()

            return DeliveryResult(
                channel=AlertChannel.STDOUT,
                success=True,
                message="Alert printed to stdout",
                timestamp=datetime.utcnow(),
            )
        except Exception as e:
            logger.exception("Failed to deliver alert to stdout")
            return DeliveryResult(
                channel=AlertChannel.STDOUT,
                success=False,
                message="Failed to print to stdout",
                timestamp=datetime.utcnow(),
                error=str(e),
            )


class TelegramChannel(AlertChannelBase):
    """Telegram delivery channel (primary)."""

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def channel_type(self) -> AlertChannel:
        return AlertChannel.TELEGRAM

    def _is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _format_message(self, alert: Alert, config: AlertConfig) -> str:
        """Format alert as HTML message for Telegram."""
        severity_emoji = {
            "P1": "🔴",
            "P2": "🟠",
            "P3": "🟡",
            "P4": "🔵",
        }
        emoji = severity_emoji.get(alert.severity.value, "⚪")

        lines = [
            f"{emoji} <b>OpenRouter Alert: {alert.severity.value}</b>",
            f"",
            f"<b>Rule:</b> {alert.rule_name}",
            f"<b>Description:</b> {alert.rule_description}",
            f"<b>Metric:</b> {alert.metric.value}",
            f"<b>Condition:</b> {alert.condition.value} {alert.threshold}",
            f"<b>Actual Value:</b> {alert.actual_value}",
            f"<b>Time:</b> {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]

        # Add metadata if present
        if alert.metadata:
            lines.append("")
            lines.append("<b>Details:</b>")
            for key, value in alert.metadata.items():
                lines.append(f"  • {key}: {value}")

        return "\n".join(lines)

    async def deliver(self, alert: Alert, config: AlertConfig) -> DeliveryResult:
        """Send alert via Telegram Bot API."""
        if not self._is_configured():
            return DeliveryResult(
                channel=AlertChannel.TELEGRAM,
                success=False,
                message="Telegram not configured (missing bot_token or chat_id)",
                timestamp=datetime.utcnow(),
                error="Not configured",
            )

        try:
            message = self._format_message(alert, config)
            session = await self._get_session()

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": config.telegram_parse_mode,
                "disable_web_page_preview": config.telegram_disable_web_page_preview,
            }

            async with session.post(url, json=payload) as response:
                data = await response.json()

                if response.status == 200 and data.get("ok"):
                    logger.info(f"Telegram alert delivered for rule: {alert.rule_name}")
                    return DeliveryResult(
                        channel=AlertChannel.TELEGRAM,
                        success=True,
                        message="Alert sent via Telegram",
                        timestamp=datetime.utcnow(),
                    )
                else:
                    error_msg = data.get("description", f"HTTP {response.status}")
                    logger.error(f"Telegram API error: {error_msg}")
                    return DeliveryResult(
                        channel=AlertChannel.TELEGRAM,
                        success=False,
                        message=f"Telegram API error: {error_msg}",
                        timestamp=datetime.utcnow(),
                        error=error_msg,
                    )

        except aiohttp.ClientError as e:
            logger.exception("Telegram delivery network error")
            return DeliveryResult(
                channel=AlertChannel.TELEGRAM,
                success=False,
                message="Network error delivering to Telegram",
                timestamp=datetime.utcnow(),
                error=str(e),
            )
        except Exception as e:
            logger.exception("Unexpected error delivering to Telegram")
            return DeliveryResult(
                channel=AlertChannel.TELEGRAM,
                success=False,
                message="Unexpected error",
                timestamp=datetime.utcnow(),
                error=str(e),
            )


class ChannelManager:
    """Manages multiple delivery channels with fallback logic."""

    def __init__(self, config: AlertConfig):
        self.config = config
        self._channels: dict[AlertChannel, AlertChannelBase] = {
            AlertChannel.STDOUT: StdoutChannel(),
            AlertChannel.TELEGRAM: TelegramChannel(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
            ),
        }

    async def deliver(self, alert: Alert) -> dict[AlertChannel, DeliveryResult]:
        """Deliver alert through all configured channels for its rule."""
        results = {}

        # Get channels for this alert (from rule or default)
        channels = alert.metadata.get("channels", self.config.default_channels)
        if isinstance(channels, list) and channels and isinstance(channels[0], str):
            channels = [AlertChannel(c) for c in channels]

        for channel_type in channels:
            channel = self._channels.get(channel_type)
            if channel is None:
                logger.warning(f"Unknown channel type: {channel_type}")
                results[channel_type] = DeliveryResult(
                    channel=channel_type,
                    success=False,
                    message=f"Unknown channel: {channel_type}",
                    timestamp=datetime.utcnow(),
                    error="Unknown channel",
                )
                continue

            try:
                result = await channel.deliver(alert, self.config)
                results[channel_type] = result

                # Log delivery result
                if result.success:
                    logger.info(f"Alert delivered via {channel_type.value}: {alert.rule_name}")
                else:
                    logger.warning(f"Alert delivery failed via {channel_type.value}: {result.error}")

            except Exception as e:
                logger.exception(f"Error delivering via {channel_type.value}")
                results[channel_type] = DeliveryResult(
                    channel=channel_type,
                    success=False,
                    message=f"Delivery error: {e}",
                    timestamp=datetime.utcnow(),
                    error=str(e),
                )

        return results

    async def close(self) -> None:
        """Close all channel connections."""
        telegram_channel = self._channels.get(AlertChannel.TELEGRAM)
        if isinstance(telegram_channel, TelegramChannel):
            await telegram_channel.close()