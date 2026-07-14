"""
strategy_log.py - NQ CALLS 2026
=================================
Records EVERY scan decision the bot makes.

Not just what it fired — everything:
- Setups it TOOK (fired as alerts)
- Setups it REJECTED (and exactly why)
- Setups it ALMOST TOOK (close but filtered out)
- What price did AFTER — so we can see if missed setups hit

This is the raw data that drives strategy discovery.
Every day we review this with Claude to find patterns we're missing.
"""

from __future__ import annotations
import os, json, csv
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
import safe_io  # data-loss fix: atomic writes + cross-process locks

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR    = os.path.join(_BASE_DIR, "data")
STRATEGY_LOG = os.path.join(_DATA_DIR, "strategy_log.csv")
CANDIDATE_FILE = os.path.join(_DATA_DIR, "strategy_candidates.txt")

os.makedirs(_DATA_DIR, exist_ok=True)

COLS = [
    # ── EXISTING — KEEP THIS ORDER EXACTLY ──
    "timestamp", "market", "tf", "setup_type", "direction",
    "price", "entry", "stop", "target", "rr",
    "conviction", "tier", "trend", "adx", "rsi", "vol_ratio",
    "htf_bias", "news_flag", "decision", "reject_reason",
    "result", "result_checked_at",
    # ── NEW: scoring transparency ──
    "score_breakdown",       # JSON dict of conviction factors and their points
    "confidence_factors",    # JSON dict: BB position, Stoch signal, MACD signal, etc.
    "detection_reason",      # Human-readable sentence explaining what the bot saw
    # ── NEW: indicator snapshot at decision time ──
    "atr", "vwap", "ema20", "ema50", "ema200", "ema21",
    "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
    "stoch_k", "stoch_d", "macd_line", "macd_signal", "macd_hist",
    # ── NEW: market context ──
    "close_price", "regime", "session_name",
    "swing_high_30", "swing_low_30", "volume_raw", "volume_20ma",
]

# ── Decision types ─────────────────────────────────────────────────
DECISION_FIRED            = "FIRED"              # alert sent
DECISION_REJECTED         = "REJECTED"           # filtered out
DECISION_ALMOST           = "ALMOST"             # passed most filters, just missed one
DECISION_SHADOW_SUSPENDED = "REJECTED_SUSPENDED" # detected but blocked by suspension
DECISION_DETECTED         = "DETECTED"           # raw detection before any filter
DECISION_CLOSED_WIN       = "CLOSED_WIN"         # trade closed as a win
DECISION_CLOSED_LOSS      = "CLOSED_LOSS"        # trade closed as a loss

# Pre-Batch 2026-04-20: Shadow log for signals that WOULD have fired but were
# blocked by a gate we've since removed (e.g., the 2-loss halt). These rows
# let us measure whether the gate saved money or cost money — CRITICAL for
# validating filter logic later.
DECISION_SHADOW_HALTED    = "SHADOW_HALTED"      # signal fired anyway despite old halt

# Pre-Batch Follow-up Part A 2026-04-20: Additional shadow-log decision types.
# Each represents "this signal/scan fired anyway; old gate would have blocked
# for this specific reason." Separate constants let us filter strategy_log.csv
# by gate type later in weekly reviews and gate-value analysis.
DECISION_SHADOW_PROFIT_LOCK    = "SHADOW_PROFIT_LOCK"     # +$150 profit lock
DECISION_SHADOW_MAX_TRADES     = "SHADOW_MAX_TRADES"      # 4th+ trade of session
DECISION_SHADOW_CORRELATION    = "SHADOW_CORRELATION"     # BTC/SOL 30-min correlation
DECISION_SHADOW_ZONE_LOCK      = "SHADOW_ZONE_LOCK"       # loss zone lockout
DECISION_SHADOW_FAMILY_CD      = "SHADOW_FAMILY_CD"       # setup family cooldown
DECISION_SHADOW_MARKET_HALT    = "SHADOW_MARKET_HALT"     # 3-loss per-market halt
DECISION_SHADOW_COOLDOWN       = "SHADOW_COOLDOWN"        # per-setup cooldown

def _ensure_csv():
    """
    Ensure strategy_log.csv exists with the current COLS schema.
    If an old-schema file exists (22 cols), migrate it in place by adding
    empty values for the new columns. Never loses data.
    """
    if not os.path.exists(STRATEGY_LOG):
        # Wave 102 (_WAVE102_STRATEGY_LOG_ATOMIC): atomic header create (was a
        # raw open("w") truncate-write on the file that already lost data once).
        safe_io.atomic_write_text(STRATEGY_LOG, ",".join(COLS) + "\r\n")
        return

    # File exists — check if header matches current COLS
    try:
        with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            existing_header = next(reader, [])
    except Exception:
        existing_header = []

    if existing_header == COLS:
        return  # already migrated

    # Migration needed. Back up, then rewrite with new schema.
    backup_path = STRATEGY_LOG + ".pre_batch2a.bak"
    try:
        import shutil
        shutil.copy2(STRATEGY_LOG, backup_path)
    except Exception:
        pass

    # Wave 102 (_WAVE102_STRATEGY_LOG_ATOMIC): atomic, locked rewrite via safe_io.
    # safe_rewrite_csv reads the old-schema rows inside its own lock (DictReader
    # on the file's current header), we remap each row to the new COLS (missing
    # columns -> ""), and it writes the result atomically. Crash-safe -- no more
    # truncate-rewrite of the file that already lost data once.
    def _migrate_to_cols(rows):
        return [{k: r.get(k, "") for k in COLS} for r in rows]
    safe_io.safe_rewrite_csv(STRATEGY_LOG, COLS, _migrate_to_cols)


def update_fired_row_result(market: str, setup_type: str, direction: str,
                            entry: float, result: str) -> bool:
    """
    Apr 30 fix: update the most recent FIRED row in strategy_log.csv to record
    the trade's WIN/LOSS outcome. This makes the 9k+ scan decisions queryable
    by win rate later — previously the 'result' column for FIRED rows stayed
    empty because outcomes were only written to outcomes.csv.

    Matches by market+setup_type+direction+entry (rounded). Updates the
    most recent unresolved FIRED row only. Safe and idempotent.
    """
    if not os.path.exists(STRATEGY_LOG):
        return False

    def _mut(rows):
        # Find the most recent FIRED row that matches and has empty result.
        # Walk in reverse; update the first match.
        for r in reversed(rows):
            if r.get("decision") != DECISION_FIRED:
                continue
            if r.get("result"):
                continue
            if r.get("market") != market:
                continue
            if r.get("setup_type") != setup_type:
                continue
            if r.get("direction") != direction:
                continue
            try:
                if abs(float(r.get("entry", 0)) - float(entry)) > 0.01 * abs(float(entry)):
                    continue
            except Exception:
                continue
            r["result"] = result
            r["result_checked_at"] = datetime.now(timezone.utc).isoformat()
            break
        return rows

    try:
        safe_io.safe_rewrite_csv(STRATEGY_LOG, COLS, _mut)
        return True
    except Exception:
        return False

def log_scan_decision(
    market: str, tf: str, setup_type: str, direction: str,
    price: float, entry: float, stop: float, target: float, rr: float,
    conviction: int, tier: str, trend: int,
    adx: float, rsi: float, vol_ratio: float,
    htf_bias: str, news_flag: bool,
    decision: str, reject_reason: str = "",
    # ── NEW optional keyword-only params (Batch 2A) ──
    *,
    context: Optional[dict] = None,
    detection_reason: str = "",
    score_breakdown: Optional[dict] = None,
    confidence_factors: Optional[dict] = None,
    result: str = "",
) -> str:
    """
    Log every scan decision — fired, rejected, almost, or detected.
    Returns the row timestamp so we can update result later.
    """
    _ensure_csv()
    row = {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "market":            market,
        "tf":                tf,
        "setup_type":        setup_type,
        "direction":         direction,
        "price":             round(float(price), 4)    if price     not in ("", None) else "",
        "entry":             round(float(entry), 4)    if entry     not in ("", None) else "",
        "stop":              round(float(stop), 4)     if stop      not in ("", None) else "",
        "target":            round(float(target), 4)   if target    not in ("", None) else "",
        "rr":                round(float(rr), 2)       if rr        not in ("", None) else "",
        "conviction":        conviction,
        "tier":              tier,
        "trend":             trend,
        "adx":               round(float(adx), 1)      if adx       not in ("", None) else "",
        "rsi":               round(float(rsi), 1)      if rsi       not in ("", None) else "",
        "vol_ratio":         round(float(vol_ratio),2) if vol_ratio not in ("", None) else "",
        "htf_bias":          htf_bias,
        "news_flag":         int(bool(news_flag)),
        "decision":          decision,
        "reject_reason":     reject_reason or "",
        "result":            result or "",
        "result_checked_at": "",
        # NEW fields
        "score_breakdown":     json.dumps(score_breakdown, default=str)    if score_breakdown    else "",
        "confidence_factors":  json.dumps(confidence_factors, default=str) if confidence_factors else "",
        "detection_reason":    detection_reason or "",
    }

    # Pull indicator snapshot from context (all optional)
    ctx = context or {}
    for key in ("atr", "vwap", "ema20", "ema50", "ema200", "ema21",
                "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
                "stoch_k", "stoch_d", "macd_line", "macd_signal", "macd_hist",
                "close_price", "regime", "session_name",
                "swing_high_30", "swing_low_30", "volume_raw", "volume_20ma"):
        val = ctx.get(key, "")
        if isinstance(val, float):
            row[key] = round(val, 4)
        else:
            row[key] = val if val not in (None,) else ""

    # Ensure every COLS key is present
    for k in COLS:
        row.setdefault(k, "")

    # Locked atomic append. Prevents check_missed_setups from clobbering
    # this row by rewriting the file with a stale snapshot.
    safe_io.safe_append_csv(STRATEGY_LOG, COLS, row)

    return row["timestamp"]


def check_missed_setups(live_frames: dict):
    """
    Called every scan. Checks REJECTED/ALMOST setups to see if
    price hit their target or stop since the alert was logged.

    Uses candle HIGH/LOW range (not just close price) so we catch
    moves that spiked through a level between scans — same method
    as outcome_tracker uses for real trades.

    DATA-LOSS FIX (2026-04-27): the old version read the file, mutated
    rows in-memory, then truncate-rewrote with `open("w")`. If
    log_scan_decision() appended a row between our read and our rewrite,
    that row was silently lost. This caused row counts to bounce
    (3.5k -> 7k -> 3k). Now we use safe_io.safe_rewrite_csv which
    re-reads inside the lock, so concurrent appenders wait their turn
    and never get clobbered.
    """
    if not os.path.exists(STRATEGY_LOG):
        return []

    updated_log: list = []

    def _mutator(rows: list[dict]) -> list[dict]:
        """Runs INSIDE safe_rewrite_csv's lock with a fresh read. Mutates
        rows in place and returns them. Cannot do other I/O on the file."""
        for row in rows:
            if row.get("result"):             # already resolved
                continue
            if row.get("decision") == DECISION_FIRED:  # handled by outcome_tracker
                continue
            market = row.get("market")
            market_data = live_frames.get(market)
            if market_data is None:
                continue

            try:
                target    = float(row.get("target", 0))
                stop      = float(row.get("stop", 0))
                direction = row.get("direction", "LONG")
                if target == 0 or stop == 0:
                    continue

                # Get candle high/low since alert — catches spikes between scans
                if isinstance(market_data, pd.DataFrame):
                    alert_ts = row.get("timestamp", "")
                    try:
                        alert_dt = pd.Timestamp(alert_ts, tz="UTC")
                        # Wave 138 (_WAVE138_MISSED_ACCURACY): age out stale paper
                        # setups - a rejection resolving DAYS later is not an
                        # actionable missed winner for an intraday bot and it
                        # polluted the loosen-candidate data.
                        if (pd.Timestamp.now(tz="UTC") - alert_dt).total_seconds() > 86400:
                            row["result"] = "EXPIRED"
                            row["result_checked_at"] = datetime.now(timezone.utc).isoformat()
                            continue
                        recent = market_data[market_data.index >= alert_dt]
                        if recent.empty:
                            # Wave 138: no bars since the alert yet - wait for
                            # real data. NEVER evaluate a rejection against an
                            # unrelated recent window (the old iloc[-5:] guess
                            # could fabricate results for old/unparsed alerts).
                            continue
                    except Exception:
                        continue  # Wave 138: unparseable timestamp - skip, never guess
                    period_high = float(recent["High"].max())
                    period_low  = float(recent["Low"].min())
                elif isinstance(market_data, (int, float)):
                    period_high = float(market_data)
                    period_low  = float(market_data)
                    recent = None  # Wave 138: scalar price - touch ORDER is
                    # unknowable, so a both-levels-hit case resolves through the
                    # conservative except path below (deliberately, not by luck)
                else:
                    continue

                hit_target = hit_stop = False
                if direction == "LONG":
                    if period_high >= target: hit_target = True
                    if period_low  <= stop:   hit_stop   = True
                else:
                    if period_low  <= target: hit_target = True
                    if period_high >= stop:   hit_stop   = True

                if hit_target and hit_stop:
                    # Wave 60: BOTH levels were touched since the alert.
                    # The old code called this a WIN, which inflated
                    # WOULD_WIN rates badly (e.g. 83W/1L buckets).
                    # Resolve by FIRST touch, bar by bar; if the same
                    # bar spans both levels, count it as a LOSS
                    # (conservative -- never optimistic with money).
                    try:
                        _first = None
                        for _ts, _bar in recent.iterrows():
                            _hi = float(_bar["High"]); _lo = float(_bar["Low"])
                            if direction == "LONG":
                                _t_hit = _hi >= target; _s_hit = _lo <= stop
                            else:
                                _t_hit = _lo <= target; _s_hit = _hi >= stop
                            if _s_hit:
                                _first = "LOSS"; break
                            if _t_hit:
                                _first = "WIN"; break
                        if _first == "WIN":
                            hit_stop = False
                        else:
                            hit_target = False
                    except Exception:
                        hit_target = False  # cannot order the touches -> conservative
                if hit_target:
                    row["result"]           = "WOULD_WIN"
                    row["result_checked_at"] = datetime.now(timezone.utc).isoformat()
                    updated_log.append(dict(row))
                elif hit_stop:
                    row["result"]           = "WOULD_LOSE"
                    row["result_checked_at"] = datetime.now(timezone.utc).isoformat()
                    updated_log.append(dict(row))
            except Exception:
                continue

        # Always return rows — safe_rewrite_csv writes whatever we return.
        # If we didn't update anything, this is a no-op rewrite of the
        # existing data (slightly wasteful but still correct).
        return rows

    # Only call the rewrite if there's something worth doing. Reading first
    # under a fresh lock-less peek is fine because if we DO mutate, the
    # safe_rewrite_csv call re-reads inside its own lock.
    try:
        # Quick peek to decide whether to bother taking the lock
        with open(STRATEGY_LOG, "r", newline="", encoding="utf-8") as f:
            sample = list(csv.DictReader(f))
        has_pending = any(
            (not r.get("result")) and r.get("decision") != DECISION_FIRED
            for r in sample
        )
        if not has_pending:
            return []
    except Exception:
        # If the peek fails for any reason, fall through and try the rewrite
        pass

    safe_io.safe_rewrite_csv(STRATEGY_LOG, COLS, _mutator)

    # Wave 60: shadow outcomes feed the learning file. Every resolved
    # WOULD_WIN/WOULD_LOSE updates the same market:setup stats that the
    # evidence-based conviction score reads, so unproven buckets can
    # earn (or lose) a track record without ever firing live.
    if updated_log:
        try:
            import outcome_tracker as _ot
            for _r in updated_log:
                _res = _r.get("result", "")
                _mkt = _r.get("market", "")
                _stp = _r.get("setup_type", "")
                if _mkt and _stp:
                    if _res == "WOULD_WIN":
                        _ot.record_trade_result(_mkt, _stp, "WIN")
                    elif _res == "WOULD_LOSE":
                        _ot.record_trade_result(_mkt, _stp, "LOSS")
        except Exception:
            pass  # learning feed is best-effort; never break the scan loop

    return updated_log


def _load_all_strategy_rows() -> list:
    """Return every strategy-log row: live file + all rotated archive parts.
    Archives (chronological by filename) first, live file last. Per-file
    DictReader so mixed headers are handled; unreadable files are skipped.
    Live and archive are disjoint (rotation MOVES rows), so no double-count."""
    import glob
    all_rows = []
    try:
        archive_dir = os.path.join(_DATA_DIR, "archive")
        parts = sorted(glob.glob(os.path.join(archive_dir, "strategy_log_*.csv")))
    except Exception:
        parts = []
    for _p in parts:
        try:
            with open(_p, newline="") as _f:
                all_rows.extend(list(csv.DictReader(_f)))
        except Exception:
            continue
    if os.path.exists(STRATEGY_LOG):
        try:
            with open(STRATEGY_LOG, newline="") as _f:
                all_rows.extend(list(csv.DictReader(_f)))
        except Exception:
            pass
    return all_rows


def build_strategy_analysis() -> str:
    """
    Analyze the strategy log (live + all rotated archives) into a clean,
    mobile-friendly report: an OVERVIEW headline (fired win rate, best/worst
    setup by real win rate), the setups we filtered that would have won, the
    indicator fingerprint of winning trades, and loosen-filter candidates.
    Honest numbers only - every figure is computed from real logged outcomes.
    Returns a monospace text report (sent inside a code block, columns aligned).
    """
    if not os.path.exists(STRATEGY_LOG):
        return "No strategy log data yet."

    # Wave 116 (_WAVE116_ANALYZE_UNION): read the live log PLUS every rotated
    # archive part so counts reflect the true total, not just the current file.
    # Wave 124 (_WAVE124_ANALYZE_REDESIGN): same full data, professional layout.
    rows = _load_all_strategy_rows()
    if not rows:
        return "Strategy log is empty."

    def _num(v, cast=float, default=0.0):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    fired    = [r for r in rows if r.get("decision") == DECISION_FIRED]
    rejected = [r for r in rows if r.get("decision") == DECISION_REJECTED]
    almost   = [r for r in rows if r.get("decision") == DECISION_ALMOST]

    missed_wins = [r for r in rejected + almost if r.get("result") == "WOULD_WIN"]

    fired_wins   = [r for r in fired if "WIN"  in str(r.get("result", ""))]
    fired_losses = [r for r in fired if "LOSS" in str(r.get("result", ""))]
    n_resolved   = len(fired_wins) + len(fired_losses)
    overall_wr   = (len(fired_wins) / n_resolved * 100.0) if n_resolved else 0.0

    # Per-setup fired win rate (real results only) -> best / worst (min sample).
    setup_stats = {}
    for r in fired:
        res = str(r.get("result", ""))
        if "WIN" in res or "LOSS" in res:
            key = "%s:%s" % (r.get("market"), r.get("setup_type"))
            s = setup_stats.setdefault(key, [0, 0])
            if "WIN" in res:
                s[0] += 1
            else:
                s[1] += 1
    ranked = []
    for key, (w, l) in setup_stats.items():
        tot = w + l
        if tot >= 8:
            ranked.append((key, w / tot * 100.0, tot))
    ranked.sort(key=lambda x: x[1], reverse=True)

    DIV = "\u2501" * 27

    def _row(label, value, width=16):
        return "  " + (label + " " * width)[:width] + value

    L = ["\U0001F4CA STRATEGY ANALYSIS", DIV, "OVERVIEW"]
    L.append(_row("Scans logged", format(len(rows), ",")))
    if n_resolved:
        L.append(_row("Fired", "%d  \u00b7  %.0f%% WR (%dW/%dL)"
                      % (len(fired), overall_wr, len(fired_wins), len(fired_losses))))
    else:
        L.append(_row("Fired", "%d  (none resolved yet)" % len(fired)))
    L.append(_row("Missed winners", "%d   (filtered, would've won)" % len(missed_wins)))
    if ranked:
        bk, bwr, bn = ranked[0]
        L.append("")
        L.append("  Best   %-22s %.0f%%  (n%d)" % (bk, bwr, bn))
        if len(ranked) > 1:
            wk, wwr, wn = ranked[-1]
            L.append("  Worst  %-22s %.0f%%  (n%d)" % (wk, wwr, wn))

    if missed_wins:
        L.append(DIV)
        L.append("\U0001F3AF TOP MISSED WINNERS")
        by_type = {}
        for r in missed_wins:
            key = "%s:%s \u00b7 %s" % (r.get("market"), r.get("setup_type"), r.get("tf"))
            by_type.setdefault(key, []).append(r)
        for key, group in sorted(by_type.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
            reasons = [str(r.get("reject_reason", "?")) for r in group]
            common = max(set(reasons), key=reasons.count)
            avg_conv = round(sum(_num(r.get("conviction"), int, 0) for r in group) / max(1, len(group)))
            L.append("  %-26s %dx" % (key, len(group)))
            L.append("    avg conv %d \u00b7 blocked by %s" % (avg_conv, common))

    if len(fired_wins) >= 3 and len(fired_losses) >= 3:
        L.append(DIV)
        L.append("\U0001F52C WINNING-TRADE PATTERNS")
        try:
            def _avg(rs, k):
                return sum(_num(r.get(k)) for r in rs) / max(1, len(rs))
            aw, al = _avg(fired_wins, "adx"), _avg(fired_losses, "adx")
            rw, rl = _avg(fired_wins, "rsi"), _avg(fired_losses, "rsi")
            tw = sum(_num(r.get("trend"), int, 0) for r in fired_wins) / max(1, len(fired_wins))
            L.append("  %-9swin %.1f   loss %.1f" % ("ADX", aw, al))
            L.append("  %-9swin %.1f   loss %.1f" % ("RSI", rw, rl))
            L.append("  %-9swin %+.1f" % ("Trend", tw))
            if aw > al + 3:
                L.append("  \U0001F4A1 Wins run higher ADX - the trend filter is working.")
            elif tw > 3:
                L.append("  \U0001F4A1 Strong trend correlation in the winners.")
        except Exception:
            pass

    cand = {}
    for r in missed_wins:
        key = "%s:%s" % (r.get("market"), r.get("setup_type"))
        cand[key] = cand.get(key, 0) + 1
    cand = [(k, c) for k, c in cand.items() if c >= 2]
    if cand:
        L.append(DIV)
        L.append("\u26A1 LOOSEN-FILTER CANDIDATES")
        for key, count in sorted(cand, key=lambda x: x[1], reverse=True)[:6]:
            L.append("  %-24s %dx missed" % (key, count))

    if len(L) <= 3:
        L.append("  Not enough data yet - keep the bot running.")

    report = "\n".join(L)
    try:
        with open(CANDIDATE_FILE, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        pass
    return report


# ── Batch 2A: auto-migrate the CSV at import time ──
try:
    _ensure_csv()
except Exception:
    # If migration fails for any reason, don't crash the bot at import time —
    # the next call to log_scan_decision will retry via _ensure_csv() anyway.
    pass
