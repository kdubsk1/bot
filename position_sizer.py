"""
position_sizer.py — NQ CALLS Dynamic Position Sizing Engine
============================================================
Designed from first principles with Opus (Claude 4.6).

Philosophy:
  Every trade passes through a waterfall of 5 independent constraints.
  Each parameter has ONE job. They don't interact with each other.
  The final contract count is transparent — the bot shows you exactly
  why it chose the size it chose.

Constraint waterfall (in order):
  1. SURVIVAL   — how much cushion can you afford to risk?
  2. KELLY      — how much does your historical edge justify?
  3. CONVICTION — how strong is this specific signal?
  4. REGIME     — how favorable is the market environment?
  5. EXPOSURE   — how much correlated risk do you already have?

The final answer = min(survival, kelly) × conviction × regime × correlation × position
"""

import math
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple


# ── Audit Finding #11 / BACKLOG #2 (2026-04-28): validation lock ──
# Force contracts = 1 until rolling 20-trade WR reaches 50%. Don't reward
# a losing system with Kelly scaling. With < 20 closed trades, default
# locked. Cache for 60s to avoid hammering outcomes.csv on every sizing.
_VALIDATION_LOCK_CACHE = {"checked_at": 0.0, "locked": True, "wr": 0.0, "n": 0}
_VALIDATION_LOCK_TTL = 60.0           # seconds
_VALIDATION_LOCK_WINDOW = 20          # rolling N trades
_VALIDATION_LOCK_WR_FLOOR = 0.50      # unlock at this WR

def _check_validation_lock() -> Tuple[bool, float, int]:
    """
    Returns (locked, rolling_wr, closed_n). Reads outcomes.csv from the
    trading bot dir; on any error, returns locked=True (fail safe).
    """
    now = time.time()
    if now - _VALIDATION_LOCK_CACHE["checked_at"] < _VALIDATION_LOCK_TTL:
        return (_VALIDATION_LOCK_CACHE["locked"],
                _VALIDATION_LOCK_CACHE["wr"],
                _VALIDATION_LOCK_CACHE["n"])
    try:
        import pandas as _pd
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outcomes.csv")
        if not os.path.exists(csv_path):
            locked, wr, n = True, 0.0, 0
        else:
            df = _pd.read_csv(csv_path)
            closed = df[df.get("result", "").astype(str).str.upper().isin(["WIN", "LOSS"])]
            n = len(closed)
            if n < _VALIDATION_LOCK_WINDOW:
                locked, wr = True, 0.0
            else:
                last = closed.tail(_VALIDATION_LOCK_WINDOW)
                wins = (last["result"].astype(str).str.upper() == "WIN").sum()
                wr = float(wins) / float(_VALIDATION_LOCK_WINDOW)
                locked = wr < _VALIDATION_LOCK_WR_FLOOR
    except Exception:
        locked, wr, n = True, 0.0, 0
    _VALIDATION_LOCK_CACHE.update({
        "checked_at": now, "locked": locked, "wr": wr, "n": n
    })
    return locked, wr, n

# ── Instrument specs ──────────────────────────────────────────────
@dataclass
class InstrumentSpec:
    """Dollar value per price point for each tradeable instrument."""
    symbol: str
    point_value: float      # $ per 1.0 price move
    tick_size: float        # minimum price increment
    tick_value: float       # $ per tick

INSTRUMENTS = {
    'MNQ': InstrumentSpec('MNQ', 2.0,   0.25, 0.50),   # Micro NQ
    'NQ':  InstrumentSpec('NQ',  20.0,  0.25, 5.00),   # Full NQ
    'MGC': InstrumentSpec('MGC', 10.0,  0.10, 1.00),   # Micro Gold
    'GC':  InstrumentSpec('GC',  100.0, 0.10, 10.00),  # Full Gold
}

def get_instrument(market: str, use_mnq: bool = True) -> str:
    """Map market name to instrument symbol based on account size preference."""
    mapping = {
        'NQ': 'MNQ' if use_mnq else 'NQ',
        'GC': 'MGC' if use_mnq else 'GC',
    }
    return mapping.get(market, market)

# ── Bayesian edge tracker ─────────────────────────────────────────
@dataclass
class SetupEdgeEstimate:
    """
    Bayesian estimate of a setup's edge, shrunk toward a conservative prior.

    Prior = "a mediocre but not worthless setup":
      - 42% win rate (below breakeven at 1:1, but positive EV with good R:R)
      - 1.8R avg win, 1.0R avg loss
      - Prior weight = 15 pseudo-trades (trust real data after ~30 real trades)

    This means:
      - With 0 real trades: estimate = prior (42% WR, 1.8R win)
      - With 10 real trades: estimate is 60% prior, 40% real data
      - With 30 real trades: estimate is 33% prior, 67% real data
      - With 100 real trades: estimate is ~13% prior, ~87% real data
    """
    setup_name: str
    regime: str
    wins: int = 0
    losses: int = 0
    total_win_r: float = 0.0
    total_loss_r: float = 0.0

    # Priors — "mediocre setup" baseline
    PRIOR_N: float = 15.0
    PRIOR_WR: float = 0.42
    PRIOR_AVG_WIN_R: float = 1.8
    PRIOR_AVG_LOSS_R: float = 1.0

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def estimated_win_rate(self) -> float:
        """Posterior win rate, shrunk toward 42% prior."""
        prior_wins = self.PRIOR_N * self.PRIOR_WR
        prior_losses = self.PRIOR_N * (1 - self.PRIOR_WR)
        return (self.wins + prior_wins) / (self.n + self.PRIOR_N)

    @property
    def estimated_avg_win_r(self) -> float:
        """Posterior avg win R, shrunk toward 1.8R prior."""
        prior_total_win_r = self.PRIOR_N * self.PRIOR_WR * self.PRIOR_AVG_WIN_R
        real_win_r = self.total_win_r if self.wins > 0 else 0
        total_win_r = real_win_r + prior_total_win_r
        total_wins = self.wins + self.PRIOR_N * self.PRIOR_WR
        return total_win_r / max(total_wins, 0.01)

    @property
    def estimated_avg_loss_r(self) -> float:
        """Posterior avg loss R, shrunk toward 1.0R prior."""
        prior_total_loss_r = self.PRIOR_N * (1 - self.PRIOR_WR) * self.PRIOR_AVG_LOSS_R
        real_loss_r = self.total_loss_r if self.losses > 0 else 0
        total_loss_r = real_loss_r + prior_total_loss_r
        total_losses = self.losses + self.PRIOR_N * (1 - self.PRIOR_WR)
        return total_loss_r / max(total_losses, 0.01)

    @property
    def expected_value_per_r(self) -> float:
        """E[V] per unit of risk = p*avgWin/avgLoss - (1-p)"""
        p = self.estimated_win_rate
        b = self.estimated_avg_win_r / max(self.estimated_avg_loss_r, 0.01)
        return p * b - (1 - p)

    @property
    def kelly_fraction(self) -> float:
        """Raw Kelly fraction (use only as input to PositionSizer, which applies safety discount)."""
        p = self.estimated_win_rate
        q = 1.0 - p
        b = self.estimated_avg_win_r / max(self.estimated_avg_loss_r, 0.01)
        if b <= 0:
            return 0.0
        f = (p * b - q) / b
        return max(f, 0.0)

    def record_trade(self, won: bool, r_multiple: float):
        """Update with a completed trade result."""
        if won:
            self.wins += 1
            self.total_win_r += max(r_multiple, 0)
        else:
            self.losses += 1
            self.total_loss_r += abs(r_multiple)


class EdgeTracker:
    """
    Maintains per-setup, per-regime Bayesian edge estimates.
    Persists to disk so knowledge accumulates across bot restarts.
    """
    def __init__(self, data_dir: str = None):
        self.estimates: Dict[tuple, SetupEdgeEstimate] = {}
        self.data_dir = data_dir
        self._load()

    def _path(self) -> Optional[str]:
        if self.data_dir:
            return os.path.join(self.data_dir, "edge_estimates.json")
        return None

    def _load(self):
        p = self._path()
        if not p or not os.path.exists(p):
            return
        try:
            with open(p) as f:
                raw = json.load(f)
            for key_str, v in raw.items():
                setup, regime = key_str.split("|||", 1)
                est = SetupEdgeEstimate(setup, regime)
                est.wins = v.get("wins", 0)
                est.losses = v.get("losses", 0)
                est.total_win_r = v.get("total_win_r", 0.0)
                est.total_loss_r = v.get("total_loss_r", 0.0)
                self.estimates[(setup, regime)] = est
        except Exception:
            pass

    def save(self):
        p = self._path()
        if not p:
            return
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            raw = {}
            for (setup, regime), est in self.estimates.items():
                raw[f"{setup}|||{regime}"] = {
                    "wins": est.wins, "losses": est.losses,
                    "total_win_r": est.total_win_r, "total_loss_r": est.total_loss_r,
                }
            with open(p, "w") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass

    def get_estimate(self, setup_name: str, regime: str) -> SetupEdgeEstimate:
        key = (setup_name, regime)
        if key not in self.estimates:
            self.estimates[key] = SetupEdgeEstimate(setup_name, regime)
        return self.estimates[key]

    def get_best_estimate(self, setup_name: str, regime: str) -> SetupEdgeEstimate:
        """
        Returns the best available estimate for this setup+regime combo.
        If regime-specific data is thin (<15 trades), blend with all-regime data.
        """
        specific = self.get_estimate(setup_name, regime)
        general  = self.get_estimate(setup_name, 'ALL')

        if specific.n >= 15:
            return specific  # Enough regime-specific data

        # Blend: weight specific data, pad with general data
        blended = SetupEdgeEstimate(setup_name, regime)
        blended.wins        = general.wins        + specific.wins
        blended.losses      = general.losses      + specific.losses
        blended.total_win_r = general.total_win_r + specific.total_win_r
        blended.total_loss_r= general.total_loss_r+ specific.total_loss_r
        return blended

    def record(self, setup_name: str, regime: str, won: bool, r_multiple: float):
        """Record a trade result into both regime-specific and all-regime buckets."""
        self.get_estimate(setup_name, regime).record_trade(won, r_multiple)
        self.get_estimate(setup_name, 'ALL').record_trade(won, r_multiple)
        self.save()

    def summary(self) -> str:
        """Human-readable summary for Telegram."""
        lines = ["📊 *Setup Edge Estimates*\n━━━━━━━━━━━━━━━━━━"]
        seen_setups = set()
        for (setup, regime), est in sorted(self.estimates.items()):
            if regime == 'ALL' and est.n >= 5:
                ev = est.expected_value_per_r
                ev_str = f"+{ev:.2f}" if ev >= 0 else f"{ev:.2f}"
                wr = round(est.estimated_win_rate * 100, 1)
                seen_setups.add(setup)
                lines.append(
                    f"  *{setup}*: {est.n} trades | WR {wr}% | EV {ev_str}R"
                )
        if not seen_setups:
            lines.append("  No data yet — keep trading!")
        lines.append("━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)


# ── Position sizer ────────────────────────────────────────────────
class PositionSizer:
    """
    Calculates contract count using a waterfall of 5 independent constraints.

    Each parameter has ONE job. Adjust them independently:
      max_dd_fraction    — how aggressive to risk per trade vs cushion
      kelly_fraction     — how much to discount Kelly (0.25 = quarter-Kelly)
      conviction_floor   — minimum conviction to trade
      regime_multipliers — how much each regime affects size
      absolute_max       — hard bug-catcher ceiling (not a trading parameter)
    """

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}

        # ── Survival parameters ───────────────────────────────────
        # Never risk more than X% of remaining cushion on one trade.
        # Cushion = balance - trailing drawdown floor.
        # As cushion shrinks, max risk auto-shrinks. Natural, continuous compression.
        self.max_dd_fraction = cfg.get('max_dd_fraction', 0.12)

        # Never let total OPEN risk exceed X% of daily loss limit.
        # Prevents death by a thousand cuts when multiple positions are open.
        self.max_daily_open_fraction = cfg.get('max_daily_open_fraction', 0.40)

        # ── Kelly parameters ──────────────────────────────────────
        # Quarter-Kelly until 100 trades. Kelly with bad estimates is worse than no Kelly.
        # Upgrades to half-Kelly automatically when you cross 100 trades.
        self.kelly_fraction_low_sample  = cfg.get('kelly_fraction_low',  0.25)
        self.kelly_fraction_high_sample = cfg.get('kelly_fraction_high', 0.50)
        self.kelly_sample_threshold     = cfg.get('kelly_threshold', 100)

        # Below this sample count, skip Kelly and use flat fraction of cushion.
        # Reason: Kelly with <20 observations is meaningless noise.
        self.min_sample_for_kelly = cfg.get('min_sample', 20)
        self.flat_risk_fraction   = cfg.get('flat_risk', 0.06)  # 6% of cushion

        # ── Conviction scaling ────────────────────────────────────
        # Maps conviction 0-100 → multiplier.
        # Conviction does NOT create edge — it only modulates how much of the
        # calculated edge you actually express in the market.
        # Nothing gets MORE than 1.0x from conviction. That keeps sizing honest.
        self.conviction_floor   = cfg.get('conviction_floor', 50)
        self.conviction_ceiling = cfg.get('conviction_ceiling', 95)
        # Linear mapping: floor → 0.40x, ceiling → 1.0x

        # ── Regime multipliers ────────────────────────────────────
        # Volatility expansion always reduces size, even on perfect setups.
        # You don't know which direction it resolves.
        self.regime_multipliers = cfg.get('regime_multipliers', {
            'TRENDING_BULL':      1.00,
            'TRENDING_BEAR':      1.00,
            'RANGING':            0.85,
            'VOLATILE_EXPANSION': 0.60,
            'UNKNOWN':            0.50,
        })

        # ── Correlation / exposure ────────────────────────────────
        # Each additional open position slightly reduces new sizing.
        # Aggregate risk grows even if individual trades are sized well.
        self.position_penalty_per_open = cfg.get('position_penalty', 0.15)
        self.max_concurrent            = cfg.get('max_concurrent', 3)

        # ── Safety ceiling ────────────────────────────────────────
        # Hard cap. This is a bug-catcher, not a trading parameter.
        # If the math somehow outputs 50 contracts, something is wrong.
        self.absolute_max = cfg.get('absolute_max', 10)

    def calculate(
        self,
        market:        str,
        use_mnq:       bool,
        entry:         float,
        stop:          float,
        conviction:    int,
        regime:        str,
        edge_estimate: SetupEdgeEstimate,
        # Account state
        balance:       float,
        dd_floor:      float,       # trailing drawdown floor = high_water - trailing_dd
        daily_used:    float,       # dollars of open risk already in play today
        daily_limit:   float,       # Topstep daily loss limit
        open_positions:int = 0,     # count of currently open trades
        correlated_risk:float = 0.0, # dollar risk on correlated open positions
    ) -> dict:
        """
        Returns a dict with:
          'contracts'   — final answer (integer ≥ 1, or 0 if rejected)
          'instrument'  — 'MNQ', 'NQ', 'GC', etc.
          'dollar_risk' — total dollar risk at final size
          'reasoning'   — human-readable string for Telegram alerts
          + detailed diagnostic fields for logging
        """
        instrument = get_instrument(market, use_mnq)

        if instrument not in INSTRUMENTS:
            return self._reject("unknown_instrument", instrument, 0)

        spec = INSTRUMENTS[instrument]

        # ── Step 0: Dollar risk per contract ─────────────────────
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return self._reject("zero_stop_distance", instrument, 0)

        risk_per_contract = stop_dist * spec.point_value

        # ── Step 1: SURVIVAL constraint ───────────────────────────
        # Cushion = how much room you have before hitting trailing drawdown
        cushion = balance - dd_floor
        if cushion <= 50:
            return self._reject("near_drawdown_limit", instrument, risk_per_contract)

        # Max dollar risk = fraction of cushion
        max_risk_survival = cushion * self.max_dd_fraction
        survival_contracts = math.floor(max_risk_survival / risk_per_contract)
        survival_contracts = max(survival_contracts, 1)

        # Daily open risk gate: if we already have X% of daily limit at risk,
        # compress new position so aggregate never exceeds the cap
        daily_headroom = daily_limit * self.max_daily_open_fraction - daily_used
        if daily_headroom <= 0:
            return self._reject("daily_risk_limit_reached", instrument, risk_per_contract)
        daily_contracts = math.floor(daily_headroom / risk_per_contract)
        daily_contracts = max(daily_contracts, 1)

        survival_contracts = min(survival_contracts, daily_contracts)

        # ── Step 2: KELLY constraint ──────────────────────────────
        n = edge_estimate.n
        kelly_method = ""

        if n >= self.min_sample_for_kelly:
            # Enough data — use Kelly
            raw_kelly_f = edge_estimate.kelly_fraction

            if raw_kelly_f <= 0:
                # Kelly says no edge — reject trade
                return self._reject(
                    f"kelly_no_edge (WR={edge_estimate.estimated_win_rate:.0%})",
                    instrument, risk_per_contract
                )

            # Apply discount: quarter-Kelly below threshold, half-Kelly above
            kf = self.kelly_fraction_high_sample if n >= self.kelly_sample_threshold \
                 else self.kelly_fraction_low_sample
            adj_kelly_f = raw_kelly_f * kf
            kelly_risk = cushion * adj_kelly_f
            kelly_contracts = math.floor(kelly_risk / risk_per_contract)
            kelly_contracts = max(kelly_contracts, 1)
            kelly_method = f"{'half' if n >= self.kelly_sample_threshold else 'quarter'}_kelly"
        else:
            # Not enough data — flat fraction of cushion
            kelly_risk = cushion * self.flat_risk_fraction
            kelly_contracts = math.floor(kelly_risk / risk_per_contract)
            kelly_contracts = max(kelly_contracts, 1)
            kelly_method = f"flat_{int(self.flat_risk_fraction*100)}pct"
            raw_kelly_f = 0.0

        # ── Binding constraint: survival vs Kelly ─────────────────
        base_contracts = min(survival_contracts, kelly_contracts)
        binding = "survival" if survival_contracts <= kelly_contracts else "kelly"

        # ── Step 3: CONVICTION multiplier ─────────────────────────
        # Linear map: conviction_floor → 0.40x, conviction_ceiling → 1.0x
        conv_clamped = max(self.conviction_floor, min(conviction, self.conviction_ceiling))
        conv_range = self.conviction_ceiling - self.conviction_floor
        conviction_mult = 0.40 + 0.60 * (conv_clamped - self.conviction_floor) / conv_range

        # ── Step 4: REGIME multiplier ─────────────────────────────
        regime_mult = self.regime_multipliers.get(regime, self.regime_multipliers.get('UNKNOWN', 0.5))

        # ── Step 5: EXPOSURE multiplier ───────────────────────────
        # Each open position reduces new position size
        position_mult = max(0.50, 1.0 - self.position_penalty_per_open * open_positions)

        # Correlated exposure penalty — if we already have risk in correlated instruments
        if correlated_risk > 0 and risk_per_contract > 0:
            corr_penalty = min(0.50, correlated_risk / (daily_limit * 0.25))
            correlation_mult = max(0.50, 1.0 - corr_penalty)
        else:
            correlation_mult = 1.0

        # ── Combine ───────────────────────────────────────────────
        combined_mult = conviction_mult * regime_mult * position_mult * correlation_mult
        adjusted = base_contracts * combined_mult

        # Floor at 1 (if we're trading, trade at least 1), ceiling at absolute_max
        final_contracts = max(1, min(math.floor(adjusted), self.absolute_max))

        # ── Audit Finding #11 / BACKLOG #2: validation lock ──────
        # Force size = 1 until rolling 20-trade WR proves out the system.
        _vlock, _vlock_wr, _vlock_n = _check_validation_lock()
        if _vlock and final_contracts > 1:
            final_contracts = 1

        # ── Final risk metrics ────────────────────────────────────
        actual_risk = final_contracts * risk_per_contract
        cushion_pct = (actual_risk / cushion) * 100 if cushion > 0 else 0

        # ── Reasoning string for Telegram ─────────────────────────
        reasoning = (
            f"surv={survival_contracts} {binding.upper()}, "
            f"kelly={kelly_contracts} ({kelly_method}), "
            f"conv={conviction_mult:.2f}, "
            f"regime={regime_mult:.2f}, "
            f"pos={position_mult:.2f}"
        )
        if _vlock:
            _vlock_msg = (f"validation lock active (n={_vlock_n}, WR={_vlock_wr:.0%})"
                          if _vlock_n >= _VALIDATION_LOCK_WINDOW
                          else f"validation lock active (n={_vlock_n} < {_VALIDATION_LOCK_WINDOW})")
            reasoning = f"LOCKED → 1 contract — {_vlock_msg} | {reasoning}"

        return {
            'contracts':           final_contracts,
            'instrument':          instrument,
            'direction':           'LONG',  # filled in by caller
            'rejected':            False,
            'dollar_risk':         round(actual_risk, 2),
            'cushion_pct':         round(cushion_pct, 1),
            'cushion_remaining':   round(cushion, 2),
            'risk_per_contract':   round(risk_per_contract, 2),
            'reasoning':           reasoning,
            # Constraint details
            'survival_max':        survival_contracts,
            'kelly_max':           kelly_contracts,
            'kelly_method':        kelly_method,
            'binding_constraint':  binding,
            # Multipliers
            'conviction_mult':     round(conviction_mult, 3),
            'regime_mult':         round(regime_mult, 3),
            'position_mult':       round(position_mult, 3),
            'correlation_mult':    round(correlation_mult, 3),
            'combined_mult':       round(combined_mult, 3),
            # Edge data
            'edge_n':              n,
            'edge_wr':             round(edge_estimate.estimated_win_rate, 3),
            'edge_ev':             round(edge_estimate.expected_value_per_r, 3),
            'kelly_f_raw':         round(raw_kelly_f if n >= self.min_sample_for_kelly else 0.0, 4),
        }

    def _reject(self, reason: str, instrument: str, risk_per_contract: float) -> dict:
        return {
            'contracts':          0,
            'instrument':         instrument,
            'rejected':           True,
            'reject_reason':      reason,
            'dollar_risk':        0,
            'cushion_pct':        0,
            'cushion_remaining':  0,
            'risk_per_contract':  round(risk_per_contract, 2),
            'reasoning':          f"REJECTED: {reason}",
        }


# ── Correlation groups ────────────────────────────────────────────
CORRELATION_GROUPS = [
    {'BTC', 'SOL'},         # crypto
    {'NQ', 'NQ_MNQ'},       # NQ family
    {'GC', 'GC_MGC'},       # Gold family
]

def correlated_open_risk(market: str, open_trades: list) -> float:
    """
    Sum the dollar risk of all open trades correlated with this market.
    open_trades is a list of dicts with 'market' and 'dollar_risk' keys.
    """
    corr_markets = set()
    for group in CORRELATION_GROUPS:
        if market in group:
            corr_markets = group - {market}
            break

    total = 0.0
    for trade in open_trades:
        if trade.get('market') in corr_markets:
            total += float(trade.get('dollar_risk', 0))
    return total


# ── Telegram format helper ────────────────────────────────────────
def format_sizing_line(result: dict) -> str:
    """
    Returns the sizing transparency line for Telegram alerts.
    Example: 📦 Size: 3 MNQ | Risk: $180 (9.0% of cushion)
             _Sizing: surv=8, kelly=5 (quarter_kelly), conv=0.87, regime=1.00_
    """
    if result.get('rejected'):
        return f"📦 *Size:* 0 — {result.get('reject_reason', 'rejected')}"

    contracts = result['contracts']
    instrument = result['instrument']
    risk = result['dollar_risk']
    pct = result['cushion_pct']
    reasoning = result['reasoning']

    line1 = f"📦 *Size:* {contracts} {instrument}  |  Risk: `${risk:,.0f}` ({pct:.1f}% of cushion)"
    line2 = f"_Sizing: {reasoning}_"
    return f"{line1}\n{line2}"


# ── Module-level singleton ────────────────────────────────────────
# Shared sizer instance used by the whole bot
_SIZER: Optional[PositionSizer] = None
_EDGE_TRACKER: Optional[EdgeTracker] = None

def get_sizer() -> PositionSizer:
    global _SIZER
    if _SIZER is None:
        _SIZER = PositionSizer()
    return _SIZER

def get_edge_tracker(data_dir: str = None) -> EdgeTracker:
    global _EDGE_TRACKER
    if _EDGE_TRACKER is None:
        _EDGE_TRACKER = EdgeTracker(data_dir)
    return _EDGE_TRACKER

def reset_sizer():
    """Call this if you want to reinitialize with new config."""
    global _SIZER, _EDGE_TRACKER
    _SIZER = None
    _EDGE_TRACKER = None


# ── Eval mode ────────────────────────────────────────────────────
_EVAL_MODE = True  # Default ON — we are in Topstep eval

def set_eval_mode(enabled: bool):
    global _EVAL_MODE
    _EVAL_MODE = enabled

def get_eval_mode() -> bool:
    return _EVAL_MODE


class EvalPositionSizer:
    """
    Topstep eval-safe wrapper around PositionSizer.
    When EVAL_MODE is True, applies additional safety constraints:
      - Eighth-Kelly instead of quarter-Kelly
      - Max 1% of trailing drawdown cushion per trade
      - Cushion-based MNQ caps (0/1/2/3)
      - 3 trades per session max
      - $150 daily profit lock
    """

    def __init__(self):
        # Use eighth-Kelly config
        self._sizer = PositionSizer({
            'kelly_fraction_low': 0.125,   # eighth-Kelly
            'kelly_fraction_high': 0.25,   # quarter-Kelly after 100 trades
            'max_dd_fraction': 0.01,       # 1% of cushion max
            'absolute_max': 3,             # hard cap 3 MNQ during eval
        })
        self._daily_trade_count = 0
        self._daily_pnl = 0.0
        self._session_date = ""

    def _check_session_reset(self):
        """Reset daily counters if session changed."""
        try:
            from session_clock import get_session_date
            current = get_session_date()
        except Exception:
            from datetime import datetime
            current = datetime.now().strftime("%Y-%m-%d")
        if current != self._session_date:
            self._session_date = current
            self._daily_trade_count = 0
            self._daily_pnl = 0.0

    def record_trade(self, pnl_dollars: float):
        """Call after each trade closes to track daily stats."""
        self._check_session_reset()
        self._daily_trade_count += 1
        self._daily_pnl += pnl_dollars

    def calculate(self, balance: float, dd_floor: float, **kwargs) -> dict:
        """
        Wraps PositionSizer.calculate() with eval-mode gates.
        dd_floor for Topstep 50k eval = 48000 (balance - max_trailing_dd).
        """
        self._check_session_reset()

        cushion = balance - dd_floor

        # Gate 1: Too close to bust
        if cushion < 800:
            return self._reject("cushion_below_800", kwargs.get('market', '?'))

        # Gate 2: Daily trade limit
        if self._daily_trade_count >= 3:
            return self._reject("max_3_trades_per_session", kwargs.get('market', '?'))

        # Gate 3: Profit lock
        if self._daily_pnl >= 150:
            return self._reject("daily_profit_locked_150", kwargs.get('market', '?'))

        # Run the normal sizer
        result = self._sizer.calculate(balance=balance, dd_floor=dd_floor, **kwargs)

        if result.get('rejected'):
            return result

        # Cushion-based MNQ cap
        contracts = result['contracts']
        if cushion < 1200:
            contracts = min(contracts, 1)
        elif cushion < 2000:
            contracts = min(contracts, 2)
        else:
            contracts = min(contracts, 3)

        result['contracts'] = contracts
        result['dollar_risk'] = round(contracts * result.get('risk_per_contract', 0), 2)
        result['reasoning'] = f"EVAL: {result['reasoning']} | cushion=${cushion:.0f} cap={contracts}mnq"
        return result

    def _reject(self, reason: str, market: str) -> dict:
        return {
            'contracts': 0,
            'instrument': 'MNQ',
            'rejected': True,
            'reject_reason': f"EVAL: {reason}",
            'dollar_risk': 0,
            'cushion_pct': 0,
            'cushion_remaining': 0,
            'risk_per_contract': 0,
            'reasoning': f"REJECTED: EVAL {reason}",
        }


# Module-level eval sizer singleton
_EVAL_SIZER: Optional[EvalPositionSizer] = None

def get_eval_sizer() -> EvalPositionSizer:
    global _EVAL_SIZER
    if _EVAL_SIZER is None:
        _EVAL_SIZER = EvalPositionSizer()
    return _EVAL_SIZER
