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

Wave 19 (May 9, 2026): Major polish for "professional website" feel.
  - Cleaner equity-curve timestamps (May 9, 2:30 PM vs raw ISO)
  - "Last updated X min ago" auto-updating badge in header
  - Per-market summary cards under main stats grid
  - Tier breakdown card
  - Empty-state messaging when no data
  - Mobile-responsive grid + refined spacing
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
# Apr 30: also write a copy into /docs so GitHub Pages can serve it.
# When you push, it becomes available at:
#   https://kdubsk1.github.io/bot/dashboard.html
# See GITHUB_PAGES_SETUP.md for one-time setup steps.
DOCS_OUTPUT = BASE_DIR / "docs" / "dashboard.html"


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

    # Wave 19: per-market and per-tier summaries
    by_market = {}
    by_tier = {}
    for t in trades:
        if t.get("status") != "CLOSED" or t.get("result") not in ("WIN", "LOSS"):
            continue
        m = t.get("market", "?")
        if m not in by_market:
            by_market[m] = {"wins": 0, "losses": 0, "total_pnl_r": 0.0}
        try:
            rr_val = float(t.get("rr", 0) or 0)
        except Exception:
            rr_val = 0.0
        if t["result"] == "WIN":
            by_market[m]["wins"] += 1
            by_market[m]["total_pnl_r"] += rr_val
        else:
            by_market[m]["losses"] += 1
            by_market[m]["total_pnl_r"] -= 1.0
        ti = t.get("tier", "?")
        if ti not in by_tier:
            by_tier[ti] = {"wins": 0, "losses": 0}
        if t["result"] == "WIN":
            by_tier[ti]["wins"] += 1
        else:
            by_tier[ti]["losses"] += 1
    for m in by_market:
        by_market[m]["total_pnl_r"] = round(by_market[m]["total_pnl_r"], 2)
        tot = by_market[m]["wins"] + by_market[m]["losses"]
        by_market[m]["wr"] = round(by_market[m]["wins"] / max(1, tot) * 100, 1) if tot else 0.0
    for ti in by_tier:
        tot = by_tier[ti]["wins"] + by_tier[ti]["losses"]
        by_tier[ti]["wr"] = round(by_tier[ti]["wins"] / max(1, tot) * 100, 1) if tot else 0.0


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
        # Wave 20 (May 9, 2026): pass full info including countdown for dashboard render
        "suspended_setups": [
            {
                "key": k,
                "reason": v.get("reason", ""),
                "suspended_at": v.get("suspended_at", ""),
            }
            for k, v in suspended.items()
        ],
        "by_market":        by_market,        # Wave 19
        "by_tier":          by_tier,          # Wave 19
    }

    # Pretty-print as JS const
    data_json = json.dumps(payload, indent=2, default=str)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NQ CALLS Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Auto-refresh every 60 seconds. Combined with auto_refresh_dashboard.py
     running locally (regenerates the file every 5 min), this keeps the
     dashboard live without needing a server. -->
<meta http-equiv="refresh" content="60">
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
  .gen-time {{ font-size: 12px; color: #8b949e; display: flex; align-items: center; gap: 6px; }}
  /* Wave 19: live indicator dot */
  .live-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #56d364;
    box-shadow: 0 0 8px #56d364;
    animation: pulse 2s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}
  /* Wave 19: market cards */
  .market-card {{ position: relative; }}
  .market-card .mkt-symbol {{ font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; font-weight: 700; }}
  .market-card .mkt-pnl {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
  .market-card .mkt-stats {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}
  .market-card .wr-bar {{ height: 4px; border-radius: 2px; background: #21262d; margin-top: 12px; overflow: hidden; }}
  .market-card .wr-fill {{ height: 100%; background: linear-gradient(90deg, #58a6ff, #56d364); transition: width 0.4s; }}
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
    <h1>📊 NQ CALLS Trading Bot</h1>
    <div class="gen-time"><span id="live-dot" class="live-dot"></span><span id="gen-time"></span></div>
  </div>
  <div class="toggle">
    <button class="active" onclick="setView('topstep')">Topstep $50K</button>
    <button onclick="setView('crypto')">Crypto $1K</button>
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

<!-- Wave 19: per-market summary cards -->
<div class="section-title">Performance by Market</div>
<div class="grid" id="market-grid"></div>

<!-- Wave 19: tier breakdown -->
<div class="section-title">Performance by Tier</div>
<div class="grid" id="tier-grid"></div>

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
        x: {{ ticks: {{ color: '#8b949e', maxRotation: 0, autoSkip: true, maxTicksLimit: 8,
                       callback: function(val, idx) {{
                         /* Wave 19: format '2026-05-09 14:30' -> 'May 9, 2:30p' */
                         const raw = this.getLabelForValue(val);
                         if (!raw || raw === 'Start') return raw;
                         try {{
                           const d = new Date(raw.replace(' ', 'T') + 'Z');
                           if (isNaN(d.getTime())) return raw;
                           const m = d.toLocaleString('en-US', {{ month: 'short', day: 'numeric' }});
                           const t = d.toLocaleString('en-US', {{ hour: 'numeric', minute: '2-digit' }}).replace(' ', '');
                           return m + ' ' + t;
                         }} catch (e) {{ return raw; }}
                       }}
        }}, grid: {{ color: '#21262d' }} }},
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
  // Wave 20: render objects with countdown to auto-unsuspend (14d)
  const AUTO_DAYS = 14;
  const now = new Date();
  list.innerHTML = DATA.suspended_setups.map(s => {{
    if (typeof s === 'string') return `<span class='susp-tag'>⛔ ${{s}}</span>`;
    const key = s.key || '?';
    const reason = s.reason || '';
    let countdown = '';
    if (s.suspended_at) {{
      try {{
        const sa = new Date(s.suspended_at);
        const daysIn = (now - sa) / (1000 * 60 * 60 * 24);
        const daysLeft = Math.max(0, AUTO_DAYS - Math.floor(daysIn));
        countdown = daysLeft > 0 ? ` <span style='opacity:0.7;'>(${{daysLeft}}d left)</span>` : ` <span style='color:#56d364;'>(eligible)</span>`;
      }} catch (e) {{ /* skip countdown */ }}
    }}
    const tooltip = reason ? ` title='${{reason.replace(/'/g, "&apos;")}}'` : '';
    return `<span class='susp-tag'${{tooltip}}>⛔ ${{key}}${{countdown}}</span>`;
  }}).join('');
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
// Wave 19: 'Last updated' that auto-updates every 30s
function updateLastUpdated() {{
  const t = new Date(DATA.generated_at);
  const now = new Date();
  const mins = Math.floor((now - t) / 60000);
  let label;
  if (mins < 1) label = 'Last updated just now';
  else if (mins < 60) label = 'Last updated ' + mins + ' min ago';
  else if (mins < 1440) label = 'Last updated ' + Math.floor(mins/60) + 'h ago';
  else label = 'Last updated ' + t.toLocaleDateString();
  const el = document.getElementById('gen-time');
  if (el) el.textContent = label;
  const dot = document.getElementById('live-dot');
  if (dot) {{
    if (mins > 30) {{ dot.style.background = '#f85149'; dot.style.boxShadow = '0 0 8px #f85149'; }}
    else if (mins > 10) {{ dot.style.background = '#d29922'; dot.style.boxShadow = '0 0 8px #d29922'; }}
  }}
}}
updateLastUpdated();
setInterval(updateLastUpdated, 30000);

// Wave 19: per-market cards
function renderMarketGrid() {{
  const grid = document.getElementById('market-grid');
  if (!grid) return;
  const markets = DATA.by_market || {{}};
  const order = ['NQ', 'GC', 'BTC', 'SOL'];
  const cards = [];
  for (const m of order) {{
    const d = markets[m];
    if (!d) continue;
    const total = d.wins + d.losses;
    if (total === 0) continue;
    const pnl = d.total_pnl_r;
    const pnlClass = pnl >= 0 ? 'pos' : 'neg';
    const pnlStr = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + 'R';
    const wrColor = d.wr >= 60 ? '#56d364' : d.wr >= 45 ? '#d29922' : '#f85149';
    cards.push(`<div class='card market-card'>
      <div class='mkt-symbol'>${{m}}</div>
      <div class='mkt-pnl ${{pnlClass}}'>${{pnlStr}}</div>
      <div class='mkt-stats'>${{d.wins}}W / ${{d.losses}}L · ${{d.wr}}% WR</div>
      <div class='wr-bar'><div class='wr-fill' style='width:${{d.wr}}%; background:${{wrColor}};'></div></div>
    </div>`);
  }}
  if (cards.length === 0) {{
    grid.innerHTML = '<div style="grid-column: 1/-1; color: #8b949e; text-align: center; padding: 20px;">No closed trades yet — bot is waiting.</div>';
  }} else {{
    grid.innerHTML = cards.join('');
  }}
}}

// Wave 19: tier cards
function renderTierGrid() {{
  const grid = document.getElementById('tier-grid');
  if (!grid) return;
  const tiers = DATA.by_tier || {{}};
  const order = ['HIGH', 'MEDIUM', 'LOW'];
  const tierIcons = {{ HIGH: '🔥', MEDIUM: '✅', LOW: '⚡' }};
  const cards = [];
  for (const t of order) {{
    const d = tiers[t];
    if (!d) continue;
    const total = d.wins + d.losses;
    if (total === 0) continue;
    const wrColor = d.wr >= 60 ? '#56d364' : d.wr >= 45 ? '#d29922' : '#f85149';
    cards.push(`<div class='card market-card'>
      <div class='mkt-symbol'>${{tierIcons[t] || ''}} ${{t}}</div>
      <div class='mkt-pnl' style='color:${{wrColor}};'>${{d.wr}}%</div>
      <div class='mkt-stats'>${{d.wins}}W / ${{d.losses}}L (${{total}} trades)</div>
      <div class='wr-bar'><div class='wr-fill' style='width:${{d.wr}}%; background:${{wrColor}};'></div></div>
    </div>`);
  }}
  if (cards.length === 0) {{
    grid.innerHTML = '<div style="grid-column: 1/-1; color: #8b949e; text-align: center; padding: 20px;">No tier data yet.</div>';
  }} else {{
    grid.innerHTML = cards.join('');
  }}
}}

renderSuspended();
renderSetups();
renderAlerts();
renderMarketGrid();
renderTierGrid();
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

    # Also write a copy to docs/ for GitHub Pages
    try:
        DOCS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        DOCS_OUTPUT.write_text(html, encoding="utf-8")
        print(f"  Also written to: {DOCS_OUTPUT}")
        print(f"  (push to GitHub for live URL — see GITHUB_PAGES_SETUP.md)")
    except Exception as e:
        print(f"  WARN: couldn't write docs copy ({e})")

    print(f"\nTip: Re-run this script anytime to refresh the data.")
    print(f"     Or run auto_refresh_dashboard.py for a 5-min refresh loop.")


if __name__ == "__main__":
    main()
