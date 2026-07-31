# Gauntlet Loop Prompt — OpenRouter Monitoring System

I want you to build a local OpenRouter monitoring system at the level of Vercel's internal usage dashboard + Datadog APM.

It should track every model's token usage, costs, latency, and error rates in real-time. Alert on budget overruns, model degradation, and anomalous patterns. Serve a local dashboard at localhost:3001 with historical trends, per-project attribution, and model ROI analysis. All via Hermes cron jobs — zero external deps beyond OpenRouter API.

Fan out sub-agents and have sub-agents tackle each piece individually so the system is utterly reliable. You should /loop on each component and have a separate sub-agent check it against the bar — Vercel's dashboard + Datadog — to ensure it catches real issues. That separate sub-agent should be a really harsh critic, and if it doesn't catch a seeded bug or miss a real anomaly, it should keep going.

Don't stop until each sub-agent's output wins in a blind A/B comparison against the reference. "Which dashboard would you trust to catch a $500 surprise bill?" The local one must win.

Use Python for ingestion/calculation, FastAPI for the dashboard, SQLite for storage. Run on Windows via git-bash. No sqlite3 CLI — use Python's sqlite3 module. No SIGALRM — use subprocess timeout.

/loop until it's utterly production-grade. Fan out sub-agents.