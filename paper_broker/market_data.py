"""Live prices for the paper broker, from yfinance 1-minute bars."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import yfinance as yf

# yfinance logs "possibly delisted" whenever a window has no new bars yet; that's normal here
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

_quote_cache: dict[str, tuple[float, tuple[float, datetime] | None]] = {}
_QUOTE_TTL = 15  # seconds


def quote(symbol: str) -> tuple[float, datetime] | None:
    """(last price, time of that price in UTC) or None.

    Index futures are ~10 minutes delayed on Yahoo; during ETF trading hours we use the
    near-live estimate from QQQ/SPY/DIA/IWM instead (see ai_hedge.data.live_estimate).
    """
    from ai_hedge.data.live_estimate import live_estimate
    est = live_estimate(symbol)
    if est:
        return (est["price"], est["as_of"])
    return delayed_quote(symbol)


def delayed_quote(symbol: str) -> tuple[float, datetime] | None:
    """Last traded price from Yahoo 1-minute bars (futures: ~10 minutes behind)."""
    hit = _quote_cache.get(symbol)
    if hit and time.time() - hit[0] < _QUOTE_TTL:
        return hit[1]
    result = None
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        if df is not None and not df.empty:
            ts = df.index[-1].to_pydatetime().astimezone(timezone.utc) + timedelta(minutes=1)
            result = (float(df["Close"].iloc[-1]), min(ts, datetime.now(timezone.utc)))
        else:
            price = yf.Ticker(symbol).fast_info.get("last_price")
            if price:
                result = (float(price), datetime.now(timezone.utc) - timedelta(days=1))  # time unknown: treat as stale
    except Exception:
        result = None
    _quote_cache[symbol] = (time.time(), result)
    return result


def bars_since(symbol: str, since: datetime) -> list[tuple[datetime, float, float, float, float]]:
    """1-minute bars (start time UTC, open, high, low, close) from `since` to now.

    yfinance keeps ~30 days of 1-minute data and serves at most 8 days per request.
    """
    now = datetime.now(timezone.utc)
    start = max(since - timedelta(minutes=2), now - timedelta(days=29))
    out = []
    while start < now:
        end = min(start + timedelta(days=7), now + timedelta(minutes=1))
        try:
            df = yf.Ticker(symbol).history(start=start, end=end, interval="1m")
        except Exception:
            df = None
        if df is not None and not df.empty:
            for idx, row in df.iterrows():
                out.append((idx.to_pydatetime().astimezone(timezone.utc),
                            float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])))
        start = end
    out.sort(key=lambda b: b[0])
    return out
