# rules.py — the single source of truth for what the bot is allowed to do.
# Wave 224: born with one switch. Wave 225: the rulebook proper.
#
# Everything the bot is allowed to do should end up in this file. Change a
# number here, redeploy, and the behaviour changes. Nothing else edits these.

# ---------------------------------------------------------------- Wave 224
ADAPTIVE_OFF = True   # True = no learned bonuses, no probation, no edge gates,
                      #        no sim/sizer rails in the fire path. Fixed rules only.

# ---------------------------------------------------------------- Wave 225

# Minimum R:R a setup must offer to be called, per market.
#
# Chosen from REAL fired+graded rows only (WIN/LOSS, never WOULD_*): 431
# trades, 13 Apr - 11 Sep 2026, deduped by alert_id across outcomes.csv and
# data/archive/. Expectancy, not win rate, per Wayne's rule 8.
#
#   NQ   1.5-2.0R  -0.396R (n=14)   2.0-2.5R  +0.088R (n=76)   -> floor 2.0
#   GC   1.5-2.0R  +0.083R (n=12)   2.0-2.5R  +0.383R (n=40)   -> floor 2.0
#   BTC  2.0-2.5R  -0.253R (n=48)   2.5-3.0R  -0.267R (n=34)   -> floor 2.5
#   SOL  negative in every band                                -> floor 2.5
#
# NOTE, honestly: this is NOT the loosening the wave name implies. Real rows
# say rr>=2.5 (+0.112R, n=193) beats rr<2.5 (-0.024R, n=238). The old
# "rr<2 = +0.304R" claim could not be reproduced on real rows -- it came from
# shadow rows, which run -0.6 to -0.9R in EVERY band and carry no R:R signal.
# The real win here is determinism: SETTINGS["min_rr"] had no persistence and
# silently reset to 1.5 on every restart.
MIN_RR = {"NQ": 2.0, "GC": 2.0, "BTC": 2.5, "SOL": 2.5}
MIN_RR_DEFAULT = 2.5

# Maximum R a swing target may price at before it is rejected as unusable.
# Wave 75's values, moved here unchanged. They were previously hardcoded in
# TWO places that had to be kept in sync by hand.
#
# _WAVE234_HOLIDAYS comment correction (no value change): an earlier note here said
# the 3.0R+ bucket is positive and GC's cap cuts its best bucket. That was
# NOMINAL R. On actual exits GC 3.0R+ is -0.318R (0 of 4 wins reached target)
# and the cap blocks 0.19% of rejections. Don't raise the caps: WAVE_227_CASE.md.
RR_CAP = {"GC": 3.0, "NQ": 3.5, "BTC": 4.0, "SOL": 4.0}
RR_CAP_DEFAULT = 3.5

# _WAVE239_CONVICTION_MIN: CHOSEN by Wayne 15 Sep 2026 (QUESTIONS_FOR_WAYNE Q2), from
# RESEARCH_NOTES section 12-I -- actual-exit R, real calls only, walk-forward
# (picked 13 Apr-30 Jun, scored 1 Jul-14 Sep):
#   NQ   50  raising it does not help (>=60 -0.004R OOS; >=70 5 OOS calls, all lost)
#   GC   70  monotonic OOS: >=50 +0.341R, >=60 +0.363R, >=70 +0.513R (no CI clears 0)
#   BTC  70  >=50 is -0.278R OOS; >=70 n=3. Near silent, and control channel only
#   SOL  off every threshold negative (CALLS_OFF_MARKETS below)
# Conviction is the setup's win rate in setup_performance.json. Wave 238 stopped
# shadow grades moving it; what replaces it is QUESTIONS_FOR_WAYNE Q8.
# None = the ladder floor of 50.
CONVICTION_MIN = {"NQ": 50, "GC": 70, "BTC": 70, "SOL": None}
CONVICTION_MIN_DEFAULT = None

# _WAVE244_CONVICTION_TABLE: conviction is a WRITTEN TABLE, not a learned number (Wayne, Q8, 16 Sep 2026).
# These are the exact scores conviction_score computed on 16 Sep 2026 from the counters in
# data/setup_performance.json - round(100 * (wins + 2) / (n + 4)), or 45 when n < 5 - frozen here so the
# number that decides what fires cannot move on its own. Wave 238 stopped shadow grades feeding it;
# real closes still did. Nothing changes a number here except a wave.
# Each line carries the wins-losses and n behind the score on that date.
# A market:setup not listed scores CONVICTION_UNKNOWN (45 = REJECT), exactly like a cold-start bucket
# today. setup_performance.json keeps being written: it is the research record, not a rule.
# WHAT IS BAKED IN, honestly: these counters include every shadow grade written up to 15 Sep 21:56 UTC,
# when Wave 238 cut that feed, plus the real closes since. Some numbers therefore carry shadow drift:
# BTC:BREAK_RETEST_BULL moved 38 -> 64 in the two days before 238 went live and is frozen here at 64.
# Freezing stops the drift; it does not undo it. Re-basing a number on real closes only is a later wave.
# Only these clear their market's floor (rules.CONVICTION_MIN, Wave 239):
#   GC  BB_REVERSION_BULL 72, MACD_CROSS_BEAR 76      (floor 70)
#   NQ  BREAK_RETEST_BULL 63, OPENING_RANGE_BREAKOUT 63, MACD_CROSS_BULL 55, VWAP_BOUNCE_BULL 55 (floor 50)
#   BTC nothing reaches 70; SOL never calls (CALLS_OFF_MARKETS).
CONVICTION_TABLE_ASOF = "16 Sep 2026"
CONVICTION_UNKNOWN = 45
CONVICTION_TABLE = {
    "BTC:BB_REVERSION_BEAR":          13,   # 13-102, n=115
    "BTC:BB_REVERSION_BULL":          35,   # 76-145, n=221
    "BTC:BREAK_RETEST_BEAR":          22,   # 12-48, n=60
    "BTC:BREAK_RETEST_BULL":          64,   # 44-24, n=68
    "BTC:EMA21_PULLBACK_BEAR":        30,   # 48-113, n=161
    "BTC:EMA21_PULLBACK_BULL":        26,   # 147-415, n=562
    "BTC:EMA50_BREAKDOWN":             3,   # 0-64, n=64
    "BTC:EMA50_RECLAIM":              16,   # 2-19, n=21
    "BTC:LAB|RSI_DIV_BEAR":           45,   # 13-16, n=29
    "BTC:LAB|RSI_DIV_BULL":           38,   # 4-8, n=12
    "BTC:MACD_CROSS_BEAR":            35,   # 25-49, n=74
    "BTC:MACD_CROSS_BULL":            32,   # 68-147, n=215
    "BTC:RSI_DIV_BEAR":               22,   # 17-66, n=83
    "BTC:RSI_DIV_BULL":               30,   # 22-55, n=77
    "BTC:STOCH_REVERSAL_BEAR":        22,   # 62-220, n=282
    "BTC:STOCH_REVERSAL_BULL":        51,   # 69-67, n=136
    "BTC:VWAP_BOUNCE_BULL":           19,   # 146-630, n=776
    "BTC:VWAP_REJECT_BEAR":           31,   # 33-75, n=108
    "GC:BB_REVERSION_BEAR":           36,   # 24-44, n=68
    "GC:BB_REVERSION_BULL":           72,   # 41-15, n=56
    "GC:BREAK_RETEST_BEAR":           24,   # 30-102, n=132
    "GC:BREAK_RETEST_BULL":           34,   # 8-17, n=25
    "GC:EMA21_PULLBACK_BEAR":          6,   # 8-154, n=162
    "GC:EMA21_PULLBACK_BULL":          2,   # 5-281, n=286
    "GC:EMA50_BREAKDOWN":             45,   # 0-4, n=4  COLD START
    "GC:EMA50_RECLAIM":               11,   # 0-15, n=15
    "GC:LAB|RSI_DIV_BEAR":            17,   # 3-22, n=25
    "GC:MACD_CROSS_BEAR":             76,   # 33-9, n=42
    "GC:MACD_CROSS_BULL":             27,   # 4-14, n=18
    "GC:RSI_DIV_BEAR":                 6,   # 3-73, n=76
    "GC:RSI_DIV_BULL":                 3,   # 1-103, n=104
    "GC:STOCH_REVERSAL_BEAR":         68,   # 68-31, n=99
    "GC:STOCH_REVERSAL_BULL":         11,   # 11-103, n=114
    "GC:VWAP_BOUNCE_BULL":            56,   # 94-73, n=167
    "GC:VWAP_REJECT_BEAR":            14,   # 38-254, n=292
    "NQ:APPROACH_SUPPORT":            45,   # 0-2, n=2  COLD START
    "NQ:BB_REVERSION_BEAR":           10,   # 3-45, n=48
    "NQ:BB_REVERSION_BULL":           26,   # 40-117, n=157
    "NQ:BREAK_RETEST_BEAR":           18,   # 9-48, n=57
    "NQ:BREAK_RETEST_BULL":           63,   # 100-58, n=158
    "NQ:EMA21_PULLBACK_BEAR":         23,   # 31-108, n=139
    "NQ:EMA21_PULLBACK_BULL":         10,   # 5-61, n=66
    "NQ:EMA50_BREAKDOWN":             27,   # 2-9, n=11
    "NQ:EMA50_RECLAIM":               13,   # 0-11, n=11
    "NQ:LAB|BB_REVERSION_BEAR":       36,   # 3-7, n=10
    "NQ:LAB|BREAK_RETEST_BEAR":       30,   # 5-14, n=19
    "NQ:LAB|EMA21_PULLBACK_BEAR":     20,   # 16-71, n=87
    "NQ:LAB|EMA50_BREAKDOWN":         45,   # 2-0, n=2  COLD START
    "NQ:LAB|MACD_CROSS_BEAR":         33,   # 5-12, n=17
    "NQ:LAB|OPENING_RANGE_BREAKOUT":  45,   # 0-3, n=3  COLD START
    "NQ:LAB|RSI_DIV_BEAR":            33,   # 1-4, n=5
    "NQ:LAB|RSI_DIV_BULL":            14,   # 0-10, n=10
    "NQ:LAB|STOCH_REVERSAL_BEAR":     41,   # 9-14, n=23
    "NQ:LAB|VWAP_REJECT_BEAR":        28,   # 16-45, n=61
    "NQ:MACD_CROSS_BEAR":             27,   # 10-31, n=41
    "NQ:MACD_CROSS_BULL":             55,   # 32-26, n=58
    "NQ:OPENING_RANGE_BREAKOUT":      63,   # 15-8, n=23
    "NQ:RSI_DIV_BEAR":                20,   # 1-10, n=11
    "NQ:RSI_DIV_BULL":                11,   # 2-29, n=31
    "NQ:STOCH_REVERSAL_BEAR":         26,   # 11-35, n=46
    "NQ:STOCH_REVERSAL_BULL":         40,   # 44-67, n=111
    "NQ:VWAP_BOUNCE_BULL":            55,   # 164-134, n=298
    "NQ:VWAP_REJECT_BEAR":            26,   # 45-132, n=177
    "SOL:APPROACH_SUPPORT":           45,   # 0-3, n=3  COLD START
    "SOL:BB_REVERSION_BEAR":          30,   # 42-99, n=141
    "SOL:BB_REVERSION_BULL":          28,   # 7-21, n=28
    "SOL:BREAK_RETEST_BEAR":          44,   # 18-23, n=41
    "SOL:BREAK_RETEST_BULL":          58,   # 58-41, n=99
    "SOL:EMA21_PULLBACK_BEAR":        32,   # 23-50, n=73
    "SOL:EMA21_PULLBACK_BULL":        40,   # 23-36, n=59
    "SOL:EMA50_BREAKDOWN":            43,   # 4-6, n=10
    "SOL:EMA50_RECLAIM":              90,   # 16-0, n=16
    "SOL:LAB|RSI_DIV_BEAR":           36,   # 2-5, n=7
    "SOL:LAB|RSI_DIV_BULL":           45,   # 1-1, n=2  COLD START
    "SOL:MACD_CROSS_BEAR":            31,   # 9-22, n=31
    "SOL:MACD_CROSS_BULL":            32,   # 11-26, n=37
    "SOL:RSI_DIV_BEAR":                5,   # 2-70, n=72
    "SOL:RSI_DIV_BULL":               15,   # 4-32, n=36
    "SOL:STOCH_REVERSAL_BEAR":        39,   # 17-28, n=45
    "SOL:STOCH_REVERSAL_BULL":        26,   # 7-23, n=30
    "SOL:VWAP_BOUNCE_BULL":           24,   # 47-154, n=201
    "SOL:VWAP_REJECT_BEAR":           38,   # 24-40, n=64
}

# _WAVE246_LAB_GRADE: LAB-grade everything. A setup that passes every market gate and fails ONLY the
# conviction floor is written to the ledger as a LAB row and graded by the real grader - real entry,
# real stop, real target, the 4:10 flatten at the actual price, r_actual, MFE/MAE, duration. It is
# never sent to Telegram. Without this, Wave 244 leaves six setups producing evidence and the rest
# producing none, so nothing else could ever earn its way back.
LAB_GRADE_EVERYTHING = True

# Ships False and should stay False: a LAB row is evidence, not a call.
LAB_TO_TELEGRAM = False

# Safety cap. On 16 Sep the live scan log holds 867 conviction rejections in one day, 592 of them one
# GC setup re-detected every scan. Hitting the cap is logged loudly, never silent.
LAB_MAX_PER_MARKET_PER_DAY = 40

# One LAB row per market:setup:direction per this many minutes - the same duplicate discipline a real
# call gets, in its own memory, so it can never affect a real call's guard.
LAB_DUP_MIN = 30

# A market listed here never sends a call (the scan still logs it, REJECTED).
CALLS_OFF_MARKETS = ("SOL",)

# _WAVE245_LAB_LANE: the LAB lane (WORKBOOK Part 2 E1). A setup here fires to the CONTROL channel only,
# is logged and graded like any other call, and is re-tested weekly. It never reaches the public
# channel, whatever PUBLIC_CALLS says. Nothing enters or leaves this list except by a wave.
#
# These three were broken since Wave 60 by `'vol_ratio' in dir()` inside detect_setups, which is always
# False there: VWAP_RECLAIM and HTF_LEVEL_BOUNCE could never fire, and VOLATILITY_CONTRACTION_BREAKOUT
# skipped its volume check entirely. Wave 245 fixes the check and gives all three a lane to earn a
# record in. Real calls from them so far: zero (Wayne, Q11, 16 Sep 2026).
LAB_SETUPS = ("VWAP_RECLAIM", "HTF_LEVEL_BOUNCE", "VOLATILITY_CONTRACTION_BREAKOUT")

# A LAB setup has no track record, so Wave 244's table scores it 45 and the conviction floor would keep
# it at zero calls for ever - it could never earn the record that gets it out of LAB. Inside the lane the
# conviction floor is skipped. EVERY other gate still applies: session windows, the R:R floor and cap,
# volume, ADX, news, the dup-guards and one open call per market.
LAB_BYPASSES_CONVICTION = True

# Calls AND exit cards for these markets go to the CONTROL channel only, even
# when PUBLIC_CALLS is True. The control channel always gets everything.
CONTROL_ONLY_MARKETS = ("BTC",)

# False = every fired call goes to the CONTROL channel only. Real subscribers
# read NQ CALLS, so the rulebook gets proven on the control channel first.
# This gates the fire-path call alert ONLY. Exit notices and the daily briefs
# are outside Wave 225 and still go where they always did.
PUBLIC_CALLS = False

# _WAVE234_HOLIDAYS ----------------------------------------------------------
# _WAVE239_CONVICTION_MIN (Q4 answered 15 Sep 2026): the conservative times stand.
# VERIFY AGAINST CME BEFORE 26 NOV 2026.
# CME closures for NQ and GC. Key = ET calendar date.
#   (None, ...)     full close: no new entries for that whole trade date,
#                   6:00 PM ET the evening before -> 6:00 PM ET that day
#   ("HH:MM", ...)  early close: no new entries from HH:MM ET to 6:00 PM ET
# CONSERVATIVE ON PURPOSE: sources disagreed (CME's own pages could not be
# fetched), so each entry uses the EARLIEST halt any source gave, and a day
# any source called closed is blocked all day. Confirm against cmegroup.com
# (QUESTIONS_FOR_WAYNE Q4); fixing a time here is a one-line edit.
FUTURES_HOLIDAYS = {
    "2026-11-26": (None,    "Thanksgiving",
                   "sources disagree: halt 1:00 PM ET vs full close -> blocked all day"),
    "2026-11-27": ("13:00", "Day after Thanksgiving (early close)",
                   "sources: 1:15 PM ET vs 1:00 PM ET -> 1:00 PM"),
    "2026-12-24": ("13:00", "Christmas Eve (early close)",
                   "sources: 1:15 PM ET vs 1:00 PM ET -> 1:00 PM"),
    "2026-12-25": (None,    "Christmas Day", "all sources: full close"),
    "2027-01-01": (None,    "New Year's Day",
                   "2027 not in the sources read; CME closes New Year's Day every year (2026: full close)"),
}

# _WAVE235_KEY_LEVELS: post the session-open key-levels card to the CONTROL channel?
# False = levels are only written to data/key_levels_YYYY-MM-DD.json (and the
# card is filed in data/bot_reports), keeping Telegram to calls, exits and
# health. True = the card is also posted to control. QUESTIONS_FOR_WAYNE Q5.
KEY_LEVELS_TO_TELEGRAM = False

# _WAVE248_FLOOR_AWARE_TARGET: an R:R floor and cap are RATIOS. They cannot tell a 15-point target from a
# 153-point one, so a huge stop makes a huge target legal. Measured on the three LAB calls of 17 Sep:
# #GC-0917-M6 asked for 153.4 points on Gold - 165% of a median Gold day - and in 42 recorded sessions
# that move was delivered before the 4:10 flatten 0 times.
#
# These are MEDIAN DAILY RANGES in points, measured 17 Sep 2026 from the recorded daily bars
# (NQ 168 days from Oct 2025, GC 251 days from Sep 2025, BTC 745 days). Re-measure them when the
# market's character changes; they are a yardstick, not a law.
DAILY_RANGE_POINTS = {"NQ": 446.2, "GC": 92.7, "BTC": 2602.7}

# A target beyond this share of a median day is RECORDED, not blocked. 1.00 = a whole day's range.
# Why log-only: of 343 real graded calls, this rule would have blocked 24 (-0.028R each, reached 4.2%
# of the time) against +0.073R for the ones it kept - the only variant tested where the blocked set was
# not BETTER than the kept set. n=24 with a CI through zero does not earn the right to block a call.
# Every ATR-based variant was worse: capping the stop at 2 ATR would have blocked 41 calls worth
# +0.248R each, and capping the target at 6 ATR would have blocked 20 worth +0.477R each - the
# trend-day tail that pays for everything else.
TARGET_MAX_DAY_SHARE = 1.00
TARGET_DISTANCE_LOG_ONLY = True

# _WAVE249_LAB_SILENT (Wayne, 18 Sep 2026): every strategy fires, is graded and is saved - but a LAB
# strategy is ledger-only. No Telegram, no badge. This is the master switch: a LAB row may notify only
# if BOTH LAB_NOTIFY and the lane's own flag (Wave 246's LAB_TO_TELEGRAM) are true, so the default is
# silence whichever one is read.
LAB_NOTIFY = False

# THE ONE EXCEPTION Wayne allows: the three Wave 245 setups are already running and keep their
# control-channel voice. They are NAMED here rather than hidden in a code path, so revoking the
# exception is one edit to this list. CONTROL CHANNEL ONLY - nothing in LAB ever reaches the public
# channel, whatever these switches say. Their ledger rows still carry lane='lab', so they still never
# enter a real statistic, and promotion out of LAB is never automatic (PROMOTION_REVIEW.md).
LAB_NOTIFY_SETUPS = ("VWAP_RECLAIM", "HTF_LEVEL_BOUNCE", "VOLATILITY_CONTRACTION_BREAKOUT")

# _WAVE250_REVIEW_CADENCE (Q17, Wayne 17 Sep 2026): the "weekly" self-review was written EVERY evening and
# announced as weekly - 121 files in data/ by 17 Sep. Now: a daily file under an honest name, and a
# weekly file on ONE weekday. Python's weekday(): Monday=0 ... Sunday=6.
WEEKLY_REVIEW_WEEKDAY = 6          # Sunday
DAILY_SELF_REVIEW = True           # keep the daily file, as daily_self_review_<date>.md
