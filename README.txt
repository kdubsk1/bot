NQ CALLS BOT - QUICK REFERENCE
================================
Built with Claude | NQ CALLS 2026

HOW TO START:
  Double-click "START BOT.bat"
  Then open Telegram and type /start

════════════════════════════════
FOLDER STRUCTURE
════════════════════════════════
Trading bot/
├── bot.py                  ← engine (runs everything)
├── outcome_tracker.py      ← brain (learns from trades)
├── markets/
│   ├── market_NQ.py        ← NQ specific settings
│   ├── market_GC.py        ← Gold specific settings
│   ├── market_BTC.py       ← BTC specific settings
│   └── market_SOL.py       ← SOL specific settings
├── data/
│   └── setup_performance.json  ← learning data (auto-created)
├── outcomes.csv            ← every trade logged (auto-created)
├── bot_log.txt             ← full bot log (auto-created)
├── requirements.txt        ← Python packages needed
└── START BOT.bat           ← double-click to run

════════════════════════════════
TELEGRAM COMMANDS
════════════════════════════════
/start    Start the bot and show the menu
/menu     Show the control panel anytime
/stats    Full performance breakdown + win rates
/open     See all currently tracked open trades
/win      Mark most recent trade as WIN
/win ID   Mark specific trade as WIN (use ID from /open)
/loss     Mark most recent trade as LOSS
/loss ID  Mark specific trade as LOSS
/skip     Mark most recent trade as SKIP (didn't take it)
/skip ID  Mark specific trade as SKIP

════════════════════════════════
MENU BUTTONS
════════════════════════════════
Scanner ON/OFF      Turn scanning on or off
✅/❌ NQ/Gold/BTC/SOL  Toggle markets on or off
📊 Status           Full bot status + cooldowns
📈 Stats            Win/loss breakdown
🌅 Morning Brief    Send today's US session brief now
🌙 Asia Brief       Send overnight/Asia brief now
🧪 Test Alert       Send test alert to NQ CALLS
🧠 What I Learned   See what setups are winning/losing

Settings (tap to cycle through options):
  Conv: XX+           Minimum conviction score (50-80)
  R:R: X.X+           Minimum risk/reward ratio
  Interval: Xm        How often the bot scans
  Cooldown: Xm        Min time between alerts per market
  Risk %: X.X         Account risk % for leverage calc
  Rescore: ON/OFF     Live trade re-scoring on/off

════════════════════════════════
HOW TRADE TRACKING WORKS
════════════════════════════════
1. Bot fires an alert → logged to outcomes.csv as OPEN
2. Every scan, bot checks if price hit target or stop
3. If target hit → auto-marked WIN → learning updates
4. If stop hit  → auto-marked LOSS → learning updates
5. If you closed early → use /win or /loss manually
6. If you didn't take it → use /skip (no learning effect)
7. After 5+ trades on a setup → bot starts adjusting scores
8. Use /stats anytime to see how each setup is performing

════════════════════════════════
SETUP TYPES
════════════════════════════════
LIQ_SWEEP_BULL      Bullish liquidity sweep (CONFIRMED)
LIQ_SWEEP_BEAR      Bearish liquidity sweep (CONFIRMED)
EMA50_RECLAIM       Price reclaimed EMA50 (CONFIRMED)
EMA50_BREAKDOWN     Price broke below EMA50 (CONFIRMED)
VWAP_BOUNCE_BULL    VWAP bullish bounce (CONFIRMED)
VWAP_REJECT_BEAR    VWAP bearish rejection (CONFIRMED)
EMA21_PULLBACK_BULL Trend pullback to EMA21, bounced (CONFIRMED)
EMA21_PULLBACK_BEAR Trend pullback to EMA21, rejected (CONFIRMED)
BREAK_RETEST_BULL   Broken resistance retested as support (CONFIRMED)
BREAK_RETEST_BEAR   Broken support retested as resistance (CONFIRMED)
RSI_DIV_BULL        RSI bullish divergence (CONFIRMED)
RSI_DIV_BEAR        RSI bearish divergence (CONFIRMED)
APPROACH_SUPPORT    Price approaching support (HEADS UP)
APPROACH_RESIST     Price approaching resistance (HEADS UP)

════════════════════════════════
CONVICTION TIERS
════════════════════════════════
🔥 HIGH    80+   Strong setup — full size
✅ MEDIUM  65-79  Good setup — normal size
⚡ LOW     50-64  Weaker setup — smaller size
REJECT    <50   Not fired — filtered out

════════════════════════════════
MARKETS
════════════════════════════════
NQ   NQ Futures (Nasdaq 100) — 15m + 1h entry
GC   Gold Futures            — 1h + 4h entry
BTC  Bitcoin                 — 15m + 1h entry
SOL  Solana                  — 15m + 1h entry

All markets scan 24/7 — good setups fire any time.
Each market has its own quality filters and settings.

════════════════════════════════
AUTO BRIEFS
════════════════════════════════
🌅 8:30am EST   US session brief — all markets
               Reads 15m, 1h, 4h and writes its own bias
🌙 6:00pm EST   Asia/overnight brief — BTC, SOL, Gold focus

════════════════════════════════
NEWS AWARENESS
════════════════════════════════
During high-impact windows (8:30am, 9:30am, 2pm, 4pm):
- 15m setups are skipped entirely
- Higher R:R required for 1h setups
- Alert includes ⚠️ news warning

════════════════════════════════
NEW TOOLS
════════════════════════════════
/dashboard       Generate HTML performance dashboard
/review          Run 7-day strategy review
/review 14       Review last 14 days
🔬 Analyze       Strategy log analysis (in menu)

python backtest.py --market NQ --days 90
python backtest.py --all --days 60 --save-csv
python dashboard.py --open
python strategy_review.py --days 14 --verbose

════════════════════════════════
TO EDIT A MARKET'S SETTINGS:
Open markets/market_NQ.py (or GC, BTC, SOL)
Change MIN_RR, MIN_ADX, COOLDOWN_MIN etc
Save and restart the bot — no other files need changing

NQ CALLS 2026 — Built with Claude
