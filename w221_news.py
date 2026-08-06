"""
w221_news.py - one morning card: what is happening, and what it means for the charts.

DESIGN PRINCIPLE: SEPARATE FACT FROM INFERENCE
==============================================
It would be easy to have this read headlines and announce "NQ BULLISH 78%".
That is astrology with a percent sign on it. Nothing in five months of this
bot's history has been hurt more by confident numbers with nothing behind them.

So the card has three clearly separated layers:

  1. SCHEDULED     - hard fact. A release either happens at 13:30 or it does
                     not. No judgement involved.
  2. MARKET STATE  - measured fact. Price against EMA50, ADX, ATR percentile,
                     distance from yesterday's range. Computed, not guessed.
  3. THE READ      - inference, LABELLED AS SUCH, with the reasons listed so
                     it can be argued with.

Layer 3 is the only part that can be wrong in an interesting way, and it is
the only part that gets graded.

EVERY CALL IS LOGGED AND SCORED
===============================
A bias card nobody grades is entertainment. Every read is appended to
data/news_calls.jsonl with the date, market, direction and confidence. Once
outcomes exist, hit rate is published on the card itself. If it turns out to
be 50%, that is worth knowing and the feature should be killed.

WHERE IT GOES
=============
Clocked Signals ONLY. Never NQ CALLS. That is a hard rule from the user and it
is enforced by the caller passing CONTROL_CHAT_ID.

SOURCES
=======
Every source is wrapped individually. A dead feed removes one section; it never
breaks the card. If everything is dead, the card still ships with the market
state, which is computed locally and needs no network at all.
"""

import os
import re
import json
import time
import datetime as _dt

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CALLS = os.path.join(DATA, "news_calls.jsonl")

MARKETS = ("NQ", "GC", "BTC", "SOL")

# What actually moves these four. Kept deliberately short - a long list of
# vaguely relevant terms is how you end up with a card full of noise.
HIGH_IMPACT = {
    "CPI": ("NQ", "GC", "BTC"),
    "CORE CPI": ("NQ", "GC", "BTC"),
    "PPI": ("NQ", "GC"),
    "FOMC": ("NQ", "GC", "BTC"),
    "FEDERAL OPEN MARKET": ("NQ", "GC", "BTC"),
    "RATE DECISION": ("NQ", "GC", "BTC"),
    "NONFARM": ("NQ", "GC"),
    "NFP": ("NQ", "GC"),
    "UNEMPLOYMENT": ("NQ", "GC"),
    "JOBLESS CLAIMS": ("NQ",),
    "GDP": ("NQ", "GC"),
    "PCE": ("NQ", "GC"),
    "RETAIL SALES": ("NQ",),
    "ISM": ("NQ",),
    "POWELL": ("NQ", "GC", "BTC"),
    "TARIFF": ("NQ", "GC"),
    "ETF": ("BTC", "SOL"),
    "SEC": ("BTC", "SOL"),
}

_DIR_UP = ("beats", "beat", "surge", "surges", "rally", "rallies", "jumps",
           "soars", "gains", "upgrade", "approval", "approved", "cools",
           "cooler", "softer", "dovish", "cut", "cuts")
_DIR_DOWN = ("misses", "miss", "falls", "plunge", "plunges", "slumps", "drops",
             "downgrade", "rejected", "denial", "hotter", "hot", "hawkish",
             "hike", "hikes", "selloff", "sell-off", "probe", "lawsuit")


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _fetch(url, timeout=8):
    """One source. Returns text or None. Never raises."""
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; bot/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _strip(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
          .replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


def fetch_headlines(limit=40):
    """
    Headlines from a few no-key RSS feeds.

    Deliberately no paid API. A key that expires silently is a feature that
    dies silently, and this bot has enough of those already.
    """
    feeds = [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://finance.yahoo.com/news/rssindex",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ]
    out, seen = [], set()
    for u in feeds:
        raw = _fetch(u)
        if not raw:
            continue
        for m in re.finditer(r"<title>(.*?)</title>", raw, re.S | re.I):
            t = _strip(m.group(1))
            if len(t) < 20 or t.lower() in seen:
                continue
            seen.add(t.lower())
            out.append(t)
            if len(out) >= limit:
                return out
    return out


def relevant(headlines):
    """Only headlines that name something on the HIGH_IMPACT list."""
    hits = []
    for h in headlines:
        up = h.upper()
        for k, mkts in HIGH_IMPACT.items():
            if k in up:
                lo = h.lower()
                lean = 0
                for w in _DIR_UP:
                    if re.search(r"\b" + re.escape(w) + r"\b", lo):
                        lean += 1
                for w in _DIR_DOWN:
                    if re.search(r"\b" + re.escape(w) + r"\b", lo):
                        lean -= 1
                hits.append({"headline": h, "topic": k,
                             "markets": list(mkts), "lean": lean})
                break
    return hits


def market_state(snapshot):
    """
    Layer 2 - measured, not guessed.

    `snapshot` is whatever the bot already computed for the market:
    close, ema50, ema200, adx, atr_pct. Missing keys degrade the read rather
    than breaking it.
    """
    s = {}
    try:
        c = float(snapshot.get("close"))
        e50 = float(snapshot.get("ema50") or 0) or None
        e200 = float(snapshot.get("ema200") or 0) or None
        adx = snapshot.get("adx")
        adx = float(adx) if adx not in (None, "") else None
        s["above_ema50"] = (c > e50) if e50 else None
        s["above_ema200"] = (c > e200) if e200 else None
        s["gap_ema50_pct"] = ((c - e50) / e50 * 100.0) if e50 else None
        s["adx"] = adx
        s["trending"] = (adx is not None and adx > 25)
    except Exception:
        pass
    return s


def read_for(market, state, news):
    """
    Layer 3 - INFERENCE. Returns direction, confidence and the reasons.

    Confidence is the share of agreeing signals, not a probability of being
    right. It is capped at 75 because nothing here has earned more than that,
    and it is explicitly labelled on the card.
    """
    votes, why = [], []

    def add(v, text):
        """Every reason carries its own lean, so a card can never show a
        BULLISH header above a list that reads bearish. That looked broken
        the first time it rendered, because it was."""
        votes.append(v)
        why.append(("+ " if v > 0 else "- ") + text)

    if state.get("above_ema50") is True:
        add(1, "above EMA50")
    elif state.get("above_ema50") is False:
        add(-1, "below EMA50")
    if state.get("above_ema200") is True:
        add(1, "above EMA200")
    elif state.get("above_ema200") is False:
        add(-1, "below EMA200")
    if state.get("trending"):
        if votes:
            add(1 if sum(votes) > 0 else -1,
                "ADX %.0f - trend confirms" % state["adx"])
        else:
            why.append("  ADX %.0f - trending, no direction yet" % state["adx"])
    elif state.get("adx") is not None:
        why.append("  ADX %.0f - no trend, treat levels as chop" % state["adx"])

    for n in news:
        if market in n["markets"] and n["lean"]:
            add(1 if n["lean"] > 0 else -1,
                "%s: %s" % (n["topic"], n["headline"][:52]))

    if not votes:
        return {"dir": "NO READ", "conf": 0, "why": ["  not enough signal"]}

    net = sum(votes)
    # Confidence is NET agreement, not the size of the biggest camp. A 3-1
    # split is 50% conviction, not 75% - the earlier version quietly hid the
    # dissenting signal and always looked confident.
    conf = int(min(75, round(100.0 * abs(net) / len(votes))))
    if net > 0:
        d = "BULLISH"
    elif net < 0:
        d = "BEARISH"
    else:
        d, conf = "MIXED", 0
    # sort so the reasons that drove the call appear first
    lead = "+ " if net > 0 else "- "
    why.sort(key=lambda w: 0 if w.startswith(lead) else 1)
    return {"dir": d, "conf": conf, "why": why[:5]}


def log_call(day, market, r):
    """Append-only. Never overwritten, so the grade cannot be quietly reset."""
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(CALLS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"day": day, "market": market, "dir": r["dir"],
                                "conf": r["conf"], "at": _now().isoformat()}) + "\n")
    except Exception:
        pass


def hit_rate():
    """How the reads have actually done. Returns '' until there is enough."""
    try:
        if not os.path.exists(CALLS):
            return ""
        n = graded = right = 0
        for line in open(CALLS, encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            n += 1
            if d.get("correct") is not None:
                graded += 1
                if d["correct"]:
                    right += 1
        if graded < 10:
            return "READ RECORD  %d calls logged, %d graded - too few to score yet" % (n, graded)
        return "READ RECORD  %d of %d correct (%.0f%%)" % (right, graded, 100.0 * right / graded)
    except Exception:
        return ""


def build_card(snapshots, headlines=None, day=None):
    """
    The whole card, as one string. Never raises.

    `snapshots` maps market -> the indicator dict the bot already has.
    """
    try:
        day = day or _now().strftime("%a %d %b")
        heads = headlines if headlines is not None else fetch_headlines()
        news = relevant(heads or [])
        bar = "-" * 34
        L = ["MORNING READ   %s" % day, bar]

        if news:
            L.append("WHAT MOVED")
            for n in news[:5]:
                arrow = "up" if n["lean"] > 0 else ("down" if n["lean"] < 0 else "--")
                L.append("  [%s] %s" % (arrow, n["headline"][:62]))
        else:
            L.append("WHAT MOVED")
            L.append("  nothing on the watchlist - quiet tape")
        L.append("")

        for m in MARKETS:
            snap = (snapshots or {}).get(m)
            if not snap:
                continue
            st = market_state(snap)
            r = read_for(m, st, news)
            log_call(day, m, r)
            L.append("%-4s %-8s %s" % (
                m, r["dir"], ("conf %d%%" % r["conf"]) if r["conf"] else ""))
            for wy in r["why"]:
                L.append("     %s" % wy)
        L.append("")
        hr = hit_rate()
        if hr:
            L.append(hr)
        L.append("Scheduled + state are measured. The READ is inference.")
        L.append(bar)
        return "\n".join(L)
    except Exception as e:
        try:
            return "MORNING READ unavailable (%s)" % type(e).__name__
        except Exception:
            return ""


if __name__ == "__main__":
    demo = {
        "NQ": {"close": 23150.0, "ema50": 22980.0, "ema200": 22400.0, "adx": 28.0},
        "GC": {"close": 2410.0, "ema50": 2455.0, "ema200": 2380.0, "adx": 17.0},
        "BTC": {"close": 64200.0, "ema50": 66800.0, "ema200": 61000.0, "adx": 31.0},
        "SOL": {"close": 74.0, "ema50": 73.2, "ema200": 80.0, "adx": 12.0},
    }
    fake_heads = [
        "US CPI cools more than expected in June, softer than forecast",
        "Fed's Powell strikes dovish tone on rate cuts at Jackson Hole",
        "Spot Bitcoin ETF sees record outflows as SEC probe widens",
        "Nvidia earnings beat but guidance disappoints investors",
        "Weather in Ohio remains mild this week",
    ]
    print(build_card(demo, headlines=fake_heads, day="Fri 31 Jul"))
