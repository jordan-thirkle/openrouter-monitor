"""Dashboard API + WebSocket + Frontend for OpenRouter Monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.dashboard.models import (
    Alert,
    AlertSeverity,
    AnomaliesResponse,
    Anomaly,
    AnomalySeverity,
    AlertsResponse,
    CostBreakdown,
    CostsResponse,
    DashboardEventType,
    DashboardSnapshot,
    Granularity,
    GroupBy,
    HealthResponse,
    UsageRecord,
    UsageResponse,
    WebSocketMessage,
)
from src.ingestion.database import get_db_path

logger = logging.getLogger(__name__)

# Global state for WebSocket connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.last_snapshot: Optional[DashboardSnapshot] = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: WebSocketMessage):
        if not self.active_connections:
            return
        data = asdict(message)
        data["timestamp"] = message.timestamp.isoformat()
        # Convert payload dataclasses to dict
        if hasattr(message.payload, '__dict__'):
            data["payload"] = {k: v for k, v in asdict(message.payload).items()}
        elif isinstance(message.payload, list):
            data["payload"] = [asdict(p) if hasattr(p, '__dataclass_fields__') else p for p in message.payload]
        else:
            data["payload"] = message.payload

        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(data))
            except Exception:
                self.disconnect(connection)

    def set_snapshot(self, snapshot: DashboardSnapshot):
        self.last_snapshot = snapshot


manager = ConnectionManager()


# Database query helpers
def get_db_connection():
    """Get database connection with row factory."""
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def query_usage(
    model: Optional[str] = None,
    project: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    granularity: Granularity = Granularity.HOUR,
    limit: int = 1000,
    offset: int = 0,
) -> tuple[List[UsageRecord], int]:
    """Query usage records from database."""
    conn = get_db_connection()
    try:
        conditions = []
        params = []

        if model:
            conditions.append("model = ?")
            params.append(model)
        if project:
            conditions.append("project = ?")
            params.append(project)
        if from_date:
            conditions.append("date_hour >= ?")
            params.append(from_date.strftime("%Y-%m-%d %H:00:00"))
        if to_date:
            conditions.append("date_hour <= ?")
            params.append(to_date.strftime("%Y-%m-%d %H:00:00"))

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        # Count total
        count_query = f"SELECT COUNT(*) as cnt FROM usage_records{where_clause}"
        total = conn.execute(count_query, params).fetchone()["cnt"]

        # Get records
        query = f"""
            SELECT model, project, date_hour, prompt_tokens, completion_tokens,
                   total_tokens, cost, request_id
            FROM usage_records
            {where_clause}
            ORDER BY date_hour DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()

        records = []
        for row in rows:
            records.append(UsageRecord(
                model=row["model"],
                project=row["project"],
                date_hour=datetime.strptime(row["date_hour"], "%Y-%m-%d %H:00:00"),
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
                cost=row["cost"],
                request_id=row["request_id"],
            ))

        return records, total
    finally:
        conn.close()


def query_costs(
    group_by: GroupBy = GroupBy.MODEL,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
) -> CostsResponse:
    """Query aggregated costs from database."""
    conn = get_db_connection()
    try:
        conditions = []
        params = []

        if from_date:
            conditions.append("date_hour >= ?")
            params.append(from_date.strftime("%Y-%m-%d %H:00:00"))
        if to_date:
            conditions.append("date_hour <= ?")
            params.append(to_date.strftime("%Y-%m-%d %H:00:00"))

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        if group_by == GroupBy.MODEL:
            group_col = "model"
        elif group_by == GroupBy.PROJECT:
            group_col = "project"
        else:  # DAY
            group_col = "date(date_hour)"

        query = f"""
            SELECT {group_col} as group_key,
                   SUM(cost) as total_cost,
                   SUM(prompt_tokens) as total_prompt_tokens,
                   SUM(completion_tokens) as total_completion_tokens,
                   SUM(total_tokens) as total_tokens,
                   COUNT(*) as record_count
            FROM usage_records
            {where_clause}
            GROUP BY {group_col}
            ORDER BY total_cost DESC
        """
        rows = conn.execute(query, params).fetchall()

        breakdowns = []
        total_cost = Decimal("0")

        for row in rows:
            group = row["group_key"]
            cost = Decimal(str(row["total_cost"]))
            total_cost += cost

            # Get model breakdown if grouping by project/day
            model_breakdown = {}
            if group_by != GroupBy.MODEL:
                if where_clause:
                    model_query = f"""
                        SELECT model, SUM(cost) as model_cost
                        FROM usage_records
                        {where_clause} AND {group_col} = ?
                        GROUP BY model
                    """
                else:
                    model_query = f"""
                        SELECT model, SUM(cost) as model_cost
                        FROM usage_records
                        WHERE {group_col} = ?
                        GROUP BY model
                    """
                model_rows = conn.execute(model_query, params + [group]).fetchall()
                model_breakdown = {r["model"]: float(r["model_cost"]) for r in model_rows}

            breakdowns.append(CostBreakdown(
                group=group,
                total_cost=float(cost),
                total_prompt_tokens=row["total_prompt_tokens"],
                total_completion_tokens=row["total_completion_tokens"],
                total_tokens=row["total_tokens"],
                record_count=row["record_count"],
                model_breakdown=model_breakdown,
            ))

        # Get period bounds
        period_start = from_date or datetime.utcnow() - timedelta(days=7)
        period_end = to_date or datetime.utcnow()

        return CostsResponse(
            breakdowns=breakdowns,
            group_by=group_by,
            total_cost=float(total_cost),
            period_start=period_start,
            period_end=period_end,
        )
    finally:
        conn.close()


def query_alerts(
    unack_only: bool = False,
    limit: int = 100,
) -> AlertsResponse:
    """Query alerts from alert state file."""
    import json
    from src.alerts.models import AlertState

    state_path = Path(__file__).parent.parent.parent / "data" / "alert_state.json"
    alerts = []

    if state_path.exists():
        try:
            with open(state_path, "r") as f:
                data = json.load(f)
            # This is just trigger timestamps, not full alerts
            # In production, alerts would be stored in a separate table
            pass
        except Exception:
            pass

    # For now, return empty - alerts would come from event system
    return AlertsResponse(
        alerts=alerts,
        total=0,
        unacknowledged=0,
    )


def query_anomalies(
    severity: Optional[AnomalySeverity] = None,
    limit: int = 100,
) -> AnomaliesResponse:
    """Query anomalies from database (if stored) or return empty."""
    # In production, anomalies would be stored
    return AnomaliesResponse(
        anomalies=[],
        total=0,
        by_severity={s.value: 0 for s in AnomalySeverity},
    )


def build_snapshot() -> DashboardSnapshot:
    """Build real-time dashboard snapshot."""
    conn = get_db_connection()
    try:
        # Last 24 hours
        from_date = datetime.utcnow() - timedelta(hours=24)
        from_str = from_date.strftime("%Y-%m-%d %H:00:00")

        # Total cost 24h
        cost_row = conn.execute(
            "SELECT SUM(cost) as total_cost FROM usage_records WHERE date_hour >= ?",
            (from_str,)
        ).fetchone()
        total_cost_24h = float(cost_row["total_cost"]) if cost_row["total_cost"] else 0.0

        # Total tokens 24h
        token_row = conn.execute(
            "SELECT SUM(total_tokens) as total_tokens FROM usage_records WHERE date_hour >= ?",
            (from_str,)
        ).fetchone()
        total_tokens_24h = token_row["total_tokens"] if token_row["total_tokens"] else 0

        # Active models
        model_row = conn.execute(
            "SELECT COUNT(DISTINCT model) as cnt FROM usage_records WHERE date_hour >= ?",
            (from_str,)
        ).fetchone()
        active_models = model_row["cnt"]

        # Active projects
        project_row = conn.execute(
            "SELECT COUNT(DISTINCT project) as cnt FROM usage_records WHERE date_hour >= ?",
            (from_str,)
        ).fetchone()
        active_projects = project_row["cnt"]

        # Usage by model
        model_rows = conn.execute(
            "SELECT model, SUM(cost) as cost FROM usage_records WHERE date_hour >= ? GROUP BY model ORDER BY cost DESC",
            (from_str,)
        ).fetchall()
        usage_by_model = {r["model"]: float(r["cost"]) for r in model_rows}

        # Usage by project
        project_rows = conn.execute(
            "SELECT project, SUM(cost) as cost FROM usage_records WHERE date_hour >= ? GROUP BY project ORDER BY cost DESC",
            (from_str,)
        ).fetchall()
        usage_by_project = {r["project"]: float(r["cost"]) for r in project_rows}

        # Cost trend 24h (hourly)
        trend_rows = conn.execute(
            "SELECT date_hour, SUM(cost) as cost FROM usage_records WHERE date_hour >= ? GROUP BY date_hour ORDER BY date_hour",
            (from_str,)
        ).fetchall()
        cost_trend_24h = [
            {"hour": r["date_hour"][-5:], "cost": float(r["cost"])}
            for r in trend_rows
        ]

        return DashboardSnapshot(
            timestamp=datetime.utcnow(),
            total_cost_24h=total_cost_24h,
            total_tokens_24h=total_tokens_24h,
            active_models=active_models,
            active_projects=active_projects,
            unacknowledged_alerts=0,
            recent_anomalies=0,
            usage_by_model=usage_by_model,
            usage_by_project=usage_by_project,
            cost_trend_24h=cost_trend_24h,
        )
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Dashboard starting up")
    yield
    # Shutdown
    logger.info("Dashboard shutting down")


app = FastAPI(
    title="OpenRouter Monitor Dashboard",
    description="Real-time token usage, cost tracking, and anomaly detection",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard HTML."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<h1>Dashboard not built yet</h1>")


@app.get("/api/usage", response_model=UsageResponse)
async def get_usage(
    model: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    granularity: Granularity = Query(Granularity.HOUR),
    limit: int = Query(1000, le=10000),
    offset: int = Query(0, ge=0),
):
    """Get usage records with filtering and pagination."""
    records, total = query_usage(model, project, from_date, to_date, granularity, limit, offset)

    # Determine date range from results
    from_dt = from_date or (records[-1].date_hour if records else datetime.utcnow())
    to_dt = to_date or (records[0].date_hour if records else datetime.utcnow())

    return UsageResponse(
        records=records,
        total_records=total,
        from_date=from_dt,
        to_date=to_dt,
        granularity=granularity,
    )


@app.get("/api/costs", response_model=CostsResponse)
async def get_costs(
    group_by: GroupBy = Query(GroupBy.MODEL),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
):
    """Get aggregated costs grouped by model, project, or day."""
    return query_costs(group_by, from_date, to_date)


@app.get("/api/alerts", response_model=AlertsResponse)
async def get_alerts(
    unack_only: bool = Query(False),
    limit: int = Query(100, le=1000),
):
    """Get alerts (triggered alerts from alert engine)."""
    return query_alerts(unack_only, limit)


@app.get("/api/anomalies", response_model=AnomaliesResponse)
async def get_anomalies(
    severity: Optional[AnomalySeverity] = Query(None),
    limit: int = Query(100, le=1000),
):
    """Get detected anomalies."""
    return query_anomalies(severity, limit)


@app.get("/api/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    conn = get_db_connection()
    db_connected = False
    try:
        conn.execute("SELECT 1").fetchone()
        db_connected = True
    except Exception:
        pass
    finally:
        conn.close()

    return HealthResponse(
        status="healthy" if db_connected else "degraded",
        version="1.0.0",
        uptime_seconds=time.time() - app.state.start_time if hasattr(app.state, 'start_time') else 0,
        database_connected=db_connected,
        event_queue_size=0,
        active_websockets=len(manager.active_connections),
        last_ingestion=None,
        last_cost_calculation=None,
        last_anomaly_scan=None,
    )


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time dashboard updates."""
    await manager.connect(websocket)
    try:
        # Send current snapshot immediately
        snapshot = build_snapshot()
        manager.set_snapshot(snapshot)
        await websocket.send_text(json.dumps(asdict(WebSocketMessage(
            type="snapshot",
            payload=asdict(snapshot),
            timestamp=datetime.utcnow(),
        ))))

        # Keep connection alive, send periodic updates
        while True:
            await asyncio.sleep(5)  # Update every 5 seconds
            snapshot = build_snapshot()
            manager.set_snapshot(snapshot)
            await websocket.send_text(json.dumps(asdict(WebSocketMessage(
                type="snapshot",
                payload=asdict(snapshot),
                timestamp=datetime.utcnow(),
            ))))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


def run_dashboard(host: str = "0.0.0.0", port: int = 3001):
    """Run the dashboard server."""
    import uvicorn
    app.state.start_time = time.time()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_dashboard()