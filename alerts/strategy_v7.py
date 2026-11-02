"""v7 = v6 + pattern flip zones (forex.com chart patterns, Tradeciety chart anatomy).

A horizontal level is where price turned 2+ times: two swing highs (or lows) within LEVEL_TOL x ATR of each
other - a double top/bottom, the flat side of an ascending/descending triangle, a range edge or a
head-and-shoulders neckline - or the neckline of a double top/bottom (the low/high between the two peaks).
Only a confirmed break counts: a candle closes BREAK x ATR beyond the level with a strong body. The broken
level then flips (old resistance -> demand zone, old support -> supply zone) and is traded on the retest
with the normal v6 rules. The numbers below were fixed before testing and must not be tuned on results.
"""
import math

from strategy import atr, track, zones

SWING = 3          # candles each side of a swing high/low (same as the v6 structure)
LEVEL_TOL = 0.3    # swing points within 0.3 ATR = same level
BREAK = 0.5        # close at least 0.5 ATR beyond the level
BODY = 0.5         # breakout candle body at least half its range
MIN_HEIGHT = 0.3   # flip zone at least 0.3 ATR tall
LOOKBACK = 100     # two swing points at most 100 candles apart


def flip_zones(df, snapshots=None):
    """Pattern flip zones. If a list is passed as snapshots, it gets, per candle, every live pattern level
    (unbroken levels and flipped levels not yet broken again) as (lo, hi)."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = atr(df)
    highs, lows = [], []    # confirmed swing points (candle, price)
    res, sup = [], []       # unbroken levels [lo, hi]: resistance (break up -> demand), support (break down -> supply)
    flipped = []            # flipped levels still alive: (side, lo, hi)

    def add(levels, lo, hi, tol):
        for lv in levels:  # merge with an overlapping level instead of stacking duplicates
            if lo <= lv[1] + tol and hi >= lv[0] - tol:
                lv[0], lv[1] = min(lv[0], lo), max(lv[1], hi)
                return
        levels.append([lo, hi])
        del levels[:-30]

    def zone(lv, side):
        lo, hi = lv
        if hi - lo < MIN_HEIGHT * a_b:
            mid = (hi + lo) / 2
            lo, hi = mid - MIN_HEIGHT * a_b / 2, mid + MIN_HEIGHT * a_b / 2
        return side, hi, lo

    def detect(b):
        out = _detect(b)
        if snapshots is not None:
            flipped[:] = [z for z in flipped + [(sd, lo, hi) for sd, hi, lo in out]
                          if not (z[0] == "dem" and c[b] < z[1]) and not (z[0] == "sup" and c[b] > z[2])]
            snapshots.append([(lv[0], lv[1]) for lv in res + sup] + [(z[1], z[2]) for z in flipped])
        return out

    def _detect(b):
        nonlocal a_b
        a_b = a[b]
        if math.isnan(a_b):
            return []
        tol, p = LEVEL_TOL * a_b, b - SWING
        if p - SWING >= 0:
            if h[p] > h[p - SWING:p].max() and h[p] >= h[p + 1:b + 1].max():
                same = [x for x in highs if abs(x[1] - h[p]) <= tol and p - x[0] <= LOOKBACK]
                if same:
                    add(res, min([h[p]] + [x[1] for x in same]), max([h[p]] + [x[1] for x in same]), tol)
                    q = max(x[0] for x in same)
                    neck = l[q:p + 1].min()         # double top neckline
                    add(sup, neck, neck, tol)
                highs.append((p, h[p]))
            if l[p] < l[p - SWING:p].min() and l[p] <= l[p + 1:b + 1].min():
                same = [x for x in lows if abs(x[1] - l[p]) <= tol and p - x[0] <= LOOKBACK]
                if same:
                    add(sup, min([l[p]] + [x[1] for x in same]), max([l[p]] + [x[1] for x in same]), tol)
                    q = max(x[0] for x in same)
                    neck = h[q:p + 1].max()         # double bottom neckline
                    add(res, neck, neck, tol)
                lows.append((p, l[p]))
            del highs[:-50], lows[:-50]
        strong = h[b] > l[b] and abs(c[b] - o[b]) >= BODY * (h[b] - l[b])
        out = []
        for lv in list(res):
            if c[b] > lv[1] + BREAK * a_b:          # broken upwards (confirmed or not, the level is used up)
                res.remove(lv)
                if strong and c[b] > o[b]:
                    out.append(zone(lv, "dem"))
        for lv in list(sup):
            if c[b] < lv[0] - BREAK * a_b:
                sup.remove(lv)
                if strong and c[b] < o[b]:
                    out.append(zone(lv, "sup"))
        return out

    a_b = math.nan
    return track(df, detect)


CONFIRM_TOL = 0.3  # a pattern level within 0.3 ATR of a base zone confirms it


def confirmed(df):
    """v7-E: v6 base zones, kept only when a pattern level on the same timeframe sits on them."""
    snaps = []
    flip_zones(df, snaps)
    a = atr(df)
    out = []
    for b, (s, d, tS, tD) in enumerate(zones(df)):
        tol = CONFIRM_TOL * a[b] if not math.isnan(a[b]) else 0.0
        ok = lambda z: any(lv[0] <= z[1] + tol and lv[1] >= z[0] - tol for lv in snaps[b])
        out.append(([z for z in s if ok(z)], [z for z in d if ok(z)], tS, tD))
    return out


def both(df):
    """v6 base zones and pattern flip zones together."""
    return [(b[0] + f[0], b[1] + f[1], b[2], b[3]) for b, f in zip(zones(df), flip_zones(df))]


# entry-zone sources for replay(entry_zones=...)
VARIANTS = {
    "A v6 base zones": (("4h", zones), ("1h", zones)),
    "B pattern H4": (("4h", flip_zones),),
    "C pattern H1": (("1h", flip_zones),),
    "D base + pattern H4+H1": (("4h", both), ("1h", both)),
    "E base confirmed by pattern": (("4h", confirmed), ("1h", confirmed)),
}
