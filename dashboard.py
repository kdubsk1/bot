"""
dashboard.py - NQ CALLS Performance Dashboard
===============================================
Reads outcomes.csv and strategy_log.csv and generates an interactive
HTML dashboard with charts.

Usage:
    python dashboard.py
    python dashboard.py --open       (auto-open in browser)
    python dashboard.py --days 30    (last 30 days only)

Output:
    data/dashboard.html

Charts:
    - Win rate by setup type
    - Win rate by market
    - Win rate by hour of day
    - Win rate by conviction tier
    - Win rate by day of week
    - Decisions breakdown (fired/rejected/almost)
    - Missed winners from strategy log
    - Trade timeline
"""

import os
import sys
import csv
import json
import argparse
import webbrowser
from datetime import datetime, timedelta
from collections import defaultdict

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTCOMES_CSV = os.path.join(_BASE_DIR, "outcomes.csv")
STRATEGY_LOG = os.path.join(_BASE_DIR, "data", "strategy_log.csv")
DASHBOARD_OUT = os.path.join(_BASE_DIR, "data", "dashboard.html")


def load_outcomes(days: int = 0) -> list:
    if not os.path.exists(OUTCOMES_CSV):
        return []
    with open(OUTCOMES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = [r for r in rows if r.get("timestamp", "") >= cutoff]
    return rows


def load_strategy_log(days: int = 0) -> list:
    if not os.path.exists(STRATEGY_LOG):
        return []
    with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = [r for r in rows if r.get("timestamp", "") >= cutoff]
    return rows


def build_dashboard(days: int = 0) -> str:
    outcomes = load_outcomes(days)
    strategy = load_strategy_log(days)

    closed = [r for r in outcomes if r.get("status") == "CLOSED"]
    open_trades = [r for r in outcomes if r.get("status") == "OPEN"]

    # ── Stats calculations ──
    total = len(outcomes)
    wins = [r for r in closed if r.get("result") == "WIN"]
    losses = [r for r in closed if r.get("result") == "LOSS"]
    skips = [r for r in closed if r.get("result") == "SKIP"]
    win_count = len(wins)
    loss_count = len(losses)
    wr = round(win_count / max(1, win_count + loss_count) * 100, 1)

    # By setup type
    setup_data = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in closed:
        key = r.get("setup", "?")
        if r.get("result") == "WIN":
            setup_data[key]["wins"] += 1
        elif r.get("result") == "LOSS":
            setup_data[key]["losses"] += 1

    setup_labels = list(setup_data.keys())
    setup_wins = [setup_data[k]["wins"] for k in setup_labels]
    setup_losses = [setup_data[k]["losses"] for k in setup_labels]
    setup_wr = [round(setup_data[k]["wins"] / max(1, setup_data[k]["wins"] + setup_data[k]["losses"]) * 100, 1)
                for k in setup_labels]

    # By market
    market_data = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in closed:
        key = r.get("market", "?")
        if r.get("result") == "WIN":
            market_data[key]["wins"] += 1
        elif r.get("result") == "LOSS":
            market_data[key]["losses"] += 1

    market_labels = list(market_data.keys())
    market_wins = [market_data[k]["wins"] for k in market_labels]
    market_losses = [market_data[k]["losses"] for k in market_labels]
    market_wr = [round(market_data[k]["wins"] / max(1, market_data[k]["wins"] + market_data[k]["losses"]) * 100, 1)
                 for k in market_labels]

    # By hour
    hour_data = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in closed:
        try:
            h = int(r.get("hour", 0))
        except (ValueError, TypeError):
            h = 0
        if r.get("result") == "WIN":
            hour_data[h]["wins"] += 1
        elif r.get("result") == "LOSS":
            hour_data[h]["losses"] += 1

    hour_labels = list(range(24))
    hour_wins = [hour_data[h]["wins"] for h in hour_labels]
    hour_losses = [hour_data[h]["losses"] for h in hour_labels]
    hour_wr = [round(hour_data[h]["wins"] / max(1, hour_data[h]["wins"] + hour_data[h]["losses"]) * 100, 1)
               for h in hour_labels]
    hour_str_labels = [f"{h:02d}:00" for h in hour_labels]

    # By conviction tier
    tier_data = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in closed:
        key = r.get("tier", "?")
        if r.get("result") == "WIN":
            tier_data[key]["wins"] += 1
        elif r.get("result") == "LOSS":
            tier_data[key]["losses"] += 1

    tier_order = ["HIGH", "MEDIUM", "LOW"]
    tier_labels = [t for t in tier_order if t in tier_data]
    tier_wins = [tier_data[k]["wins"] for k in tier_labels]
    tier_losses = [tier_data[k]["losses"] for k in tier_labels]
    tier_wr = [round(tier_data[k]["wins"] / max(1, tier_data[k]["wins"] + tier_data[k]["losses"]) * 100, 1)
               for k in tier_labels]

    # By day of week (from timestamp)
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_data = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in closed:
        try:
            ts = r.get("timestamp", "")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            d = dt.weekday()
            if r.get("result") == "WIN":
                dow_data[d]["wins"] += 1
            elif r.get("result") == "LOSS":
                dow_data[d]["losses"] += 1
        except Exception:
            pass

    dow_labels = [dow_names[i] for i in range(7) if i in dow_data]
    dow_indices = [i for i in range(7) if i in dow_data]
    dow_wins = [dow_data[i]["wins"] for i in dow_indices]
    dow_losses = [dow_data[i]["losses"] for i in dow_indices]
    dow_wr = [round(dow_data[i]["wins"] / max(1, dow_data[i]["wins"] + dow_data[i]["losses"]) * 100, 1)
              for i in dow_indices]

    # Strategy log — decisions breakdown
    fired = sum(1 for r in strategy if r.get("decision") == "FIRED")
    rejected = sum(1 for r in strategy if r.get("decision") == "REJECTED")
    almost = sum(1 for r in strategy if r.get("decision") == "ALMOST")

    # Missed winners
    missed_wins = [r for r in strategy if r.get("result") == "WOULD_WIN"
                   and r.get("decision") in ("REJECTED", "ALMOST")]
    missed_losses = [r for r in strategy if r.get("result") == "WOULD_LOSE"
                     and r.get("decision") in ("REJECTED", "ALMOST")]

    # Top rejection reasons
    reject_reasons = defaultdict(int)
    for r in strategy:
        if r.get("decision") in ("REJECTED", "ALMOST"):
            reason = r.get("reject_reason", "unknown")
            if reason:
                reject_reasons[reason] += 1
    top_reasons = sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:8]
    reason_labels = [r[0][:40] for r in top_reasons]
    reason_counts = [r[1] for r in top_reasons]

    # Trade timeline (cumulative W/L over time)
    timeline_dates = []
    cum_wins = 0
    cum_losses = 0
    timeline_w = []
    timeline_l = []
    for r in sorted(closed, key=lambda x: x.get("timestamp", "")):
        ts = r.get("timestamp", "")[:10]
        if r.get("result") == "WIN":
            cum_wins += 1
        elif r.get("result") == "LOSS":
            cum_losses += 1
        timeline_dates.append(ts)
        timeline_w.append(cum_wins)
        timeline_l.append(cum_losses)

    # Cumulative PnL from sim history
    pnl_dates = []
    pnl_values = []
    sim_history_path = os.path.join(_BASE_DIR, "data", "sim_history.json")
    try:
        if os.path.exists(sim_history_path):
            with open(sim_history_path) as f:
                sim_hist = json.load(f)
            cum_pnl = 0
            for day in sorted(sim_hist, key=lambda x: x.get("date", "")):
                cum_pnl += day.get("pnl", 0)
                pnl_dates.append(day.get("date", "")[-5:])
                pnl_values.append(round(cum_pnl, 2))
    except Exception:
        pass

    # Win streak tracking
    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    current_loss_streak = 0
    for r in sorted(closed, key=lambda x: x.get("timestamp", "")):
        if r.get("result") == "WIN":
            current_streak += 1
            current_loss_streak = 0
            max_win_streak = max(max_win_streak, current_streak)
        elif r.get("result") == "LOSS":
            current_loss_streak += 1
            current_streak = 0
            max_loss_streak = max(max_loss_streak, current_loss_streak)

    period_label = f"Last {days} days" if days > 0 else "All time"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NQ CALLS Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #0d1117;
        color: #c9d1d9;
        padding: 20px;
    }}
    .header {{
        text-align: center;
        padding: 30px 0;
        border-bottom: 1px solid #21262d;
        margin-bottom: 30px;
    }}
    .header h1 {{
        font-size: 2em;
        color: #58a6ff;
        margin-bottom: 8px;
    }}
    .header .subtitle {{
        color: #8b949e;
        font-size: 0.95em;
    }}
    .stats-row {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
        margin-bottom: 30px;
    }}
    .stat-card {{
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
    }}
    .stat-card .value {{
        font-size: 2em;
        font-weight: 700;
        color: #58a6ff;
    }}
    .stat-card .value.green {{ color: #3fb950; }}
    .stat-card .value.red {{ color: #f85149; }}
    .stat-card .value.yellow {{ color: #d29922; }}
    .stat-card .label {{
        font-size: 0.85em;
        color: #8b949e;
        margin-top: 4px;
    }}
    .charts-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(500px, 1fr));
        gap: 24px;
        margin-bottom: 30px;
    }}
    .chart-card {{
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 10px;
        padding: 20px;
    }}
    .chart-card h3 {{
        color: #58a6ff;
        margin-bottom: 15px;
        font-size: 1.1em;
    }}
    .chart-container {{
        position: relative;
        height: 300px;
    }}
    .full-width {{
        grid-column: 1 / -1;
    }}
    .insights {{
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 10px;
        padding: 24px;
        margin-bottom: 30px;
    }}
    .insights h3 {{
        color: #58a6ff;
        margin-bottom: 15px;
    }}
    .insight-item {{
        padding: 8px 0;
        border-bottom: 1px solid #21262d;
        display: flex;
        align-items: center;
        gap: 10px;
    }}
    .insight-item:last-child {{ border-bottom: none; }}
    .insight-icon {{ font-size: 1.2em; }}
    .footer {{
        text-align: center;
        padding: 20px;
        color: #484f58;
        font-size: 0.85em;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>NQ CALLS Dashboard</h1>
    <div class="subtitle">{period_label} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | {total} total alerts</div>
</div>

<div class="stats-row">
    <div class="stat-card">
        <div class="value {'green' if wr >= 55 else 'red' if wr < 45 else 'yellow'}">{wr}%</div>
        <div class="label">Win Rate</div>
    </div>
    <div class="stat-card">
        <div class="value green">{win_count}</div>
        <div class="label">Wins</div>
    </div>
    <div class="stat-card">
        <div class="value red">{loss_count}</div>
        <div class="label">Losses</div>
    </div>
    <div class="stat-card">
        <div class="value">{len(open_trades)}</div>
        <div class="label">Open Trades</div>
    </div>
    <div class="stat-card">
        <div class="value">{total}</div>
        <div class="label">Total Alerts</div>
    </div>
    <div class="stat-card">
        <div class="value green">{max_win_streak}</div>
        <div class="label">Best Win Streak</div>
    </div>
    <div class="stat-card">
        <div class="value red">{max_loss_streak}</div>
        <div class="label">Worst Loss Streak</div>
    </div>
    <div class="stat-card">
        <div class="value yellow">{len(missed_wins)}</div>
        <div class="label">Missed Winners</div>
    </div>
</div>

<div class="charts-grid">

    <div class="chart-card">
        <h3>Win Rate by Setup Type</h3>
        <div class="chart-container"><canvas id="setupChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Win Rate by Market</h3>
        <div class="chart-container"><canvas id="marketChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Win Rate by Hour (UTC)</h3>
        <div class="chart-container"><canvas id="hourChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Win Rate by Conviction Tier</h3>
        <div class="chart-container"><canvas id="tierChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Win Rate by Day of Week</h3>
        <div class="chart-container"><canvas id="dowChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Scan Decisions (Strategy Log)</h3>
        <div class="chart-container"><canvas id="decisionChart"></canvas></div>
    </div>

    <div class="chart-card full-width">
        <h3>Cumulative PnL Over Time</h3>
        <div class="chart-container"><canvas id="pnlChart"></canvas></div>
    </div>

    <div class="chart-card full-width">
        <h3>Cumulative Wins vs Losses Over Time</h3>
        <div class="chart-container"><canvas id="timelineChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Top Rejection Reasons</h3>
        <div class="chart-container"><canvas id="reasonChart"></canvas></div>
    </div>

    <div class="chart-card">
        <h3>Missed Opportunities</h3>
        <div class="chart-container"><canvas id="missedChart"></canvas></div>
    </div>

</div>

<div class="insights">
    <h3>Key Insights</h3>
    {"".join(f'<div class="insight-item"><span class="insight-icon">{icon}</span><span>{text}</span></div>' for icon, text in _generate_insights(wr, setup_data, market_data, hour_data, tier_data, missed_wins, missed_losses))}
</div>

<div class="footer">
    NQ CALLS Bot | Built with Claude | Dashboard auto-generated
</div>

<script>
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#21262d';

const barOptions = {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: true }} }},
    scales: {{
        y: {{ beginAtZero: true, grid: {{ color: '#21262d' }} }},
        x: {{ grid: {{ color: '#21262d' }} }}
    }}
}};

// Setup chart
new Chart(document.getElementById('setupChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(setup_labels)},
        datasets: [
            {{ label: 'Wins', data: {json.dumps(setup_wins)}, backgroundColor: '#3fb950' }},
            {{ label: 'Losses', data: {json.dumps(setup_losses)}, backgroundColor: '#f85149' }}
        ]
    }},
    options: barOptions
}});

// Market chart
new Chart(document.getElementById('marketChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(market_labels)},
        datasets: [
            {{ label: 'Wins', data: {json.dumps(market_wins)}, backgroundColor: '#3fb950' }},
            {{ label: 'Losses', data: {json.dumps(market_losses)}, backgroundColor: '#f85149' }}
        ]
    }},
    options: barOptions
}});

// Hour chart
new Chart(document.getElementById('hourChart'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(hour_str_labels)},
        datasets: [{{
            label: 'Win Rate %',
            data: {json.dumps(hour_wr)},
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.1)',
            fill: true,
            tension: 0.3
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ display: true }} }},
        scales: {{
            y: {{ beginAtZero: true, max: 100, grid: {{ color: '#21262d' }},
                  ticks: {{ callback: v => v + '%' }} }},
            x: {{ grid: {{ color: '#21262d' }} }}
        }}
    }}
}});

// Tier chart
new Chart(document.getElementById('tierChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(tier_labels)},
        datasets: [
            {{ label: 'Wins', data: {json.dumps(tier_wins)}, backgroundColor: '#3fb950' }},
            {{ label: 'Losses', data: {json.dumps(tier_losses)}, backgroundColor: '#f85149' }}
        ]
    }},
    options: barOptions
}});

// Day of week chart
new Chart(document.getElementById('dowChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(dow_labels)},
        datasets: [
            {{ label: 'Wins', data: {json.dumps(dow_wins)}, backgroundColor: '#3fb950' }},
            {{ label: 'Losses', data: {json.dumps(dow_losses)}, backgroundColor: '#f85149' }}
        ]
    }},
    options: barOptions
}});

// Decision pie chart
new Chart(document.getElementById('decisionChart'), {{
    type: 'doughnut',
    data: {{
        labels: ['Fired', 'Rejected', 'Almost'],
        datasets: [{{
            data: [{fired}, {rejected}, {almost}],
            backgroundColor: ['#3fb950', '#f85149', '#d29922']
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ position: 'bottom' }} }}
    }}
}});

// Timeline chart
new Chart(document.getElementById('timelineChart'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(timeline_dates)},
        datasets: [
            {{ label: 'Cumulative Wins', data: {json.dumps(timeline_w)}, borderColor: '#3fb950',
               backgroundColor: 'rgba(63,185,80,0.1)', fill: true }},
            {{ label: 'Cumulative Losses', data: {json.dumps(timeline_l)}, borderColor: '#f85149',
               backgroundColor: 'rgba(248,81,73,0.1)', fill: true }}
        ]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ display: true }} }},
        scales: {{
            y: {{ beginAtZero: true, grid: {{ color: '#21262d' }} }},
            x: {{ grid: {{ color: '#21262d' }},
                  ticks: {{ maxTicksLimit: 15 }} }}
        }}
    }}
}});

// Cumulative PnL chart
new Chart(document.getElementById('pnlChart'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(pnl_dates)},
        datasets: [{{
            label: 'Cumulative PnL ($)',
            data: {json.dumps(pnl_values)},
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.15)',
            fill: true,
            tension: 0.3,
            pointBackgroundColor: {json.dumps(pnl_values)}.map(v => v >= 0 ? '#3fb950' : '#f85149'),
            pointRadius: 4
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{ display: true }},
            tooltip: {{ callbacks: {{ label: ctx => '$' + ctx.parsed.y.toLocaleString() }} }}
        }},
        scales: {{
            y: {{ grid: {{ color: '#21262d' }}, ticks: {{ callback: v => '$' + v.toLocaleString() }} }},
            x: {{ grid: {{ color: '#21262d' }} }}
        }}
    }}
}});

// Rejection reasons chart
new Chart(document.getElementById('reasonChart'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(reason_labels)},
        datasets: [{{
            label: 'Rejections',
            data: {json.dumps(reason_counts)},
            backgroundColor: '#d29922'
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {{ legend: {{ display: true }} }},
        scales: {{
            y: {{ beginAtZero: true, grid: {{ color: '#21262d' }} }},
            x: {{ grid: {{ color: '#21262d' }} }}
        }}
    }}
}});

// Missed opportunities chart
new Chart(document.getElementById('missedChart'), {{
    type: 'doughnut',
    data: {{
        labels: ['Missed Winners', 'Missed Losers (good filter)', 'Unresolved'],
        datasets: [{{
            data: [{len(missed_wins)}, {len(missed_losses)}, {max(0, rejected + almost - len(missed_wins) - len(missed_losses))}],
            backgroundColor: ['#d29922', '#3fb950', '#484f58']
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ position: 'bottom' }} }}
    }}
}});
</script>
</body>
</html>"""

    return html


def _generate_insights(wr, setup_data, market_data, hour_data, tier_data,
                        missed_wins, missed_losses):
    """Generate insight items for the dashboard."""
    insights = []

    if wr >= 60:
        insights.append(("🟢", f"Win rate is strong at {wr}% — keep doing what you're doing"))
    elif wr >= 50:
        insights.append(("🟡", f"Win rate at {wr}% — room for improvement, review losing setups"))
    elif wr > 0:
        insights.append(("🔴", f"Win rate at {wr}% — needs attention, consider tightening filters"))

    # Best setup
    if setup_data:
        best = max(setup_data.items(),
                   key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
        bw, bl = best[1]["wins"], best[1]["losses"]
        bwr = round(bw / max(1, bw + bl) * 100, 1)
        if bw + bl >= 2:
            insights.append(("⭐", f"Best setup: {best[0]} ({bwr}% WR over {bw+bl} trades)"))

    # Worst setup
    if setup_data:
        worst_list = [(k, v) for k, v in setup_data.items() if v["wins"] + v["losses"] >= 2]
        if worst_list:
            worst = min(worst_list,
                        key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
            ww, wl = worst[1]["wins"], worst[1]["losses"]
            wwr = round(ww / max(1, ww + wl) * 100, 1)
            if wwr < 45:
                insights.append(("⚠️", f"Weakest setup: {worst[0]} ({wwr}% WR) — consider raising filters"))

    # Missed winners
    if missed_wins:
        insights.append(("💡", f"{len(missed_wins)} setups were rejected but would have won — review filter thresholds"))

    # Good filters
    if missed_losses:
        insights.append(("✅", f"{len(missed_losses)} rejected setups would have lost — filters working correctly"))

    # HIGH tier
    if "HIGH" in tier_data:
        hw = tier_data["HIGH"]["wins"]
        hl = tier_data["HIGH"]["losses"]
        hwr = round(hw / max(1, hw + hl) * 100, 1)
        if hw + hl >= 2:
            insights.append(("🔥", f"HIGH conviction trades: {hwr}% WR — {'trust these' if hwr >= 60 else 'needs review'}"))

    # Best hour
    if hour_data:
        best_h = max(hour_data.items(),
                     key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
        hw, hl = best_h[1]["wins"], best_h[1]["losses"]
        hwr = round(hw / max(1, hw + hl) * 100, 1)
        if hw + hl >= 2:
            insights.append(("🕐", f"Best hour: {best_h[0]:02d}:00 UTC ({hwr}% WR)"))

    if not insights:
        insights.append(("📊", "Not enough data yet — keep running the bot to build insights"))

    return insights


def main():
    parser = argparse.ArgumentParser(description="NQ CALLS Dashboard Generator")
    parser.add_argument("--days", type=int, default=0, help="Days to include (0=all)")
    parser.add_argument("--open", action="store_true", help="Auto-open in browser")
    args = parser.parse_args()

    print("Building dashboard...")

    html = build_dashboard(args.days)

    os.makedirs(os.path.dirname(DASHBOARD_OUT), exist_ok=True)
    with open(DASHBOARD_OUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard saved to: {DASHBOARD_OUT}")

    if args.open:
        webbrowser.open(f"file:///{DASHBOARD_OUT.replace(os.sep, '/')}")
        print("Opened in browser.")


if __name__ == "__main__":
    main()
