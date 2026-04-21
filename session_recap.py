"""
session_recap.py — Daily session recap for Wayne's NQ CALLS bot
================================================================
Created: 2026-04-20 (Pre-Batch)

Generates a session recap after each FUTURES_SESSION_CLOSE (4 PM ET).
Output:
  - data/recap_YYYY-MM-DD.md  (full local file, detailed)
  - Condensed text returned to caller for Telegram send

Sections in the .md file:
  1. Header (date, session type, bot uptime)
  2. Today's alerts fired (full table)
  3. Today's outcomes (W/L, R-multiples, PnL)
  4. Setup contribution (which setups fired most, won most)
  5. Sim account state (balance, today PnL, drawdown, target progress)
  6. Top filter rejection reasons (top 10)
  7. Shadow halt activity (how often halt would have fired, and did blocked signals win?)
  8. Anomalies (flagged patterns — tight stops, correlated fires, etc.)
  9. Open questions for tomorrow

All sections are defensively coded to handle empty/missing data without crashing.
"""

from __future__ import annotations
import os
import csv
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
import pandas as pd

_BASE_DIR   = Path(__file__).resolve().parent
_DATA_DIR   = _BASE_DIR / "data"
_OUTCOMES   = _BASE_DIR / "outcomes.csv"
_STRATLOG   = _DATA_DIR / "strategy_log.csv"
_SIM_FILE   = _DATA_DIR / "sim_account.json"


def generate_recap(session_date: date) -> Tuple[Path, str]:
    """
    Build the markdown recap for `session_date` and write it to
    data/recap_{session_date}.md. Returns (md_path, telegram_text).

    Never raises. On partial failure, produces a recap with "section unavailable"
    notes so the caller (bot.py _on_session_close) doesn't crash.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    md_path = _DATA_DIR / f"recap_{session_date.isoformat()}.md"

    # --- Load data defensively ---
    outcomes_today = _load_outcomes_for(session_date)
    stratlog_today = _load_stratlog_for(session_date)
    sim_state      = _load_sim_state()

    # --- Build sections ---
    header_md   = _build_header(session_date, stratlog_today)
    alerts_md   = _build_alerts_section(outcomes_today)
    outcomes_md = _build_outcomes_section(outcomes_today)
    setups_md   = _build_setup_contribution(outcomes_today)
    sim_md      = _build_sim_section(sim_state, outcomes_today)
    reject_md   = _build_rejection_section(stratlog_today)
    shadow_md   = _build_shadow_section(stratlog_today, outcomes_today)
    anomaly_md  = _build_anomaly_section(outcomes_today, stratlog_today)
    questions_md = _build_open_questions(outcomes_today, stratlog_today, anomaly_md)

    # --- Assemble markdown ---
    md = "\n\n".join([
        header_md,
        alerts_md,
        outcomes_md,
        setups_md,
        sim_md,
        reject_md,
        shadow_md,
        anomaly_md,
        questions_md,
    ])
    md_path.write_text(md, encoding="utf-8")

    # --- Build condensed Telegram version ---
    tg_text = _build_telegram_summary(
        session_date, outcomes_today, sim_state, stratlog_today, anomaly_md
    )

    return md_path, tg_text


# ----------------------------------------------------------------------------
# Data loaders (defensive, never raise)
# ----------------------------------------------------------------------------

def _load_outcomes_for(session_date: date) -> pd.DataFrame:
    if not _OUTCOMES.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_OUTCOMES)
        if "timestamp" in df.columns:
            df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            # Filter to ET session date — a trade at 20:30 UTC on 2026-04-19
            # counts toward the 2026-04-19 ET session if ET date matches
            df["et_date"] = df["ts"].dt.tz_convert("America/New_York").dt.date
            return df[df["et_date"] == session_date].copy()
        return df
    except Exception as e:
        _log(f"_load_outcomes_for failed: {e}")
        return pd.DataFrame()


def _load_stratlog_for(session_date: date) -> pd.DataFrame:
    if not _STRATLOG.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_STRATLOG)
        if "timestamp" in df.columns:
            df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df["et_date"] = df["ts"].dt.tz_convert("America/New_York").dt.date
            return df[df["et_date"] == session_date].copy()
        return df
    except Exception as e:
        _log(f"_load_stratlog_for failed: {e}")
        return pd.DataFrame()


def _load_sim_state() -> dict:
    if not _SIM_FILE.exists():
        return {}
    try:
        with open(_SIM_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        _log(f"_load_sim_state failed: {e}")
        return {}


# ----------------------------------------------------------------------------
# Section builders — each returns a markdown block
# ----------------------------------------------------------------------------

def _build_header(session_date: date, stratlog_today: pd.DataFrame) -> str:
    scan_count = len(stratlog_today[stratlog_today.get("decision", "") == "DETECTED"]) \
                 if not stratlog_today.empty else 0
    lines = [
        f"# 📅 NQ CALLS Session Recap — {session_date.strftime('%A, %B %d, %Y')}",
        f"",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ET",
        f"**Scans logged today:** {scan_count}",
        f"**Source files:** outcomes.csv, data/strategy_log.csv, data/sim_account.json",
        f"",
        f"---",
    ]
    return "\n".join(lines)


def _build_alerts_section(outcomes_today: pd.DataFrame) -> str:
    if outcomes_today.empty:
        return "## 🔔 Alerts Fired Today\n\n_No alerts fired today._"

    lines = ["## 🔔 Alerts Fired Today", ""]
    lines.append("| Time (ET) | Market | Setup | TF | Direction | Conv | Tier | Entry | Stop | Target | Status | Result |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")

    df = outcomes_today.sort_values("ts") if "ts" in outcomes_today.columns else outcomes_today
    for _, r in df.iterrows():
        try:
            ts_et = r["ts"].tz_convert("America/New_York").strftime("%H:%M") if "ts" in r else "?"
        except Exception:
            ts_et = "?"
        lines.append(
            f"| {ts_et} | {r.get('market','?')} | {r.get('setup','?')} | "
            f"{r.get('tf','?')} | {r.get('direction','?')} | {r.get('conviction','?')} | "
            f"{r.get('tier','?')} | {r.get('entry','?')} | {r.get('stop','?')} | "
            f"{r.get('target','?')} | {r.get('status','?')} | {r.get('result','') or '-'} |"
        )
    return "\n".join(lines)


def _build_outcomes_section(outcomes_today: pd.DataFrame) -> str:
    if outcomes_today.empty:
        return "## 📊 Outcomes Summary\n\n_No trades closed today._"

    closed = outcomes_today[outcomes_today.get("status", "") == "CLOSED"] \
             if not outcomes_today.empty else outcomes_today
    wins = closed[closed.get("result", "") == "WIN"] if not closed.empty else closed
    losses = closed[closed.get("result", "") == "LOSS"] if not closed.empty else closed
    skips = closed[closed.get("result", "") == "SKIP"] if not closed.empty else closed

    total_closed = len(wins) + len(losses)
    wr = round(len(wins) / max(1, total_closed) * 100, 1)

    # R-multiple calc (WIN = target-entry, LOSS = stop-entry, both abs, divided by initial risk)
    def _r_mult(row):
        try:
            entry = float(row.get("entry", 0))
            stop = float(row.get("stop", 0))
            exit_p = float(row.get("exit_price", 0))
            risk = abs(entry - stop)
            if risk == 0:
                return 0.0
            direction = str(row.get("direction", ""))
            if "LONG" in direction:
                return (exit_p - entry) / risk
            else:
                return (entry - exit_p) / risk
        except Exception:
            return 0.0

    if not closed.empty:
        closed = closed.copy()
        closed["r_mult"] = closed.apply(_r_mult, axis=1)
        avg_r = round(closed["r_mult"].mean(), 2)
        sum_r = round(closed["r_mult"].sum(), 2)
        expectancy = round(sum_r / max(1, total_closed), 2)
    else:
        avg_r = sum_r = expectancy = 0.0

    lines = [
        "## 📊 Outcomes Summary",
        "",
        f"- **Closed trades:** {total_closed} ({len(wins)}W / {len(losses)}L / {len(skips)} skipped)",
        f"- **Win rate:** {wr}%",
        f"- **Sum of R:** {sum_r:+.2f}R",
        f"- **Average R per trade:** {avg_r:+.2f}R",
        f"- **Expectancy (R per trade):** {expectancy:+.2f}R",
        "",
    ]
    if expectancy < 0:
        lines.append(f"> ⚠️ Expectancy is NEGATIVE. The bot lost on average per trade today.")
    elif expectancy > 0:
        lines.append(f"> ✅ Expectancy positive. The bot was net profitable per trade today.")
    return "\n".join(lines)


def _build_setup_contribution(outcomes_today: pd.DataFrame) -> str:
    if outcomes_today.empty:
        return "## 🎯 Setup Contribution\n\n_No setups fired today._"

    closed = outcomes_today[outcomes_today.get("status", "") == "CLOSED"].copy()
    if closed.empty:
        return "## 🎯 Setup Contribution\n\n_No setups closed today._"

    def _key(row):
        return f"{row.get('market','?')}:{row.get('setup','?')}"
    closed["key"] = closed.apply(_key, axis=1)

    grp = closed.groupby("key")
    lines = ["## 🎯 Setup Contribution", "",
             "| Setup | Fires | Wins | Losses | WR% | Notes |",
             "|---|---|---|---|---|---|"]
    for key, g in grp:
        wins = len(g[g.get("result", "") == "WIN"])
        losses = len(g[g.get("result", "") == "LOSS"])
        total = wins + losses
        wr = round(wins / max(1, total) * 100, 1) if total > 0 else 0
        flag = "⚠️ 2+ losses" if losses >= 2 else "🔥 positive" if wins >= 2 and wr >= 60 else ""
        lines.append(f"| {key} | {len(g)} | {wins} | {losses} | {wr}% | {flag} |")
    return "\n".join(lines)


def _build_sim_section(sim_state: dict, outcomes_today: pd.DataFrame) -> str:
    if not sim_state:
        return "## 💰 Sim Account\n\n_Sim state unavailable._"

    balance = sim_state.get("balance", 0)
    starting = sim_state.get("starting_balance", 50000)
    peak = sim_state.get("peak_balance", balance)
    today_pnl = sim_state.get("today_pnl", 0)
    total_pnl = sim_state.get("total_pnl", 0)
    daily_limit = sim_state.get("daily_loss_limit", 1000)
    max_dd = sim_state.get("max_drawdown", 2000)
    profit_target = sim_state.get("profit_target", 3000)

    drawdown = peak - balance
    dd_used_pct = round(drawdown / max(1, max_dd) * 100, 1)
    daily_used = abs(min(0, today_pnl))
    daily_used_pct = round(daily_used / max(1, daily_limit) * 100, 1)
    target_progress_pct = round(total_pnl / max(1, profit_target) * 100, 1)

    lines = [
        "## 💰 Sim Account (Topstep $50K Eval Simulation)",
        "",
        f"- **Balance:** ${balance:,.2f} (starting ${starting:,.0f}, peak ${peak:,.2f})",
        f"- **Today P&L:** ${today_pnl:+,.2f}",
        f"- **Total P&L (toward $3K target):** ${total_pnl:+,.2f} ({target_progress_pct:.1f}%)",
        f"- **Daily limit used:** ${daily_used:,.2f} of ${daily_limit:,.0f} ({daily_used_pct}%)",
        f"- **Drawdown:** ${drawdown:,.2f} of ${max_dd:,.0f} max ({dd_used_pct}% used)",
        "",
    ]
    return "\n".join(lines)


def _build_rejection_section(stratlog_today: pd.DataFrame) -> str:
    if stratlog_today.empty:
        return "## 🚧 Top Filter Rejections\n\n_No strategy log entries today._"

    rejected = stratlog_today[stratlog_today.get("decision", "").str.contains("REJECT", na=False)] \
               if "decision" in stratlog_today.columns else pd.DataFrame()
    if rejected.empty:
        return "## 🚧 Top Filter Rejections\n\n_No rejections logged today._"

    reason_col = "reject_reason" if "reject_reason" in rejected.columns else None
    if reason_col is None:
        return "## 🚧 Top Filter Rejections\n\n_Rejection reason column not found._"

    reason_counts = rejected[reason_col].value_counts().head(10)
    total_rej = len(rejected)

    lines = [
        "## 🚧 Top Filter Rejections (top 10)",
        "",
        f"**Total rejections today:** {total_rej}",
        "",
        "| Reason | Count | % of rejections |",
        "|---|---|---|",
    ]
    for reason, count in reason_counts.items():
        pct = round(count / max(1, total_rej) * 100, 1)
        reason_display = (reason[:80] + "...") if isinstance(reason, str) and len(reason) > 80 else reason
        lines.append(f"| {reason_display} | {count} | {pct}% |")
    return "\n".join(lines)


def _build_shadow_section(stratlog_today: pd.DataFrame, outcomes_today: pd.DataFrame) -> str:
    """
    Full shadow-tracking report.
    Shows every SHADOW_* and REJECTED_SUSPENDED row with:
      - Count fired in shadow
      - Of those resolved (WOULD_WIN / WOULD_LOSE): how many would have won vs lost
      - Would-WR% of resolved shadow signals
    Also breaks down setup-level shadows by market:setup for suspension review.
    """
    if stratlog_today.empty:
        return "## 👻 Shadow Tracking\n\n_No strategy log entries today._"

    # All shadow-type decisions we track
    shadow_decisions = [
        "SHADOW_HALTED", "SHADOW_PROFIT_LOCK", "SHADOW_MAX_TRADES",
        "SHADOW_CORRELATION", "SHADOW_ZONE_LOCK", "SHADOW_FAMILY_CD",
        "SHADOW_MARKET_HALT", "SHADOW_COOLDOWN", "REJECTED_SUSPENDED",
    ]

    shadow = stratlog_today[stratlog_today.get("decision", "").isin(shadow_decisions)]

    if shadow.empty:
        return "## 👻 Shadow Tracking\n\n_No shadow-logged events today. (All gates passed, no suspended setups detected.)_"

    lines = [
        "## 👻 Shadow Tracking",
        "",
        f"**Total shadow events today: {len(shadow)}**",
        "_(Signals where old gates OR suspension would have blocked — we let them fire/log for counterfactual data.)_",
        "",
        "### By decision type",
        "",
        "| Decision | Count | Resolved | Would-Win | Would-Lose | Would-WR% |",
        "|---|---|---|---|---|---|",
    ]

    for dec in shadow_decisions:
        subset = shadow[shadow["decision"] == dec]
        if subset.empty:
            continue
        # check_missed_setups() writes WOULD_WIN/WOULD_LOSE into the result column
        resolved = subset[subset.get("result", "").isin(["WOULD_WIN", "WOULD_LOSE", "WIN", "LOSS"])]
        wins = len(resolved[resolved.get("result", "").isin(["WOULD_WIN", "WIN"])])
        losses = len(resolved[resolved.get("result", "").isin(["WOULD_LOSE", "LOSS"])])
        total_resolved = wins + losses
        wr = round(wins / max(1, total_resolved) * 100, 1) if total_resolved > 0 else 0
        lines.append(
            f"| {dec} | {len(subset)} | {total_resolved} | {wins} | {losses} | "
            f"{wr if total_resolved >= 3 else '—'}% |"
        )

    # Setup-level shadows (exclude scan-level SHADOW_SCAN rows)
    lines.append("")
    lines.append("### Suspended & per-setup shadow outcomes (today)")
    lines.append("")

    setup_level = shadow[shadow.get("setup_type", "") != "SHADOW_SCAN"]
    if setup_level.empty:
        lines.append("_No setup-level shadow events today._")
    else:
        setup_level = setup_level.copy()
        setup_level["key"] = setup_level["market"].astype(str) + ":" + setup_level["setup_type"].astype(str)

        lines.append("| Setup | Fires | Would-Win | Would-Lose | Notes |")
        lines.append("|---|---|---|---|---|")

        for key, g in setup_level.groupby("key"):
            wins = len(g[g.get("result", "").isin(["WOULD_WIN", "WIN"])])
            losses = len(g[g.get("result", "").isin(["WOULD_LOSE", "LOSS"])])
            flag = ""
            if wins >= 2 and wins > losses * 2:
                flag = "🔥 proving itself"
            elif losses >= 3 and losses > wins * 2:
                flag = "⚠️ still bad"
            lines.append(f"| {key} | {len(g)} | {wins} | {losses} | {flag} |")

    lines.append("")
    lines.append("_Shadow outcomes (WOULD_WIN / WOULD_LOSE) are resolved by `check_missed_setups()` "
                 "using live candle HIGH/LOW ranges — same method as real trade outcomes._")

    return "\n".join(lines)


def _build_anomaly_section(outcomes_today: pd.DataFrame, stratlog_today: pd.DataFrame) -> str:
    anomalies = []

    if not outcomes_today.empty:
        closed = outcomes_today[outcomes_today.get("status", "") == "CLOSED"]
        # Anomaly 1: high conviction (≥90) that lost
        if not closed.empty and "conviction" in closed.columns:
            try:
                hi_conv_losses = closed[(closed["conviction"] >= 90) & (closed["result"] == "LOSS")]
                for _, r in hi_conv_losses.iterrows():
                    anomalies.append(
                        f"⚠️ **High-conviction loss:** {r.get('market','?')} {r.get('setup','?')} "
                        f"[{r.get('tf','?')}] {r.get('direction','?')} — conviction {r.get('conviction','?')}, lost."
                    )
            except Exception:
                pass

        # Anomaly 2: any setup that fired and lost 2+ times today
        try:
            closed_group = closed.assign(
                key=lambda d: d["market"].astype(str) + ":" + d["setup"].astype(str)
            ).groupby("key")
            for key, g in closed_group:
                losses = len(g[g["result"] == "LOSS"])
                if losses >= 2:
                    anomalies.append(f"⚠️ **Setup losing streak:** {key} had {losses} losses today.")
        except Exception:
            pass

        # Anomaly 3: tight stops (stop < 0.5% of entry) in crypto
        try:
            crypto = closed[closed.get("market", "").isin(["BTC", "SOL"])]
            for _, r in crypto.iterrows():
                entry = float(r.get("entry", 0))
                stop = float(r.get("stop", 0))
                if entry > 0 and abs(entry - stop) / entry < 0.005:  # stop < 0.5% away
                    pct = round(abs(entry - stop) / entry * 100, 2)
                    anomalies.append(
                        f"⚠️ **Very tight crypto stop:** {r.get('market','?')} {r.get('setup','?')} — "
                        f"stop was {pct}% from entry. Stop-out on normal noise is likely."
                    )
        except Exception:
            pass

    if not anomalies:
        return "## 🔍 Anomalies\n\n_No anomalies detected today._"

    lines = ["## 🔍 Anomalies", ""]
    for a in anomalies:
        lines.append(f"- {a}")
    return "\n".join(lines)


def _build_open_questions(outcomes_today, stratlog_today, anomaly_md: str) -> str:
    lines = ["## ❓ Open Questions for Tomorrow", ""]
    if "⚠️" in anomaly_md:
        lines.append("- Review anomalies above; are any setups candidates for suspension or kill?")
    if not outcomes_today.empty:
        closed = outcomes_today[outcomes_today.get("status", "") == "CLOSED"]
        if not closed.empty and len(closed) >= 2:
            wr = len(closed[closed.get("result", "") == "WIN"]) / len(closed) * 100
            if wr < 40:
                lines.append(f"- Win rate today was {wr:.0f}%. Check if filter calibration is correct.")
    if not stratlog_today.empty:
        shadow = stratlog_today[stratlog_today.get("decision", "") == "SHADOW_HALTED"]
        if len(shadow) >= 1:
            lines.append(f"- Halt would have blocked {len(shadow)} signals today. Track their outcomes.")
    if len(lines) == 2:
        lines.append("- No specific questions flagged.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Telegram summary (condensed, ≤3500 chars)
# ----------------------------------------------------------------------------

def _build_telegram_summary(
    session_date: date, outcomes_today, sim_state, stratlog_today, anomaly_md: str
) -> str:
    closed = outcomes_today[outcomes_today.get("status", "") == "CLOSED"] \
             if not outcomes_today.empty else outcomes_today
    wins = len(closed[closed.get("result", "") == "WIN"]) if not closed.empty else 0
    losses = len(closed[closed.get("result", "") == "LOSS"]) if not closed.empty else 0
    total = wins + losses
    wr = round(wins / max(1, total) * 100, 1) if total > 0 else 0

    today_pnl = sim_state.get("today_pnl", 0) if sim_state else 0
    balance = sim_state.get("balance", 0) if sim_state else 0

    # Pre-Batch Follow-up Part B 2026-04-21: count ALL shadow types + suspended
    shadow_types = [
        "SHADOW_HALTED", "SHADOW_PROFIT_LOCK", "SHADOW_MAX_TRADES",
        "SHADOW_CORRELATION", "SHADOW_ZONE_LOCK", "SHADOW_FAMILY_CD",
        "SHADOW_MARKET_HALT", "SHADOW_COOLDOWN", "REJECTED_SUSPENDED",
    ]
    shadow_count = 0
    if not stratlog_today.empty and "decision" in stratlog_today.columns:
        shadow_count = len(stratlog_today[stratlog_today["decision"].isin(shadow_types)])

    anomaly_count = anomaly_md.count("⚠️")

    lines = [
        f"📒 *Session Recap — {session_date.strftime('%Y-%m-%d')}*",
        "━━━━━━━━━━━━━━━━━━",
        f"*Trades:* {total} ({wins}W / {losses}L)  |  *WR:* {wr}%",
        f"*Sim P&L:* ${today_pnl:+,.2f}  |  *Balance:* ${balance:,.2f}",
    ]
    if shadow_count > 0:
        lines.append(f"*Shadow events:* {shadow_count} (old gates/suspension would have blocked)")
    if anomaly_count > 0:
        lines.append(f"⚠️ *{anomaly_count} anomalies* — see data/recap_{session_date}.md")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"_Full recap: data/recap_{session_date}.md_")

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _log(msg: str):
    """Minimal logger — session_recap must never crash the caller."""
    try:
        print(f"[session_recap] {msg}")
    except Exception:
        pass


if __name__ == "__main__":
    # Dry run — generate recap for today
    from datetime import date
    path, tg = generate_recap(date.today())
    print(f"Recap written to: {path}")
    print()
    print(tg)
