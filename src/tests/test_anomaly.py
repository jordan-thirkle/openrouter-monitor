"""Critic tests for AnomalyDetector."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pytest

from src.anomaly.detector import (
    Anomaly,
    AnomalyDetector,
    AnomalyScore,
    DailyMetrics,
    TimeSeries,
)


class TestAnomalyDetectorZScore:
    """Test Z-score anomaly detection."""

    def test_zscore_detects_spike(self):
        """Test that Z-score detects a clear spike in data."""
        detector = AnomalyDetector(zscore_threshold=2.0)

        # Normal data around 10.0
        timestamps = [datetime.utcnow() - timedelta(hours=i) for i in range(20, 0, -1)]
        values = [10.0] * 19 + [50.0]  # Spike at the end

        series = TimeSeries(
            timestamps=timestamps,
            values=values,
            metric_name="cost",
            model="test-model",
        )

        scores = detector._zscore_detect(series)

        # Last point should be anomalous
        assert len(scores) == 20
        last_score = scores[-1]
        assert last_score.is_anomaly is True
        assert last_score.score > 0.5
        assert last_score.method == "zscore"

    def test_zscore_no_false_positive_on_normal(self):
        """Test that Z-score doesn't flag normal variation."""
        detector = AnomalyDetector(zscore_threshold=3.0)

        # Normal data with small variation
        timestamps = [datetime.utcnow() - timedelta(hours=i) for i in range(30, 0, -1)]
        values = [10.0 + (i % 3) * 0.1 for i in range(30)]  # Small variation 10.0-10.2

        series = TimeSeries(
            timestamps=timestamps,
            values=values,
            metric_name="cost",
            model="test-model",
        )

        scores = detector._zscore_detect(series)

        # None should be anomalous (zscore < 3)
        anomalous = [s for s in scores if s.is_anomaly]
        assert len(anomalous) == 0


class TestAnomalyDetectorRateOfChange:
    """Test rate-of-change anomaly detection."""

    def test_rate_of_change_detects_sudden_jump(self):
        """Test that rate-of-change detects sudden jump."""
        detector = AnomalyDetector(rate_of_change_threshold=1.0)  # 100% change

        timestamps = [datetime.utcnow() - timedelta(hours=i) for i in range(10, 0, -1)]
        values = [10.0] * 8 + [10.0, 30.0]  # 3x jump at the end

        series = TimeSeries(
            timestamps=timestamps,
            values=values,
            metric_name="cost",
            model="test-model",
        )

        scores = detector._rate_of_change_detect(series)

        # Last point should be anomalous (change_ratio = 2.0 > 1.0)
        assert len(scores) == 9  # N-1 scores
        last_score = scores[-1]
        assert last_score.is_anomaly is True
        assert last_score.method == "rate_of_change"
        assert last_score.metadata["change_ratio"] > 1.0

    def test_rate_of_change_ignores_gradual_change(self):
        """Test that gradual change doesn't trigger."""
        detector = AnomalyDetector(rate_of_change_threshold=1.0)

        timestamps = [datetime.utcnow() - timedelta(hours=i) for i in range(10, 0, -1)]
        values = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5]  # 5% per step

        series = TimeSeries(
            timestamps=timestamps,
            values=values,
            metric_name="cost",
            model="test-model",
        )

        scores = detector._rate_of_change_detect(series)

        # No single step should exceed 100% change
        anomalous = [s for s in scores if s.is_anomaly]
        assert len(anomalous) == 0


class TestAnomalyDetectorIntegration:
    """Integration tests for AnomalyDetector."""

    @pytest.fixture
    def detector(self):
        """Create detector with event queue."""
        queue = asyncio.Queue()
        return AnomalyDetector(event_queue=queue, min_training_days=5)

    @pytest.fixture
    def training_data(self):
        """Create 30 days of normal training data."""
        data = []
        base_date = datetime.utcnow() - timedelta(days=30)

        for day in range(30):
            date = base_date + timedelta(days=day)
            data.append(DailyMetrics(
                date=date.replace(hour=0, minute=0, second=0, microsecond=0),
                total_cost=10.0 + (day % 7) * 0.5,  # Small weekly pattern
                total_prompt_tokens=50000,
                total_completion_tokens=25000,
                total_tokens=75000,
                project_costs={"project-a": 6.0, "project-b": 4.0},
                model_costs={"gpt-4": 7.0, "gpt-3.5-turbo": 3.0},
                hourly_costs={h: 0.4 for h in range(24)},
                hourly_tokens={h: 3000 for h in range(24)},
            ))
        return data

    def test_insufficient_training_returns_empty(self, detector):
        """Test that detector returns empty list with insufficient training."""
        metrics = DailyMetrics(
            date=datetime.utcnow(),
            total_cost=100.0,  # Spike!
            total_prompt_tokens=50000,
            total_completion_tokens=25000,
            total_tokens=75000,
            project_costs={"project-a": 100.0},
            model_costs={"gpt-4": 100.0},
            hourly_costs={},
            hourly_tokens={},
        )

        anomalies = detector.detect(metrics)
        assert anomalies == []

    def test_detects_spike_after_training(self, detector, training_data):
        """Test that detector finds spike after sufficient training."""
        for m in training_data:
            detector.add_training_data(m)

        assert detector.has_sufficient_training() is True

        # Now test with a spike day - include ALL models and projects from training so spike is detectable
        spike_metrics = DailyMetrics(
            date=datetime.utcnow(),
            total_cost=100.0,  # 10x normal!
            total_prompt_tokens=500000,
            total_completion_tokens=250000,
            total_tokens=750000,
            project_costs={"project-a": 50.0, "project-b": 30.0, "project-c": 20.0},
            model_costs={"gpt-4": 50.0, "gpt-3.5-turbo": 30.0, "claude-3-opus": 20.0},
            hourly_costs={},
            hourly_tokens={},
        )

        anomalies = detector.detect(spike_metrics)

        # Should detect high/critical anomalies
        assert len(anomalies) > 0
        high_or_critical = [a for a in anomalies if a.severity in ("high", "critical")]
        assert len(high_or_critical) > 0

        # Verify anomaly properties
        for anomaly in high_or_critical:
            assert anomaly.score > 0.6
            assert anomaly.deviation_pct > 100  # 10x = 1000% deviation
            assert anomaly.method in ("zscore", "rate_of_change", "isolation_forest")


class TestAnomalyDetectorSeedAnomalies:
    """Test seeding anomalies for testing."""

    def test_seed_anomalies_creates_records(self):
        """Test that seed_anomalies inserts records into database."""
        detector = AnomalyDetector()

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        try:
            # Create minimal schema
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE usage_records (
                    model TEXT NOT NULL,
                    project TEXT NOT NULL,
                    date_hour TEXT NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cost REAL,
                    request_id TEXT,
                    PRIMARY KEY (model, project, date_hour)
                )
            """)
            # Add some baseline data
            base_date = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
            conn.execute("""
                INSERT INTO usage_records VALUES
                ('gpt-4', 'project-a', ?, 1000, 500, 1500, 0.01, 'baseline')
            """, (base_date.strftime("%Y-%m-%d %H:00:00"),))
            conn.commit()
            conn.close()

            # Seed anomalies
            count = detector.seed_anomalies(db_path, days=7)

            # Verify records were inserted
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute("SELECT COUNT(*) FROM usage_records WHERE request_id LIKE 'seeded_anomaly_%'")
            seeded_count = cursor.fetchone()[0]
            conn.close()

            assert seeded_count > 0
            assert seeded_count == count

        finally:
            if db_path.exists():
                db_path.unlink()


class TestAnomalyDetectorCombined:
    """Test combined scoring methods."""

    def test_score_combines_all_methods(self):
        """Test that score() returns max of all methods."""
        detector = AnomalyDetector(zscore_threshold=2.0, rate_of_change_threshold=1.0)

        timestamps = [datetime.utcnow() - timedelta(hours=i) for i in range(20, 0, -1)]
        # Normal then spike
        values = [10.0] * 18 + [10.0, 100.0]

        series = TimeSeries(
            timestamps=timestamps,
            values=values,
            metric_name="cost",
            model="test-model",
        )

        scores = detector.score(series)

        # Should have scores for all points
        assert len(scores) == 20

        # Last point should have high score from both methods
        last_score = scores[-1]
        assert last_score.is_anomaly is True
        assert last_score.score > 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])