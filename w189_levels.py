# ==================================================================
# Wave 189 (_WAVE189_LEVELS): the public EXPECTED RANGE card.
#
# Reads data/extreme_projection_report.json - the walk-forward validated
# extension bands - and turns them into one plain-English range per market,
# anchored on the CURRENT price.
#
# WHY ONE RANGE AND ONE NUMBER
# ----------------------------
# The first draft printed two numbers per market ("stays under X 73% of the
# time / stays over Y 70% of the time"). That reads as if the RANGE holds about
# 70% of the time. It does not. Two one-sided probabilities do not combine that
# way, and the true joint rate is always lower than either. Rather than let the
# reader do that arithmetic wrongly, the report now MEASURES how often both
# sides held in the same window, and this card prints that single honest number.
#
# WHY THIS WIDTH
# --------------
# The width is not assumed. A wider range is always more accurate - that is
# arithmetic, not skill - so "the most accurate range" has a useless answer.
# The report picks the KNEE of the width-versus-accuracy curve: the width past
# which extra points stop buying meaningful accuracy.
#
# SAFETY
# ------
# It never invents a number. Anything not marked PUBLISH is silently omitted.
# If the report is missing, unreadable, or stale, the entire block is omitted
# and the brief simply does not mention ranges. A projection that cannot be
# backed by a measured, out-of-sample rate does not reach the channel.
# ==================================================================

import os
import json
from datetime import datetime, timezone

_W189_REPORT = "extreme_projection_report.json"

# Which horizons appear, and what to call them in plain English.
_W189_VIEWS = [
    (2, "NEXT 2 HOURS"),
    (8, "REST OF THE DAY"),
]

# Public-facing market names. "GOLD" reads better than "GC" to a subscriber.
_W189_NAMES = {"NQ": "NQ", "GC": "GOLD", "BTC": "BTC", "SOL": "SOL"}

# Guards. Each one exists because breaking it would put a misleading number in
# front of paying subscribers.
_W189_MIN_RATE     = 65.0   # a range right less than this is not worth printing
_W189_MIN_WINDOWS  = 30     # fewer test windows than this is a coin flip
_W189_MAX_AGE_DAYS = 45     # past this the statistics are stale, say nothing


def _w189_load(base_dir):
    """Load the report, or None. Never raises."""
    try:
        p = os.path.join(base_dir, "data", _W189_REPORT)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            rep = json.load(f)
    except Exception:
        return None
    # staleness: an old band is not automatically wrong, but it is unverified,
    # and unverified numbers do not go in the public channel.
    try:
        gen = rep.get("generated_at") or ""
        if gen and gen != "test":
            t = datetime.fromisoformat(gen.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - t).days
            if age > _W189_MAX_AGE_DAYS:
                return None
    except Exception:
        pass
    return rep


def _w189_range_row(report, market, hours):
    """The validated RANGE row for this market/horizon, or None."""
    try:
        for r in report.get("results", []):
            if (r.get("market") == market
                    and int(r.get("hours", -1)) == int(hours)
                    and r.get("side") == "RANGE"
                    and r.get("verdict") == "PUBLISH"):
                if float(r.get("measured", 0)) < _W189_MIN_RATE:
                    return None
                if int(r.get("n_test", 0)) < _W189_MIN_WINDOWS:
                    return None
                return r
        return None
    except Exception:
        return None


def _w189_fmt(v, price):
    """Decimals that suit the instrument: none for index/metal, two for SOL."""
    try:
        f = float(v)
        p = abs(float(price))
    except Exception:
        return str(v)
    return "{:,.0f}".format(f) if p >= 1000 else "{:,.2f}".format(f)


def build_levels_block(base_dir, markets, price_lookup):
    """
    Build the public EXPECTED RANGE block.

    price_lookup(market) -> current price (float) or None.
    Returns "" when there is nothing validated to say - callers should treat an
    empty string as "print nothing", not as an error.
    """
    report = _w189_load(base_dir)
    if not report:
        return ""

    sections = []
    windows_used = []
    for hours, title in _W189_VIEWS:
        # Gather first, format second: column widths can only be known once
        # every row for this section is in hand. Telegram's normal font is
        # proportional, so "GOLD" and "NQ" would not line up as bold text -
        # the rows go inside a fenced block, where they will.
        cells = []
        for mkt in markets:
            row = _w189_range_row(report, mkt, hours)
            if not row:
                continue
            try:
                px = float(price_lookup(mkt) or 0)
            except Exception:
                px = 0.0
            if px <= 0:
                continue
            try:
                lo = px - float(row["dn_band"])
                hi = px + float(row["band"])
                rate = float(row["measured"])
            except Exception:
                continue
            windows_used.append(int(row.get("n_test", 0)))
            cells.append((_W189_NAMES.get(mkt, mkt),
                          _w189_fmt(lo, px), _w189_fmt(hi, px),
                          "%.0f%%" % rate))
        if not cells:
            continue
        wn = max(len(c[0]) for c in cells)
        wl = max(len(c[1]) for c in cells)
        wh = max(len(c[2]) for c in cells)
        body = "\n".join(
            "%s  %s - %s   %s"
            % (n.ljust(wn), lo.rjust(wl), hi.rjust(wh), rt.rjust(4))
            for n, lo, hi, rt in cells)
        sections.append("*%s*\n```\n%s\n```" % (title, body))

    if not sections:
        return ""

    bar = "━" * 22
    out = [bar, "\U0001f4d0 *EXPECTED RANGE*", ""]
    out.append("\n\n".join(sections))
    out.append("")
    if windows_used:
        out.append("_The percentage is how often price stayed inside that "
                   "range, measured on %s past windows the bot had never "
                   "seen._" % "{:,}".format(max(windows_used)))
    out.append("_This is where price has tended to stay. It is not a call on "
               "direction._")
    out.append(bar)
    return "\n".join(out)
