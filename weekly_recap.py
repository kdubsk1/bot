"""
weekly_recap.py — Weekly rollup of real + shadow outcomes
==========================================================
Created: 2026-04-21 (Pre-Batch Follow-up Part B)

Aggregates 7 days of:
  - Real fired trades (outcomes.csv)
  - Shadow-tracked signals (strategy_log.csv with SHADOW_* / REJECTED_SUSPENDED)

Produces: data/weekly_recap_YYYY-MM-DD.md
(named with the Monday of the week being reviewed — e.g. recap for the
week of April 14-20 is filed under weekly_recap_2026-04-14.md)

Called from bot.py scan_loop at ~8 AM ET every Monday if not already generated.
Also produces a condensed Telegram summary.
"""

from __future__ import annotations
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Tuple
import pandas as pd

_BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _BASE_DIR / "data"
_OUTCOMES = _BASE_DIR / "outcomes.csv"
_STRATLOG = _DATA_DIR / "strategy_log.csv"

SHADOW_TYPES = [
    "SHADOW_HALTED", "SHADOW_PROFIT_LOCK", "SHADOW_MAX_TRADES",
    "SHADOW_CORRELATION", "SHADOW_ZONE_LOCK", "SHADOW_FAMILY_CD",
    "SHADOW_MARKET_HALT", "SHADOW_COOLDOWN", "REJECTED_SUSPENDED",
]

GATE_LABELS = {
    "SHADOW_HALTED":         "2-consecutive-loss halt",
    "SHADOW_MARKET_HALT":    "3-loss per-market halt",
    "SHADOW_CORRELATION":    "BTC/SOL correlation lockout",
    "SHADOW_PROFIT_LOCK":    "+$150 profit lock",
    "SHADOW_MAX_TRADES":     "Max 3 daily trades cap",
    "SHADOW_COOLDOWN":       "Per-setup cooldown",
    "SHADOW_FAMILY_CD":      "Family cooldown after loss",
    "SHADOW_ZONE_LOCK":      "Loss zone lockout",
    "REJECTED_SUSPENDED":    "Suspended setup (outcome-tracked)",
}


def generate_weekly_recap(week_start: date) -> Tuple[Path, str]:
    """
    Build weekly recap for 7 days starting on week_start (inclusive).
    Returns (md_path, telegram_text). Never raises.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    md_path = _DATA_DIR / f"weekly_recap_{week_start.isoformat()}.md"
    week_end = week_start + timedelta(days=6)

    outcomes = _load_outcomes_range(week_start, week_end)
    stratlog = _load_stratlog_range(week_start, week_end)

    sections = [
        _build_header(week_start, week_end),
        _build_trade_summary(outcomes),
        _build_shadow_summary(stratlog),
        _build_suspension_review(stratlog),
        _build_gate_analysis(stratlog),
        _build_weekly_conclusions(outcomes, stratlog),
    ]

    md = "\n\n".join(sections)
    md_path.write_text(md, encoding="utf-8")

    tg = _build_telegram_summary(week_start, week_end, outcomes, stratlog)
    return md_path, tg


# ─── Data loaders (defensive, never raise) ─────────────────────────

def _load_outcomes_range(start: date, end: date) -> pd.DataFrame:
    if not _OUTCOMES.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_OUTCOMES)
        if "timestamp" in df.columns:
            df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df["et_date"] = df["ts"].dt.tz_convert("America/New_York").dt.date
            return df[(df["et_date"] >= start) & (df["et_date"] <= end)].copy()
        return df
    except Exception:
        return pd.DataFrame()


def _load_stratlog_range(start: date, end: date) -> pd.DataFrame:
    if not _STRATLOG.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_STRATLOG)
        if "timestamp" in df.columns:
            df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df["et_date"] = df["ts"].dt.tz_convert("America/New_York").dt.date
            return df[(df["et_date"] >= start) & (df["et_date"] <= end)].copy()
        return df
    except Exception:
        return pd.DataFrame()


# ─── Section builders ───────────────────────────────────────────────

def _build_header(start: date, end: date) -> str:
    return f"""# 📅 NQ CALLS Weekly Recap
## {start.strftime('%b %d')} — {end.strftime('%b %d, %Y')}

**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ET

This recap reviews 7 days of real trades AND shadow-tracked signals
(signals where gates/suspension would have blocked — we track their
outcomes to decide which gates, if any, deserve to come back).

---"""


def _build_trade_summary(outcomes: pd.DataFrame) -> str:
    if outcomes.empty:
        return "## 📊 Real Trades This Week\n\n_No trades fired this week._"

    closed = outcomes[outcomes.get("status", "") == "CLOSED"]
    if closed.empty:
        return "## 📊 Real Trades This Week\n\n_No trades closed this week._"

    wins = len(closed[closed.get("result", "") == "WIN"])
    losses = len(closed[closed.get("result", "") == "LOSS"])
    total = wins + losses
    wr = round(wins / max(1, total) * 100, 1) if total > 0 else 0

    lines = [
        "## 📊 Real Trades This Week",
        "",
        f"- **Closed trades:** {total}  |  **WR:** {wr}%",
        f"- **Wins:** {wins}  **Losses:** {losses}",
        "",
        "### By market",
        "",
        "| Market | Trades | Wins | Losses | WR% |",
        "|---|---|---|---|---|",
    ]
    for mkt, g in closed.groupby("market"):
        w = len(g[g.get("result", "") == "WIN"])
        l = len(g[g.get("result", "") == "LOSS"])
        t = w + l
        mwr = round(w / max(1, t) * 100, 1) if t > 0 else 0
        lines.append(f"| {mkt} | {t} | {w} | {l} | {mwr}% |")
    return "\n".join(lines)


def _build_shadow_summary(stratlog: pd.DataFrame) -> str:
    if stratlog.empty:
        return "## 👻 Shadow Events This Week\n\n_No strategy log data._"

    shadow = stratlog[stratlog.get("decision", "").isin(SHADOW_TYPES)]
    if shadow.empty:
        return "## 👻 Shadow Events This Week\n\n_No shadow events this week._"

    lines = [
        "## 👻 Shadow Events This Week",
        "",
        f"**Total shadow events:** {len(shadow)}",
        "_(Signals where old gates WOULD have blocked. We fired anyway and tracked outcomes.)_",
        "",
        "### By gate type",
        "",
        "| Gate | Count | Would-Win | Would-Lose | Would-WR% | Verdict |",
        "|---|---|---|---|---|---|",
    ]

    for gate in SHADOW_TYPES:
        subset = shadow[shadow["decision"] == gate]
        if subset.empty:
            continue
        resolved = subset[subset.get("result", "").isin(["WOULD_WIN", "WOULD_LOSE", "WIN", "LOSS"])]
        wins = len(resolved[resolved.get("result", "").isin(["WOULD_WIN", "WIN"])])
        losses = len(resolved[resolved.get("result", "").isin(["WOULD_LOSE", "LOSS"])])
        total = wins + losses
        wr = round(wins / max(1, total) * 100, 1) if total > 0 else 0
        verdict = "—"
        if total >= 5:
            if wr < 40:
                verdict = "✅ gate was right (blocked losers)"
            elif wr >= 60:
                verdict = "❌ gate was WRONG (blocked winners)"
            else:
                verdict = "😐 gate neutral"
        label = GATE_LABELS.get(gate, gate)
        lines.append(
            f"| {label} | {len(subset)} | {wins} | {losses} | "
            f"{wr if total >= 3 else '—'}% | {verdict} |"
        )

    return "\n".join(lines)


def _build_suspension_review(stratlog: pd.DataFrame) -> str:
    if stratlog.empty:
        return "## 🚫 Suspended Setups Review\n\n_No data._"

    suspended = stratlog[stratlog.get("decision", "") == "REJECTED_SUSPENDED"]
    if suspended.empty:
        return "## 🚫 Suspended Setups Review\n\n_No suspended-setup events this week._"

    lines = [
        "## 🚫 Suspended Setups Review",
        "",
        "Suspended setups detected this week (not fired as alerts, but outcomes tracked):",
        "",
        "| Setup | Detected | Would-Win | Would-Lose | Would-WR% | Recommendation |",
        "|---|---|---|---|---|---|",
    ]

    suspended = suspended.copy()
    suspended["key"] = suspended["market"].astype(str) + ":" + suspended["setup_type"].astype(str)

    for key, g in suspended.groupby("key"):
        resolved = g[g.get("result", "").isin(["WOULD_WIN", "WOULD_LOSE"])]
        wins = len(resolved[resolved["result"] == "WOULD_WIN"])
        losses = len(resolved[resolved["result"] == "WOULD_LOSE"])
        total = wins + losses
        wr = round(wins / max(1, total) * 100, 1) if total > 0 else 0

        rec = "—"
        if total >= 10:
            if wr >= 60:
                rec = "🔥 CONSIDER UNSUSPENDING — proving itself"
            elif wr < 30:
                rec = "✅ Keep suspended — still bad"
            else:
                rec = "😐 Keep watching — inconclusive"
        elif total >= 5:
            if wr >= 70:
                rec = "🟡 Encouraging — watch for more data"
            else:
                rec = "⏳ Not enough data yet"

        lines.append(f"| {key} | {len(g)} | {wins} | {losses} | "
                     f"{wr if total >= 3 else '—'}% | {rec} |")

    return "\n".join(lines)


def _build_gate_analysis(stratlog: pd.DataFrame) -> str:
    """
    Which gates are worth re-adding? Which are dead weight?
    A gate 'had value' if signals it would have blocked went on to LOSE.
    A gate was 'wrong' if signals it would have blocked went on to WIN.
    """
    if stratlog.empty:
        return "## ⚖️ Gate Value Analysis\n\n_No data._"

    shadow = stratlog[stratlog.get("decision", "").isin(SHADOW_TYPES)]
    if shadow.empty:
        return "## ⚖️ Gate Value Analysis\n\n_No shadow data this week._"

    lines = [
        "## ⚖️ Gate Value Analysis",
        "",
        "For each removed gate: when it would have blocked, did the signal actually lose?",
        "- **Low would-WR%** → gate had value (would have saved us from losers)",
        "- **High would-WR%** → gate was wrong (would have blocked winners)",
        "",
    ]

    candidates_to_readd = []
    candidates_to_drop = []
    insufficient = []

    # Exclude REJECTED_SUSPENDED from gate analysis (it's a suspension, not a gate)
    for gate, label in GATE_LABELS.items():
        if gate == "REJECTED_SUSPENDED":
            continue
        subset = shadow[shadow["decision"] == gate]
        if subset.empty:
            continue
        resolved = subset[subset.get("result", "").isin(["WOULD_WIN", "WOULD_LOSE", "WIN", "LOSS"])]
        wins = len(resolved[resolved.get("result", "").isin(["WOULD_WIN", "WIN"])])
        losses = len(resolved[resolved.get("result", "").isin(["WOULD_LOSE", "LOSS"])])
        total = wins + losses
        if total < 5:
            insufficient.append((label, len(subset), total))
            continue
        wr = round(wins / max(1, total) * 100, 1)
        if wr < 40:
            candidates_to_readd.append((label, wins, losses, wr))
        elif wr >= 55:
            candidates_to_drop.append((label, wins, losses, wr))

    if candidates_to_readd:
        lines.append("### 🟢 Gates worth re-adding (blocked losers)")
        lines.append("")
        for label, w, l, wr in candidates_to_readd:
            lines.append(f"- **{label}:** {w}W / {l}L at {wr}% WR → if blocked, would have saved losses.")
        lines.append("")

    if candidates_to_drop:
        lines.append("### 🔴 Gates confirmed useless (blocked winners)")
        lines.append("")
        for label, w, l, wr in candidates_to_drop:
            lines.append(f"- **{label}:** {w}W / {l}L at {wr}% WR → blocking these would have hurt.")
        lines.append("")

    if insufficient:
        lines.append("### ⏳ Need more data")
        lines.append("")
        for label, count, resolved in insufficient:
            lines.append(f"- **{label}:** {count} events, only {resolved} resolved — keep watching.")
        lines.append("")

    if not candidates_to_readd and not candidates_to_drop and not insufficient:
        lines.append("_No shadow events at all this week — gates never came close to blocking anything._")

    return "\n".join(lines)


def _build_weekly_conclusions(outcomes, stratlog) -> str:
    lines = ["## 🎯 Weekly Conclusions", ""]

    if not outcomes.empty:
        closed = outcomes[outcomes.get("status", "") == "CLOSED"]
        if not closed.empty:
            wins = len(closed[closed.get("result", "") == "WIN"])
            losses = len(closed[closed.get("result", "") == "LOSS"])
            total = wins + losses
            if total >= 3:
                wr = wins / total * 100
                if wr < 30:
                    lines.append(f"- ⚠️ Real WR this week: {wr:.0f}% — significantly below break-even.")
                    lines.append("  Conviction score may not be predictive. Review signal quality.")
                elif wr < 45:
                    lines.append(f"- 😐 Real WR this week: {wr:.0f}% — needs 2.3:1+ R:R to be profitable.")
                else:
                    lines.append(f"- ✅ Real WR this week: {wr:.0f}% — profitable range with reasonable R:R.")

    if not stratlog.empty:
        shadow = stratlog[stratlog.get("decision", "").isin(SHADOW_TYPES)]
        if len(shadow) >= 10:
            lines.append(f"- 📊 {len(shadow)} shadow events this week — data forming for gate review.")

    if len(lines) == 2:
        lines.append("- Not enough data this week to form strong conclusions. Keep the scanner running.")

    return "\n".join(lines)


def _build_telegram_summary(start: date, end: date, outcomes, stratlog) -> str:
    closed = outcomes[outcomes.get("status", "") == "CLOSED"] if not outcomes.empty else outcomes
    wins = len(closed[closed.get("result", "") == "WIN"]) if not closed.empty else 0
    losses = len(closed[closed.get("result", "") == "LOSS"]) if not closed.empty else 0
    total = wins + losses
    wr = round(wins / max(1, total) * 100, 1) if total > 0 else 0

    shadow_count = 0
    if not stratlog.empty and "decision" in stratlog.columns:
        shadow_count = len(stratlog[stratlog["decision"].isin(SHADOW_TYPES)])

    lines = [
        f"📅 *Weekly Recap — {start.strftime('%b %d')} to {end.strftime('%b %d')}*",
        "━━━━━━━━━━━━━━━━━━",
        f"*Real trades:* {total} ({wins}W / {losses}L) — WR {wr}%",
        f"*Shadow events:* {shadow_count}",
        "━━━━━━━━━━━━━━━━━━",
        f"_Full recap: data/weekly_recap_{start.isoformat()}.md_",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # Dry run — generate recap for this week's Monday
    from datetime import date, timedelta
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    path, tg = generate_weekly_recap(monday)
    print(f"Weekly recap: {path}")
    print()
    print(tg)
