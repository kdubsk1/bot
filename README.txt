NQ CALLS BOT - QUICK REFERENCE
================================
Built with Claude (FABEL) | NQ CALLS 2026
Updated: June 2026 (Wave 63)

WHAT THIS BOT IS NOW
====================
An autonomous trading-alert bot for NQ futures, Gold (GC), BTC and SOL
that scores every setup from EVIDENCE, not vibes:

- Conviction score = the shrunk historical win rate (0-100) of that
  exact market:setup combination, learned continuously from real trades
  AND shadow outcomes. HIGH tier >= 60, MEDIUM >= 53, LOW >= 48,
  below 48 = REJECT (shadow-only).
- Risk/reward floor = computed from the same stats: breakeven RR for a
  win rate p is (1-p)/p; the bot demands a 25% margin above breakeven
  (clamped 0.8R - 2.5R). Strong buckets may take closer targets; weak
  buckets must be paid more to play.
- Unproven combinations NEVER risk money. They shadow-track until they
  earn at least 5 resolved outcomes and a passing win rate. Shadow
  resolution is first-touch honest: if price hit both target and stop,
  whichever was touched FIRST decides (ties count as losses).
- No data is ever deleted. Every scan decision, outcome, and shadow
  result is logged and backed up to GitHub.

There are NO hand-written win-rate claims in this file. Live, current
numbers come from /stats, /performance and /edge - those read the real
learning files.

DATA SOURCES
============
- NQ / GC intraday (15m/1h/4h): TopstepX (real CME futures data)
- NQ / GC daily: yfinance continuous contracts (~2 years of history)
- BTC / SOL: ccxt exchanges (coinbase/kraken/bybit) with fallbacks
- Per-timeframe caching + circuit breakers keep providers healthy.

HOW TO START
============
  Double-click "START BOT.bat"  (local)
  Production runs on Railway and auto-deploys from GitHub main.
  Then open Telegram and type /start

TELEGRAM COMMANDS
=================
/start    Start the bot and show the menu
/menu     Show the control panel anytime
/stats    Full performance breakdown + live win rates
/open     See all currently tracked open trades
/win      Mark most recent trade as WIN   (/win ID for specific)
/loss     Mark most recent trade as LOSS  (/loss ID for specific)
/skip     Mark most recent trade as SKIP  (/skip ID for specific)
/performance  Per-bucket scoring drift view
/edge     Per-setup edge estimates

NOTE: the "Conv" number in Settings is from the old 0-100 hype scale
and no longer gates anything; the live gate is conviction >= 48 on the
win-rate scale (set in code, Wave 60).

DEPLOY WORKFLOW (one wave = one purpose)
========================================
Each change ships as DEPLOY_WAVE<N>.bat -> deploy_wave<N>_*.py which:
fetches LIVE source from GitHub, patches in memory, verifies (compile,
exact anchors, dependency checks, no new non-ASCII), backs up originals
locally, then commits atomically. Railway auto-redeploys. Re-running a
wave is always safe (idempotent markers).

KEY FILES
=========
bot.py              engine (scan loop, gates, Telegram)
outcome_tracker.py  brain (conviction score, RR floors, learning)
strategy_log.py     records EVERY scan decision + shadow resolution
data_layer.py       unified data module (TopstepX/yfinance/TwelveData/ccxt)
auto_sync.py        pushes data/ to GitHub on schedule
data/setup_performance.json  the learning file (per market:setup W/L)
ROADMAP_waves_60plus.md      current plan and shipped-wave log
