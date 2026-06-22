# 📅 NQ CALLS Weekly Recap
## Jun 15 — Jun 21, 2026

**Generated:** 2026-06-22 12:01:01 ET

This recap reviews 7 days of real trades AND shadow-tracked signals
(signals where gates/suspension would have blocked — we track their
outcomes to decide which gates, if any, deserve to come back).

---

## 📊 Real Trades This Week

- **Closed trades:** 38  |  **WR:** 23.7%
- **Wins:** 9  **Losses:** 29

### By market

| Market | Trades | Wins | Losses | WR% |
|---|---|---|---|---|
| BTC | 11 | 2 | 9 | 18.2% |
| GC | 4 | 2 | 2 | 50.0% |
| NQ | 11 | 2 | 9 | 18.2% |
| SOL | 12 | 3 | 9 | 25.0% |

## 👻 Shadow Events This Week

**Total shadow events:** 12773
_(Signals where old gates WOULD have blocked. We fired anyway and tracked outcomes.)_

### By gate type

| Gate | Count | Would-Win | Would-Lose | Would-WR% | Verdict |
|---|---|---|---|---|---|
| 2-consecutive-loss halt | 28 | 4 | 21 | 16.0% | ✅ gate was right (blocked losers) |
| Max 3 daily trades cap | 8684 | 0 | 0 | —% | — |
| BTC/SOL correlation lockout | 418 | 0 | 0 | —% | — |
| Loss zone lockout | 35 | 0 | 0 | —% | — |
| Family cooldown after loss | 33 | 0 | 0 | —% | — |
| 3-loss per-market halt | 539 | 0 | 0 | —% | — |
| Per-setup cooldown | 46 | 0 | 0 | —% | — |
| Suspended setup (outcome-tracked) | 2990 | 0 | 0 | —% | — |

## 🚫 Suspended Setups Review

Suspended setups detected this week (not fired as alerts, but outcomes tracked):

| Setup | Detected | Would-Win | Would-Lose | Would-WR% | Recommendation |
|---|---|---|---|---|---|
| BTC:BB_REVERSION_BEAR | 159 | 0 | 0 | —% | — |
| BTC:BB_REVERSION_BULL | 175 | 0 | 0 | —% | — |
| BTC:BREAK_RETEST_BEAR | 103 | 0 | 0 | —% | — |
| BTC:BREAK_RETEST_BULL | 210 | 0 | 0 | —% | — |
| BTC:EMA21_PULLBACK_BEAR | 44 | 0 | 0 | —% | — |
| BTC:EMA21_PULLBACK_BULL | 47 | 0 | 0 | —% | — |
| BTC:EMA50_RECLAIM | 30 | 0 | 0 | —% | — |
| BTC:MACD_CROSS_BEAR | 132 | 0 | 0 | —% | — |
| BTC:MACD_CROSS_BULL | 52 | 0 | 0 | —% | — |
| BTC:RSI_DIV_BEAR | 17 | 0 | 0 | —% | — |
| BTC:STOCH_REVERSAL_BULL | 87 | 0 | 0 | —% | — |
| BTC:VWAP_BOUNCE_BULL | 180 | 0 | 0 | —% | — |
| BTC:VWAP_REJECT_BEAR | 359 | 0 | 0 | —% | — |
| GC:BREAK_RETEST_BEAR | 19 | 0 | 0 | —% | — |
| GC:EMA21_PULLBACK_BEAR | 17 | 0 | 0 | —% | — |
| GC:MACD_CROSS_BULL | 63 | 0 | 0 | —% | — |
| GC:STOCH_REVERSAL_BULL | 19 | 0 | 0 | —% | — |
| GC:VWAP_BOUNCE_BULL | 302 | 0 | 0 | —% | — |
| GC:VWAP_REJECT_BEAR | 131 | 0 | 0 | —% | — |
| NQ:BB_REVERSION_BEAR | 101 | 0 | 0 | —% | — |
| NQ:BREAK_RETEST_BEAR | 36 | 0 | 0 | —% | — |
| NQ:BREAK_RETEST_BULL | 68 | 0 | 0 | —% | — |
| NQ:EMA21_PULLBACK_BULL | 69 | 0 | 0 | —% | — |
| NQ:EMA50_RECLAIM | 2 | 0 | 0 | —% | — |
| NQ:MACD_CROSS_BEAR | 6 | 0 | 0 | —% | — |
| NQ:MACD_CROSS_BULL | 28 | 0 | 0 | —% | — |
| NQ:RSI_DIV_BEAR | 7 | 0 | 0 | —% | — |
| NQ:RSI_DIV_BULL | 1 | 0 | 0 | —% | — |
| NQ:STOCH_REVERSAL_BEAR | 76 | 0 | 0 | —% | — |
| SOL:BB_REVERSION_BULL | 120 | 0 | 0 | —% | — |
| SOL:BREAK_RETEST_BEAR | 191 | 0 | 0 | —% | — |
| SOL:BREAK_RETEST_BULL | 38 | 0 | 0 | —% | — |
| SOL:EMA21_PULLBACK_BEAR | 6 | 0 | 0 | —% | — |
| SOL:EMA21_PULLBACK_BULL | 62 | 0 | 0 | —% | — |
| SOL:RSI_DIV_BEAR | 18 | 0 | 0 | —% | — |
| SOL:STOCH_REVERSAL_BEAR | 3 | 0 | 0 | —% | — |
| SOL:STOCH_REVERSAL_BULL | 12 | 0 | 0 | —% | — |

## ⚖️ Gate Value Analysis

For each removed gate: when it would have blocked, did the signal actually lose?
- **Low would-WR%** → gate had value (would have saved us from losers)
- **High would-WR%** → gate was wrong (would have blocked winners)

### 🟢 Gates worth re-adding (blocked losers)

- **2-consecutive-loss halt:** 4W / 21L at 16.0% WR → if blocked, would have saved losses.

### ⏳ Need more data

- **3-loss per-market halt:** 539 events, only 0 resolved — keep watching.
- **BTC/SOL correlation lockout:** 418 events, only 0 resolved — keep watching.
- **Max 3 daily trades cap:** 8684 events, only 0 resolved — keep watching.
- **Per-setup cooldown:** 46 events, only 0 resolved — keep watching.
- **Family cooldown after loss:** 33 events, only 0 resolved — keep watching.
- **Loss zone lockout:** 35 events, only 0 resolved — keep watching.


## 🎯 Weekly Conclusions

- ⚠️ Real WR this week: 24% — significantly below break-even.
  Conviction score may not be predictive. Review signal quality.
- 📊 12773 shadow events this week — data forming for gate review.