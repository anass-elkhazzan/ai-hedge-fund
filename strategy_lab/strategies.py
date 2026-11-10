"""Rule-based intraday strategies. Each scans one session and proposes trade setups.

A setup = (signal bar index i, side ±1, stop price, target price). The trade enters at the
NEXT bar's open (no look-ahead) and is managed by sim.run_trade.

1. Opening-range breakout (ORB)
   Range: ORH, ORL over the first k minutes.  Long when C_t > ORH, short when C_t < ORL
   (first break only, before 11:30).  Stop = opposite side of the range (or its midpoint
   M = (ORH+ORL)/2).  Risk R = |entry − stop|; target = entry ± m·R.

2. VWAP mean reversion (VMR)
   Stretch: |z_t| ≥ k, where z = (C − VWAP)/σ.  Signal when price closes back inside the
   band after a stretch (z_{t−1} beyond ±k, z_t back within).  Fade toward VWAP:
   target = VWAP_t, stop = session extreme of the stretch ± b·σ_t.

3. Trend-day pullback (TPB)
   Trend up when C_t > VWAP_t, EMA9 > EMA21 and VWAP_t > VWAP_{t−6} (rising).
   Pullback = L_t ≤ EMA21_t and C_t > EMA21_t and C_t > O_t (buyers defend the average).
   Stop = lowest low of the last 3 bars − 0.25·ATR; target = entry + m·R. Mirror for shorts.
"""

from __future__ import annotations

import pandas as pd

from strategy_lab.indicators import atr, ema, opening_range, session_vwap


def _t(day: pd.DataFrame, i: int) -> str:
    return day.index[i].strftime("%H:%M")


def orb(day: pd.DataFrame, or_minutes: int = 15, rr: float = 2.0, stop_mode: str = "opposite",
        last_entry: str = "11:30", **_) -> list[tuple]:
    orh, orl, k = opening_range(day, or_minutes)
    mid = (orh + orl) / 2
    for i in range(k, len(day) - 1):
        if _t(day, i) > last_entry:
            break
        c = day["close"].iloc[i]
        if c > orh or c < orl:
            side = 1 if c > orh else -1
            stop = (orl if side == 1 else orh) if stop_mode == "opposite" else mid
            return [(i, side, stop, None, rr)]  # target set from the actual entry price
    return []


def vwap_reversion(day: pd.DataFrame, k: float = 2.0, buf: float = 0.5, start: str = "10:00",
                   end: str = "15:00", max_trades: int = 2, **_) -> list[tuple]:
    d = session_vwap(day)
    out, extreme_hi, extreme_lo = [], None, None
    for i in range(1, len(d) - 1):
        z_prev, z = d["z"].iloc[i - 1], d["z"].iloc[i]
        hi, lo = d["high"].iloc[i], d["low"].iloc[i]
        extreme_hi = hi if extreme_hi is None else max(extreme_hi, hi)
        extreme_lo = lo if extreme_lo is None else min(extreme_lo, lo)
        if not (start <= _t(d, i) <= end) or pd.isna(z) or pd.isna(z_prev):
            continue
        sd, vw = d["vwap_sd"].iloc[i], d["vwap"].iloc[i]
        if z_prev >= k and z < k:  # stretched up, now closing back in: short toward VWAP
            out.append((i, -1, extreme_hi + buf * sd, vw, None))
        elif z_prev <= -k and z > -k:
            out.append((i, 1, extreme_lo - buf * sd, vw, None))
        if len(out) >= max_trades:
            break
    return out


def trend_pullback(day: pd.DataFrame, rr: float = 2.0, start: str = "10:00", end: str = "14:30",
                   max_trades: int = 2, **_) -> list[tuple]:
    d = session_vwap(day)
    e9, e21, a = ema(d["close"], 9), ema(d["close"], 21), atr(d, 14)
    out = []
    for i in range(21, len(d) - 1):
        if not (start <= _t(d, i) <= end):
            continue
        c, o, h, l = (d[x].iloc[i] for x in ("close", "open", "high", "low"))
        vw, vw_prev = d["vwap"].iloc[i], d["vwap"].iloc[i - 6]
        up = c > vw and e9.iloc[i] > e21.iloc[i] and vw > vw_prev
        down = c < vw and e9.iloc[i] < e21.iloc[i] and vw < vw_prev
        if up and l <= e21.iloc[i] < c and c > o:
            out.append((i, 1, float(d["low"].iloc[i - 2:i + 1].min() - 0.25 * a.iloc[i]), None, rr))
        elif down and h >= e21.iloc[i] > c and c < o:
            out.append((i, -1, float(d["high"].iloc[i - 2:i + 1].max() + 0.25 * a.iloc[i]), None, rr))
        if len(out) >= max_trades:
            break
    return out


STRATEGIES = {"ORB": orb, "VWAP reversion": vwap_reversion, "Trend pullback": trend_pullback}

GRIDS = {
    "ORB": [dict(or_minutes=m, rr=r, stop_mode=s) for m in (15, 30) for r in (1.0, 1.5, 2.0) for s in ("opposite", "mid")],
    "VWAP reversion": [dict(k=k, buf=b) for k in (1.5, 2.0, 2.5) for b in (0.25, 0.5, 1.0)],
    "Trend pullback": [dict(rr=r) for r in (1.0, 1.5, 2.0, 3.0)],
}
