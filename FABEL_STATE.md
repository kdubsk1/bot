# FABEL_STATE.md
### Living state of the bot. Read this FIRST, before touching anything.
_Last updated: 2026-07-25 (Waves 169-182)_

This file lives in the repo on purpose. Desktop copies go stale; GitHub is the
only source of truth. If you are a new session picking this up, read this whole
file before you write a line of code.

---

## 1. THE MISSION
Pass a Topstep $50,000 evaluation. Balance has been frozen at **$50,591.16**
since Jul 17. Target $53,000. Daily loss limit $1,000, trailing drawdown $2,000.
In 64+ sessions the bot has never blown an account and never passed - both facts
had the same cause: it was gated so tightly it strangled itself.

---

## 2. WHAT WAS WRONG, AND WHAT IS NOW FIXED

| # | Problem (all measured, not assumed) | Fixed by |
|---|---|---|
| 1 | Suspension + conviction gates read a scorecard that was **99.8% paper** (3,472 blended "trades" vs 8 real) | 169, 170, 176 |
| 2 | Bench gate judged **win rate**, which cannot tell a profitable low-hit-rate setup from a losing one | 170 |
| 3 | **All 8 NQ short setups benched**, every one on 0 or 1 real trade - the bot could not short its own eval instrument | 170 |
| 4 | Conviction penalty still paper-driven after 170; NQ:BREAK_RETEST_BULL was docked -12 while **profitable** | 176 |
| 5 | **93% of scan rows ungraded** - `check_missed_setups` skips every `target==0` row forever | 173, 174 |
| 6 | Every automatic message went to the PUBLIC channel, including the **account balance** | 177, 179 |
| 7 | Fired NQ/GC alerts often never opened an eval trade, leaving **no trace anywhere** | 175 (diagnostic) |

### Deployed and verified live
169, 170, 173, 174, 175, 176, 177, 178, 179 (+180, 181, 182 as shipped).

---

## 3. THE NUMBERS THAT MATTER (measured, keep them honest)

**The eval book is PROFITABLE in expectancy.** 359 real closed trades:

    EVAL (NQ + GC)   63W/107L   35.9% WR   2.53R avg win   = +0.269R / trade
    Crypto (BTC+SOL) 35W/144L   19.6% WR                   = losing

At 2.53R average, breakeven is **28.3%**, not 50%. The edge is real. The bot
never passed because it barely fired, not because it cannot win.

**Win rate alone is meaningless.** The proof, from live data:

    BTC:BREAK_RETEST_BEAR  22.2% WR at 3.82R  -> +0.070R  PROFITABLE
    SOL:VWAP_BOUNCE_BULL   15.4% WR at 1.82R  -> -0.566R  genuinely bad

Always judge setups on **expectancy**, never win rate.

---

## 4. KNOWN BUGS STILL OPEN (in priority order)

1. **Eval-open gap (mission critical).** Jul 16-21: 7 real NQ/GC alerts fired,
   ~2 became eval trades. Wave 175 now logs every decision to
   `data/eval_open_trace.jsonl`. **Read that file** - any alert with `attempt`
   but no `opened` is a missed trade and the record says why.
2. **R:R rules are inverted.** rr4+ = **-0.753R** over 702 graded samples
   (3.8% hit rate - fantasy targets) while rr<2 = **+0.304R** and is REJECTED by
   the floors. Live rejects show `RR 2.5 < min 4.0`. Cap R:R ~4, lower the floor.
3. **Regime classifier is trend-blind.** `TRENDING_BULL`/`TRENDING_BEAR` appear
   **0 times in 93,997 rows**. Cause: `regime_classifier.py` L101 demands
   `ema50_slope_pct > 0.15` **per bar** on a 50-period EMA; measured median slope
   is **0.0084%** (~18x too strict). Also `bot.py` L2491 logs the failure at
   DEBUG, so it hides. Recalibrate toward p90 (~0.05-0.08).
4. **ADX is inverted too.** adx<18 = **+0.292R** (rejected by the bot) vs
   adx35+ = **-0.313R** (traded happily). Drop the floor, add a ~35 ceiling.
5. **BREAK_RETEST invalid stops.** 391 rows have the stop on the WRONG SIDE of
   entry - every one a BREAK_RETEST setup. Likely cause of the $0 PnL trades
   (`suggest_contracts` returns contracts=1 "zero_stop", then PnL computes ~0).
6. **103 winners** were killed by `Conviction 45 just short of 48`.

---

## 5. DATA: WHAT EXISTS, AND ITS LIMITS

| source | reality (measured Jul 25) |
|---|---|
| hourly bars | 500 per market (that is OUR request, not a broker cap) |
| daily bars | **31 NQ / 43 GC** - TopstepX genuinely has no more |
| yfinance | **RATE LIMITED on Railway** - Yahoo blocks the datacenter IP |
| strategy_log | ~94k scan rows, but only ~4% were graded before Wave 173 |
| outcomes.csv + `data/archive/outcomes_*.csv` | the REAL trade ledger, 359 closes |

**Do not trust the strategy_log as a price series.** It is a scan log with
uneven hourly coverage; session samples from it came out n=0-7.

### Files that accumulate (never delete these)
    data/eval_open_trace.jsonl        why each eval trade did/didn't open (W175)
    data/grade_backfill_report.json   grading progress every run (W173/174)
    data/session_projection_report.json  projection verdicts (W178/182)
    data/ui_selftest_report.json      which commands work (W180)
    data/wave1NN_audit.json           full before/after for each migration
    data/daily_report_YYYY-MM-DD.txt  permanent per-day record

---

## 6. THE PRICE PROJECTION - STATUS AND THE RULE

Goal: publish "NQ expected 29,670 - 30,020 by 4pm" with a **stated, true** hit rate.

`session_projection.py` fits a band on the **oldest 60%** of sessions and scores
it on the **newest 40% it has never seen**. Fitting a 68% band and reporting it
held 68% of the same data measures nothing.

**Tolerance is the binomial 95% CI for the sample size**, not a fixed number: at
~48 test sessions the sampling error alone is +/-13 points, and an identical
stable market measured 52.1% on one seed and 83.3% on another. A 15-point hard
floor sits on top.

**Blocker:** 500 hourly bars = ~21 days = ~15 weekday sessions per type, below
the 20-session minimum. Wave 182 adds `--deep N`, which raises the TopstepX
request (the 500 was only ever our own number) and always restores it in a
`finally`. If TopstepX serves 5,000 hourly bars that is ~208 days, and the
projection can be validated immediately instead of after months of accumulation.

> **THE RULE: never publish a projection whose verdict is HOLD.**
> If NQ cannot hold its claim, ship Gold-only or ship nothing.

---

## 7. HOW TO WORK ON THIS BOT

* **GitHub is truth.** Desktop copies are stale - one earlier session "fixed" a
  5MB sync cap that had already been raised to 25MB in Wave 59. Always read the
  live file.
* **One numbered wave per change**, strict sequence, no skipped numbers.
* Each wave ships as `DEPLOY_WAVEnnn.bat` that Wayne double-clicks. It fetches
  the live file, patches in memory, and refuses unless **every anchor matches
  exactly once**, `ast.parse` passes, and the non-ASCII count is as expected.
* **Test before he clicks.** Behaviour tests pass BEFORE the deploy script is
  generated. Tests have caught, in this project alone: an anchor that split a
  function call mid-statement, a `TypeError` crash on a string counter, a
  statistically wrong tolerance, and a 404 that refused to create a new file.
* **The data is sacred.** Add fields, never overwrite. Back up before writing.
* Wayne is not an engineer. Explain in plain English, show the measurement, and
  say plainly when you were wrong.

---

## 8. WHAT TO DO NEXT (in order)

1. Read `data/eval_open_trace.jsonl` - it should now name why fired trades never
   reached the eval account. **This is the mission-critical one.**
2. Run `python session_projection.py --deep 5000` and see how many hourly bars
   TopstepX actually serves. That decides whether the projection ships this week
   or waits for accumulation.
3. Run `python ui_selftest.py --verbose` - the ERROR/SUSPECT rows are the UI
   revamp list. (It is slow: it fetches live frames per market.)
4. Fix the R:R inversion (open bug #2) - the single biggest measured leak.
5. Fix the regime classifier (bug #3), then ADX (bug #4).
6. Then: MFE tracking on fired trades (recorded nowhere today), and a fire-rate
   alarm so the bot can never go quiet for five weeks again.
