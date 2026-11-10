from datetime import datetime, timedelta, timezone

import pytest

from paper_broker.broker import DEFAULT_CONFIG, PRESETS, BrokerError, PaperBroker

# Thursday 2026-10-01, 10:00 New York (14:00 UTC)
T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
MNQ = "MNQ=F"


class Market:
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

    def bar(self, sym, at, o, h, l, c):
        self.bars.setdefault(sym, []).append((at, o, h, l, c))
        self.last[sym] = c


@pytest.fixture
def mkt():
    return Market()


@pytest.fixture
def broker(mkt, tmp_path):
    cfg = {**DEFAULT_CONFIG, **PRESETS["mff_rapid_50k"]}
    return PaperBroker(mkt.quote, mkt.bars_since, tmp_path / "mff.db", config=cfg, clock=mkt.clock)


def ny(day, h, m):
    """UTC datetime for a New York wall time in early October 2026 (UTC-4)."""
    return datetime(2026, 10, day, h + 4, m, tzinfo=timezone.utc)


def test_futures_only(broker, mkt):
    mkt.last["AAPL"] = 100.0
    with pytest.raises(BrokerError, match="futures only"):
        broker.place_order("AAPL", "buy", 1)


def test_contract_cap_counts_micros_as_tenth_of_a_mini(broker, mkt):
    mkt.last[MNQ] = 20000.0
    mkt.last["NQ=F"] = 20000.0
    broker.place_order("NQ", "buy", 4, stop_loss=19900)
    broker.place_order("MNQ", "buy", 10, stop_loss=19900)  # 4 + 1 = 5 minis: allowed
    with pytest.raises(BrokerError, match="mini-contract equivalents"):
        broker.place_order("MNQ", "buy", 1, stop_loss=19900)


def test_positions_auto_liquidated_at_410pm_and_orders_cancelled(broker, mkt):
    mkt.last[MNQ] = 20000.0
    broker.place_order("MNQ", "buy", 2, take_profit=20500, stop_loss=19500)
    mkt.bar(MNQ, ny(1, 16, 5), 20050, 20060, 20040, 20055)
    mkt.bar(MNQ, ny(1, 16, 10), 20100, 20600, 20090, 20550)  # would hit the target, but only after the close
    mkt.now = ny(1, 16, 20)
    acct = broker.account()
    assert acct["positions"] == [] and acct["open_orders"] == []
    liquidation = broker.history(1)[0]
    assert liquidation["price"] == 20100 and liquidation["ts"].startswith("2026-10-01T20:10")
    assert acct["realized_pnl"] == (20100 - 20000) * 2 * 2 - 4 * 0.75
    with pytest.raises(BrokerError, match="Market closed"):
        broker.place_order("MNQ", "buy", 1)


def test_eod_trailing_floor_moves_only_at_day_end_and_locks(broker, mkt):
    mkt.last[MNQ] = 20000.0
    assert broker.account()["max_loss_floor"] == 48000.0
    broker.place_order("MNQ", "buy", 10, stop_loss=19000)
    mkt.last[MNQ] = 20100.0  # +$2,000 open profit intraday
    assert broker.account()["max_loss_floor"] == 48000.0  # EOD: no intraday trailing
    broker.close_position("MNQ")
    mkt.now = ny(2, 9, 0)  # next trading day
    acct = broker.account()
    # EOD balance: 100 pts x 10 x $2 - 20 sides x $0.75 = +$1,985 -> floor $49,985 (below the $50,100 lock)
    assert acct["max_loss_floor"] == 49985.0
    assert acct["evaluation"]["daily_pnl"]["2026-10-01"] == pytest.approx(1985.0)


def test_consistency_rule_blocks_pass_until_profit_is_spread(broker, mkt):
    mkt.last[MNQ] = 20000.0
    broker.place_order("MNQ", "buy", 10)
    mkt.last[MNQ] = 20160.0
    broker.close_position("MNQ")  # one big day: +$3,185
    ev = broker.account()["evaluation"]
    assert ev["status"] == "in progress" and ev["consistency_ok"] is False and ev["trading_days"] == 1
    assert ev["profit_needed_for_consistency"] == pytest.approx(3185.0)


def test_intrabar_breach_detected_between_checks(broker, mkt):
    mkt.last[MNQ] = 20000.0
    broker.place_order("MNQ", "buy", 10)  # $20 per point
    mkt.now = ny(1, 11, 0)
    mkt.bar(MNQ, ny(1, 10, 30), 20000, 20010, 19890, 19995)  # dipped 110 pts = -$2,200 then recovered
    acct = broker.account()
    assert acct["trading_locked"] and "max loss limit" in acct["trading_locked"]
    assert acct["evaluation"]["status"] == "FAILED"


def test_update_bracket_moves_stop_to_breakeven(broker, mkt):
    mkt.last[MNQ] = 20000.0
    broker.place_order("MNQ", "buy", 1, take_profit=20100, stop_loss=19950)
    mkt.last[MNQ] = 20040.0
    broker.update_bracket("MNQ", stop_loss=20000.0)
    mkt.now = ny(1, 11, 0)
    mkt.bar(MNQ, ny(1, 10, 30), 20040, 20045, 19990, 19995)
    broker.process()
    stop = [o for o in broker.orders("filled") if o.get("note") == "stop loss"][0]
    assert stop["fill_price"] == 20000.0
    with pytest.raises(BrokerError, match="No open"):
        broker.update_bracket("MNQ", stop_loss=19990.0)


def test_earliest_trigger_wins_across_orders_after_a_gap(broker, mkt):
    # Short MES: the stop is hit at 11:49, the target only at 13:19. Checked once, at 14:00.
    mes = "MES=F"
    mkt.last[mes] = 7770.0
    mkt.now = ny(1, 11, 0)
    broker.place_order("MES", "sell", 2, take_profit=7755.0, stop_loss=7778.0)
    mkt.bar(mes, ny(1, 11, 49), 7774, 7780, 7773, 7779)
    mkt.bar(mes, ny(1, 13, 19), 7760, 7761, 7750, 7752)
    mkt.now = ny(1, 14, 0)
    broker.process()
    filled = broker.orders("filled")
    assert [o["note"] for o in filled if o.get("parent_id")] == ["stop loss"]
    assert broker.account()["positions"] == []


def test_same_bar_target_and_stop_assumes_stop(broker, mkt):
    mkt.last[MNQ] = 20000.0
    broker.place_order("MNQ", "buy", 1, take_profit=20020, stop_loss=19990)
    mkt.now = ny(1, 10, 30)
    mkt.bar(MNQ, ny(1, 10, 5), 20000, 20025, 19985, 20010)  # both levels inside one bar
    broker.process()
    assert [o["note"] for o in broker.orders("filled") if o.get("parent_id")] == ["stop loss"]


def test_engine_never_uses_bars_from_the_future(broker, mkt):
    mkt.last[MNQ] = 20000.0
    broker.place_order("MNQ", "buy", 1, "limit", limit_price=19900, stop_loss=19800)
    mkt.bar(MNQ, ny(1, 12, 0), 19950, 19960, 19890, 19900)  # data exists, but it's only 10:00 now
    mkt.now = ny(1, 10, 0)
    broker.process()
    assert broker.orders("filled") == []


def test_equity_marks_no_worse_than_working_stop(broker, mkt):
    """5 Nov 2026: the live quote fell past the stop before the delayed bars did; the account must not breach on the quote."""
    mkt.now = ny(5, 9, 40)
    mkt.last[MNQ] = 28000.0
    broker.place_order("MNQ", "buy", 1, stop_loss=27900.0)
    mkt.now = ny(5, 9, 47)
    mkt.last[MNQ] = 27000.0  # quote gaps far below the stop; no delayed bar has reached 27,900 yet
    acct = broker.account()
    assert acct["trading_locked"] is None
    assert acct["equity"] == pytest.approx(50000 - 200 - 0.75 * 2, abs=2)  # marked at the stop (-100 pts x $2), not at 27,000
    # once the delayed bars show the stop traded, it fills at the stop, as a resting order would
    mkt.bar(MNQ, ny(5, 9, 44), 27990.0, 27995.0, 27850.0, 27860.0)
    acct = broker.account()
    assert acct["positions"] == []
    assert acct["trading_locked"] is None
