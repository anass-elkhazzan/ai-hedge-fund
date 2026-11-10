"""Historical intraday futures data for the lab (Yahoo: 5-minute bars, ~60 days)."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
CACHE = Path(__file__).resolve().parent / "cache"

SPECS = {
    # symbol: (yahoo symbol, $ per point, tick size, commission per side)
    "MNQ": ("MNQ=F", 2.0, 0.25, 0.75),
    "MES": ("MES=F", 5.0, 0.25, 0.75),
}


def load(symbol: str, refresh: bool = False) -> pd.DataFrame:
    """5-minute bars of the 9:30-16:00 ET cash session, indexed in New York time."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{symbol}_5m.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
        return df
    raw = yf.Ticker(SPECS[symbol][0]).history(period="60d", interval="5m")
    raw.index = raw.index.tz_convert("America/New_York")
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df.between_time("09:30", "15:55")
    # keep only complete sessions (78 five-minute bars); half days and today's partial day drop out
    sizes = df.groupby(df.index.date).size()
    full_days = set(sizes[sizes >= 76].index)
    df = df[[d in full_days for d in df.index.date]]
    df.to_csv(path)
    return df


def sessions(df: pd.DataFrame) -> list[pd.DataFrame]:
    return [g for _, g in df.groupby(df.index.date)]
