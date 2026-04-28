# NQ CALLS — BACKLOG

**Source of truth for outstanding work.** Replaces all the old BATCH_*.md and PROMPT_*.md files (deleted Apr 27, 2026 as part of the post-data-loss-fix cleanup). Anything that was already shipped is removed; everything below is real work waiting to be done.

Last cleanup: **Mon Apr 27 2026** by Claude/Wayne.

---

## 🟢 SHIPPED (just to remember what's already done — don't redo)

- **Persistence:** auto_sync.py committing data/ + outcomes.csv to GitHub every 6h (commit `8d93b01` and earlier). Manual `/sync` Telegram command.
- **Data-loss fix (Apr 27):** safe_io.py — atomic writes + cross-process file locks. strategy_log.py and outcome_tracker.py routed through it. Fixed the "7k → 3k row clobber" race in `check_missed_setups` and the truncate-write race in `_write_all`/`_save_performance`/`_save_suspended_setups`. Commit `1a53217`.
- **Auto-deploy loop fix (Apr 27):** Railway Watch Paths configured to `**/*.py`, `requirements.txt`, `Procfile`, `runtime.txt`, `nixpacks.toml`. Data-only commits no longer trigger redeploys.
- **Pre-Batch (Apr 20):** Removed all hard halts (DAILY_LOSS_GATE, market halt, correlation lockout, profit lock, max trades, zone lockout, family cooldown, per-setup cooldown). All shadow-logged so we can analyze "would have blocked" outcomes.
- **Topstep 4:10 PM rule:** No new NQ/GC entries 3:30-4:10 PM ET. Force-flatten at 4:10. Reopen 6:00 PM. Crypto unaffected.
- **Daily Recap:** session_recap.py generates `data/recap_YYYY-MM-DD.md` at 4PM session close + Telegram summary.
- **Weekly Recap:** weekly_recap.py runs Mondays 8AM ET, aggregates 7 days of real + shadow outcomes, gate-value analysis, suspension recommendations.
- **Batch 2A (Observability):** strategy_log.csv expanded to 47 columns. Every scan logs raw indicator snapshot (BB, Stoch, MACD, EMA20/50/200/21, VWAP, ATR, swings, volume, regime, session) plus score breakdown, confidence factors, detection reason. New `/detections` Telegram command.
- **TopstepX integration:** Primary data source for NQ/GC. Probes contracts on startup. TwelveData → yfinance fallback chain.
- **Data feed fix:** TwelveData symbol candidates expanded (NDX, QQQ, GLD, etc.). yfinance throttle 1s → 3s + per-ticker 90s cooldown. Stale cache fallback on rate limit.
- **Session boundary safety net:** 4PM close fires even if SessionClock missed. 8PM daily report scheduler.

---

## 🔴 OUTSTANDING WORK

### Bucket 1 — Strategy fixes (data-driven, from Apr 23 audit)

These came out of the audit where the bot was at 30% WR. They're the highest-leverage strategy improvements waiting to land. Most of the file references below need to be re-verified before editing — files may have shifted since the original prompts were written.

**1. structure_target() guards — Finding #1**
File: `outcome_tracker.py`, function `structure_target`.
- Add minimum stop distance: if `abs(entry - stop) < 0.5 * atr_val`, return `(0, 0, "stop_too_tight")`. Prevents firing on noise-level stops that get hit in 0-3 bars.
- Change swing lookback from 5 to 20 bars (real structure, not 11-bar noise). Look for `swing_points(df, 5)` calls inside `nearest_swing_level` and add a parameter rather than changing the global default — verify no other caller breaks.
- DELETE the NQ strong-trend override (`if market == "NQ" and abs(trend_score_val) >= 7: min_rr = min(min_rr, 1.2)`). Default min_rr 1.5 should apply uniformly. Strong trends are when reversals are MOST likely, not least.
- Add max RR cap of 4.0: if `rr > 4.0`, reject as `"rr_too_high"`. RRs above 4 are rarely reached and indicate either noise stop or unreachable target.

**2. Position sizer 1-contract validation lock — Finding #11**
File: `position_sizer.py`, class `EvalPositionSizer`.
- Read rolling 20-trade WR from `outcomes.csv` (closed WIN/LOSS only).
- If `rolling_20_wr < 0.50`, force `contracts = 1` regardless of Kelly calculation.
- If `< 20 closed trades` total, default to locked.
- Cache WR for 60s to avoid CSV re-reads.
- Log on each sizing call: `[SIZER] Validation lock active (20-trade WR = X.X%) — forcing size = 1` or `[SIZER] Validation lock lifted (...)` when it crosses 50%.

**3. Time-of-day + base conviction reduction — Findings #7 + #2**
- Block crypto setups 2-5 AM ET (lowest-quality window historically).
- Drop conviction base score from 30 → 15. Setups now have to earn more of their score from actual factors, not just exist.

**4. Volume gating at scan level — Findings #6 + #9**
- Hard reject any setup where `vol_ratio < 0.8` at scan time. Currently we just penalize in conviction; a hard gate is more honest.
- Use session-aware rolling baseline (London/NY/Asia have different normal volumes — using 24h rolling overstates Asia activity).

**5. detect_setups volume context — Finding #3**
File: `outcome_tracker.py::detect_setups`.
- Some setups (BREAK_RETEST, FAILED_BREAK*) already have `vol_ratio` checks inline. Audit the rest. Setups without volume confirmation should require it as a condition, not a bonus.

**6. Multi-TF outcome tracking — Finding #4**
- Currently outcome tracking uses 15m candle range. For 1h/4h setups, we should be checking the 1h/4h candle range, otherwise we're undermeasuring time-to-win for slower setups.

**7. Lower MIN_RR for VWAP_BOUNCE_BULL**
- This setup is at 83% WR (best in the bot) but min_rr requirements are blocking ~60% of detections.
- Lower its tier-specific min_rr by 0.5 across the board. Better to take 1.5R wins than miss 3R wins entirely.

**8. Lower GC VWAP_BOUNCE_BULL ADX threshold**
- Currently 18, drop to 10. Gold's structure is different — it bounces VWAP cleanly even in low-ADX regimes.
- File: `markets/market_GC.py` → `ADX_MIN_BY_SETUP`.

**9. Auto-calibrating Bayesian conviction (Batch 2C — supersedes _performance_bonus)**
File: `outcome_tracker.py::_performance_bonus` → rename and rewrite as `_performance_adjustment`.
- Bayesian prior: 50% WR with 10 pseudo-trades (prevents small-sample overreaction).
- Sample confidence scales 0.0 (no data) → 1.0 (40+ trades).
- Range: +30 (100% WR with 40+ trades) to -40 (0% WR with 40+ trades). Losses penalize harder than wins reward.
- Returns `(adjustment_points, reason_string)` so we can log WHY the adjustment changed conviction.
- Update `conviction_score()` to call the new function and feed the reason into the score breakdown.

---

### Bucket 2 — Eval-Loop Mode (Batch 2B — paused for Opus 4.7 review)

The full vision: bot lives in continuous "trying to pass a $50k Topstep eval" mode. Always scanning, always sim'ing, auto-restart on bust or pass, archive every attempt forever.

**Why it was paused:** Opus 4.7 pushed back — "eval tracking built on a sim that doesn't record trades correctly is a feature that will produce confidently-wrong archives." Need to verify sim recording works end-to-end with live data first.

**The 12 features (when ready):**

1. **Scanner always ON by default** — change `SETTINGS["scanner_on"]` default to `True`, update `_load_scanner_state()` defaults. Button label: `🟢 SCANNING • NQ GC BTC SOL` when on, `⏸ PAUSED — tap to resume` when off.
2. **Sim always ON, toggle removed** — `DEFAULT_STATE["enabled"] = True`, force in `load_state()`, deprecate `toggle_sim` callback.
3. **Balance carries day-to-day (THE CRITICAL FIX)** — currently 4PM ET wipes balance via `_reset_to_fresh_preset()`. Add `_reset_daily_counters()` that ONLY resets `today_pnl`/`today_date`/`session_date`, KEEPS balance/peak/total_pnl/trades. Update `_ensure_session_current` to call `check_eval_outcome()` first, only do full reset on bust/pass.
4. **Eval outcome detection** — new `sim_account.check_eval_outcome(state)` function. Statuses: `BUSTED_MAX_DD`, `BUSTED_DAILY_LOSS`, `PASSED_TARGET`, `ACTIVE`. Pass requires `days_traded >= 2` (Topstep rule).
5. **Eval history archive** — new `data/eval_history.json`. New functions: `_archive_eval_attempt`, `_generate_eval_summary`. Per-eval narrative: best setup, worst setup, peak balance, longest streaks, key observation.
6. **Auto-restart on bust or pass** — `_handle_eval_ended(app, outcome)` archives, resets, increments `eval_attempt_num`, sends two Telegram messages (eval result + new eval dashboard).
7. **Pass streak tracking** — `🔥 Streak 3` in menu when ≥ 2.
8. **Smart eval IDs** — `eval_2026-04-18_a` with letter suffix for same-day attempts.
9. **Data outage safety net** — no-data days don't count as busts. `_had_real_trading_today(state)`: returns False if no closed trades today + no opens today + today_pnl == 0.
10. **Position sizing caps** — clamp at 5 MNQ / 1 NQ / 1 GC in BOTH `format_sim_block()` AND `suggest_contracts()` (defense in depth).
11. **Consolidated startup message** — replace current 4-5 separate messages with ONE message containing: scanner state, open trades, suspensions, data source health, eval status, observability, market bias. Smart truncation if > 4096 chars.
12. **Menu redesign** — `[📘 Eval #1 • Day 2] [+$127 today] [🏆 0P / 💀 0B]` row with progress bars: profit target (🟢🟢🟢🟢🟢⚪⚪⚪⚪⚪ 50%), drawdown cushion (🛡🛡🛡🛡🛡🛡🛡⬜⬜⬜ 70% safe), daily limit (🟩🟩🟩🟩🟩🟩🟩🟩⬜⬜ 80% left).

**Open questions before building:**
- Did sim P&L investigation resolve? Confirm sim records trades correctly end-to-end with live data.
- "2 trading days minimum" — exact Topstep wording? Days with a trade vs days account active vs calendar days.
- Topstep consistency rule (best day ≤ 50% of total profit) — model now or defer?

---

### Bucket 3 — Topstep account

- **Combine activation:** No reply from `dashboard@projectx.com` since Apr 22 inquiry. Treating silence as green light. Just need the Combine to start.

---

## 📋 NOTES FOR FUTURE CLAUDE/OPUS SESSIONS

**Bot architecture (verified Apr 27):**
- `bot.py` — engine (3500+ lines)
- `outcome_tracker.py` — brain: setup detection, conviction scoring, learning, suspension system
- `strategy_log.py` — memory: every scan decision logged to `data/strategy_log.csv`
- `safe_io.py` — file safety: atomic writes + cross-process locks (THE thing that fixes data loss)
- `data_layer.py` — data feeds: TopstepX → TwelveData → yfinance
- `auto_sync.py` — persistence: commits data/ + outcomes.csv to GitHub every 6h
- `sim_account.py` — paper trading sim with Topstep eval rules + edge tracking
- `dashboard.py`, `strategy_review.py`, `live_brief.py`, `session_recap.py`, `weekly_recap.py` — analytics
- `regime_classifier.py` — TRENDING_BULL / TRENDING_BEAR / RANGING / VOLATILE_EXPANSION
- `position_sizer.py` — Bayesian Kelly + survival constraint sizer (used by sim_account)
- `session_clock.py` — DST-aware NY/futures session events
- `markets/market_*.py` — per-market config (ADX mins, setup tunings, conviction factors)
- `watchdog.py` + `START_WATCHDOG.bat` — LOCAL only auto-restart (Railway uses `python -u bot.py` directly per `Procfile`)
- `_archive/backtest.py`, `_archive/backtest_analysis.py` — standalone CLI tools, not part of runtime (gitignored)

**Wayne's preferences:**
- Non-developer, needs complete drop-in files — never partial diffs.
- Pre-mortem habit: 3-6 likely user questions answered inline before sending Claude Code prompts.
- Browser batching over single tool calls.
- API keys never in code or git — only Railway env vars or desktop `.txt` files (gitignored).
- All trades executed manually on TopstepX phone app.

**Known constraints:**
- Topstep: no NQ/GC entries 3:30-4:10 PM ET; reopen 6 PM ET. Crypto 24/7.
- Budget: $14.50/mo TopstepX, ~$5/mo Railway.
- TwelveData free tier doesn't support futures — uses NDX/QQQ/GLD as proxies.

**Don't repeat:**
- Don't ship code without verifying current file state first — assumed line numbers are usually wrong.
- Don't pre-write line counts in prompts — files have changed.
- Don't claim a fix is shipped unless you can see it in the actual file via `read_text_file`.
