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
# FLAG FOR A LATER WAVE, not changed here: on real rows the 3.0R+ bucket is
# POSITIVE in every market that has one (NQ +0.203 n=19, GC +1.024 n=10,
# BTC +0.295 n=33). GC's 3.0 cap is cutting off GC's best bucket. Samples are
# thin, so this wave moves the numbers without touching them.
RR_CAP = {"GC": 3.0, "NQ": 3.5, "BTC": 4.0, "SOL": 4.0}
RR_CAP_DEFAULT = 3.5

# ############################################################################
# #  PLACEHOLDER -- NOT YET CHOSEN. DO NOT INVENT A NUMBER HERE.             #
# ############################################################################
# None means "behave exactly as the bot does today". The wiring ships now so
# that setting a number later is a one-line change instead of a new wave.
#
# Monday's ledger under Wave 224 gives the RAW conviction distribution: every
# DETECT/REJECT row's score_breakdown JSON carries learning_bonus,
# directional_bias, w7_setup_boost and w7_market_mult -- subtract them from
# conviction to get the raw score. The chat sends the numbers Tuesday.
#
# Until then this stays None and the conviction gate is unchanged.
CONVICTION_MIN = {"NQ": None, "GC": None, "BTC": None, "SOL": None}
CONVICTION_MIN_DEFAULT = None

# False = every fired call goes to the CONTROL channel only. Real subscribers
# read NQ CALLS, so the rulebook gets proven on the control channel first.
# This gates the fire-path call alert ONLY. Exit notices and the daily briefs
# are outside Wave 225 and still go where they always did.
PUBLIC_CALLS = False
