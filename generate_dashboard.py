"""
generate_dashboard.py - NQ CALLS 2026
======================================
Builds a self-contained HTML dashboard from the bot's data files.
Open the resulting `dashboard.html` in any browser — no server needed.

Sections:
  - Top toggle: Topstep Sim ↔ Crypto Sim
  - PnL equity curve (Chart.js, embedded)
  - Sim balance + today's PnL
  - By-setup win rate table
  - Recent alerts list
  - Suspended setups
  - 9k+ scan decisions summary (counts only, last 7 days)

Re-run anytime: `python generate_dashboard.py`
Then double-click dashboard.html to view.

For a live public URL: push dashboard.html to GitHub, enable Pages, done.
"""
import os
import json
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTCOMES = BASE_DIR / "outcomes.csv"
OUTPUT   = BASE_DIR / "dashboard.html"


def _safe_load_json(path: Path, default):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        print(f"  WARN: couldn't load {path.name}: {e}")
    return default


def _read_outcomes():
    if not OUTCOMES.exists():
        return []
    rows = []
    try:
        with open(OUTCOMES, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception as e:
        print(f"  WARN: couldn't read outcomes.csv: {e}")
    return rows


def _build_equity_curve(trades, starting_balance, market_filter=None):
    """
    Walk closed trades chronologically. For each WIN/LOSS, compute the cumulative
    sim balance using R-multiples × dollar-per-R approximation.
    Returns list of {time, balance} points.
    """
    points = [{"time": "Start", "balance": starting_balance}]
    balance = starting_balance

    closed = [t for t in trades
              if t.get("status") == "CLOSED"
              and t.get("result") in ("WIN", "LOSS")
              and (market_filter is None or t.get("market") in market_filter)]

    closed.sort(key=lambda r: r.get("timestamp", ""))

    for t in closed:
        try:
            entry = float(t.get("entry", 0) or 0)
            stop  = float(t.get("stop", 0) or 0)
            rr    = float(t.get("rr", 0) or 0)
            risk_pct = 1.5 / 100  # 1.5% account risk per trade
            risk_dollars = balance * risk_pct
            if t["result"] == "WIN":
                pnl = risk_dollars * rr
            else:
                pnl = -risk_dollars
            balance += pnl
            ts_short = t.get("timestamp", "")[:16].replace("T", " ")
            points.append({"time": ts_short, "balance": round(balance, 2)})
        except Exception:
            continue

    return points


def _by_setup_stats(trades):
    """Compute WR per market:setup, sorted by total fires."""
    stats = {}
    for t in trades:
        if t.get("status") != "CLOSED":
            continue
        if t.get("result") not in ("WIN", "LOSS"):
            continue
        key = f"{t.get('market', '?')}:{t.get('setup', '?')}"
        if key not in stats:
            stats[key] = {"wins": 0, "losses": 0, "total": 0}
        stats[key]["total"] += 1
        if t["result"] == "WIN":
            stats[key]["wins"] += 1
        else:
            stats[key]["losses"] += 1

    rows = []
    for key, s in stats.items():
        wr = round(s["wins"] / max(1, s["total"]) * 100, 1)
        rows.append({
            "setup": key,
            "fires": s["total"],
            "wins":  s["wins"],
            "losses": s["losses"],
            "wr":    wr,
        })
    rows.sort(key=lambda r: r["fires"], reverse=True)
    return rows


def _recent_alerts(trades, limit=20):
    """Last N alerts, newest first."""
    sorted_trades = sorted(trades, key=lambda r: r.get("timestamp", ""), reverse=True)
    out = []
    for t in sorted_trades[:limit]:
        out.append({
            "time":     t.get("timestamp", "")[:16].replace("T", " "),
            "market":   t.get("market", "?"),
            "setup":    t.get("setup", "?"),
            "tf":       t.get("tf", ""),
            "dir":      t.get("direction", ""),
            "entry":    t.get("entry", ""),
            "stop":     t.get("stop", ""),
            "target":   t.get("target", ""),
            "rr":       t.get("rr", ""),
            "conv":     t.get("conviction", ""),
            "tier":     t.get("tier", ""),
            "status":   t.get("status", ""),
            "result":   t.get("result", "") or "",
            "exit":     t.get("exit_price", "") or "",
        })
    return out


def main():
    print("Building dashboard...")

    # Load all data
    sim_state    = _safe_load_json(DATA_DIR / "sim_account.json", {})
    crypto_state = _safe_load_json(DATA_DIR / "crypto_sim.json", {})
    perf         = _safe_load_json(DATA_DIR / "setup_performance.json", {})
    suspended    = _safe_load_json(DATA_DIR / "suspended_setups.json", {})
    trades       = _read_outcomes()

    print(f"  Loaded {len(trades)} trades from outcomes.csv")
    print(f"  Topstep sim balance: ${sim_state.get('balance', 0):,.2f}")
    print(f"  Crypto sim balance:  ${crypto_state.get('balance', 0):,.2f}")

    # Build derived data
    topstep_curve = _build_equity_curve(trades, sim_state.get("starting_balance", 50000), {"NQ", "GC"})
    crypto_curve  = _build_equity_curve(trades, crypto_state.get("starting_balance", 1000), {"BTC", "SOL"})

    # Use the crypto sim's actual closed_trades for the crypto equity curve (more accurate)
    crypto_closed = crypto_state.get("closed_trades", [])
    if crypto_closed:
        crypto_curve = [{"time": "Start", "balance": crypto_state.get("starting_balance", 1000)}]
        running = crypto_state.get("starting_balance", 1000)
        crypto_closed_sorted = sorted(crypto_closed, key=lambda r: r.get("opened_at", ""))
        for c in crypto_closed_sorted:
            running += c.get("pnl_dollars", 0)
            crypto_curve.append({
                "time":    c.get("closed_at", "")[:16].replace("T", " "),
                "balance": round(running, 2),
            })

    setup_stats   = _by_setup_stats(trades)
    recent        = _recent_alerts(trades, limit=25)

    # All-time wins/losses
    closed_all = [t for t in trades if t.get("status") == "CLOSED" and t.get("result") in ("WIN","LOSS")]
    total_w    = sum(1 for t in closed_all if t["result"] == "WIN")
    total_l    = sum(1 for t in closed_all if t["result"] == "LOSS")
    total_wr   = round(total_w / max(1, total_w + total_l) * 100, 1)

    # Today
    today = datetime.now().strftime("%Y-%m-%d")
    today_closed = [t for t in closed_all if t.get("timestamp", "").startswith(today)]
    today_w = sum(1 for t in today_closed if t["result"] == "WIN")
    today_l = sum(1 for t in today_closed if t["result"] == "LOSS")
    today_wr = round(today_w / max(1, today_w + today_l) * 100, 1) if today_closed else 0

    # Build payload that the HTML reads
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "topstep": {
            "balance":          sim_state.get("balance", 50000),
            "starting_balance": sim_state.get("starting_balance", 50000),
            "today_pnl":        sim_state.get("today_pnl", 0),
            "total_pnl":        sim_state.get("total_pnl", 0),
            "max_drawdown":     sim_state.get("max_drawdown", 2000),
            "daily_loss_limit": sim_state.get("daily_loss_limit", 1000),
            "profit_target":    sim_state.get("profit_target", 3000),
            "open_count":       len(sim_state.get("open_sim_trades", [])),
            "equity_curve":     topstep_curve,
        },
        "crypto": {
            "balance":          crypto_state.get("balance", 1000),
            "starting_balance": crypto_state.get("starting_balance", 1000),
            "total_pnl":        crypto_state.get("total_pnl", 0),
            "open_count":       len(crypto_state.get("open_trades", [])),
            "closed_count":     len(crypto_state.get("closed_trades", [])),
            "equity_curve":     crypto_curve,
        },
        "all_time": {
            "wins": total_w, "losses": total_l, "wr": total_wr,
            "total_trades": len(closed_all),
        },
        "today": {
            "wins": today_w, "losses": today_l, "wr": today_wr,
            "total_trades": len(today_closed),
        },
        "setup_stats":      setup_stats,
        "recent_alerts":    recent,
        "suspended_setups": list(suspended.keys()),
    }

    # Pretty-print as JS const
    data_json = json.dumps(payload, indent=2, default=str)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NQ CALLS Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 16px;
    line-height: 1.5;
  }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
    padding: 16px;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
  }}
  h1 {{ font-size: 24px; color: #58a6ff; }}
  .gen-time {{ font-size: 12px; color: #8b949e; }}
  .toggle {{
    display: flex;
    gap: 4px;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 4px;
  }}
  .toggle button {{
    background: transparent;
    border: none;
    color: #c9d1d9;
    padding: 8px 16px;
    cursor: pointer;
    border-radius: 4px;
    font-weight: 600;
    font-size: 14px;
    transition: all 0.15s;
  }}
  .toggle button.active {{
    background: #1f6feb;
    color: #fff;
  }}
  .toggle button:hover:not(.active) {{ background: #21262d; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}
  .card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 20px;
  }}
  .card h2 {{ font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; font-weight: 600; }}
  .stat-value {{ font-size: 28px; font-weight: 700; color: #58a6ff; }}
  .stat-sub {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}
  .pos {{ color: #56d364; }}
  .neg {{ color: #f85149; }}
  .neutral {{ color: #c9d1d9; }}
  .chart-card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 24px;
    height: 380px;
  }}
  .section-title {{
    font-size: 18px;
    font-weight: 600;
    margin: 24px 0 12px;
    color: #c9d1d9;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  th, td {{
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid #21262d;
  }}
  th {{
    background: #0d1117;
    color: #8b949e;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.5px;
  }}
  tr:hover {{ background: #1c2128; }}
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
  }}
  .badge-win {{ background: #1f6feb33; color: #58a6ff; }}
  .badge-loss {{ background: #f8514933; color: #f85149; }}
  .badge-open {{ background: #d2992233; color: #d29922; }}
  .badge-skip {{ background: #6e768133; color: #8b949e; }}
  .susp-list {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .susp-tag {{
    background: #f8514933;
    color: #f85149;
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
  }}
  .footer {{
    text-align: center;
    padding: 24px;
    color: #8b949e;
    font-size: 12px;
  }}
  .refresh-btn {{
    background: #1f6feb;
    color: #fff;
    border: none;
    padding: 8px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
  }}
  .refresh-btn:hover {{ background: #388bfd; }}
</style>
</head>
<body>
<div class="container">

<header>
  <div>
    <h1>📊 NQ CALLS Dashboard</h1>
    <div class="gen-time" id="gen-time"></div>
  </div>
  <div class="toggle">
    <button class="active" onclick="setView('topstep')">Topstep Sim</button>
    <button onclick="setView('crypto')">Crypto Sim</button>
  </div>
</header>

<div class="grid">
  <div class="card">
    <h2>Sim Balance</h2>
    <div class="stat-value" id="sim-balance">$0</div>
    <div class="stat-sub" id="sim-balance-sub">Starting: $0</div>
  </div>
  <div class="card">
    <h2>Total P&L</h2>
    <div class="stat-value" id="sim-pnl">$0</div>
    <div class="stat-sub" id="sim-pnl-sub">All time</div>
  </div>
  <div class="card">
    <h2>Today</h2>
    <div class="stat-value" id="today-stat">0W / 0L</div>
    <div class="stat-sub" id="today-sub">0% WR</div>
  </div>
  <div class="card">
    <h2>All-Time WR</h2>
    <div class="stat-value" id="alltime-stat">0%</div>
    <div class="stat-sub" id="alltime-sub">0W / 0L</div>
  </div>
  <div class="card">
    <h2>Open Trades</h2>
    <div class="stat-value" id="open-count">0</div>
    <div class="stat-sub">Currently in market</div>
  </div>
  <div class="card">
    <h2>Total Closed</h2>
    <div class="stat-value" id="closed-count">0</div>
    <div class="stat-sub">All-time</div>
  </div>
</div>

<div class="chart-card">
  <h2 style="margin-bottom: 12px;">Equity Curve</h2>
  <canvas id="equity-chart" style="max-height: 320px;"></canvas>
</div>

<div class="section-title">Suspended Setups</div>
<div class="card">
  <div class="susp-list" id="suspended-list"></div>
</div>

<div class="section-title">By-Setup Performance</div>
<div class="card" style="overflow-x: auto;">
  <table>
    <thead>
      <tr>
        <th>Setup</th>
        <th>Fires</th>
        <th>Wins</th>
        <th>Losses</th>
        <th>Win Rate</th>
      </tr>
    </thead>
    <tbody id="setup-table"></tbody>
  </table>
</div>

<div class="section-title">Recent Alerts (last 25)</div>
<div class="card" style="overflow-x: auto;">
  <table>
    <thead>
      <tr>
        <th>Time</th>
        <th>Market</th>
        <th>Setup</th>
        <th>TF</th>
        <th>Dir</th>
        <th>Conv</th>
        <th>Tier</th>
        <th>Entry</th>
        <th>Target</th>
        <th>RR</th>
        <th>Status</th>
        <th>Result</th>
      </tr>
    </thead>
    <tbody id="alerts-table"></tbody>
  </table>
</div>

<div class="footer">
  <p>To see fresh data, re-run <code>python generate_dashboard.py</code></p>
  <p style="margin-top: 8px;">NQ CALLS · self-improving trading bot · 2026</p>
</div>

</div>

<script>
const DATA = {data_json};

function fmtMoney(n) {{
  if (n >= 0) return '$' + n.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
  return '-$' + Math.abs(n).toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
}}

function colorClass(n) {{
  if (n > 0) return 'pos';
  if (n < 0) return 'neg';
  return 'neutral';
}}

let chart = null;

function setView(which) {{
  // Toggle button highlighting
  document.querySelectorAll('.toggle button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');

  const data = DATA[which];

  // Top stats
  document.getElementById('sim-balance').textContent = fmtMoney(data.balance);
  document.getElementById('sim-balance').className = 'stat-value';
  document.getElementById('sim-balance-sub').textContent = 'Starting: ' + fmtMoney(data.starting_balance);

  const pnl = data.total_pnl || 0;
  const pnlEl = document.getElementById('sim-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + fmtMoney(pnl);
  pnlEl.className = 'stat-value ' + colorClass(pnl);
  document.getElementById('sim-pnl-sub').textContent = 'All time';

  document.getElementById('open-count').textContent = data.open_count;
  document.getElementById('closed-count').textContent = data.closed_count !== undefined ? data.closed_count : DATA.all_time.total_trades;

  // Today/All-time always shown across both
  document.getElementById('today-stat').textContent = `${{DATA.today.wins}}W / ${{DATA.today.losses}}L`;
  document.getElementById('today-sub').textContent = `${{DATA.today.wr}}% WR`;
  document.getElementById('alltime-stat').textContent = `${{DATA.all_time.wr}}%`;
  document.getElementById('alltime-sub').textContent = `${{DATA.all_time.wins}}W / ${{DATA.all_time.losses}}L`;

  // Render equity chart
  renderChart(data.equity_curve, which);
}}

function renderChart(points, label) {{
  const ctx = document.getElementById('equity-chart').getContext('2d');
  if (chart) chart.destroy();

  chart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: points.map(p => p.time),
      datasets: [{{
        label: label === 'topstep' ? 'Topstep $50K Sim' : 'Crypto $1K Build-Up',
        data: points.map(p => p.balance),
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88, 166, 255, 0.1)',
        fill: true,
        tension: 0.2,
        pointRadius: 3,
        pointHoverRadius: 6,
        borderWidth: 2,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ color: '#c9d1d9' }} }},
        tooltip: {{
          callbacks: {{
            label: (ctx) => fmtMoney(ctx.parsed.y),
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e', maxRotation: 0, autoSkip: true }}, grid: {{ color: '#21262d' }} }},
        y: {{
          ticks: {{ color: '#8b949e', callback: (v) => fmtMoney(v) }},
          grid: {{ color: '#21262d' }}
        }}
      }}
    }}
  }});
}}

function renderSuspended() {{
  const list = document.getElementById('suspended-list');
  if (!DATA.suspended_setups.length) {{
    list.innerHTML = '<div style="color: #56d364;">✅ No setups currently suspended</div>';
    return;
  }}
  list.innerHTML = DATA.suspended_setups.map(s => `<span class="susp-tag">⛔ ${{s}}</span>`).join('');
}}

function renderSetups() {{
  const tbody = document.getElementById('setup-table');
  tbody.innerHTML = DATA.setup_stats.map(s => {{
    const wrClass = s.wr >= 60 ? 'pos' : (s.wr < 40 ? 'neg' : 'neutral');
    return `<tr>
      <td>${{s.setup}}</td>
      <td>${{s.fires}}</td>
      <td class="pos">${{s.wins}}</td>
      <td class="neg">${{s.losses}}</td>
      <td class="${{wrClass}}"><strong>${{s.wr}}%</strong></td>
    </tr>`;
  }}).join('') || '<tr><td colspan="5" style="text-align:center; color:#8b949e;">No closed trades yet.</td></tr>';
}}

function renderAlerts() {{
  const tbody = document.getElementById('alerts-table');
  tbody.innerHTML = DATA.recent_alerts.map(a => {{
    let badge = '';
    if (a.result === 'WIN') badge = '<span class="badge badge-win">WIN</span>';
    else if (a.result === 'LOSS') badge = '<span class="badge badge-loss">LOSS</span>';
    else if (a.result === 'SKIP') badge = '<span class="badge badge-skip">SKIP</span>';
    else if (a.status === 'OPEN') badge = '<span class="badge badge-open">OPEN</span>';
    else badge = a.result || a.status;

    return `<tr>
      <td>${{a.time}}</td>
      <td><strong>${{a.market}}</strong></td>
      <td>${{a.setup}}</td>
      <td>${{a.tf}}</td>
      <td>${{a.dir}}</td>
      <td>${{a.conv}}</td>
      <td>${{a.tier}}</td>
      <td>${{a.entry}}</td>
      <td>${{a.target}}</td>
      <td>${{a.rr}}</td>
      <td>${{a.status}}</td>
      <td>${{badge}}</td>
    </tr>`;
  }}).join('') || '<tr><td colspan="12" style="text-align:center; color:#8b949e;">No alerts yet.</td></tr>';
}}

// Init
document.getElementById('gen-time').textContent = 'Generated ' + new Date(DATA.generated_at).toLocaleString();
renderSuspended();
renderSetups();
renderAlerts();
setView.bind(null, 'topstep')();
// Default to Topstep view
(function() {{
  // Simulate the click for initial render
  const data = DATA.topstep;
  document.getElementById('sim-balance').textContent = fmtMoney(data.balance);
  document.getElementById('sim-balance-sub').textContent = 'Starting: ' + fmtMoney(data.starting_balance);
  const pnl = data.total_pnl || 0;
  const pnlEl = document.getElementById('sim-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + fmtMoney(pnl);
  pnlEl.className = 'stat-value ' + colorClass(pnl);
  document.getElementById('open-count').textContent = data.open_count;
  document.getElementById('closed-count').textContent = data.closed_count !== undefined ? data.closed_count : DATA.all_time.total_trades;
  document.getElementById('today-stat').textContent = `${{DATA.today.wins}}W / ${{DATA.today.losses}}L`;
  document.getElementById('today-sub').textContent = `${{DATA.today.wr}}% WR`;
  document.getElementById('alltime-stat').textContent = `${{DATA.all_time.wr}}%`;
  document.getElementById('alltime-sub').textContent = `${{DATA.all_time.wins}}W / ${{DATA.all_time.losses}}L`;
  renderChart(data.equity_curve, 'topstep');
}})();
</script>

</body>
</html>"""

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"\nDashboard built: {OUTPUT}")
    print(f"  Size: {OUTPUT.stat().st_size / 1024:.1f} KB")
    print(f"  Open in browser: file://{OUTPUT.resolve()}")
    print(f"\nTip: Re-run this script anytime to refresh the data.")


if __name__ == "__main__":
    main()
