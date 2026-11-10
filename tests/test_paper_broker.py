from datetime import datetime, timedelta, timezone

import pytest

from paper_broker.broker import BrokerError, PaperBroker, resolve

T0 = datetime(2026, 9, 30, 14, 0, 0, tzinfo=timezone.utc)  # 10:00 New York


class FakeMarket:
    def __init__(self):
        self.now = T0
        self.bars: dict[str, list] = {}
        self.last: dict[str, float] = {}

    def clock(self):
        return self.now

    def quote(self, sym):
        return (self.last[sym], self.now) if sym in self.last else None

    def bars_since(self, sym, since):
        return [b for b in self.bars.get(sym, []) if b[0] >= since - timedelta(minutes=2)]

    def add_bar(self, sym, minute, o, h, l, c):
        self.bars.setdefault(sym, []).append((T0 + timedelta(minutes=minute), o, h, l, c))
        self.last[sym] = c
        self.now = T0 + timedelta(minutes=minute + 1)


CONFIG = {"starting_cash": 50000.0, "daily_loss_limit": 1000.0, "max_drawdown": 2500.0,
          "max_contracts_per_symbol": 10, "futures_commission_per_side": 2.5,
          "micro_futures_commission_per_side": 0.75, "stock_commission_per_share": 0.0}


@pytest.fixture
def mkt():
    return FakeMarket()


@pytest.fixture
def broker(mkt, tmp_path):
    return PaperBroker(mkt.quote, mkt.bars_since, tmp_path / "t.db", config=dict(CONFIG), clock=mkt.clock)


def test_resolve_symbols():
    assert resolve("mnq").multiplier == 2.0 and resolve("NQ=F").data_symbol == "NQ=F"
    assert resolve("QQQ").is_future is False
    with pytest.raises(BrokerError, match="index"):
        resolve("^NDX")


def test_market_buy_fills_at_live_price_and_marks_equity(broker, mkt):
    mkt.last["AAPL"] = 100.0
    o = broker.place_order("AAPL", "buy", 10)
    assert o["status"] == "filled" and o["fill_price"] == 100.0
    mkt.last["AAPL"] = 110.0
    acct = broker.account()
    assert acct["equity"] == 50100.0 and acct["unrealized_pnl"] == 100.0


def test_bracket_take_profit_fills_and_cancels_stop(broker, mkt):
    mkt.last["AAPL"] = 100.0
    entry = broker.place_order("AAPL", "buy", 10, take_profit=105.0, stop_loss=95.0)
    mkt.add_bar("AAPL", 1, 100, 103, 99, 102)
    mkt.add_bar("AAPL", 2, 102, 106, 101, 105.5)
    broker.process()
    orders = {o["note"]: o for o in broker.orders("all") if o.get("parent_id") == entry["id"]}
    assert orders["take profit"]["status"] == "filled" and orders["take profit"]["fill_price"] == 105.0
    assert orders["stop loss"]["status"] == "cancelled"
    acct = broker.account()
    assert acct["positions"] == [] and acct["realized_pnl"] == 50.0 and acct["equity"] == 50050.0


def test_limit_order_never_fills_from_bar_before_it_was_placed(broker, mkt):
    mkt.add_bar("AAPL", 0, 100, 100, 90, 100)  # dipped to 90 before the order existed
    mkt.now = T0 + timedelta(minutes=0, seconds=30)
    broker.place_order("AAPL", "buy", 1, "limit", limit_price=95.0)
    broker.process()
    assert broker.orders("open")[0]["status"] == "working"
    mkt.add_bar("AAPL", 1, 99, 99, 94, 96)
    broker.process()
    assert broker.orders("filled")[0]["fill_price"] == 95.0


def test_futures_pnl_uses_multiplier_and_commission(broker, mkt):
    mkt.last["MNQ=F"] = 20000.0
    broker.place_order("MNQ", "sell", 2, stop_loss=20100.0)  # short 2 micros
    mkt.add_bar("MNQ=F", 1, 20000, 20010, 19950, 19960)
    broker.close_position("MNQ")
    acct = broker.account()
    # (20000 - 19960) * 2 contracts * $2 = $160, minus 4 sides * $0.75
    assert acct["realized_pnl"] == 160.0 - 3.0
    assert acct["equity"] == 50000.0 + 157.0
    assert broker.orders("open") == []


def test_daily_loss_limit_locks_new_entries_but_allows_exits(broker, mkt):
    mkt.last["NQ=F"] = 20000.0
    broker.place_order("NQ", "buy", 1)
    mkt.last["NQ=F"] = 19940.0  # -60 pts * $20 = -$1,200
    acct = broker.account()
    assert acct["trading_locked"].startswith("daily loss limit")
    with pytest.raises(BrokerError, match="locked"):
        broker.place_order("NQ", "buy", 1)
    assert broker.close_position("NQ")["status"] == "filled"


def test_buying_power_and_contract_limits(broker, mkt):
    mkt.last["AAPL"] = 100.0
    with pytest.raises(BrokerError, match="buying power"):
        broker.place_order("AAPL", "buy", 600)
    mkt.last["MES=F"] = 6000.0
    with pytest.raises(BrokerError, match="limit is 10"):
        broker.place_order("MES", "buy", 11)


def test_bracket_prices_must_be_on_the_right_side(broker, mkt):
    mkt.last["AAPL"] = 100.0
    with pytest.raises(BrokerError, match="stop_loss"):
        broker.place_order("AAPL", "buy", 1, stop_loss=101.0)


def test_market_order_when_quote_is_stale_waits_for_next_bar_open(broker, mkt):
    mkt.last["AAPL"] = 100.0
    mkt.now = T0 + timedelta(hours=10)  # market closed: last quote is old
    real_quote = mkt.quote
    mkt.quote = lambda s: (100.0, T0)
    broker.quote_fn = mkt.quote
    o = broker.place_order("AAPL", "buy", 1)
    assert o["status"] == "working"
    mkt.bars["AAPL"] = [(T0 + timedelta(hours=10, minutes=5), 103.0, 104.0, 102.0, 103.5)]
    mkt.now = T0 + timedelta(hours=10, minutes=6)  # that bar has happened
    broker.process()
    assert broker.orders("filled")[0]["fill_price"] == 103.0
    mkt.quote = real_quote
