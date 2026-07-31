# ARCHITECTURE.md — OpenRouter Monitor Coordination Contract

**Every agent must read this before writing code. It is the only coordination mechanism.**

---

## Directory Ownership

| Path | Owner | Description |
|------|-------|-------------|
| `/src/api/` | sub-1 | OpenRouter REST client, auth, rate limiting |
| `/src/ingestion/` | sub-2 | Cron job: fetch usage, normalize, store |
| `/src/costs/` | sub-3 | Cost engine: pricing lookup, calculation, attribution |
| `/src/alerts/` | sub-4 | Alert rules, Telegram delivery, deduplication |
| `/src/dashboard/` | sub-5 | FastAPI app, HTML/JS frontend, WebSocket updates |
| `/src/anomaly/` | sub-6 | Statistical detection, seeding, model health scoring |
| `/src/tests/` | sub-7 | Integration tests, seeded anomalies, contract tests |
| `/data/` | **shared** | SQLite DB (`monitor.db`), migrations |
| `/config/` | **shared** | `settings.yaml`, `pricing.yaml`, `alert_rules.yaml` |

---

## Interfaces (Contracts)

### 1. OpenRouter Client (`src/api/client.py`)
```python
class OpenRouterClient:
    async def get_usage(self, start: datetime, end: datetime) -> List[UsageRecord]
    async def get_models(self) -> List[ModelInfo]
    async def get_key_info(self) -> KeyInfo
```
- **Rate limit**: 60 RPM / 500 RPH (configurable)
- **Retry**: Exponential backoff, max 3 attempts
- **Auth**: Bearer token from `config/settings.yaml`

### 2. Ingestion Pipeline (`src/ingestion/pipeline.py`)
```python
class IngestionPipeline:
    async def run_incremental(self) -> IngestionResult  # since last cursor
    async def run_full(self, days: int) -> IngestionResult
    def get_last_cursor(self) -> datetime
```
- **Cursor**: Stored in `data/cursor.txt` (ISO timestamp)
- **Idempotent**: Upsert on `(model, project, date_hour)`

### 3. Cost Engine (`src/costs/engine.py`)
```python
class CostEngine:
    def calculate(self, usage: UsageRecord) -> CostBreakdown
    def attribute(self, usage: List[UsageRecord]) -> Dict[str, ProjectCost]
    def get_pricing(self, model: str) -> ModelPricing
```
- **Pricing source**: `config/pricing.yaml` (synced from OpenRouter API weekly)
- **Fallback**: Hardcoded defaults for known models

### 4. Alert Engine (`src/alerts/engine.py`)
```python
class AlertEngine:
    def evaluate(self, metrics: DailyMetrics) -> List[Alert]
    async def deliver(self, alert: Alert) -> DeliveryResult
```
- **Channels**: Telegram (primary), stdout (fallback)
- **Dedup**: 1 alert per rule per 4 hours
- **Rules**: Defined in `config/alert_rules.yaml`

### 5. Dashboard API (`src/dashboard/api.py`)
```python
# REST
GET /api/usage?model=&project=&from=&to=&granularity=
GET /api/costs?group_by=model|project|day
GET /api/alerts?unack_only=true
GET /api/anomalies?severity=high
GET /api/health  # liveness/readiness

# WebSocket
WS /ws/live  # real-time updates
```

### 6. Anomaly Detector (`src/anomaly/detector.py`)
```python
class AnomalyDetector:
    def score(self, series: TimeSeries) -> AnomalyScore
    def detect(self, metrics: DailyMetrics) -> List[Anomaly]
    def seed_anomalies(self, db: Database) -> int  # for testing
```
- **Methods**: Z-score (threshold), Isolation Forest (ML), Rate-of-change
- **Training window**: 30 days minimum

---

## Events (Pub/Sub via asyncio.Queue)

| Event | Publisher | Subscribers | Payload |
|-------|-----------|-------------|---------|
| `usage.ingested` | ingestion | costs, alerts, anomaly, dashboard | `IngestionResult` |
| `costs.calculated` | costs | alerts, dashboard | `ProjectCost[]` |
| `alert.triggered` | alerts | dashboard | `Alert` |
| `anomaly.detected` | anomaly | alerts, dashboard | `Anomaly` |
| `dashboard.update` | dashboard | — | `DashboardSnapshot` |

---

## Hard Rules

1. **No cross-directory imports** — Use events only. `src/api` never imports `src/costs`.
2. **Shared config only** — `config/` and `data/` are the only shared paths.
3. **Database schema migrations** — Only `src/tests/migrate.py` touches schema. Others use read-only connections.
4. **No global state** — All dependencies injected via constructors.
5. **Windows/git-bash compatible** — No `sqlite3` CLI, no `SIGALRM`, no `du --exclude`. Use Python stdlib.
6. **Type hints mandatory** — `mypy --strict` passes on all src.
7. **Tests are contracts** — Integration tests in `src/tests/` define expected behavior. Code must pass them.
8. **Secrets never in code** — `settings.yaml` is gitignored. Template at `config/settings.yaml.example`.

---

## Data Flow

```
OpenRouter API
      │
      ▼
┌─────────────┐     usage.ingested      ┌─────────────┐
│  Ingestion  │ ──────────────────────▶ │   Costs     │
│  (cron)     │                         │  Engine     │
└─────────────┘                         └──────┬──────┘
      │                                        │
      │ costs.calculated                       │
      ▼                                        ▼
┌─────────────┐                         ┌─────────────┐
│  Anomaly    │ ◀────────────────────── │  Alerts     │
│  Detector   │   anomaly.detected      │  Engine     │
└──────┬──────┘                         └──────┬──────┘
       │                                       │
       │ alert.triggered                       │
       ▼                                       ▼
┌─────────────────────────────────────────────────────┐
│                   Dashboard (FastAPI)               │
│  REST API + WebSocket + Static HTML/JS Frontend     │
└─────────────────────────────────────────────────────┘
```

---

## Deployment Contract

| Component | Schedule | Timeout | Retries |
|-----------|----------|---------|---------|
| Ingestion | Every 15 min | 60s | 2 |
| Cost recalc | Hourly | 30s | 1 |
| Anomaly scan | Hourly | 45s | 1 |
| Alert eval | Every 5 min | 20s | 0 |
| Dashboard | Always on | — | — |
| Pricing sync | Weekly (Sun 3am) | 120s | 2 |

---

## Verification Gates (Critic Checks)

Each component must pass its critic before merge:

| Component | Critic Test | Pass Criteria |
|-----------|-------------|---------------|
| API client | Mock OpenRouter 429/500/timeout | Retries, backs off, surfaces error |
| Ingestion | Seed 10k records, re-run | Idempotent, cursor advances, 0 dupes |
| Costs | Known usage + pricing = expected cost | ±$0.01 accuracy |
| Alerts | Seed threshold breach | Fires once, dedups 4h, delivers Telegram |
| Dashboard | Load test 100 concurrent WS | <100ms p95, no memory leak |
| Anomaly | Inject known spike/dip | Detected with severity ≥ P2 |
| Integration | Full 24h simulation | All gates green, dashboard shows truth |

---

## Version
`v1.0` — Initial contract. Update only via lead agent with all sub-agents notified.