"""Paper trading engine: simulated account, orders and fills on live market prices.

Fills are decided from real 1-minute bars (yfinance), never from the price the
caller hopes for. Supports stocks/ETFs and CME index futures (NQ, MNQ, ES, MES...)
with contract multipliers, bracket orders (take-profit + stop-loss, OCO), and
prop-firm style guardrails (daily loss limit, trailing max drawdown).

Price access is injected (quote_fn / bars_fn) so the engine is testable offline.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "paper_broker.db"
CONFIG_PATH = HERE / "config.json"

DEFAULT_CONFIG = {
    "rule_set": "custom",
    "starting_cash": 50000.0,
    # Prop-firm style guardrails; 0 / "" disables a rule
    "daily_loss_limit": 1000.0,
    "max_drawdown": 2500.0,
    "drawdown_type": "intraday_trailing",  # or "eod_trailing": the floor only moves at end of day
    "drawdown_lock_balance": 0.0,  # the floor stops trailing once it reaches this balance
    "max_contracts_per_symbol": 10,
    "max_mini_equivalents": 0.0,  # account-wide futures cap; 10 micros = 1 mini
    "session_close_et": "",  # e.g. "16:10": flatten + cancel everything; no trading until 18:00 ET
    "futures_only": False,
    "profit_target": 0.0,
    "consistency_pct": 0.0,  # best day may be at most this share of total profit
    "min_trading_days": 0,
    "futures_commission_per_side": 2.50,
    "micro_futures_commission_per_side": 0.75,
    "stock_commission_per_share": 0.0,
}

# Source: help.myfundedfutures.com "Rapid Plan 50k - A Comprehensive Look" (updated 2026-08-24);
# 4:10 PM ET flat rule and $100 lock from MFF plan summaries (Sep 2026).
PRESETS = {
    "mff_rapid_50k": {
        "rule_set": "MyFundedFutures Rapid 50K (evaluation)",
        "starting_cash": 50000.0,
        "daily_loss_limit": 0.0,
        "max_drawdown": 2000.0,
        "drawdown_type": "eod_trailing",
        "drawdown_lock_balance": 50100.0,
        "max_contracts_per_symbol": 50,
        "max_mini_equivalents": 5.0,
        "session_close_et": "16:10",
        "futures_only": True,
        "profit_target": 3000.0,
        "consistency_pct": 0.5,
        "min_trading_days": 2,
    },
}


@dataclass(frozen=True)
class Instrument:
    symbol: str  # our canonical symbol, e.g. "AAPL" or "MNQ"
    data_symbol: str  # yfinance symbol, e.g. "AAPL" or "MNQ=F"
    multiplier: float
    is_future: bool
    is_micro: bool
    tradingview: str


# root -> (multiplier, TradingView continuous symbol)
FUTURES = {
    "NQ": (20.0, "CME_MINI:NQ1!"), "MNQ": (2.0, "CME_MINI:MNQ1!"),
    "ES": (50.0, "CME_MINI:ES1!"), "MES": (5.0, "CME_MINI:MES1!"),
    "YM": (5.0, "CBOT_MINI:YM1!"), "MYM": (0.5, "CBOT_MINI:MYM1!"),
    "RTY": (50.0, "CME_MINI:RTY1!"), "M2K": (5.0, "CME_MINI:M2K1!"),
    "CL": (1000.0, "NYMEX:CL1!"), "MCL": (100.0, "NYMEX:MCL1!"),
    "GC": (100.0, "COMEX:GC1!"), "MGC": (10.0, "COMEX:MGC1!"),
}
INDEX_HINTS = {
    "^NDX": "MNQ or NQ futures, or the QQQ ETF", "^IXIC": "MNQ or NQ futures, or the QQQ ETF",
    "^GSPC": "MES or ES futures, or the SPY ETF", "^SPX": "MES or ES futures, or the SPY ETF",
    "^DJI": "MYM or YM futures, or the DIA ETF", "^RUT": "M2K or RTY futures, or the IWM ETF",
}


class BrokerError(ValueError):
    """A rejected request; the message is shown to the user as-is."""


def resolve(symbol: str) -> Instrument:
    s = symbol.strip().upper()
    if s in INDEX_HINTS:
        raise BrokerError(f"{s} is an index and can't be traded directly. Trade {INDEX_HINTS[s]} instead.")
    root = s.removesuffix("=F").removesuffix("1!")
    if root in FUTURES:
        mult, tv = FUTURES[root]
        return Instrument(root, f"{root}=F", mult, True, root.startswith("M"), tv)
    if not s or not all(c.isalnum() or c in ".-" for c in s):
        raise BrokerError(f"Unrecognised symbol '{symbol}'.")
    return Instrument(s, s, 1.0, False, False, s)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


# A bar: (start_time_utc, open, high, low, close)
Bar = tuple[datetime, float, float, float, float]
QuoteFn = Callable[[str], tuple[float, datetime] | None]
BarsFn = Callable[[str, datetime], list[Bar]]


class PaperBroker:
    def __init__(self, quote_fn: QuoteFn, bars_fn: BarsFn, db_path: Path | str = DEFAULT_DB,
                 config: dict | None = None, clock: Callable[[], datetime] = _now):
        self.quote_fn, self.bars_fn, self.clock = quote_fn, bars_fn, clock
        self.config = {**DEFAULT_CONFIG, **(config if config is not None else _load_config())}
        # 30 s: the executor and the MCP server share this file; 5 s was too short (export failed 7 Oct 15:50)
        self.db = sqlite3.connect(str(db_path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    # ---------- storage ----------
    def _init_db(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1), starting_cash REAL, cash REAL, realized_pnl REAL,
            equity_high REAL, day TEXT, day_start_equity REAL, breached TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL, type TEXT,
            limit_price REAL, stop_price REAL, status TEXT, parent_id INTEGER, created_at TEXT,
            checked_until TEXT, filled_at TEXT, fill_price REAL, note TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL);
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, symbol TEXT, side TEXT, qty REAL,
            price REAL, commission REAL, realized_pnl REAL, ts TEXT);
        CREATE TABLE IF NOT EXISTS day_stats (day TEXT PRIMARY KEY, start_equity REAL, end_equity REAL);
        """)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(account)")}
        for col, typ in (("floor", "REAL"), ("last_close_applied", "TEXT")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE account ADD COLUMN {col} {typ}")
        if not self.db.execute("SELECT 1 FROM account").fetchone():
            self._reset_account(float(self.config["starting_cash"]))
        self.db.commit()

    def _reset_account(self, cash: float) -> None:
        now = self.clock()
        for t in ("account", "orders", "positions", "fills", "day_stats"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.execute("INSERT INTO account (id, starting_cash, cash, realized_pnl, equity_high, day, day_start_equity, "
                        "breached, created_at, floor, last_close_applied) VALUES (1, ?, ?, 0, ?, ?, ?, NULL, ?, NULL, ?)",
                        (cash, cash, cash, self._trading_day(now), cash, _iso(now), _iso(now)))

    def _acct(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM account WHERE id = 1").fetchone()

    @staticmethod
    def _trading_day(dt: datetime) -> str:
        # CME session rolls at 17:00 New York time; use that for everyone
        ny = dt.astimezone(NY)
        if ny.hour >= 17:
            ny += timedelta(days=1)
        return ny.date().isoformat()

    # ---------- prices ----------
    def _mark(self, inst: Instrument) -> float | None:
        q = self.quote_fn(inst.data_symbol)
        return q[0] if q else None

    def _protected_mark(self, pos: sqlite3.Row, px: float) -> float:
        """Mark a position no worse than its working stop.

        The live quote can run ahead of the delayed bars that fill stops (5 Nov 2026: the quote read 27,641 while the
        stop at 27,777.75 had not been processed yet, so equity breached the floor and the account failed, although
        a real resting stop would have exited near 27,777). Until the stop fills from the bars, equity is marked at
        the stop; the fill itself still uses the bars, so a gap through the stop is still charged at the gap price.
        """
        qty = pos["qty"]
        stops = [r["stop_price"] for r in self.db.execute(
            "SELECT stop_price FROM orders WHERE status = 'working' AND type = 'stop' AND symbol = ? AND side = ?",
            (pos["symbol"], "sell" if qty > 0 else "buy")) if r["stop_price"] is not None]
        if not stops:
            return px
        return max(px, max(stops)) if qty > 0 else min(px, min(stops))

    def _commission(self, inst: Instrument, qty: float) -> float:
        if inst.is_future:
            key = "micro_futures_commission_per_side" if inst.is_micro else "futures_commission_per_side"
            return float(self.config[key]) * qty
        return float(self.config["stock_commission_per_share"]) * qty

    # ---------- account view ----------
    def account(self) -> dict:
        self.process()
        a = self._acct()
        positions, unrealized, stock_value = [], 0.0, 0.0
        for p in self.db.execute("SELECT * FROM positions WHERE qty != 0 ORDER BY symbol"):
            inst = resolve(p["symbol"])
            px = self._mark(inst) or p["avg_price"]
            upnl = (self._protected_mark(p, px) - p["avg_price"]) * p["qty"] * inst.multiplier  # last_price stays the real quote
            unrealized += upnl
            if not inst.is_future:
                stock_value += p["qty"] * px
            positions.append({
                "symbol": p["symbol"], "quantity": p["qty"], "side": "long" if p["qty"] > 0 else "short",
                "avg_price": round(p["avg_price"], 4), "last_price": round(px, 4),
                "unrealized_pnl": round(upnl, 2), "multiplier": inst.multiplier,
                "chart": f"https://www.tradingview.com/chart/?symbol={inst.tradingview}",
            })
        equity = a["cash"] + stock_value + sum(
            pp["unrealized_pnl"] for pp in positions if resolve(pp["symbol"]).is_future)
        return {
            "equity": round(equity, 2), "cash": round(a["cash"], 2), "starting_cash": a["starting_cash"],
            "total_pnl": round(equity - a["starting_cash"], 2), "realized_pnl": round(a["realized_pnl"], 2),
            "unrealized_pnl": round(unrealized, 2), "today_pnl": round(equity - a["day_start_equity"], 2),
            "trading_day": a["day"], "equity_high": round(a["equity_high"], 2),
            "rule_set": self.config["rule_set"],
            "rules": {k: self.config[k] for k in (
                "daily_loss_limit", "max_drawdown", "drawdown_type", "drawdown_lock_balance", "max_contracts_per_symbol",
                "max_mini_equivalents", "session_close_et", "futures_only", "profit_target", "consistency_pct",
                "min_trading_days")},
            "max_loss_floor": round(a["floor"], 2) if a["floor"] is not None else None,
            "room_to_floor": round(equity - a["floor"], 2) if a["floor"] is not None else None,
            "trading_locked": a["breached"], "evaluation": self._evaluation(equity), "positions": positions,
            "open_orders": self.orders("open"),
        }

    def _evaluation(self, equity: float) -> dict | None:
        target = float(self.config["profit_target"])
        if not target:
            return None
        a = self._acct()
        days = {r["day"]: r["end_equity"] - r["start_equity"] for r in self.db.execute("SELECT * FROM day_stats")}
        days[a["day"]] = equity - a["day_start_equity"]
        traded = {self._trading_day(_parse(f["ts"])) for f in self.db.execute("SELECT ts FROM fills")}
        total = equity - a["starting_cash"]
        best_day, best = max(days.items(), key=lambda kv: kv[1]) if days else (None, 0.0)
        pct = float(self.config["consistency_pct"])
        consistent = total <= 0 or not pct or best <= pct * total + 1e-9
        min_days = int(self.config["min_trading_days"])
        if a["breached"] and not a["breached"].startswith("daily"):
            status = "FAILED"
        elif total >= target and consistent and len(traded) >= min_days:
            status = "PASSED"
        else:
            status = "in progress"
        return {
            "status": status, "profit": round(total, 2), "profit_target": target,
            "remaining_to_target": round(max(0.0, target - total), 2),
            "best_day": best_day, "best_day_profit": round(best, 2),
            "consistency_rule": f"best day <= {pct:.0%} of total profit" if pct else None,
            "consistency_ok": consistent,
            "profit_needed_for_consistency": round(max(0.0, best / pct - total), 2) if pct and best > 0 else 0.0,
            "trading_days": len(traded), "min_trading_days": min_days,
            "daily_pnl": {d: round(v, 2) for d, v in sorted(days.items())},
        }

    def _equity(self) -> float:
        a = self._acct()
        eq = a["cash"]
        for p in self.db.execute("SELECT * FROM positions WHERE qty != 0"):
            inst = resolve(p["symbol"])
            px = self._mark(inst) or p["avg_price"]
            px = self._protected_mark(p, px)
            eq += (px - p["avg_price"]) * p["qty"] * inst.multiplier if inst.is_future else p["qty"] * px
        return eq

    def _update_risk_state(self) -> None:
        a, eq, now = self._acct(), self._equity(), self.clock()
        day = self._trading_day(now)
        eod = self.config["drawdown_type"] == "eod_trailing"
        if day != a["day"]:
            # close the books on the previous trading day
            self.db.execute("INSERT OR REPLACE INTO day_stats VALUES (?, ?, ?)", (a["day"], a["day_start_equity"], eq))
            self.db.execute("UPDATE account SET day = ?, day_start_equity = ? WHERE id = 1", (day, eq))
            if eod and eq > a["equity_high"]:
                self.db.execute("UPDATE account SET equity_high = ? WHERE id = 1", (eq,))
            # a daily-loss lock expires with the day; a drawdown breach does not
            if a["breached"] and a["breached"].startswith("daily"):
                self.db.execute("UPDATE account SET breached = NULL WHERE id = 1")
            a = self._acct()
        if not eod and eq > a["equity_high"]:
            self.db.execute("UPDATE account SET equity_high = ? WHERE id = 1", (eq,))
            a = self._acct()
        dll, mdd = float(self.config["daily_loss_limit"]), float(self.config["max_drawdown"])
        floor = None
        if mdd:
            floor = a["equity_high"] - mdd
            lock = float(self.config["drawdown_lock_balance"])
            if lock:
                floor = min(floor, lock)
        self.db.execute("UPDATE account SET floor = ? WHERE id = 1", (floor,))
        if not a["breached"]:
            if floor is not None and eq <= floor:
                self.db.execute("UPDATE account SET breached = ? WHERE id = 1",
                                (f"max loss limit hit: equity ${eq:,.2f} fell to the ${floor:,.2f} floor "
                                 f"({'end-of-day' if eod else 'intraday'} trailing ${mdd:,.0f})",))
            elif dll and eq - a["day_start_equity"] <= -dll:
                self.db.execute("UPDATE account SET breached = ? WHERE id = 1",
                                (f"daily loss limit hit: down ${a['day_start_equity'] - eq:,.2f} today (limit ${dll:,.0f})",))
        self.db.commit()

    # ---------- orders ----------
    def place_order(self, symbol: str, side: str, quantity: float, order_type: str = "market",
                    limit_price: float | None = None, stop_price: float | None = None,
                    take_profit: float | None = None, stop_loss: float | None = None, note: str = "") -> dict:
        self.process()
        inst = resolve(symbol)
        if self.config["futures_only"] and not inst.is_future:
            raise BrokerError(f"This account ({self.config['rule_set']}) trades futures only, e.g. MNQ or MES.")
        closed_msg = self._session_closed_message(self.clock())
        if closed_msg:
            raise BrokerError(closed_msg)
        side, order_type = side.lower().strip(), order_type.lower().strip()
        if side not in ("buy", "sell"):
            raise BrokerError("side must be 'buy' or 'sell' (sell with no position = open a short).")
        if order_type not in ("market", "limit", "stop"):
            raise BrokerError("order_type must be 'market', 'limit' or 'stop'.")
        if not quantity or quantity <= 0 or (inst.is_future and quantity != int(quantity)):
            raise BrokerError("quantity must be positive" + (" whole contracts." if inst.is_future else "."))
        if order_type == "limit" and not limit_price:
            raise BrokerError("A limit order needs limit_price.")
        if order_type == "stop" and not stop_price:
            raise BrokerError("A stop order needs stop_price.")

        pos = self._position(inst.symbol)
        signed = quantity if side == "buy" else -quantity
        opening = pos == 0 or (pos > 0) == (signed > 0) or abs(signed) > abs(pos)
        if opening:
            self._check_can_open(inst, pos + signed)

        ref = limit_price or stop_price or self._mark(inst)
        if ref is None:
            raise BrokerError(f"No price data for {inst.symbol}.")
        if take_profit is not None and ((side == "buy") != (take_profit > ref)):
            raise BrokerError(f"take_profit {take_profit} is on the wrong side of the entry (~{ref:.2f}) for a {side}.")
        if stop_loss is not None and ((side == "buy") != (stop_loss < ref)):
            raise BrokerError(f"stop_loss {stop_loss} is on the wrong side of the entry (~{ref:.2f}) for a {side}.")

        now = self.clock()
        oid = self._insert_order(inst.symbol, side, quantity, order_type, limit_price, stop_price, "working", None, now, note)
        exit_side = "sell" if side == "buy" else "buy"
        if take_profit is not None:
            self._insert_order(inst.symbol, exit_side, quantity, "limit", take_profit, None, "held", oid, now, "take profit")
        if stop_loss is not None:
            self._insert_order(inst.symbol, exit_side, quantity, "stop", None, stop_loss, "held", oid, now, "stop loss")
        self.db.commit()

        if order_type == "market":
            q = self.quote_fn(inst.data_symbol)
            # Fill now only if the quote is live; otherwise wait for the next real bar's open
            if q and now - q[1] <= timedelta(minutes=3):
                self._fill(self._order(oid), q[0], now)
                self.db.commit()
        self.process()
        return self._order_view(self._order(oid))

    def _check_can_open(self, inst: Instrument, new_pos: float) -> None:
        self._update_risk_state()
        a = self._acct()
        if a["breached"]:
            raise BrokerError(f"Trading locked: {a['breached']}. Only closing orders are allowed.")
        if inst.is_future:
            cap = self.config["max_contracts_per_symbol"]
            if cap and abs(new_pos) > cap:
                raise BrokerError(f"That would hold {abs(new_pos):g} {inst.symbol} contracts; the limit is {cap:g}.")
            mini_cap = float(self.config["max_mini_equivalents"])
            if mini_cap:
                def size(sym, q):
                    return abs(q) * (0.1 if resolve(sym).is_micro else 1.0)
                others = sum(size(p["symbol"], p["qty"]) for p in self.db.execute(
                    "SELECT * FROM positions WHERE qty != 0 AND symbol != ?", (inst.symbol,)) if resolve(p["symbol"]).is_future)
                total = others + size(inst.symbol, new_pos)
                if total > mini_cap + 1e-9:
                    raise BrokerError(f"That would hold {total:g} mini-contract equivalents; the limit is {mini_cap:g} "
                                      f"minis ({mini_cap * 10:g} micros).")
        else:
            px = self._mark(inst)
            if px is None:
                raise BrokerError(f"No price data for {inst.symbol}.")
            gross = sum(abs(p["qty"]) * (self._mark(resolve(p["symbol"])) or p["avg_price"])
                        for p in self.db.execute("SELECT * FROM positions WHERE qty != 0 AND symbol != ?", (inst.symbol,))
                        if not resolve(p["symbol"]).is_future)
            equity = self._equity()
            if gross + abs(new_pos) * px > equity:
                raise BrokerError(f"Not enough buying power: that position is ${abs(new_pos) * px:,.0f}, "
                                  f"available ${max(0.0, equity - gross):,.0f} (no margin in this account).")

    def _insert_order(self, symbol, side, qty, otype, limit_price, stop_price, status, parent_id, now, note) -> int:
        cur = self.db.execute(
            "INSERT INTO orders (symbol, side, qty, type, limit_price, stop_price, status, parent_id, created_at, "
            "checked_until, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, qty, otype, limit_price, stop_price, status, parent_id, _iso(now), _iso(now), note))
        return cur.lastrowid

    def _order(self, oid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        if not row:
            raise BrokerError(f"No order #{oid}.")
        return row

    def _position(self, symbol: str) -> float:
        row = self.db.execute("SELECT qty FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        return row["qty"] if row else 0.0

    def cancel_order(self, order_id: int) -> dict:
        o = self._order(order_id)
        if o["status"] not in ("working", "held"):
            raise BrokerError(f"Order #{order_id} is already {o['status']}.")
        self._cancel(order_id, "cancelled by user")
        for child in self.db.execute("SELECT id FROM orders WHERE parent_id = ? AND status IN ('working','held')", (order_id,)).fetchall():
            self._cancel(child["id"], "parent cancelled")
        self.db.commit()
        return self._order_view(self._order(order_id))

    def _cancel(self, oid: int, reason: str) -> None:
        self.db.execute("UPDATE orders SET status = 'cancelled', reason = ? WHERE id = ?", (reason, oid))

    def update_bracket(self, symbol: str, stop_loss: float | None = None, take_profit: float | None = None) -> dict:
        """Move the open stop-loss / take-profit exits for a position (e.g. stop to breakeven)."""
        inst = resolve(symbol)
        self.process()
        pos = self._position(inst.symbol)
        if pos == 0:
            raise BrokerError(f"No open {inst.symbol} position.")
        mark = self._mark(inst)
        exit_side = "sell" if pos > 0 else "buy"
        exits = self.db.execute("SELECT * FROM orders WHERE symbol = ? AND side = ? AND status = 'working' "
                                "AND parent_id IS NOT NULL", (inst.symbol, exit_side)).fetchall()
        changed = []
        for o in exits:
            if o["type"] == "stop" and stop_loss is not None:
                if mark is not None and ((pos > 0 and stop_loss >= mark) or (pos < 0 and stop_loss <= mark)):
                    raise BrokerError(f"stop_loss {stop_loss} is past the current price {mark:.2f}; close the position instead.")
                self.db.execute("UPDATE orders SET stop_price = ? WHERE id = ?", (stop_loss, o["id"]))
                changed.append(o["id"])
            if o["type"] == "limit" and take_profit is not None:
                self.db.execute("UPDATE orders SET limit_price = ? WHERE id = ?", (take_profit, o["id"]))
                changed.append(o["id"])
        if not changed:
            raise BrokerError(f"No working bracket exits to update for {inst.symbol}.")
        self.db.commit()
        return {"symbol": inst.symbol, "updated_orders": [self._order_view(self._order(i)) for i in changed]}

    def close_position(self, symbol: str, note: str = "close position") -> dict:
        inst = resolve(symbol)
        self.process()
        for o in self.db.execute("SELECT id FROM orders WHERE symbol = ? AND status IN ('working','held')", (inst.symbol,)).fetchall():
            self._cancel(o["id"], "position closed")
        self.db.commit()
        pos = self._position(inst.symbol)
        if pos == 0:
            return {"symbol": inst.symbol, "message": "No open position; any open orders for it were cancelled."}
        return self.place_order(inst.symbol, "sell" if pos > 0 else "buy", abs(pos), "market", note=note)

    def orders(self, status: str = "open") -> list[dict]:
        if status == "open":
            rows = self.db.execute("SELECT * FROM orders WHERE status IN ('working','held') ORDER BY id")
        elif status == "all":
            rows = self.db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200")
        else:
            rows = self.db.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT 200", (status,))
        return [self._order_view(o) for o in rows]

    def history(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))]

    @staticmethod
    def _order_view(o: sqlite3.Row) -> dict:
        v = {k: o[k] for k in o.keys() if o[k] is not None and k != "checked_until"}
        if o["status"] == "held":
            v["status"] = "waiting for entry fill"
        return v

    # ---------- fill engine ----------
    def process(self) -> None:
        """Check working orders against real 1-minute bars since they were last checked.

        Fills happen in time order across all orders: each pass finds every order's first
        trigger and fills only the earliest, because that fill can change what happens next
        (e.g. a take-profit cancels its stop-loss). In a tie on the same bar, stops fill
        first: within a 1-minute bar we can't know which price came first, so assume the worst.
        """
        bars_cache: dict[str, list[Bar]] = {}
        now = self.clock()
        for _ in range(500):
            candidates = []  # (bar time, stop-first rank, order id, order, price)
            scanned = {}  # order id -> last bar start checked, when nothing triggered
            for o in self.db.execute("SELECT * FROM orders WHERE status = 'working' ORDER BY id").fetchall():
                inst = resolve(o["symbol"])
                since = _parse(o["checked_until"])
                if inst.data_symbol not in bars_cache:
                    oldest = min(_parse(r["checked_until"]) for r in self.db.execute(
                        "SELECT checked_until FROM orders WHERE status IN ('working','held') AND symbol = ?", (o["symbol"],)))
                    bars_cache[inst.data_symbol] = [b for b in self.bars_fn(inst.data_symbol, oldest) if b[0] <= now]
                # Only bars that start after the order existed: the bar in progress when it was
                # placed may have hit its price before the order was there (no look-ahead fills)
                first = since.replace(second=0, microsecond=0) + timedelta(minutes=1)
                bars = [b for b in bars_cache[inst.data_symbol] if b[0] >= first]
                # Nothing fills at or after the session close: the close flattens and cancels everything
                cut = self._next_session_close(since)
                if cut is not None and cut <= now:
                    bars = [b for b in bars if b[0] < cut]
                for ts, op, hi, lo, _cl in bars:
                    price = self._trigger(o, op, hi, lo)
                    if price is not None:
                        candidates.append((ts, 0 if o["type"] == "stop" else 1, o["id"], o, price))
                        break
                else:
                    if bars:
                        scanned[o["id"]] = bars[-1][0]
            if not candidates:
                for oid, ts in scanned.items():
                    self.db.execute("UPDATE orders SET checked_until = ? WHERE id = ?", (_iso(ts), oid))
                self.db.commit()
                break
            ts, _rank, _oid, o, price = min(candidates, key=lambda c: c[:3])
            self._fill(o, price, ts)
            self.db.commit()
        self._check_intrabar_drawdown()
        self._apply_session_closes()
        self._update_risk_state()

    # ---------- session close (prop-firm flat rule) ----------
    def _close_time(self) -> tuple[int, int] | None:
        v = str(self.config.get("session_close_et") or "").strip()
        if not v:
            return None
        h, m = v.split(":")
        return int(h), int(m)

    def _next_session_close(self, after: datetime) -> datetime | None:
        """First weekday session close (New York time) strictly after `after`, in UTC."""
        hm = self._close_time()
        if hm is None:
            return None
        ny = after.astimezone(NY)
        for i in range(8):
            d = (ny + timedelta(days=i)).date()
            if d.weekday() >= 5:
                continue
            cut = datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=NY)
            if cut > ny:
                return cut.astimezone(timezone.utc)
        return None

    def _session_closed_message(self, now: datetime) -> str | None:
        hm = self._close_time()
        if hm is None:
            return None
        ny = now.astimezone(NY)
        close = ny.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        reopen = ny.replace(hour=18, minute=0, second=0, microsecond=0)
        wd = ny.weekday()
        weekend = wd == 5 or (wd == 4 and ny >= close) or (wd == 6 and ny < reopen)
        if weekend or close <= ny < reopen:
            label = close.strftime("%I:%M %p").lstrip("0")
            return (f"Market closed for this account: trading stops at {label} ET and reopens at 6:00 PM ET "
                    f"(Sunday 6:00 PM after the weekend).")
        return None

    def _apply_session_closes(self) -> None:
        a = self._acct()
        since = _parse(a["last_close_applied"] or a["created_at"])
        now = self.clock()
        cut = self._next_session_close(since)
        while cut is not None and cut <= now:
            for o in self.db.execute("SELECT id FROM orders WHERE status IN ('working','held')").fetchall():
                self._cancel(o["id"], "session close: open orders cancelled")
            for p in self.db.execute("SELECT * FROM positions WHERE qty != 0").fetchall():
                inst = resolve(p["symbol"])
                bars = [b for b in self.bars_fn(inst.data_symbol, cut) if b[0] >= cut]
                price = bars[0][1] if bars else (self._mark(inst) or p["avg_price"])
                side = "sell" if p["qty"] > 0 else "buy"
                oid = self._insert_order(inst.symbol, side, abs(p["qty"]), "market", None, None, "working", None, cut,
                                         "auto-liquidated at session close")
                self._fill(self._order(oid), price, cut)
            self.db.execute("UPDATE account SET last_close_applied = ? WHERE id = 1", (_iso(cut),))
            self.db.commit()
            cut = self._next_session_close(cut)

    def _check_intrabar_drawdown(self) -> None:
        """Catch a max-loss breach that happened between checks, using each bar's worst price."""
        a = self._acct()
        if a["breached"] or a["floor"] is None:
            return
        positions = self.db.execute("SELECT * FROM positions WHERE qty != 0").fetchall()
        if not positions:
            return
        since = max(_parse(f["ts"]) for f in self.db.execute("SELECT ts FROM fills"))
        worst_eq = a["cash"]
        for p in positions:
            inst = resolve(p["symbol"])
            bars = [b for b in self.bars_fn(inst.data_symbol, since) if b[0] > since]
            if not bars:
                return
            worst = min(b[3] for b in bars) if p["qty"] > 0 else max(b[2] for b in bars)
            worst_eq += (worst - p["avg_price"]) * p["qty"] * inst.multiplier if inst.is_future else p["qty"] * worst
        if worst_eq <= a["floor"]:
            self.db.execute("UPDATE account SET breached = ? WHERE id = 1",
                            (f"max loss limit hit intraday: equity touched about ${worst_eq:,.2f}, "
                             f"below the ${a['floor']:,.2f} floor",))
            self.db.commit()

    @staticmethod
    def _trigger(o: sqlite3.Row, op: float, hi: float, lo: float) -> float | None:
        buy = o["side"] == "buy"
        if o["type"] == "market":
            return op
        if o["type"] == "limit":
            lp = o["limit_price"]
            if buy and lo <= lp:
                return min(op, lp)
            if not buy and hi >= lp:
                return max(op, lp)
        if o["type"] == "stop":
            sp = o["stop_price"]
            if buy and hi >= sp:
                return max(op, sp)
            if not buy and lo <= sp:
                return min(op, sp)
        return None

    def _fill(self, o: sqlite3.Row, price: float, ts: datetime) -> None:
        inst = resolve(o["symbol"])
        qty = o["qty"]
        signed = qty if o["side"] == "buy" else -qty
        row = self.db.execute("SELECT * FROM positions WHERE symbol = ?", (inst.symbol,)).fetchone()
        pos, avg = (row["qty"], row["avg_price"]) if row else (0.0, 0.0)

        realized = 0.0
        if pos == 0 or (pos > 0) == (signed > 0):
            new_pos = pos + signed
            new_avg = (avg * abs(pos) + price * abs(signed)) / abs(new_pos)
        else:
            closed = min(abs(signed), abs(pos))
            realized = (price - avg) * closed * (1 if pos > 0 else -1) * inst.multiplier
            new_pos = pos + signed
            new_avg = avg if abs(signed) <= abs(pos) else price
        commission = self._commission(inst, qty)

        cash_delta = (realized if inst.is_future else -signed * price) - commission
        self.db.execute("INSERT OR REPLACE INTO positions VALUES (?, ?, ?)", (inst.symbol, new_pos, new_avg if new_pos else 0.0))
        self.db.execute("UPDATE account SET cash = cash + ?, realized_pnl = realized_pnl + ? WHERE id = 1",
                        (cash_delta, realized - commission))
        self.db.execute("UPDATE orders SET status = 'filled', filled_at = ?, fill_price = ? WHERE id = ?",
                        (_iso(ts), price, o["id"]))
        self.db.execute("INSERT INTO fills (order_id, symbol, side, qty, price, commission, realized_pnl, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (o["id"], inst.symbol, o["side"], qty, price, commission, realized, _iso(ts)))

        # Entry filled: arm its take-profit / stop-loss from this bar onward
        self.db.execute("UPDATE orders SET status = 'working', checked_until = ? WHERE parent_id = ? AND status = 'held'",
                        (_iso(ts), o["id"]))
        # An exit filled: cancel its sibling (one-cancels-other)
        if o["parent_id"]:
            self.db.execute("UPDATE orders SET status = 'cancelled', reason = 'other bracket leg filled' "
                            "WHERE parent_id = ? AND id != ? AND status IN ('working','held')", (o["parent_id"], o["id"]))

    # ---------- settings ----------
    def configure(self, **changes) -> dict:
        unknown = set(changes) - set(DEFAULT_CONFIG)
        if unknown:
            raise BrokerError(f"Unknown settings: {', '.join(sorted(unknown))}")
        self.config.update({k: v if isinstance(DEFAULT_CONFIG[k], (str, bool)) else float(v)
                            for k, v in changes.items()})
        _save_config(self.config)
        return dict(self.config)

    def apply_preset(self, name: str, reset: bool = False) -> dict:
        if name not in PRESETS:
            raise BrokerError(f"Unknown rule set '{name}'. Available: {', '.join(PRESETS)}")
        self.config.update(PRESETS[name])
        _save_config(self.config)
        if reset:
            self._reset_account(float(self.config["starting_cash"]))
            self.db.commit()
        return self.account()

    def reset(self, starting_cash: float | None = None) -> dict:
        cash = float(starting_cash or self.config["starting_cash"])
        if starting_cash:
            self.configure(starting_cash=cash)
        self._reset_account(cash)
        self.db.commit()
        return self.account()


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
