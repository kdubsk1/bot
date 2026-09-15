# -*- coding: utf-8 -*-
"""
key_levels.py -- Wave 235 (K1). Key price levels for a market's trade date.

Information only. Nothing in the fire path reads this module: levels are
computed at each market's session open, written to
data/key_levels_YYYY-MM-DD.json, and (only if rules.KEY_LEVELS_TO_TELEGRAM is
True) posted to the control channel.

Definitions match RESEARCH_NOTES section 13 exactly, on 1-hour bars, using only
bars that have CLOSED:
  PDH / PDL      prior trade date's RTH high / low (bars starting 09:00-15:00 ET)
  ONH / ONL      most recent completed overnight window, 18:00-09:00 ET
  LonH / LonL    most recent completed London window, 03:00-08:00 ET
  NYAMH / NYAML  most recent completed NY morning window, 09:00-11:00 ET
  4H_SwH / SwL   latest 2-bar fractal swing high / low on 4H blocks anchored 18:00 ET
  WkH / WkL      prior ISO week's high / low
A trade date starts at 18:00 ET the evening before.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-4))

NAMES = ["PDH", "PDL", "ONH", "ONL", "LonH", "LonL", "NYAMH", "NYAML", "4H_SwH", "4H_SwL", "WkH", "WkL"]
LABELS = {"PDH": "Prior-day high", "PDL": "Prior-day low", "ONH": "Overnight high", "ONL": "Overnight low",
          "LonH": "London high", "LonL": "London low", "NYAMH": "NY-AM high", "NYAML": "NY-AM low",
          "4H_SwH": "4H swing high", "4H_SwL": "4H swing low", "WkH": "Prior-week high", "WkL": "Prior-week low"}


def trade_date(t):
    """ET trade date for an aware datetime (18:00 ET starts the next date)."""
    return (t.astimezone(ET) + timedelta(hours=6)).date()


def _bars(df, now_utc):
    """(start_utc, o, h, l, c) for every 1h bar that closed at or before now_utc."""
    out = []
    try:
        for ts, row in df.iterrows():
            t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t + timedelta(hours=1) <= now_utc:
                out.append((t, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])))
    except Exception:
        return []
    out.sort(key=lambda b: b[0])
    return out


def compute(df_1h, now_utc=None):
    """Levels for the trade date containing now_utc. Returns a dict, or None if
    there is not enough closed history. Never raises."""
    try:
        now_utc = now_utc or datetime.now(timezone.utc)
        done = _bars(df_1h, now_utc)
        if len(done) < 48:
            return None
        now_et = now_utc.astimezone(ET)
        by_td = defaultdict(list)
        for b in done[-24 * 20:]:
            e = b[0].astimezone(ET)
            by_td[trade_date(b[0])].append((e, b))
        tds = sorted(by_td)
        cur = trade_date(now_utc)

        def hl(sel):
            return (max(b[2] for _, b in sel), min(b[3] for _, b in sel)) if sel else None

        lv = {}
        for d in reversed([d for d in tds if d < cur]):
            x = hl([(e, b) for e, b in by_td[d] if 9 <= e.hour <= 15])
            if x:
                lv["PDH"], lv["PDL"] = x
                break

        def last_window(hours, end_hour):
            for d in reversed(tds):
                sel = [(e, b) for e, b in by_td[d] if e.hour in hours]
                if not sel:
                    continue
                last_end = max(e for e, _ in sel) + timedelta(hours=1)
                if last_end.hour != end_hour:
                    continue
                if last_end <= now_et:
                    return hl(sel)
            return None

        x = last_window({18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}, 9)
        if x:
            lv["ONH"], lv["ONL"] = x
        x = last_window({3, 4, 5, 6, 7}, 8)
        if x:
            lv["LonH"], lv["LonL"] = x
        x = last_window({9, 10}, 11)
        if x:
            lv["NYAMH"], lv["NYAML"] = x
        wk = now_et.isocalendar()[:2]
        wsel = [(e, b) for d in tds for e, b in by_td[d]
                if d.isocalendar()[:2] < wk and (wk[1] - d.isocalendar()[1]) in (1, -51, -52)]
        x = hl(wsel)
        if x:
            lv["WkH"], lv["WkL"] = x
        blocks = defaultdict(list)
        for b in done[-24 * 15:]:
            e = b[0].astimezone(ET)
            anchor = e.replace(minute=0, second=0, microsecond=0) - timedelta(hours=(e.hour - 18) % 4)
            blocks[anchor].append(b)
        f4 = [(k, max(b[2] for b in v), min(b[3] for b in v)) for k, v in sorted(blocks.items()) if len(v) >= 3]
        sh = sl = None
        for i in range(len(f4) - 3, 1, -1):
            if sh is None and f4[i][1] > max(f4[i - 1][1], f4[i - 2][1], f4[i + 1][1], f4[i + 2][1]):
                sh = f4[i][1]
            if sl is None and f4[i][2] < min(f4[i - 1][2], f4[i - 2][2], f4[i + 1][2], f4[i + 2][2]):
                sl = f4[i][2]
            if sh is not None and sl is not None:
                break
        if sh is not None:
            lv["4H_SwH"] = sh
        if sl is not None:
            lv["4H_SwL"] = sl
        return {
            "trade_date": cur.isoformat(),
            "computed_at": now_utc.isoformat(),
            "source_tf": "1h",
            "last_closed_bar": done[-1][0].isoformat(),
            "ref_price": done[-1][4],
            "levels": {k: round(lv[k], 4) for k in NAMES if k in lv},
            "missing": [k for k in NAMES if k not in lv],
        }
    except Exception:
        return None


def card(market, result):
    """Plain-text control-channel card: levels sorted high to low around the price."""
    try:
        ref = float(result["ref_price"])
        dec = 2 if abs(ref) >= 1000 else 4
        items = sorted(result["levels"].items(), key=lambda kv: -kv[1])
        lines = ["Key levels - %s - trade date %s" % (market, result["trade_date"])]
        placed = False
        for k, v in items:
            if not placed and v < ref:
                lines.append("   ---- price %.*f ----" % (dec, ref))
                placed = True
            lines.append("%-16s %.*f" % (LABELS.get(k, k), dec, v))
        if not placed:
            lines.append("   ---- price %.*f ----" % (dec, ref))
        if result.get("missing"):
            lines.append("not enough history: " + ", ".join(result["missing"]))
        return "\n".join(lines)
    except Exception:
        return "Key levels - %s: unavailable" % market
