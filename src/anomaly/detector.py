"""Anomaly detection data models and detector implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.costs.models import ProjectCost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimeSeries:
    """Time series data for anomaly detection."""
    timestamps: List[datetime]
    values: List[float]
    metric_name: str
    model: str
    project: Optional[str] = None


@dataclass(frozen=True)
class AnomalyScore:
    """Anomaly score result for a single data point."""
    timestamp: datetime
    value: float
    score: float  # 0.0 to 1.0, higher = more anomalous
    method: str  # "zscore", "isolation_forest", "rate_of_change"
    threshold: float
    is_anomaly: bool
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Anomaly:
    """Detected anomaly with full context."""
    id: str
    model: str
    project: Optional[str]
    metric: str  # e.g., "cost", "tokens", "latency"
    severity: str  # "low", "medium", "high", "critical"
    score: float
    expected_value: float
    actual_value: float
    deviation_pct: float
    detected_at: datetime
    description: str
    method: str


@dataclass(frozen=True)
class AnomalyEvent:
    """Event emitted when anomaly is detected."""
    event_type: str = "anomaly.detected"
    anomaly: Optional[Anomaly] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class DailyMetrics:
    """Daily aggregated metrics for anomaly detection."""
    date: datetime
    total_cost: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    project_costs: Dict[str, float]
    model_costs: Dict[str, float]
    hourly_costs: Dict[int, float]  # hour -> cost
    hourly_tokens: Dict[int, int]   # hour -> tokens
    anomaly_scores: Dict[str, float] = field(default_factory=dict)
    cost_growth_rate: float = 0.0


class AnomalyDetector:
    """
    Anomaly detector for OpenRouter usage metrics.

    Methods:
    - Z-score: Statistical threshold (3 sigma)
    - Isolation Forest: ML-based outlier detection (optional sklearn)
    - Rate-of-change: Sudden spikes/drops detection

    Training window: 30 days minimum.
    Emits anomaly.detected events via asyncio.Queue.
    """

    def __init__(
        self,
        event_queue: Optional[asyncio.Queue] = None,
        zscore_threshold: float = 3.0,
        rate_of_change_threshold: float = 2.0,  # 2x change
        min_training_days: int = 30,
    ):
        self.event_queue = event_queue
        self.zscore_threshold = zscore_threshold
        self.rate_of_change_threshold = rate_of_change_threshold
        self.min_training_days = min_training_days
        self._training_data: List[DailyMetrics] = []
        self._isolation_forest = None
        self._sklearn_available = False
        self._try_import_sklearn()

    def _try_import_sklearn(self) -> None:
        """Try to import sklearn for Isolation Forest."""
        try:
            from sklearn.ensemble import IsolationForest
            self._sklearn_available = True
            self._IsolationForest = IsolationForest
            logger.info("scikit-learn available, Isolation Forest enabled")
        except ImportError:
            self._sklearn_available = False
            logger.warning("scikit-learn not available, Isolation Forest disabled (graceful fallback)")

    def _train_isolation_forest(self, data: List[float]) -> None:
        """Train Isolation Forest model if sklearn available."""
        if not self._sklearn_available or len(data) < 10:
            return
        try:
            # Reshape for sklearn
            X = [[v] for v in data]
            self._isolation_forest = self._IsolationForest(
                contamination=0.1,
                random_state=42,
                n_estimators=100,
            )
            self._isolation_forest.fit(X)
            logger.debug(f"Isolation Forest trained on {len(data)} samples")
        except Exception as e:
            logger.warning(f"Failed to train Isolation Forest: {e}")
            self._isolation_forest = None

    def add_training_data(self, metrics: DailyMetrics) -> None:
        """Add daily metrics to training data."""
        self._training_data.append(metrics)
        # Keep only last 90 days
        cutoff = datetime.utcnow() - timedelta(days=90)
        self._training_data = [m for m in self._training_data if m.date >= cutoff]

    def has_sufficient_training(self) -> bool:
        """Check if we have enough training data."""
        return len(self._training_data) >= self.min_training_days

    def _zscore_detect(self, series: TimeSeries) -> List[AnomalyScore]:
        """Detect anomalies using Z-score method."""
        if len(series.values) < 2:
            return []

        mean = statistics.mean(series.values)
        stdev = statistics.stdev(series.values) if len(series.values) > 1 else 0.0

        if stdev == 0:
            return []

        scores = []
        for ts, val in zip(series.timestamps, series.values):
            zscore = abs(val - mean) / stdev
            normalized_score = min(1.0, zscore / self.zscore_threshold)
            scores.append(AnomalyScore(
                timestamp=ts,
                value=val,
                score=normalized_score,
                method="zscore",
                threshold=self.zscore_threshold,
                is_anomaly=zscore > self.zscore_threshold,
                metadata={"mean": mean, "stdev": stdev, "zscore": zscore},
            ))
        return scores

    def _rate_of_change_detect(self, series: TimeSeries) -> List[AnomalyScore]:
        """Detect anomalies using rate-of-change method."""
        if len(series.values) < 2:
            return []

        scores = []
        for i in range(1, len(series.values)):
            prev = series.values[i - 1]
            curr = series.values[i]
            if prev == 0:
                change_ratio = float('inf') if curr > 0 else 1.0
            else:
                change_ratio = abs(curr - prev) / abs(prev)

            normalized_score = min(1.0, change_ratio / self.rate_of_change_threshold)
            scores.append(AnomalyScore(
                timestamp=series.timestamps[i],
                value=curr,
                score=normalized_score,
                method="rate_of_change",
                threshold=self.rate_of_change_threshold,
                is_anomaly=change_ratio > self.rate_of_change_threshold,
                metadata={"prev_value": prev, "change_ratio": change_ratio},
            ))
        return scores

    def _isolation_forest_detect(self, series: TimeSeries) -> List[AnomalyScore]:
        """Detect anomalies using Isolation Forest."""
        if not self._sklearn_available or self._isolation_forest is None or len(series.values) < 10:
            return []

        try:
            X = [[v] for v in series.values]
            predictions = self._isolation_forest.predict(X)
            scores_raw = self._isolation_forest.decision_function(X)

            # Normalize scores: -1 = anomaly, 1 = normal
            # Convert to 0-1 where higher = more anomalous
            normalized_scores = [(1 - s) / 2 for s in scores_raw]

            result = []
            for ts, val, pred, score in zip(series.timestamps, series.values, predictions, normalized_scores):
                result.append(AnomalyScore(
                    timestamp=ts,
                    value=val,
                    score=max(0.0, min(1.0, score)),
                    method="isolation_forest",
                    threshold=0.5,
                    is_anomaly=pred == -1,
                    metadata={"raw_score": score, "prediction": int(pred)},
                ))
            return result
        except Exception as e:
            logger.warning(f"Isolation Forest detection failed: {e}")
            return []

    def score(self, series: TimeSeries) -> List[AnomalyScore]:
        """
        Score a time series using all available methods.

        Returns combined anomaly scores (max of all methods per point).
        """
        all_scores: Dict[datetime, AnomalyScore] = {}

        # Z-score
        for s in self._zscore_detect(series):
            if s.timestamp not in all_scores or s.score > all_scores[s.timestamp].score:
                all_scores[s.timestamp] = s

        # Rate of change
        for s in self._rate_of_change_detect(series):
            if s.timestamp not in all_scores or s.score > all_scores[s.timestamp].score:
                all_scores[s.timestamp] = s

        # Isolation Forest
        for s in self._isolation_forest_detect(series):
            if s.timestamp not in all_scores or s.score > all_scores[s.timestamp].score:
                all_scores[s.timestamp] = s

        return sorted(all_scores.values(), key=lambda x: x.timestamp)

    def detect(self, metrics: DailyMetrics) -> List[Anomaly]:
        """
        Detect anomalies in daily metrics.

        Analyzes cost, token, and model-level time series.
        Returns list of detected anomalies with severity ≥ P2.
        """
        if not self.has_sufficient_training():
            logger.debug(f"Insufficient training data: {len(self._training_data)} days, need {self.min_training_days}")
            return []

        # Add current metrics to training data for analysis
        all_data = self._training_data + [metrics]
        
        anomalies = []

        # Build time series for each metric
        series_list = []

        # Total cost series
        timestamps = [m.date for m in all_data]
        cost_values = [m.total_cost for m in all_data]
        series_list.append(TimeSeries(timestamps, cost_values, "cost", "all", None))

        # Total tokens series
        token_values = [m.total_tokens for m in all_data]
        series_list.append(TimeSeries(timestamps, token_values, "tokens", "all", None))

        # Per-model cost series
        all_models = set()
        for m in all_data:
            all_models.update(m.model_costs.keys())
        for model in all_models:
            model_values = [m.model_costs.get(model, 0.0) for m in all_data]
            series_list.append(TimeSeries(timestamps, model_values, "cost", model, None))

        # Per-project cost series
        all_projects = set()
        for m in all_data:
            all_projects.update(m.project_costs.keys())
        for project in all_projects:
            project_values = [m.project_costs.get(project, 0.0) for m in all_data]
            series_list.append(TimeSeries(timestamps, project_values, "cost", "all", project))

        # Score each series and convert to anomalies
        for series in series_list:
            scores = self.score(series)
            for score in scores:
                if score.is_anomaly:
                    # Only flag the LAST point (current day) as anomaly
                    if score.timestamp != timestamps[-1]:
                        continue
                        
                    # Determine severity based on score
                    if score.score >= 0.8:
                        severity = "critical"
                    elif score.score >= 0.6:
                        severity = "high"
                    elif score.score >= 0.4:
                        severity = "medium"
                    else:
                        severity = "low"

                    # Only return severity ≥ P2 equivalent (high/critical)
                    if severity in ("high", "critical"):
                        deviation_pct = 0.0
                        if score.metadata.get("mean", 0) != 0:
                            deviation_pct = abs((score.value - score.metadata["mean"]) / score.metadata["mean"]) * 100

                        anomaly = Anomaly(
                            id=f"{series.metric_name}_{series.model}_{series.project or 'global'}_{score.timestamp.strftime('%Y%m%d%H%M%S')}",
                            model=series.model,
                            project=series.project,
                            metric=series.metric_name,
                            severity=severity,
                            score=score.score,
                            expected_value=score.metadata.get("mean", 0.0),
                            actual_value=score.value,
                            deviation_pct=deviation_pct,
                            detected_at=datetime.utcnow(),
                            description=f"Anomaly detected in {series.metric_name} for {series.model}" +
                                       (f" (project: {series.project})" if series.project else "") +
                                       f" via {score.method}: value={score.value:.4f}, expected~{score.metadata.get('mean', 0):.4f}",
                            method=score.method,
                        )
                        anomalies.append(anomaly)

                        # Emit event
                        if self.event_queue:
                            try:
                                self.event_queue.put_nowait(("anomaly.detected", anomaly))
                            except asyncio.QueueFull:
                                logger.warning("Event queue full, dropping anomaly.detected event")

        return anomalies

    def seed_anomalies(self, db_path: Path, days: int = 30) -> int:
        """
        Seed known anomalies into database for testing.

        Injects synthetic spike/dip anomalies into the usage_records table.
        Returns number of anomalies seeded.
        """
        import sqlite3

        seeded = 0
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Get existing models and projects
            cursor.execute("SELECT DISTINCT model FROM usage_records")
            models = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT DISTINCT project FROM usage_records")
            projects = [row[0] for row in cursor.fetchall()]

            if not models or not projects:
                logger.warning("No models or projects in database to seed anomalies")
                return 0

            import random
            base_date = datetime.utcnow() - timedelta(days=days)

            for _ in range(min(10, days)):
                model = random.choice(models)
                project = random.choice(projects)
                # Random day within range
                anomaly_date = base_date + timedelta(days=random.randint(0, days - 1))
                anomaly_hour = random.randint(0, 23)
                date_hour = anomaly_date.replace(hour=anomaly_hour, minute=0, second=0, microsecond=0)

                # Create spike (10x normal) or dip (0.1x normal)
                is_spike = random.choice([True, False])

                # Get baseline for this model/project/hour
                cursor.execute(
                    "SELECT AVG(cost) FROM usage_records WHERE model=? AND project=? AND strftime('%H', date_hour)=?",
                    (model, project, f"{anomaly_hour:02d}")
                )
                row = cursor.fetchone()
                baseline = row[0] if row and row[0] else 0.01

                if is_spike:
                    anomaly_cost = baseline * 10
                else:
                    anomaly_cost = baseline * 0.1

                # Upsert the anomaly
                cursor.execute(
                    """INSERT OR REPLACE INTO usage_records
                       (model, project, date_hour, prompt_tokens, completion_tokens, total_tokens, cost, request_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        model,
                        project,
                        date_hour.strftime("%Y-%m-%d %H:00:00"),
                        10000 if is_spike else 100,
                        5000 if is_spike else 50,
                        15000 if is_spike else 150,
                        anomaly_cost,
                        f"seeded_anomaly_{seeded}",
                    )
                )
                seeded += 1

            conn.commit()
            conn.close()
            logger.info(f"Seeded {seeded} synthetic anomalies for testing")

        except Exception as e:
            logger.error(f"Failed to seed anomalies: {e}")

        return seeded


# Backwards compatibility alias
AnomalyDetectorConfig = AnomalyDetector