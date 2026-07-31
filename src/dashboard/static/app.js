/**
 * OpenRouter Monitor Dashboard - Frontend
 * Real-time WebSocket updates with Chart.js visualization
 */

// Chart.js registration
const { Chart, registerables } = Chart;
Chart.register(...registerables);

// State
let ws = null;
let reconnectAttempts = 0;
const maxReconnectAttempts = 10;
let costChart = null;
let currentTimeRange = '24h';
let snapshotCache = null;

// DOM Elements
const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
const timeRangeButtons = document.querySelectorAll('.time-range button');
const tabButtons = document.querySelectorAll('.tab');
const tabPanels = document.querySelectorAll('.tab-panel');

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    initWebSocket();
    initTimeRange();
    initTabs();
    initCostChart();
    fetchInitialData();
});

// WebSocket Connection
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/live`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
        reconnectAttempts = 0;
        updateConnectionStatus(true);
    };

    ws.onmessage = (event) => {
        try {
            const message = JSON.parse(event.data);
            handleWebSocketMessage(message);
        } catch (e) {
            console.error('Failed to parse WebSocket message:', e);
        }
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected');
        updateConnectionStatus(false);
        attemptReconnect();
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function attemptReconnect() {
    if (reconnectAttempts >= maxReconnectAttempts) {
        console.log('Max reconnect attempts reached');
        return;
    }

    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
    reconnectAttempts++;
    console.log(`Reconnecting in ${delay}ms (attempt ${reconnectAttempts})`);

    setTimeout(initWebSocket, delay);
}

function updateConnectionStatus(connected) {
    if (connected) {
        statusDot.classList.remove('disconnected');
        statusText.textContent = 'Live';
    } else {
        statusDot.classList.add('disconnected');
        statusText.textContent = 'Disconnected';
    }
}

function handleWebSocketMessage(message) {
    const { type, payload, timestamp } = message;

    switch (type) {
        case 'snapshot':
            snapshotCache = payload;
            updateOverviewMetrics(payload);
            updateCostChart(payload);
            updateModelTable(payload);
            updateProjectTable(payload);
            break;
        case 'alert':
            addAlertToTable(payload);
            break;
        case 'anomaly':
            addAnomalyToTable(payload);
            break;
        case 'usage_update':
            // Could update usage table incrementally
            break;
        case 'cost_update':
            // Could update cost tables incrementally
            break;
    }
}

// Time Range Selector
function initTimeRange() {
    timeRangeButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            timeRangeButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTimeRange = btn.dataset.range;
            fetchDataForRange(currentTimeRange);
        });
    });
}

function fetchDataForRange(range) {
    // Map range to hours/days
    const rangeMap = {
        '1h': { hours: 1 },
        '6h': { hours: 6 },
        '24h': { hours: 24 },
        '7d': { days: 7 },
        '30d': { days: 30 }
    };

    const params = rangeMap[range] || { hours: 24 };
    fetchUsage(params);
    fetchCosts(params);
    fetchAlerts(params);
    fetchAnomalies(params);
}

// Tabs
function initTabs() {
    tabButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            tabButtons.forEach(b => b.classList.remove('active'));
            tabPanels.forEach(p => p.classList.remove('active'));

            btn.classList.add('active');
            document.getElementById(`panel-${btn.dataset.tab}`).classList.add('active');
        });
    });
}

// Cost Chart
function initCostChart() {
    const ctx = document.getElementById('costChart').getContext('2d');
    costChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Cost ($)',
                data: [],
                borderColor: '#58a6ff',
                backgroundColor: 'rgba(88, 166, 255, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                pointHoverRadius: 4,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index',
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: '#161b22',
                    titleColor: '#e6edf3',
                    bodyColor: '#8b949e',
                    borderColor: '#30363d',
                    borderWidth: 1,
                    padding: 12,
                    callbacks: {
                        label: (context) => `$${context.parsed.y.toFixed(4)}`
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(48, 54, 61, 0.5)' },
                    ticks: { color: '#8b949e', font: { size: 10 } }
                },
                y: {
                    grid: { color: 'rgba(48, 54, 61, 0.5)' },
                    ticks: {
                        color: '#8b949e',
                        font: { size: 10 },
                        callback: (value) => `$${value.toFixed(4)}`
                    },
                    beginAtZero: true
                }
            }
        }
    });
}

// Data Fetching
async function fetchInitialData() {
    await fetchDataForRange('24h');
}

async function fetchUsage(params) {
    try {
        const query = new URLSearchParams();
        if (params.hours) query.set('hours', params.hours);
        if (params.days) query.set('days', params.days);
        query.set('limit', '100');

        const res = await fetch(`/api/usage?${query}`);
        const data = await res.json();

        if (data.records) {
            updateUsageTable(data.records);
        }
    } catch (e) {
        console.error('Failed to fetch usage:', e);
    }
}

async function fetchCosts(params) {
    try {
        const query = new URLSearchParams();
        if (params.hours) query.set('from_date', new Date(Date.now() - (params.hours || params.days * 24) * 3600000).toISOString());
        if (params.days) query.set('from_date', new Date(Date.now() - params.days * 86400000).toISOString());
        query.set('group_by', 'model');

        const res = await fetch(`/api/costs?${query}`);
        const data = await res.json();

        if (data.breakdowns) {
            // Data is already shown in model/project tables from snapshot
        }
    } catch (e) {
        console.error('Failed to fetch costs:', e);
    }
}

async function fetchAlerts(params) {
    try {
        const res = await fetch('/api/alerts');
        const data = await res.json();
        if (data.alerts && data.alerts.length > 0) {
            data.alerts.forEach(addAlertToTable);
        }
    } catch (e) {
        console.error('Failed to fetch alerts:', e);
    }
}

async function fetchAnomalies(params) {
    try {
        const res = await fetch('/api/anomalies');
        const data = await res.json();
        if (data.anomalies && data.anomalies.length > 0) {
            data.anomalies.forEach(addAnomalyToTable);
        }
    } catch (e) {
        console.error('Failed to fetch anomalies:', e);
    }
}

// UI Updates
function updateOverviewMetrics(snapshot) {
    document.getElementById('metricCost').textContent = formatCost(snapshot.total_cost_24h);
    document.getElementById('metricTokens').textContent = formatNumber(snapshot.total_tokens_24h);
    document.getElementById('metricModels').textContent = snapshot.active_models;
    document.getElementById('metricProjects').textContent = snapshot.active_projects;
}

function updateCostChart(snapshot) {
    if (!costChart || !snapshot.cost_trend_24h) return;

    const labels = snapshot.cost_trend_24h.map(d => d.hour);
    const data = snapshot.cost_trend_24h.map(d => d.cost);

    costChart.data.labels = labels;
    costChart.data.datasets[0].data = data;
    costChart.update('none');
}

function updateModelTable(snapshot) {
    const tbody = document.querySelector('#modelTable tbody');
    if (!tbody) return;

    const entries = Object.entries(snapshot.usage_by_model || {});
    const totalCost = entries.reduce((sum, [_, cost]) => sum + cost, 0);

    tbody.innerHTML = entries.map(([model, cost]) => `
        <tr>
            <td><code>${escapeHtml(model)}</code></td>
            <td>${formatCost(cost)}</td>
            <td>-</td>
            <td>${totalCost > 0 ? ((cost / totalCost) * 100).toFixed(1) : '0'}%</td>
        </tr>
    `).join('');
}

function updateProjectTable(snapshot) {
    const tbody = document.querySelector('#projectTable tbody');
    if (!tbody) return;

    const entries = Object.entries(snapshot.usage_by_project || {});
    const totalCost = entries.reduce((sum, [_, cost]) => sum + cost, 0);

    tbody.innerHTML = entries.map(([project, cost]) => `
        <tr>
            <td><code>${escapeHtml(project)}</code></td>
            <td>${formatCost(cost)}</td>
            <td>-</td>
            <td>${totalCost > 0 ? ((cost / totalCost) * 100).toFixed(1) : '0'}%</td>
        </tr>
    `).join('');
}

function updateUsageTable(records) {
    const tbody = document.querySelector('#usageTable tbody');
    if (!tbody) return;

    tbody.innerHTML = records.slice(0, 50).map(r => `
        <tr>
            <td>${formatTime(r.date_hour)}</td>
            <td><code>${escapeHtml(r.model)}</code></td>
            <td><code>${escapeHtml(r.project)}</code></td>
            <td>${formatNumber(r.prompt_tokens)}</td>
            <td>${formatNumber(r.completion_tokens)}</td>
            <td>${formatNumber(r.total_tokens)}</td>
            <td>${formatCost(r.cost)}</td>
        </tr>
    `).join('');
}

function addAlertToTable(alert) {
    const emptyState = document.getElementById('alertsEmpty');
    const container = document.getElementById('alertsTableContainer');
    const tbody = document.querySelector('#alertsTable tbody');

    if (emptyState) emptyState.style.display = 'none';
    if (container) container.style.display = 'block';

    if (!tbody) return;

    const row = document.createElement('tr');
    row.innerHTML = `
        <td><span class="badge badge-${alert.severity.toLowerCase()}">${alert.severity}</span></td>
        <td>${escapeHtml(alert.rule_name)}</td>
        <td>${escapeHtml(alert.metric)}</td>
        <td>${alert.threshold}</td>
        <td>${alert.actual_value}</td>
        <td>${formatTime(alert.timestamp)}</td>
    `;
    tbody.insertBefore(row, tbody.firstChild);
}

function addAnomalyToTable(anomaly) {
    const emptyState = document.getElementById('anomaliesEmpty');
    const container = document.getElementById('anomaliesTableContainer');
    const tbody = document.querySelector('#anomaliesTable tbody');

    if (emptyState) emptyState.style.display = 'none';
    if (container) container.style.display = 'block';

    if (!tbody) return;

    const row = document.createElement('tr');
    row.innerHTML = `
        <td><span class="badge badge-${anomaly.severity}">${anomaly.severity}</span></td>
        <td><code>${escapeHtml(anomaly.model)}</code></td>
        <td><code>${escapeHtml(anomaly.project || '-')}</code></td>
        <td>${escapeHtml(anomaly.metric)}</td>
        <td>${(anomaly.score * 100).toFixed(1)}%</td>
        <td>${anomaly.deviation_pct.toFixed(1)}%</td>
        <td>${formatTime(anomaly.detected_at)}</td>
    `;
    tbody.insertBefore(row, tbody.firstChild);
}

// Helpers
function formatCost(value) {
    if (value >= 1) return `$${value.toFixed(2)}`;
    if (value >= 0.01) return `$${value.toFixed(4)}`;
    return `$${value.toFixed(6)}`;
}

function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toLocaleString();
}

function formatTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Expose for debugging
window.dashboard = {
    ws: () => ws,
    snapshot: () => snapshotCache,
    reconnect: initWebSocket,
};