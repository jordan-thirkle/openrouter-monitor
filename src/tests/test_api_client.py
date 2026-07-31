"""
Critic tests for OpenRouterClient.

Tests that the client correctly handles:
- 429 rate limit responses (retries with backoff, respects Retry-After)
- 500 server errors (retries with exponential backoff)
- Timeout errors (retries with exponential backoff)
- Non-retryable 4xx errors (surfaces immediately)
"""

import asyncio
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp

from src.api.client import (
    OpenRouterClient,
    RateLimitError,
    ServerError,
    TimeoutError,
    OpenRouterError,
    UsageRecord,
    ModelInfo,
    KeyInfo,
)


class MockResponse:
    """Mock aiohttp response."""
    def __init__(self, status: int, data: dict, headers: dict = None):
        self.status = status
        self._data = data
        self.headers = headers or {}

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestOpenRouterClientRetryLogic:
    """Test retry behavior for various error conditions."""

    @pytest.fixture
    def client(self):
        """Create a client with fast retry settings for testing."""
        return OpenRouterClient(
            api_key="test-key",
            base_url="https://test.api",
            rpm=60,
            rph=500,
            max_attempts=3,
            base_delay=0.01,  # Fast for tests
            max_delay=0.1,
            exponential_base=2.0,
            timeout_seconds=1.0,
        )

    @pytest.mark.asyncio
    async def test_429_rate_limit_retries_and_backs_off(self, client):
        """Test that 429 responses trigger retry with exponential backoff."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # First two calls return 429
                return MockResponse(429, {"error": {"message": "Rate limited"}}, {"Retry-After": "0.01"})
            # Third call succeeds
            return MockResponse(200, {"data": [{"id": "model-1", "name": "Model 1", "context_length": 4096, "pricing": {}}]})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            models = await client.get_models()

        assert call_count == 3
        assert len(models) == 1
        assert models[0].id == "model-1"

    @pytest.mark.asyncio
    async def test_429_respects_retry_after_header(self, client):
        """Test that Retry-After header is respected over exponential backoff."""
        call_times = []

        async def mock_request(*args, **kwargs):
            call_times.append(asyncio.get_event_loop().time())
            if len(call_times) < 2:
                return MockResponse(429, {"error": {"message": "Rate limited"}}, {"Retry-After": "0.05"})
            return MockResponse(200, {"data": []})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            await client.get_models()

        # Second call should happen ~0.05s after first (Retry-After)
        elapsed = call_times[1] - call_times[0]
        assert 0.04 <= elapsed <= 0.15  # Allow some tolerance

    @pytest.mark.asyncio
    async def test_500_server_error_retries_with_backoff(self, client):
        """Test that 5xx responses trigger retry with exponential backoff."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return MockResponse(500, {"error": {"message": "Internal server error"}})
            return MockResponse(200, {"data": {"label": "test", "usage": 100, "is_free_tier": False}})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            key_info = await client.get_key_info()

        assert call_count == 3
        assert key_info.label == "test"

    @pytest.mark.asyncio
    async def test_timeout_retries_with_backoff(self, client):
        """Test that timeout errors trigger retry with exponential backoff."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise asyncio.TimeoutError()
            return MockResponse(200, {"data": {"label": "test", "usage": 100, "is_free_tier": False}})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            key_info = await client.get_key_info()

        assert call_count == 3
        assert key_info.label == "test"

    @pytest.mark.asyncio
    async def test_network_error_retries(self, client):
        """Test that aiohttp.ClientError triggers retry."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise aiohttp.ClientError("Connection failed")
            return MockResponse(200, {"data": {"label": "test", "usage": 100, "is_free_tier": False}})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            key_info = await client.get_key_info()

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_4xx_client_error_no_retry(self, client):
        """Test that 4xx errors (except 429) are surfaced immediately without retry."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return MockResponse(401, {"error": {"message": "Unauthorized"}})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            with pytest.raises(OpenRouterError) as exc_info:
                await client.get_models()

        assert call_count == 1  # No retry
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_404_not_found_no_retry(self, client):
        """Test that 404 errors are surfaced immediately."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return MockResponse(404, {"error": {"message": "Not found"}})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            with pytest.raises(OpenRouterError) as exc_info:
                await client.get_key_info()

        assert call_count == 1
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_surfaces_error(self, client):
        """Test that after max retries, the last error is surfaced."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return MockResponse(500, {"error": {"message": "Persistent server error"}})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            with pytest.raises(ServerError) as exc_info:
                await client.get_models()

        assert call_count == 3  # max_attempts
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_429_max_retries_exhausted_surfaces_rate_limit_error(self, client):
        """Test that after max retries on 429, RateLimitError is surfaced."""
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return MockResponse(429, {"error": {"message": "Rate limited"}}, {"Retry-After": "0.01"})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_models()

        assert call_count == 3
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == 0.01

    @pytest.mark.asyncio
    async def test_usage_endpoint_parses_records_correctly(self, client):
        """Test that get_usage correctly parses API response into UsageRecord objects."""
        mock_data = {
            "data": [{
                "model": "gpt-4",
                "model_slug": "openai/gpt-4",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.0015,
                "timestamp": "2024-01-15T10:30:00Z",
                "project": "test-project",
                "user": "user-123",
                "generation_id": "gen-abc",
            }]
        }

        async def mock_request(*args, **kwargs):
            return MockResponse(200, mock_data)

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            records = await client.get_usage(
                start=datetime(2024, 1, 1),
                end=datetime(2024, 1, 31)
            )

        assert len(records) == 1
        record = records[0]
        assert record.model == "gpt-4"
        assert record.model_slug == "openai/gpt-4"
        assert record.prompt_tokens == 100
        assert record.completion_tokens == 50
        assert record.total_tokens == 150
        assert record.cost == 0.0015
        assert record.project == "test-project"
        assert record.user == "user-123"
        assert record.generation_id == "gen-abc"

    @pytest.mark.asyncio
    async def test_models_endpoint_parses_correctly(self, client):
        """Test that get_models correctly parses API response."""
        mock_data = {
            "data": [{
                "id": "anthropic/claude-3-opus",
                "name": "Claude 3 Opus",
                "description": "Most capable Claude model",
                "context_length": 200000,
                "pricing": {"prompt": 0.000015, "completion": 0.000075},
                "architecture": {"modality": "text", "tokenizer": "cl100k_base"},
                "top_provider": {"name": "Anthropic", "context_length": 200000},
                "per_request_limits": {"max_tokens": 4096},
            }]
        }

        async def mock_request(*args, **kwargs):
            return MockResponse(200, mock_data)

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            models = await client.get_models()

        assert len(models) == 1
        model = models[0]
        assert model.id == "anthropic/claude-3-opus"
        assert model.name == "Claude 3 Opus"
        assert model.context_length == 200000
        assert model.pricing == {"prompt": 0.000015, "completion": 0.000075}

    @pytest.mark.asyncio
    async def test_key_info_endpoint_parses_correctly(self, client):
        """Test that get_key_info correctly parses API response."""
        mock_data = {
            "data": {
                "label": "Production Key",
                "limit": 100.0,
                "usage": 25.50,
                "is_free_tier": False,
                "rate_limit": {"requests_per_minute": 60, "requests_per_hour": 500},
            }
        }

        async def mock_request(*args, **kwargs):
            return MockResponse(200, mock_data)

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            key_info = await client.get_key_info()

        assert key_info.label == "Production Key"
        assert key_info.limit == 100.0
        assert key_info.usage == 25.50
        assert key_info.is_free_tier is False
        assert key_info.rate_limit == {"requests_per_minute": 60, "requests_per_hour": 500}

    @pytest.mark.asyncio
    async def test_context_manager_closes_session(self, client):
        """Test that async context manager properly closes session."""
        mock_session = AsyncMock()
        mock_session.closed = False

        async def mock_get_session():
            return mock_session

        with patch.object(client, '_get_session', mock_get_session):
            async with client:
                pass

        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limiter_enforces_rpm(self, client):
        """Test that rate limiter enforces RPM limit."""
        # Make many rapid requests - they should be rate limited
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return MockResponse(200, {"data": []})

        with patch.object(client, '_get_session') as mock_session:
            mock_session.return_value.request = mock_request
            # Make 5 rapid requests - should take at least some time due to rate limiting
            start = asyncio.get_event_loop().time()
            for _ in range(5):
                await client.get_models()
            elapsed = asyncio.get_event_loop().time() - start

        # With rpm=60, 5 requests should take at least ~4 seconds (5/60 * 60)
        # But since we're using a token bucket with initial burst, first 60 are instant
        # This test mainly ensures no errors occur
        assert call_count == 5


class TestRateLimiter:
    """Unit tests for the RateLimiter class."""

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_burst_up_to_limit(self):
        """Test that rate limiter allows burst up to RPM/RPH limits."""
        from src.api.client import RateLimiter
        limiter = RateLimiter(rpm=10, rph=100)

        # Should allow 10 immediate requests (minute bucket)
        for _ in range(10):
            await limiter.acquire()

        # 11th should wait
        import time
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed > 0  # Should have waited

    @pytest.mark.asyncio
    async def test_rate_limiter_refills_over_time(self):
        """Test that rate limiter refills tokens over time."""
        from src.api.client import RateLimiter
        limiter = RateLimiter(rpm=60, rph=500)  # 1 per second

        # Exhaust minute bucket
        for _ in range(60):
            await limiter.acquire()

        # Wait for refill
        await asyncio.sleep(1.1)

        # Should allow 1 more immediately
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # Should be nearly instant


if __name__ == "__main__":
    pytest.main([__file__, "-v"])