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

# A market listed here never sends a call (the scan still logs it, REJECTED).
CALLS_OFF_MARKETS = ("SOL",)

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
