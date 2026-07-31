# OpenRouter Monitor

> Local-first OpenRouter usage monitor with anomaly detection, alerting, and real-time dashboard. Built with Gauntlet Loop methodology.

## Why?

OpenRouter shows you **totals** — not *why* costs spike, which project blew the budget, or when a runaway process starts burning tokens at 3 AM. This gives you:

- **Real-time dashboard** — WebSocket updates, no refresh needed
- **Anomaly detection** — Z-score + rate-of-change + Isolation Forest (30-day training)
- **Alerting** — 8 built-in rules, Telegram + stdout, 4h cooldown persistence
- **Project attribution** — Tag usage by project/model, see per-project costs
- **Local-first** — Your data never leaves your machine, SQLite storage
- **Free forever** — No SaaS fees, runs on your hardware

## Quick Start

```bash
# Clone
git clone https://github.com/jordan-thirkle/openrouter-monitor
cd openrouter-monitor

# Virtual env
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Install
pip install -r requirements.txt

# Configure
cp config/settings.yaml.example config/settings.yaml
# Edit: add OpenRouter API key + Telegram bot_token/chat_id

# Run dashboard
python -m src.dashboard.api
# Open http://localhost:3001
```

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Ingestion  │────▶│    Costs     │────▶│   Alerts    │
│  (15min)    │     │  Engine      │     │  (5min)     │
└─────────────┘     └──────────────┘     └─────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────────────────────────────────────────────┐
│              Event Queue (asyncio.Queue)            │
│  usage.ingested │ costs.calculated │ alert.triggered │
└─────────────────────────────────────────────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Anomaly    │     │  Dashboard   │     │  Telegram   │
│  Detector   │     │  (WS + REST) │     │  / Stdout   │
└─────────────┘     └──────────────┘     └─────────────┘
```

**7 Components** — All with contract-first design (see `ARCHITECTURE.md`):
1. **API Client** — Rate-limited, retries, 16 critic tests
2. **Ingestion** — Idempotent upsert, cursor tracking, 4 tests
3. **Cost Engine** — 50+ models, Decimal precision, fallbacks
4. **Alerting** — 8 rules, SQLite cooldown, 10 tests
5. **Anomaly Detector** — Z-score + ROC + Isolation Forest
6. **Dashboard** — FastAPI + WebSocket + vanilla JS
7. **Tests** — 53 critic tests, evidence-based verification

## Configuration

| File | Purpose |
|------|---------|
| `config/settings.yaml` | API keys, Telegram, rate limits |
| `config/alert_rules.yaml` | 8 alert thresholds |
| `config/pricing.yaml` | 50+ model prices (weekly sync) |

## Alert Rules (Default)

| Rule | Metric | Threshold | Severity |
|------|--------|-----------|----------|
| `daily_cost_breach` | Total daily cost | $100 | P1 |
| `hourly_cost_spike` | Hourly cost | $25 | P2 |
| `project_cost_overrun` | Per-project daily | $50 | P2 |
| `model_cost_anomaly` | Per-model daily | $30 | P3 |
| `anomaly_detected_high` | Anomaly score | ≥0.8 | P1 |
| `anomaly_detected_medium` | Anomaly score | ≥0.5 | P3 |
| `token_usage_spike` | Hourly tokens | 500k | P2 |
| `cost_increase_rate` | Growth rate | 2x | P2 |

## Testing

```bash
# All tests (53 passing)
pytest src/tests/ -v

# Component tests
pytest src/tests/test_api_client.py -v       # 16 tests
pytest src/tests/test_ingestion_idempotent.py -v  # 4 tests
pytest src/tests/test_alerts.py -v           # 10 tests
pytest src/tests/test_anomaly.py -v          # 8 tests
pytest src/tests/test_dashboard.py -v        # 15 tests
```

## Production Deployment

**Cron Schedule:**
```bash
# Ingestion - every 15 min
*/15 * * * * cd /path/to/openrouter-monitor && python -m src.ingestion.pipeline

# Costs - hourly
0 * * * * cd /path/to/openrouter-monitor && python -m src.costs.engine

# Anomaly - hourly
0 * * * * cd /path/to/openrouter-monitor && python -m src.anomaly.detector

# Alerts - every 5 min
*/5 * * * * cd /path/to/openrouter-monitor && python -m src.alerts.engine

# Dashboard - always on (systemd/docker)
python -m src.dashboard.api
```

## Gauntlet Loop Methodology

Built using **Matt Shumer's Gauntlet Loop** (byjtt.com formalization):

- **Goal → Bar → Fan-out → Critic → Loop → Verify**
- **Bar**: Vercel Dashboard + Datadog APM
- **Wave 1**: 3 components, all 5/5 critic score on iteration 1
- **Evidence-based**: "Seed 10k records → re-run → 0 dupes" not "looks idempotent"
- **Contract-first**: `ARCHITECTURE.md` = sole coordination mechanism

## License

MIT — Use freely, contribute back.

## Contributing

1. Read `ARCHITECTURE.md` — all components have explicit contracts
2. Run `pytest src/tests/ -v` — all 53 tests must pass
3. Follow the component's directory ownership (no cross-imports)
3. Add critic tests for new features (evidence-based)
4. Update `ARCHITECTURE.md` for interface changes

---

**Built with**: Python 3.11+, FastAPI, SQLite, asyncio, Gauntlet Loop methodology