from datetime import datetime, timedelta, timezone

import pandas as pd

from ai_hedge.data import live_estimate as le


def _frame(start, closes):
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz="America/New_York")
    return pd.DataFrame({"Close": closes}, index=idx)


def test_estimate_scales_delayed_future_by_etf_move(monkeypatch):
    now_ny = pd.Timestamp.now(tz="America/New_York").floor("min")
    etf = _frame(now_ny - pd.Timedelta(minutes=11), [500.0] + [501.0] * 10 + [505.0])  # ETF +1% since 10 min ago
    fut = _frame(now_ny - pd.Timedelta(minutes=11), [20000.0])  # futures feed stuck 10 min back
    monkeypatch.setattr(le, "_bars", lambda s: fut if s.endswith("=F") else etf)
    est = le.live_estimate("MNQ=F")
    assert est["proxy"] == "QQQ"
    assert abs(est["price"] - 20000.0 * 505.0 / 500.0) < 1e-6


def test_no_estimate_when_etf_is_not_trading(monkeypatch):
    old = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(hours=5)
    monkeypatch.setattr(le, "_bars", lambda s: _frame(old, [100.0, 101.0]))
    assert le.live_estimate("MES=F") is None


def test_no_estimate_for_stocks():
    assert le.live_estimate("AAPL") is None
