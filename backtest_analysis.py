"""
backtest_analysis.py - NQ CALLS 2026
======================================
Standalone analysis script.  Reads all trade data (live + archived),
calculates per-setup stats, Topstep eval simulation, and outputs a
report to data/backtest_report.txt + sends a Telegram summary.

Usage:
    python backtest_analysis.py
"""

import csv, json, os, glob
from datetime import datetime, timezone
from collections import defaultdict

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTCOMES    = os.path.join(BASE_DIR, "outcomes.csv")
ARCHIVE_DIR = os.path.join(BASE_DIR, "data", "archive")
REPORT_FILE = os.path.join(BASE_DIR, "data", "backtest_report.txt")

CSV_COLS = [
    "alert_id","timestamp","market","tf","setup","direction",
    "entry","stop","target","rr","method",
    "trend_score","conviction","tier","leverage","suggested_hold",
    "rsi","atr","adx","htf_bias","hour","vol_ratio","news_flag",
    "status","result","bars_to_resolution","exit_price","last_rescore_conviction",
    "partial_exit_done","session_id"
]


def _load_all_trades() -> list[dict]:
    """Load trades from outcomes.csv + all archive files."""
    rows = []
    # Live file
    if os.path.exists(OUTCOMES):
        with open(OUTCOMES, newline="", encoding="utf-8") as f:
            rows.extend(list(csv.DictReader(f)))
    # Archive files
    if os.path.exists(ARCHIVE_DIR):
        for path in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "outcomes_*.csv"))):
            with open(path, newline="", encoding="utf-8") as f:
                archive_rows = list(csv.DictReader(f))
            # Deduplicate by alert_id
            seen = {r["alert_id"] for r in rows}
            for r in archive_rows:
                if r.get("alert_id") not in seen:
                    rows.append(r)
                    seen.add(r["alert_id"])
    return rows


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def analyze():
    rows = _load_all_trades()
    closed = [r for r in rows if r.get("status") == "CLOSED" and r.get("result") in ("WIN", "LOSS")]

    if not closed:
        return "No closed trades to analyze."

    # ── Per-setup stats ──────────────────────────────────────────
    by_setup = defaultdict(lambda: {"wins": 0, "losses": 0, "rr_wins": [], "trades": []})
    for r in closed:
        key = f"{r.get('market')}:{r.get('setup')}"
        rr = _safe_float(r.get("rr"), 0)
        if r["result"] == "WIN":
            by_setup[key]["wins"] += 1
            by_setup[key]["rr_wins"].append(rr)
        else:
            by_setup[key]["losses"] += 1
        by_setup[key]["trades"].append(r)

    setup_stats = []
    for key, data in by_setup.items():
        w = data["wins"]
        l = data["losses"]
        total = w + l
        wr = round(w / max(1, total) * 100, 1)
        avg_rr_win = round(sum(data["rr_wins"]) / max(1, len(data["rr_wins"])), 2) if data["rr_wins"] else 0
        avg_loss = -1.0  # always lose 1R
        # EV per trade in R: (WR * avg_win_R) + ((1-WR) * -1R)
        ev_per_r = round((wr / 100) * avg_rr_win + (1 - wr / 100) * avg_loss, 3)
        setup_stats.append({
            "key": key,
            "total": total,
            "wins": w,
            "losses": l,
            "wr": wr,
            "avg_rr_win": avg_rr_win,
            "ev_per_r": ev_per_r,
        })

    setup_stats.sort(key=lambda x: x["ev_per_r"], reverse=True)

    # ── Topstep Eval Simulation ──────────────────────────────────
    # Group trades by session, simulate each session as a $50k eval
    by_session = defaultdict(list)
    for r in closed:
        sid = r.get("session_id", "unknown")
        by_session[sid].append(r)

    eval_results = []
    for sid in sorted(by_session.keys()):
        session_trades = by_session[sid]
        balance = 50_000.0
        peak = balance
        daily_pnl = 0.0
        busted = False
        bust_reason = ""

        for t in session_trades:
            rr = _safe_float(t.get("rr"), 2.0)
            # Estimate dollar P&L per trade: ~$40/pt MNQ, ~20pt stop avg
            # Simplified: 1R = $100 for MNQ sizing
            r_dollars = 100.0
            if t["result"] == "WIN":
                pnl = r_dollars * rr
            else:
                pnl = -r_dollars

            balance += pnl
            daily_pnl += pnl
            peak = max(peak, balance)

            # Check bust conditions
            trailing_dd = peak - balance
            if trailing_dd >= 2000:
                busted = True
                bust_reason = f"trailing DD ${trailing_dd:,.0f}"
                break
            if daily_pnl <= -1000:
                busted = True
                bust_reason = f"daily loss ${abs(daily_pnl):,.0f}"
                break

        profit = balance - 50_000
        passed = profit >= 3000 and not busted
        eval_results.append({
            "session": sid,
            "trades": len(session_trades),
            "profit": round(profit, 2),
            "busted": busted,
            "bust_reason": bust_reason,
            "passed": passed,
        })

    passed_count = sum(1 for e in eval_results if e["passed"])
    total_sessions = len(eval_results)

    # ── Best/Worst setups ────────────────────────────────────────
    qualified = [s for s in setup_stats if s["total"] >= 3]
    best_3 = qualified[:3]
    worst_3 = qualified[-3:] if len(qualified) >= 3 else qualified

    # Estimate P&L impact of suspending worst setups
    worst_keys = {s["key"] for s in worst_3 if s["ev_per_r"] < 0}
    saved_r = 0.0
    for s in setup_stats:
        if s["key"] in worst_keys:
            saved_r += abs(s["ev_per_r"]) * s["total"]

    # ── Build Report ─────────────────────────────────────────────
    lines = [
        "NQ CALLS BACKTEST ANALYSIS",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Total closed trades: {len(closed)}",
        f"Sessions analyzed: {total_sessions}",
        "=" * 60,
        "",
        "SETUP PERFORMANCE (sorted by EV)",
        "-" * 60,
        f"{'Setup':<35} {'Trades':>6} {'W/L':>8} {'WR%':>6} {'AvgRR':>6} {'EV/R':>7}",
        "-" * 60,
    ]
    for s in setup_stats:
        lines.append(
            f"{s['key']:<35} {s['total']:>6} "
            f"{s['wins']}W/{s['losses']}L{'':<2} "
            f"{s['wr']:>5}% {s['avg_rr_win']:>5} {s['ev_per_r']:>+7.3f}"
        )

    lines += [
        "",
        "=" * 60,
        "TOP 3 SETUPS",
        "-" * 60,
    ]
    for s in best_3:
        lines.append(f"  + {s['key']}: {s['wr']}% WR, EV {s['ev_per_r']:+.3f}R ({s['total']} trades)")

    lines += [
        "",
        "WORST 3 SETUPS",
        "-" * 60,
    ]
    for s in worst_3:
        lines.append(f"  - {s['key']}: {s['wr']}% WR, EV {s['ev_per_r']:+.3f}R ({s['total']} trades)")

    lines += [
        "",
        f"Estimated R saved by suspending worst setups: {saved_r:+.1f}R",
        "",
        "=" * 60,
        "TOPSTEP EVAL SIMULATION",
        f"($50k eval, $3k target, $2k trailing DD, $1k daily loss)",
        "-" * 60,
    ]
    for e in eval_results:
        status = "PASSED" if e["passed"] else ("BUSTED" if e["busted"] else "INCOMPLETE")
        detail = f" ({e['bust_reason']})" if e["busted"] else ""
        icon = "+" if e["profit"] >= 0 else ""
        lines.append(
            f"  {e['session']}: {e['trades']} trades, "
            f"{icon}${e['profit']:,.0f}, {status}{detail}"
        )
    lines += [
        "",
        f"Passed: {passed_count}/{total_sessions} sessions",
        f"Pass rate: {round(passed_count/max(1,total_sessions)*100,1)}%",
        "",
        "=" * 60,
        "END OF REPORT",
    ]

    report = "\n".join(lines)

    # Save report
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report saved to {REPORT_FILE}")

    # ── Telegram summary ─────────────────────────────────────────
    tg_lines = [
        "📊 *Backtest Analysis*",
        "━━━━━━━━━━━━━━━━━━",
        f"*Trades:* `{len(closed)}` across `{total_sessions}` sessions",
        "",
        "🔥 *Best Setups:*",
    ]
    for s in best_3:
        tg_lines.append(f"  🟢 `{s['key']}`: {s['wr']}% WR, EV {s['ev_per_r']:+.3f}R")

    tg_lines += ["", "💀 *Worst Setups:*"]
    for s in worst_3:
        tg_lines.append(f"  🔴 `{s['key']}`: {s['wr']}% WR, EV {s['ev_per_r']:+.3f}R")

    tg_lines += [
        "",
        f"📈 *Suspending worst saves est. {saved_r:+.1f}R*",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"*Topstep Eval Sim:* {passed_count}/{total_sessions} passed ({round(passed_count/max(1,total_sessions)*100)}%)",
    ]
    for e in eval_results:
        icon = "✅" if e["passed"] else ("❌" if e["busted"] else "🟡")
        tg_lines.append(f"  {icon} {e['session']}: ${e['profit']:+,.0f}")

    tg_lines.append("━━━━━━━━━━━━━━━━━━")
    tg_lines.append(f"_Full report: data/backtest_report.txt_")

    tg_msg = "\n".join(tg_lines)

    # Send to Telegram
    try:
        from config import TELEGRAM_TOKEN, CHAT_ID
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": tg_msg,
            "parse_mode": "Markdown",
        }, timeout=10)
        if resp.ok:
            print("Telegram summary sent!")
        else:
            print(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
            # Try plain text fallback
            clean = tg_msg.replace("*", "").replace("`", "").replace("_", "")
            resp2 = requests.post(url, json={
                "chat_id": CHAT_ID,
                "text": clean,
            }, timeout=10)
            if resp2.ok:
                print("Telegram sent (plain text fallback)")
            else:
                print(f"Plain text also failed: {resp2.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

    return report


if __name__ == "__main__":
    report = analyze()
    print()
    print(report)
