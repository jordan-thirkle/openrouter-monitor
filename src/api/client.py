"""Async OpenRouter API client with retry and rate-limit handling."""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

import aiohttp


class OpenRouterError(Exception):
    """Base error for OpenRouter client failures."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenRouterAPIError(OpenRouterError):
    """Compatibility alias for API failures."""


class RateLimitError(OpenRouterError):
    """Raised after retrying a 429 response."""

    def __init__(self, message: str = "Rate limited", status_code: int = 429, retry_after: float | None = None) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


class ServerError(OpenRouterError):
    """Raised after retrying a 5xx response."""


class TimeoutError(OpenRouterError):
    """Raised after retrying a timeout."""


class RateLimiter:
    """Simple token buckets for requests-per-minute and requests-per-hour."""

    def __init__(self, rpm: int = 60, rph: int = 500) -> None:
        self.rpm = rpm
        self.rph = rph
        self._minute_tokens = float(rpm)
        self._hour_tokens = float(rph)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._last_refill = now
                self._minute_tokens = min(self.rpm, self._minute_tokens + elapsed * self.rpm / 60)
                self._hour_tokens = min(self.rph, self._hour_tokens + elapsed * self.rph / 3600)
                if self._minute_tokens >= 1 and self._hour_tokens >= 1:
                    self._minute_tokens -= 1
                    self._hour_tokens -= 1
                    return
                minute_wait = (1 - self._minute_tokens) * 60 / self.rpm
                hour_wait = (1 - self._hour_tokens) * 3600 / self.rph
                delay = max(0.001, max(minute_wait, hour_wait))
            await asyncio.sleep(delay)


@dataclass(frozen=True)
class UsageRecord:
    model: str
    project: str
    date_hour: datetime
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    request_id: Optional[str] = None


@dataclass(frozen=True)
class RawUsageRecord:
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
    id: str
    name: str
    context_length: int
    pricing: dict[str, float]

    @property
    def pricing_prompt(self) -> float:
        return self.pricing.get("prompt", 0.0)

    @property
    def pricing_completion(self) -> float:
        return self.pricing.get("completion", 0.0)


@dataclass(frozen=True)
class KeyInfo:
    label: str
    limit: Optional[float]
    usage: float
    is_free_tier: bool
    rate_limit: dict[str, Any] | None = None


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1", *, rpm: int = 60, rph: int = 500, max_attempts: int = 3, base_delay: float = 0.5, max_delay: float = 30.0, exponential_base: float = 2.0, timeout_seconds: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.timeout_seconds = timeout_seconds
        self.rate_limiter = RateLimiter(rpm, rph)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        return self._session

    async def __aenter__(self) -> "OpenRouterClient":
        self._session = await self._get_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            await self.rate_limiter.acquire()
            try:
                session = await self._get_session()
                request_result = session.request("GET", f"{self.base_url}{path}", params=params, timeout=self.timeout_seconds)
                if inspect.isawaitable(request_result):
                    request_result = await request_result
                async with request_result as response:
                    data = await response.json()
                    if response.status == 429:
                        retry_after = float(response.headers.get("Retry-After", self.base_delay * (self.exponential_base ** attempt)))
                        last = RateLimitError(data.get("error", {}).get("message", "Rate limited"), retry_after=retry_after)
                        if attempt + 1 < self.max_attempts:
                            await asyncio.sleep(min(retry_after, self.max_delay))
                            continue
                        raise last
                    if response.status >= 500:
                        last = ServerError(data.get("error", {}).get("message", "Server error"), response.status)
                        if attempt + 1 < self.max_attempts:
                            await asyncio.sleep(min(self.base_delay * (self.exponential_base ** attempt), self.max_delay))
                            continue
                        raise last
                    if response.status >= 400:
                        raise OpenRouterError(data.get("error", {}).get("message", "Request failed"), response.status)
                    return data
            except asyncio.TimeoutError as exc:
                last = exc
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(self.base_delay * (self.exponential_base ** attempt), self.max_delay))
                    continue
                raise TimeoutError("Request timed out") from exc
            except aiohttp.ClientError as exc:
                last = exc
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(self.base_delay * (self.exponential_base ** attempt), self.max_delay))
                    continue
                raise OpenRouterError(str(exc)) from exc
        raise OpenRouterError(str(last or "Request failed"))

    async def get_usage(self, start: datetime, end: datetime) -> List[RawUsageRecord]:
        data = await self._request("/generation", {"start_date": start.isoformat(), "end_date": end.isoformat()})
        records = []
        for item in data.get("data", []):
            ts = item.get("timestamp") or item.get("created_at")
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) else start
            records.append(RawUsageRecord(model=item.get("model", "unknown"), model_slug=item.get("model_slug", item.get("model", "unknown")), prompt_tokens=int(item.get("prompt_tokens", 0)), completion_tokens=int(item.get("completion_tokens", 0)), total_tokens=int(item.get("total_tokens", 0)), cost=float(item.get("cost", 0)), timestamp=timestamp, project=item.get("project"), user=item.get("user"), generation_id=item.get("generation_id")))
        return records

    async def get_models(self) -> List[ModelInfo]:
        data = await self._request("/models")
        return [ModelInfo(id=x["id"], name=x.get("name", x["id"]), context_length=int(x.get("context_length", 0)), pricing={k: float(v) for k, v in x.get("pricing", {}).items()}) for x in data.get("data", [])]

    async def get_key_info(self) -> KeyInfo:
        data = await self._request("/auth/key")
        x = data.get("data", data)
        return KeyInfo(label=x.get("label", ""), limit=x.get("limit"), usage=float(x.get("usage", 0)), is_free_tier=bool(x.get("is_free_tier", False)), rate_limit=x.get("rate_limit"))


def normalize_usage_records(raw_records: List[RawUsageRecord | UsageRecord]) -> List[UsageRecord]:
    from collections import defaultdict
    grouped: dict[tuple[str, str, datetime], dict[str, Any]] = defaultdict(lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "request_ids": []})
    for record in raw_records:
        if isinstance(record, UsageRecord):
            date_hour = record.date_hour.replace(minute=0, second=0, microsecond=0)
            project, request_id = record.project, record.request_id
        else:
            date_hour = record.timestamp.replace(minute=0, second=0, microsecond=0)
            project, request_id = record.project or "default", record.generation_id
        key = (record.model, project, date_hour)
        grouped[key]["prompt_tokens"] += record.prompt_tokens
        grouped[key]["completion_tokens"] += record.completion_tokens
        grouped[key]["total_tokens"] += record.total_tokens
        grouped[key]["cost"] += record.cost
        if request_id:
            grouped[key]["request_ids"].append(request_id)
    return [UsageRecord(model=m, project=p, date_hour=dt, prompt_tokens=v["prompt_tokens"], completion_tokens=v["completion_tokens"], total_tokens=v["total_tokens"], cost=v["cost"], request_id=",".join(v["request_ids"]) or None) for (m, p, dt), v in grouped.items()]

__all__ = ["OpenRouterClient", "OpenRouterError", "OpenRouterAPIError", "RateLimitError", "ServerError", "TimeoutError", "RateLimiter", "UsageRecord", "RawUsageRecord", "ModelInfo", "KeyInfo", "normalize_usage_records"]

# Backwards-compatible test alias.
asyncio_timeout_error = asyncio.TimeoutError
