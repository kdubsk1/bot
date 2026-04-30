# NQ CALLS — Roadmap

Living doc for Wayne + Claude. Things we said "next session" to.
Each item has why, scope, design notes, and acceptance criteria.

---

## P0 — High impact, ready to scope

### 1. Backtest Harness
**Why:** We have 9k+ scan decisions in `data/strategy_log.csv` but no way to replay
them with new logic. Every conviction-formula tweak is currently a guess. With a
backtest, we can know within minutes whether a change improves WR or kills it.

**Scope:**
- New file: `backtest.py`
- Replays every FIRED row in strategy_log.csv against new conviction logic
- Outputs delta: `+X% WR, +Y trades fired, -Z avg drawdown`
- Supports A/B comparing two configs side-by-side
- Per-setup breakdown so we can see which setups gain/lose under the change

**Design notes:**
- Read strategy_log.csv → for each FIRED row, we already have all the indicator
  context (BB, Stoch, MACD, regime, etc.) saved as JSON in the `context` col
- Re-run conviction_score() with the new logic against that saved snapshot
- Compare new score vs original — would it still have fired? Same tier?
- Cross-reference with outcomes.csv to know if it actually won or lost
- Output a simple text report: total trades, WR by setup, conviction shift

**Acceptance criteria:**
- `python backtest.py --baseline current --variant lower_min_conv 60`
  produces a report showing how lowering min_conviction to 60 would have
  changed historical performance
- Runs in <10 seconds on the current 9k-row log
- No false reports — if outcomes.csv has WIN/LOSS for the row, that's the truth

**Time estimate:** 1 focused session (3-4 hours) to do it right.

---

### 2. Long-Bias Regime Detection (Crypto)
**Why:** Bot has been over-shorting BTC/SOL in choppy markets and bleeding the
$1k crypto sim ($300 lost overnight Apr 30). Need regime-aware bias adjustment
so the bot doesn't keep shorting into upticks.

**Scope:**
- Add `_market_regime_bias()` helper to outcome_tracker.py
- Reads 1h frame and computes a directional bias score (-100 to +100)
- Factors: HTF EMA stack, VWAP relative to price, recent swing structure,
  Bollinger position, trend score
- Returns a "preferred direction" — bot then applies a conviction MULTIPLIER:
  - SHORT setups in BULL regime: conviction × 0.7
  - LONG setups in BULL regime: conviction × 1.1
  - Symmetric for BEAR regime
  - Both directions at conviction × 0.9 in CHOP regime

**Design notes:**
- This is DIFFERENT from existing regime_classifier.py which only detects
  TRENDING_BULL/BEAR/RANGING/VOLATILE_EXPANSION. We need a graded bias score.
- Should NOT replace existing regime gating — should layer on top
- Critical: don't overweight any single timeframe. Use weighted blend.

**Acceptance criteria:**
- When BTC is in clear uptrend (1h bullish stack + HH_HL + above VWAP):
  bot's BTC SHORT alerts get penalized 30% in conviction → fewer fire
- Backtest harness (item #1) confirms this WOULD HAVE prevented the
  Apr 29-30 BTC SHORT bleed without killing the legitimate shorts

**Time estimate:** Half a session (2 hours) once item #1 exists.

---

## P1 — Medium impact, do when ready

### 3. Archive Cleanup
**Why:** `_archive/` and `data/archive/` folders accumulate session backups
that bloat the repo without being used.

**Scope:**
- One-time script: `cleanup_archives.py`
- Removes session archives older than 30 days
- Keeps last 30 days as cold storage
- Updates `.gitignore` to prevent future accumulation if needed
- Wayne runs it manually when storage matters

**Acceptance criteria:**
- Script is destructive — Wayne reviews + confirms before delete
- Output: "Removed 47 files (12.3 MB), kept 8 most recent sessions"
- Doesn't touch active data files (outcomes.csv, sim_account.json, etc.)

**Time estimate:** 30 min.

---

### 4. ML Scoring of Confidence Factors
**Why:** Bot already saves rich qualitative flags (`confidence_factors`)
on every FIRED row — bb_position, stoch_signal, macd_signal, trend_strength,
adx_regime, rsi_zone. But these aren't used to score. Real ML would learn
which combinations actually predict wins.

**Scope:**
- Build a simple logistic regression model
- Features: all the categorical confidence_factors as one-hot
- Target: WIN (1) or LOSS (0) from outcomes.csv
- Train on last 60 days of closed trades
- Output: feature importances + a `predict_win_probability()` function

**Design notes:**
- Use scikit-learn (already in requirements? if not, add)
- Need at least 100 closed trades for the model to be meaningful
- Should be a separate scoring layer, not replace conviction
- Surface the prediction in alert text: "ML edge: 64% win probability"

**Acceptance criteria:**
- Model training is reproducible (`python train_ml.py`)
- Cross-validated WR predictions are at least 5% better than random
- Model artifact saved as `data/ml_model.pkl`
- Bot loads it and uses prediction as a confidence multiplier

**Time estimate:** 2 sessions — one to build + train, one to integrate.

**Risk:** With only 9k decisions and ~50 closed trades, model will be
noisy. Should probably wait until we have 200+ closed trades.

---

## Nice-to-haves (lower priority)

### 5. Live Dashboard Server (Option C from the website discussion)
- HTTP server next to bot.py
- Serves dashboard.html + a `/api/data` endpoint with real-time data
- Replaces the 5-min auto-refresh local script
- **Wait until:** bot is profitable AND Wayne wants public URL with auth.

### 6. Subscription Website (Option B)
- Real product. Stripe payments. User accounts. Filtered alerts.
- 2-3 week project. **Wait until:** bot has a track record (60%+ WR over
  100+ trades) and Wayne wants to scale to real customers.

### 7. Topstep Auto-Trading
- Bot places trades directly via TopstepX API
- **CHECK FIRST:** Topstep TOS forbids automated trading on combine.
  Wait until funded account, verify automation is allowed there too.

---

## Items completed this session (Apr 30 PM)

- ✅ 6 new setups: BB_REVERSION, STOCH_REVERSAL, MACD_CROSS (both directions)
- ✅ Per-setup RR floors via SETUP_RR_FLOORS dict + get_rr_floor() helper
- ✅ TopstepX contract filter loosened (3-pass: exact/prefix/contains)
- ✅ /recap command for on-demand daily reports
- ✅ Static HTML dashboard generator
- ✅ Auto-refresh: dashboard.html reloads every 60s, regen script every 5min
- ✅ GitHub Pages setup (see GITHUB_PAGES_SETUP.md)
- ✅ Toned-down sim Telegram alerts

## Items completed Apr 30 AM

- ✅ Dup-guard: blocks same market+setup+direction within 10 min
- ✅ Volume nan fix: 996 silent rejections/week → treated as neutral
- ✅ Dynamic R:R: 1.5R-5R band, 2-3R sweet spot
- ✅ Auto-suspend tightened: 4 trades min, 40% WR floor
- ✅ Strategy log result back-update for queryability
