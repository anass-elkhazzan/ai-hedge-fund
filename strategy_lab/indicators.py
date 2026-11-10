"""Intraday indicator math for the strategy lab (all on New York cash-session bars).

Notation: bar t has open O_t, high H_t, low L_t, close C_t, volume V_t.
Typical price  TP_t = (H_t + L_t + C_t) / 3

Session VWAP (anchored at 9:30 ET):
    VWAP_t   = Σ_{i≤t} TP_i·V_i / Σ_{i≤t} V_i
VWAP standard deviation (volume-weighted):
    σ_t      = sqrt( Σ V_i·TP_i² / Σ V_i  −  VWAP_t² )
Distance from VWAP in σ units (z-score):
    z_t      = (C_t − VWAP_t) / σ_t
Exponential moving average with period n:
    α = 2/(n+1),   EMA_t = α·C_t + (1−α)·EMA_{t−1}
True range and Average True Range (Wilder, period n):
    TR_t     = max(H_t − L_t, |H_t − C_{t−1}|, |L_t − C_{t−1}|)
    ATR_t    = ATR_{t−1} + (TR_t − ATR_{t−1}) / n
Opening range over the first k minutes:
    ORH = max(H) and ORL = min(L) over bars in [9:30, 9:30+k)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def session_vwap(day: pd.DataFrame) -> pd.DataFrame:
    """Add vwap, vwap_sd and z columns to one session's bars."""
    tp = (day["high"] + day["low"] + day["close"]) / 3.0
    v = day["volume"].replace(0, np.nan).fillna(1.0)
    cum_v = v.cumsum()
    vwap = (tp * v).cumsum() / cum_v
    var = (v * tp * tp).cumsum() / cum_v - vwap * vwap
    sd = np.sqrt(var.clip(lower=0))
    out = day.copy()
    out["vwap"] = vwap
    out["vwap_sd"] = sd
    out["z"] = (day["close"] - vwap) / sd.replace(0, np.nan)
    return out


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def opening_range(day: pd.DataFrame, minutes: int) -> tuple[float, float, int]:
    """(ORH, ORL, index of the first bar after the range)."""
    start = day.index[0]
    in_range = day.index < start + pd.Timedelta(minutes=minutes)
    k = int(in_range.sum())
    return float(day["high"].iloc[:k].max()), float(day["low"].iloc[:k].min()), k
