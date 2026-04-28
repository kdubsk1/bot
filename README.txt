NQ CALLS BOT - QUICK REFERENCE
================================
Built with Claude | NQ CALLS 2026

HOW TO START:
  Local:   Double-click "START BOT.bat" (or "START_WATCHDOG.bat" for auto-restart)
  Cloud:   Already running 24/7 on Railway — just open Telegram and type /start

════════════════════════════════
FOLDER STRUCTURE (post-cleanup Apr 27 2026)
════════════════════════════════
Trading bot/
├── bot.py                  ← engine (runs everything)
├── outcome_tracker.py      ← brain (learns from trades, suspends bad setups)
├── strategy_log.py         ← memory (every scan decision logged)
├── safe_io.py              ← atomic writes + cross-process locks
├── data_layer.py           ← market data feeds (TopstepX → TwelveData → yfinance)
├── auto_sync.py            ← commits data to GitHub every 6h (persistence)
├── sim_account.py          ← paper trading sim (Topstep eval rules)
├── position_sizer.py       ← Bayesian Kelly + survival sizing
├── session_clock.py        ← DST-aware NY/futures session events
├── regime_classifier.py    ← TRENDING_BULL/BEAR / RANGING / VOLATILE_EXPANSION
├── dashboard.py            ← HTML performance dashboard generator
├── strategy_review.py      ← multi-day strategy analysis
├── live_brief.py           ← on-demand market brief per market
├── session_recap.py        ← daily 4PM session recap (markdown + Telegram)
├── weekly_recap.py         ← Monday 8AM weekly rollup
├── config.py               ← Telegram credentials (env-var first, hardcoded fallback)
├── watchdog.py             ← LOCAL auto-restart (Railway uses bot.py directly)
├── markets/
│   ├── market_NQ.py        ← NQ specific settings
│   ├── market_GC.py        ← Gold specific settings
│   ├── market_BTC.py       ← BTC specific settings
│   └── market_SOL.py       ← SOL specific settings
├── data/
│   ├── strategy_log.csv    ← every scan decision (47 columns)
│   ├── setup_performance.json  ← learning data (per-setup W/L)
│   ├── suspended_setups.json   ← auto-banned setups
│   ├── archive/                ← per-session outcome archives
│   └── ... (plus cooldowns, recap files, scanner state, etc.)
├── outcomes.csv            ← every trade logged (auto-created)
├── BACKLOG.md              ← outstanding work (replaces all old BATCH docs)
├── requirements.txt        ← Python packages
├── Procfile                ← Railway start command
├── runtime.txt             ← Python version
├── START BOT.bat           ← double-click to run locally
└── START_WATCHDOG.bat      ← double-click for auto-restart

════════════════════════════════
TELEGRAM COMMANDS
════════════════════════════════
/start        Start the bot, show menu
/menu         Show control panel anytime
/stats        Performance breakdown — current session
/open         List open trades
/win  [ID]    Mark trade as WIN (most recent or by ID)
/loss [ID]    Mark trade as LOSS
/skip [ID]    Mark trade as SKIP (didn't take it — no learning effect)
/report       Today's daily report
/analyze      Strategy log analysis dump
/brief        Live brief for all enabled markets
/dashboard    Generate HTML performance dashboard
/review [N]   Strategy review (default 7 days, e.g. /review 14)

/session      Current session stats only
/history      Most recent archived session (or /history YYYY-MM-DD)
/lifetime     Lifetime stats across all sessions

/rejected     Last 10 rejected/almost-fired scan decisions
/detections [MKT]  Last 20 raw detections w/ context (or filter by NQ/GC/BTC/SOL)

/sync         Force commit data + outcomes.csv to GitHub now
/help         Quick guide

Sim controls:
/simon        Enable sim trading
/simoff       Disable sim trading
/simstatus    Show sim account state
/simreset [PRESET]  Reset sim (50k / 100k / 150k)
/mnq          Toggle MNQ (micro) vs NQ (full)
/simweekly    7-day sim P&L summary

════════════════════════════════
MENU (inline buttons)
════════════════════════════════
Scanner toggle | NQ/GC/BTC/SOL toggles | WIN/LOSS/SKIP/Open
Status | Stats | Learned | Help
Morning brief | Asia brief | Report | Analyze | Live
SIM toggle | MNQ/NQ | Today P&L | Reset preset | Weekly
Conviction min | Scan interval | Cooldown | Risk %
Session | History | Lifetime
Test | Rescore toggle | RR info | Dashboard

════════════════════════════════
HOW TRADE TRACKING WORKS
════════════════════════════════
1. Bot fires alert → logged to outcomes.csv as OPEN
2. Every scan, bot checks if price hit target or stop (using candle HIGH/LOW range)
3. If target hit → auto-marked WIN → learning updates
4. If stop hit  → auto-marked LOSS → learning updates
5. If you closed early → use /win or /loss manually
6. If you didn't take it → /skip (no learning effect)
7. After 5+ trades on a setup → bot adjusts conviction score
8. After 5+ trades AT < 35% WR → setup auto-SUSPENDED (still shadow-tracked)
9. Suspended setup recovers if shadow-tracked WR climbs above 45%

════════════════════════════════
SETUP TYPES (16 confirmed setups)
════════════════════════════════
LIQ_SWEEP_BULL/BEAR              Bullish/bearish liquidity sweep
EMA50_RECLAIM / EMA50_BREAKDOWN  EMA50 reclaim / breakdown
EMA21_PULLBACK_BULL/BEAR         Trend pullback to EMA21
VWAP_BOUNCE_BULL                 VWAP bullish bounce (★ 83% WR)
VWAP_REJECT_BEAR                 VWAP bearish rejection
VWAP_RECLAIM                     VWAP reclaim after 3+ bars below
BREAK_RETEST_BULL/BEAR           Broken level retested
RSI_DIV_BULL/BEAR                RSI divergence
APPROACH_SUPPORT/RESIST          Approaching key level (HEADS UP only)
FAILED_BREAKDOWN_BULL            Failed breakdown (bear trap reversal)
FAILED_BREAKOUT_BEAR             Failed breakout (bull trap reversal)
VOLATILITY_CONTRACTION_BREAKOUT  Squeeze breakout
HTF_LEVEL_BOUNCE                 Bounce off 1H key level w/ engulfing/pin
OPENING_RANGE_BREAKOUT           NQ/GC 9:30-10:30 ET only

════════════════════════════════
CONVICTION TIERS
════════════════════════════════
🔥 HIGH    80+    Strong setup — full size (5 MNQ / 1 NQ cap)
✅ MEDIUM  65-79  Good setup — normal size
⚡ LOW     50-64  Weaker setup — smaller size
REJECT    <50    Not fired — filtered out

Dynamic R:R: HIGH ≥ 1.5R, MEDIUM ≥ 2.0R, LOW ≥ 2.5R
Targets are real swing levels only — no fabricated multiples.

════════════════════════════════
MARKETS
════════════════════════════════
NQ   NQ Futures (Nasdaq 100) — 15m + 1h entry — 9:30 AM-3:30 PM, 6 PM-3:30 PM ET
GC   Gold Futures            — 1h + 4h entry  — same hours as NQ
BTC  Bitcoin                 — 15m + 1h entry — 24/7
SOL  Solana                  — 15m + 1h entry — 24/7

Topstep rules baked in:
  - No new NQ/GC entries 3:30-4:10 PM ET
  - Force-flatten NQ/GC at 4:10 PM ET
  - NQ/GC reopen 6 PM ET
  - Crypto unaffected

════════════════════════════════
AUTO BRIEFS / RECAPS
════════════════════════════════
🌅 8:30 AM ET  Morning brief — NQ/GC/BTC/SOL bias
🌙 6:00 PM ET  Asia brief — overnight focus
📋 8:00 PM ET  Daily report — today's performance
📅 Mon 8 AM ET Weekly recap — gate-value analysis + suspension review
🔬 Every 10 closed trades  Auto strategy review

════════════════════════════════
NEWS AWARENESS
════════════════════════════════
High-impact windows: 8:30 AM, 9:30 AM, 2:00 PM, 4:00 PM ET
- 15m setups skipped during news
- Higher R:R required for 1h setups
- Alert includes ⚠️ news warning

════════════════════════════════
PERSISTENCE / DATA SAFETY
════════════════════════════════
Auto-sync commits data/ + outcomes.csv to GitHub every 6h.
Manual /sync available anytime.
safe_io.py provides atomic writes + cross-process locks — no row clobbering.
Watch Paths config in Railway: data-only commits don't trigger redeploys.

════════════════════════════════
TO EDIT A MARKET'S SETTINGS
════════════════════════════════
Open markets/market_NQ.py (or GC, BTC, SOL).
Change MIN_RR, MIN_ADX, COOLDOWN_MIN, ADX_MIN_BY_SETUP, etc.
Save and push to GitHub — Railway auto-deploys on .py changes.

NQ CALLS 2026 — Built with Claude
