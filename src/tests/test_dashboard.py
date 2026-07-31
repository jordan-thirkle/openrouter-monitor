"""Critic tests for Dashboard API."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import app, build_snapshot, query_usage, query_costs
from src.dashboard.models import (
    Granularity,
    GroupBy,
    UsageRecord,
    UsageResponse,
    CostBreakdown,
    CostsResponse,
)
from src.ingestion.database import init_db, upsert_usage_records
from src.api.client import UsageRecord as APIUsageRecord


class TestDashboardAPI:
    """Test dashboard REST endpoints."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Initialize test database before each test."""
        # Use temp database
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        # Monkey-patch the db path
        import src.ingestion.database as db_module
        import src.dashboard.api as dashboard_api
        original_get_db_path = db_module.get_db_path
        original_get_cursor_path = db_module.get_cursor_path

        def mock_get_db_path():
            return db_path

        def mock_get_cursor_path():
            return db_path.parent / "cursor.txt"

        db_module.get_db_path = mock_get_db_path
        db_module.get_cursor_path = mock_get_cursor_path
        # Also patch the dashboard API's get_db_path
        dashboard_api.get_db_path = mock_get_db_path

        init_db()

        yield

        # Restore
        db_module.get_db_path = original_get_db_path
        db_module.get_cursor_path = original_get_cursor_path
        dashboard_api.get_db_path = original_get_db_path

        if db_path.exists():
            db_path.unlink()
        cursor_path = db_path.parent / "cursor.txt"
        if cursor_path.exists():
            cursor_path.unlink()

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    @pytest.fixture
    def sample_records(self):
        """Create sample usage records."""
        base_time = datetime(2024, 1, 15, 14, 0, 0)
        records = []

        models = ["gpt-4", "gpt-3.5-turbo", "claude-3-opus"]
        projects = ["project-a", "project-b", "project-c"]

        for i in range(100):
            model = models[i % len(models)]
            project = projects[i % len(projects)]
            timestamp = base_time + timedelta(hours=i)

            records.append(APIUsageRecord(
                model=model,
                project=project,
                date_hour=timestamp.replace(minute=0, second=0, microsecond=0),
                prompt_tokens=1000 + (i % 5000),
                completion_tokens=500 + (i % 2000),
                total_tokens=1500 + (i % 7000),
                cost=0.01 + (i % 100) * 0.001,
                request_id=f"req-{i:06d}",
            ))

        return records

    def test_health_endpoint(self, client, sample_records):
        """Test /api/health endpoint."""
        # Insert some data first
        upsert_usage_records(sample_records[:10])

        response = client.get("/api/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] in ("healthy", "degraded")
        assert data["version"] == "1.0.0"
        assert "database_connected" in data
        assert "active_websockets" in data

    def test_usage_endpoint_basic(self, client, sample_records):
        """Test /api/usage returns records."""
        upsert_usage_records(sample_records)

        response = client.get("/api/usage")
        assert response.status_code == 200

        data = response.json()
        assert "records" in data
        assert "total_records" in data
        assert "from_date" in data
        assert "to_date" in data
        assert "granularity" in data
        assert len(data["records"]) > 0
        assert data["total_records"] == 100

    def test_usage_endpoint_filter_by_model(self, client, sample_records):
        """Test /api/usage filters by model."""
        upsert_usage_records(sample_records)

        response = client.get("/api/usage?model=gpt-4")
        assert response.status_code == 200

        data = response.json()
        assert all(r["model"] == "gpt-4" for r in data["records"])

    def test_usage_endpoint_filter_by_project(self, client, sample_records):
        """Test /api/usage filters by project."""
        upsert_usage_records(sample_records)

        response = client.get("/api/usage?project=project-a")
        assert response.status_code == 200

        data = response.json()
        assert all(r["project"] == "project-a" for r in data["records"])

    def test_usage_endpoint_pagination(self, client, sample_records):
        """Test /api/usage pagination."""
        upsert_usage_records(sample_records)

        # First page
        response = client.get("/api/usage?limit=10&offset=0")
        assert response.status_code == 200
        data1 = response.json()
        assert len(data1["records"]) == 10

        # Second page
        response = client.get("/api/usage?limit=10&offset=10")
        assert response.status_code == 200
        data2 = response.json()
        assert len(data2["records"]) == 10

        # Should be different records
        assert data1["records"][0]["request_id"] != data2["records"][0]["request_id"]

    def test_usage_endpoint_date_range(self, client, sample_records):
        """Test /api/usage filters by date range."""
        upsert_usage_records(sample_records)

        from_date = datetime(2024, 1, 16, 0, 0, 0)
        to_date = datetime(2024, 1, 17, 0, 0, 0)

        response = client.get(
            f"/api/usage?from_date={from_date.isoformat()}&to_date={to_date.isoformat()}"
        )
        assert response.status_code == 200

        data = response.json()
        for record in data["records"]:
            record_time = datetime.fromisoformat(record["date_hour"])
            assert from_date <= record_time <= to_date

    def test_costs_endpoint_group_by_model(self, client, sample_records):
        """Test /api/costs grouped by model."""
        upsert_usage_records(sample_records)

        response = client.get("/api/costs?group_by=model")
        assert response.status_code == 200

        data = response.json()
        assert "breakdowns" in data
        assert "group_by" in data
        assert "total_cost" in data
        assert data["group_by"] == "model"
        assert len(data["breakdowns"]) == 3  # 3 models
        assert data["total_cost"] > 0

    def test_costs_endpoint_group_by_project(self, client, sample_records):
        """Test /api/costs grouped by project."""
        upsert_usage_records(sample_records)

        response = client.get("/api/costs?group_by=project")
        assert response.status_code == 200

        data = response.json()
        assert data["group_by"] == "project"
        assert len(data["breakdowns"]) == 3  # 3 projects

    def test_costs_endpoint_group_by_day(self, client, sample_records):
        """Test /api/costs grouped by day."""
        upsert_usage_records(sample_records)

        response = client.get("/api/costs?group_by=day")
        assert response.status_code == 200

        data = response.json()
        assert data["group_by"] == "day"

    def test_alerts_endpoint(self, client):
        """Test /api/alerts endpoint."""
        response = client.get("/api/alerts")
        assert response.status_code == 200

        data = response.json()
        assert "alerts" in data
        assert "total" in data
        assert "unacknowledged" in data

    def test_anomalies_endpoint(self, client):
        """Test /api/anomalies endpoint."""
        response = client.get("/api/anomalies")
        assert response.status_code == 200

        data = response.json()
        assert "anomalies" in data
        assert "total" in data
        assert "by_severity" in data


class TestDashboardQueryFunctions:
    """Test internal query functions directly."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Initialize test database."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        import src.ingestion.database as db_module
        import src.dashboard.api as dashboard_api
        original_get_db_path = db_module.get_db_path
        original_get_cursor_path = db_module.get_cursor_path

        def mock_get_db_path():
            return db_path

        def mock_get_cursor_path():
            return db_path.parent / "cursor.txt"

        db_module.get_db_path = mock_get_db_path
        db_module.get_cursor_path = mock_get_cursor_path
        # Also patch the dashboard API's get_db_path
        dashboard_api.get_db_path = mock_get_db_path

        init_db()

        yield

        db_module.get_db_path = original_get_db_path
        db_module.get_cursor_path = original_get_cursor_path
        dashboard_api.get_db_path = original_get_db_path

        if db_path.exists():
            db_path.unlink()

    def test_query_usage_returns_correct_format(self):
        """Test query_usage returns proper structure."""
        base_time = datetime(2024, 1, 15, 14, 0, 0)
        records = [
            APIUsageRecord(
                model="gpt-4",
                project="project-a",
                date_hour=base_time,
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost=0.01,
                request_id="req-1",
            ),
        ]
        upsert_usage_records(records)

        results, total = query_usage()

        assert total == 1
        assert len(results) == 1
        assert isinstance(results[0], UsageRecord)
        assert results[0].model == "gpt-4"
        assert results[0].cost == 0.01

    def test_query_costs_by_model(self):
        """Test query_costs groups correctly by model."""
        base_time = datetime(2024, 1, 15, 14, 0, 0)
        records = [
            APIUsageRecord(
                model="gpt-4",
                project="project-a",
                date_hour=base_time,
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost=0.03,
                request_id="req-1",
            ),
            APIUsageRecord(
                model="gpt-3.5-turbo",
                project="project-a",
                date_hour=base_time + timedelta(hours=1),
                prompt_tokens=2000,
                completion_tokens=1000,
                total_tokens=3000,
                cost=0.01,
                request_id="req-2",
            ),
        ]
        upsert_usage_records(records)

        response = query_costs(group_by=GroupBy.MODEL)

        assert isinstance(response, CostsResponse)
        assert response.group_by == GroupBy.MODEL
        assert len(response.breakdowns) == 2

        # Check gpt-4 cost
        gpt4 = next(b for b in response.breakdowns if b.group == "gpt-4")
        assert gpt4.total_cost == 0.03

        gpt35 = next(b for b in response.breakdowns if b.group == "gpt-3.5-turbo")
        assert gpt35.total_cost == 0.01


class TestDashboardWebSocket:
    """Test WebSocket functionality."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)

    def test_websocket_connection(self, client):
        """Test WebSocket can connect and receive snapshot."""
        with client.websocket_connect("/ws/live") as ws:
            # Should receive initial snapshot
            data = ws.receive_text()
            message = json.loads(data)

            assert message["type"] == "snapshot"
            assert "payload" in message
            assert "timestamp" in message


class TestDashboardPerformance:
    """Performance tests for dashboard."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Initialize test database with larger dataset."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        import src.ingestion.database as db_module
        import src.dashboard.api as dashboard_api
        original_get_db_path = db_module.get_db_path
        original_get_cursor_path = db_module.get_cursor_path

        def mock_get_db_path():
            return db_path

        def mock_get_cursor_path():
            return db_path.parent / "cursor.txt"

        db_module.get_db_path = mock_get_db_path
        db_module.get_cursor_path = mock_get_cursor_path
        # Also patch the dashboard API's get_db_path
        dashboard_api.get_db_path = mock_get_db_path

        init_db()

        # Insert 10k records for performance testing
        base_time = datetime(2024, 1, 1, 0, 0, 0)
        records = []
        for i in range(10000):
            records.append(APIUsageRecord(
                model=f"model-{i % 10}",
                project=f"project-{i % 5}",
                date_hour=base_time + timedelta(hours=i),
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost=0.01,
                request_id=f"perf-{i}",
            ))

        upsert_usage_records(records)

        yield

        db_module.get_db_path = original_get_db_path
        db_module.get_cursor_path = original_get_cursor_path
        dashboard_api.get_db_path = original_get_db_path

        if db_path.exists():
            db_path.unlink()

    @pytest.fixture
    def client(self, setup_db):
        """Create test client."""
        return TestClient(app)
        """Test usage query completes within reasonable time."""
        import time

        start = time.time()
        response = client.get("/api/usage?limit=1000")
        elapsed = time.time() - start

        assert response.status_code == 200
        assert elapsed < 1.0  # Should complete within 1 second

    def test_costs_query_performance(self, client):
        """Test costs query completes within reasonable time."""
        import time

        start = time.time()
        response = client.get("/api/costs?group_by=model")
        elapsed = time.time() - start

        assert response.status_code == 200
        assert elapsed < 1.0

    def test_build_snapshot_performance(self):
        """Test build_snapshot completes within reasonable time."""
        import time

        start = time.time()
        snapshot = build_snapshot()
        elapsed = time.time() - start

        assert isinstance(snapshot.total_cost_24h, float)
        assert isinstance(snapshot.total_tokens_24h, int)
        assert elapsed < 0.5  # Should be fast


if __name__ == "__main__":
    pytest.main([__file__, "-v"])