# ==================================================================
# Wave 179 (_WAVE179_PUBLIC_UI): clean public alert cards.
#
# The public channel was receiving the operator's dashboard: conviction
# scores, W7 layer adjustments, the Read grade, trend/ADX/RSI telemetry -
# and, in the trade-closed alert, the ACCOUNT BALANCE and remaining daily
# loss limit. Wayne is adding subscribers to that channel, so it now shows
# only what a follower needs to act on and to judge the result honestly.
#
# Nothing is lost: the full diagnostic version still goes to the control
# channel on every fire and every close, so the operator view is unchanged.
# ==================================================================

# Wave 198: the last failure reason, so a silent fallback stops being silent.
#
# Both formatters end in a bare `except: return None`, and the caller treats
# None as "use the old card". That is the right safety behaviour - a formatting
# bug must never kill a live alert - but it meant these cards failed on EVERY
# call for weeks with nothing anywhere saying so. The reason is now recorded
# here and reported by diag_ui.py.
_W179_LAST_ERROR = None


def _md(s):
    """Escape Telegram Markdown. Defined LOCALLY on purpose.

    The original relied on bot.py's _md and on get_market_config being present
    in the namespace at call time. They are not - this is a separate module -
    so every call raised NameError, was swallowed by the bare except, and
    returned None. The public cards never rendered once.
    """
    try:
        out = str(s)
        for ch in ("_", "*", "`", "["):
            out = out.replace(ch, "\\" + ch)
        return out
    except Exception:
        return str(s)


_W179_ACRONYMS = {
    "VWAP": "VWAP", "EMA": "EMA", "EMA20": "EMA20", "EMA21": "EMA21",
    "EMA50": "EMA50", "EMA200": "EMA200", "RSI": "RSI", "MACD": "MACD",
    "BB": "BB", "ATR": "ATR", "ORB": "ORB", "HTF": "HTF", "VIX": "VIX",
    "STOCH": "Stoch", "DIV": "Divergence",
}


def _w179_decimals(price):
    """One decimal convention per market, chosen from price magnitude, so the
    prices and the point deltas in the same card always agree."""
    try:
        f = abs(float(price))
    except Exception:
        return 2
    if f >= 1000:
        return 2
    if f >= 100:
        return 2
    if f >= 1:
        return 3
    return 5


def _w179_num(v, decimals=2):
    """Price/point formatter. `decimals` is fixed per card by _w179_decimals."""
    try:
        f = float(v)
    except Exception:
        return str(v)
    return ("{:,.%df}" % decimals).format(f)


def _w179_setup_name(raw):
    """VWAP_BOUNCE_BULL -> 'VWAP Bounce'. Keeps acronyms upper-case and drops
    the BULL/BEAR suffix, which is redundant next to LONG/SHORT."""
    try:
        parts = [p for p in str(raw).upper().split("_") if p]
        if parts and parts[-1] in ("BULL", "BEAR"):
            parts = parts[:-1]
        out = []
        for p in parts:
            out.append(_W179_ACRONYMS.get(p, p.capitalize()))
        return " ".join(out) if out else str(raw)
    except Exception:
        return str(raw)


def _w179_bar(rr):
    """Ten-cell R:R bar scaled so 3.0R fills it. Scaling to 2.0R made a 2.4R
    setup look identical to a 2.0R one, throwing away the difference."""
    try:
        filled = int(round(max(0.0, min(1.0, float(rr) / 3.0)) * 10))
    except Exception:
        filled = 0
    return "\u2588" * filled + "\u2591" * (10 - filled)


def format_alert_public(market, tf, setup, tier, target, rr, size_line=""):
    """
    Wave 179: the PUBLIC entry card. Direction, setup, the three prices, the
    risk/reward and the size. No conviction score, no W7 internals, no Read
    grade, no ADX/RSI/trend, no account figures.
    """
    try:
        direction = setup.get("direction", "")
        is_long = "LONG" in direction
        is_watch = "WATCH" in direction
        entry = float(setup.get("entry", 0))
        stop = float(setup.get("raw_stop", 0))
        tgt = float(target)
        risk_pts = abs(entry - stop)
        rew_pts = abs(tgt - entry)
        icon = "\U0001f7e2" if is_long else "\U0001f534"
        side = "LONG" if is_long else "SHORT"
        if is_watch:
            icon = "\U0001f440"
            side = "WATCH " + side
        dp = _w179_decimals(entry)
        setup_name = _md(_w179_setup_name(setup.get("type", "")))
        bar = "━" * 23
        lines = [
            "%s *%s %s*          `%s`" % (icon, market, side, tier),
            bar,
            "   %s  ·  %s" % (setup_name, tf),
            "",
            "   Entry    `%s`" % _w179_num(entry, dp),
            "   Stop     `%s`   ⛔ %s" % (_w179_num(stop, dp), _w179_num(-risk_pts, dp)),
            "   Target   `%s`   \U0001f3af +%s" % (_w179_num(tgt, dp), _w179_num(rew_pts, dp)),
            "   R:R      *%.1f*  %s" % (float(rr), _w179_bar(rr)),
        ]
        if size_line:
            lines.append("   %s" % size_line.replace("\U0001f4e6 *Size:* ", "Size     "))

        # Wave 200: the setup's real track record, added HERE rather than in
        # bot.py. This module is already called at the fire site, so the line
        # needs no new anchor in a 402 KB file I cannot read from the dev side -
        # which is what caused two failed deploys earlier today.
        #
        # It returns None whenever the evidence is thin or the setup's history
        # is built on broken stops, and None simply means the line is omitted.
        try:
            import os as _os
            import w200_edge as _w200
            _edge = _w200.edge_line(
                _os.path.dirname(_os.path.abspath(__file__)),
                market, setup.get("type", ""))
            if _edge:
                lines.append(_edge)
        except Exception:
            pass

        lines.append(bar)
        return "\n".join(lines)
    except Exception as e:
        # Never let formatting break a live alert - fall back to the full card.
        # But RECORD why, so the fallback can be seen instead of guessed at.
        global _W179_LAST_ERROR
        _W179_LAST_ERROR = "format_alert_public: %s: %s" % (type(e).__name__, e)
        return None


def format_exit_public(market, tf, setup_name, direction, tier, result,
                       entry_p, exit_p, pts_str, pct_str, day_w, day_l, rr_realised=None):
    """
    Wave 179: the PUBLIC exit card. Shows the result honestly - wins and losses
    use the same shape so a loss is never hidden. Deliberately contains NO
    account balance, NO daily P&L and NO remaining-limit figures.
    """
    try:
        won = str(result).upper() == "WIN"
        icon = "✅" if won else "❌"
        bar = "━" * 23
        cells = 8
        blocks = ("\U0001f7e9" if won else "\U0001f7e5") * cells
        tail = "target hit" if won else "stopped out"
        rr_txt = ""
        if rr_realised is not None:
            try:
                rr_txt = "          *%+.1fR*" % float(rr_realised)
            except Exception:
                rr_txt = ""
        name = _md(_w179_setup_name(setup_name))
        lines = [
            "%s *%s %s CLOSED*%s" % (icon, market, direction, rr_txt),
            bar,
            "   %s  ·  %s" % (name, tf),
            "",
            "   `%s`  →  `%s`" % (entry_p, exit_p),
            "   %s pts  (%s)" % (pts_str, pct_str),
            "   %s %s" % (blocks, tail),
            bar,
            "   Today: *%sW / %sL*" % (day_w, day_l),
        ]
        return "\n".join(lines)
    except Exception as e:
        global _W179_LAST_ERROR
        _W179_LAST_ERROR = "format_exit_public: %s: %s" % (type(e).__name__, e)
        return None
