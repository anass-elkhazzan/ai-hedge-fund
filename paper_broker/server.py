"""MCP server exposing the paper trading account to Claude.

Run: python paper_broker/server.py   (stdio transport; registered in .mcp.json)
Simulated money only — no connection to any real broker.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import MCPServer  # noqa: E402

from paper_broker import market_data  # noqa: E402
from paper_broker.broker import BrokerError, PaperBroker, resolve  # noqa: E402

mcp = MCPServer(
    name="paper-broker",
    instructions=(
        "Simulated paper-trading account (no real money) with live yfinance prices. "
        "Trades stocks/ETFs (AAPL, QQQ, SPY) and CME index futures (MNQ, NQ, MES, ES, MYM, YM, M2K, RTY). "
        "Indexes like ^NDX can't be traded: use futures or the ETF. Always attach stop_loss to new positions. "
        "Prop-firm style rules apply: daily loss limit and trailing max drawdown lock new entries when hit."
    ),
)
_broker: PaperBroker | None = None


def broker() -> PaperBroker:
    global _broker
    if _broker is None:
        _broker = PaperBroker(market_data.quote, market_data.bars_since)
    return _broker


def _safe(fn):
    try:
        return fn()
    except BrokerError as e:
        return {"error": str(e)}


@mcp.tool()
def get_quote(symbol: str) -> dict:
    """Latest price for a stock/ETF (AAPL, QQQ), future (MNQ, NQ, MES, ES) or index (^NDX, quote only)."""
    def run():
        if symbol.strip().startswith("^"):
            q = market_data.quote(symbol.strip().upper())
            if not q:
                raise BrokerError(f"No price data for {symbol}.")
            return {"symbol": symbol.upper(), "price": round(q[0], 2), "as_of_utc": q[1].isoformat(timespec="seconds"),
                    "tradable": False, "note": "Indexes are quote-only; trade the matching future or ETF."}
        from ai_hedge.data.live_estimate import live_estimate
        inst = resolve(symbol)
        est = live_estimate(inst.data_symbol) if inst.is_future else None
        q = (est["price"], est["as_of"]) if est else market_data.delayed_quote(inst.data_symbol)
        if not q:
            raise BrokerError(f"No price data for {inst.symbol}.")
        out = {"symbol": inst.symbol, "price": round(q[0], 4), "as_of_utc": q[1].isoformat(timespec="seconds"),
               "contract_multiplier": inst.multiplier, "is_future": inst.is_future,
               "chart": f"https://www.tradingview.com/chart/?symbol={inst.tradingview}"}
        if est:
            out["price_source"] = (f"near-live estimate ({est['method']}); typical error about 1 MNQ point. "
                                   f"Last delayed futures print {est['futures_delayed_price']} at "
                                   f"{est['futures_delayed_time'].isoformat(timespec='seconds')}")
        elif inst.is_future:
            out["price_source"] = "delayed futures price (about 10 minutes old; no live ETF reference outside 9:30-16:00 ET)"
        return out
    return _safe(run)


@mcp.tool()
def get_account() -> dict:
    """Account summary: equity, cash, P&L (total, today, open), positions, open orders, risk-rule status."""
    return _safe(lambda: broker().account())


@mcp.tool()
def place_order(symbol: str, side: str, quantity: float, order_type: str = "market",
                limit_price: float | None = None, stop_price: float | None = None,
                take_profit: float | None = None, stop_loss: float | None = None, note: str = "") -> dict:
    """Place a paper order.

    side: "buy" or "sell" (sell with no position opens a short).
    order_type: "market", "limit" (needs limit_price) or "stop" (needs stop_price).
    quantity: shares for stocks/ETFs, whole contracts for futures.
    take_profit / stop_loss: optional bracket exits, activated once the entry fills (one cancels the other).
    note: the reason for the trade (e.g. the run id and thesis) — kept in the journal.
    """
    return _safe(lambda: broker().place_order(symbol, side, quantity, order_type, limit_price, stop_price,
                                              take_profit, stop_loss, note))


@mcp.tool()
def cancel_order(order_id: int) -> dict:
    """Cancel a working order (and its bracket exits)."""
    return _safe(lambda: broker().cancel_order(order_id))


@mcp.tool()
def update_bracket(symbol: str, stop_loss: float | None = None, take_profit: float | None = None) -> dict:
    """Move an open position's stop-loss and/or take-profit (e.g. stop to breakeven once in profit)."""
    return _safe(lambda: broker().update_bracket(symbol, stop_loss, take_profit))


@mcp.tool()
def close_position(symbol: str) -> dict:
    """Close the whole position in a symbol at market and cancel its open orders."""
    return _safe(lambda: broker().close_position(symbol))


@mcp.tool()
def list_orders(status: str = "open") -> list[dict] | dict:
    """Orders by status: "open" (default), "filled", "cancelled" or "all"."""
    return _safe(lambda: (broker().process(), broker().orders(status))[1])


@mcp.tool()
def trade_history(limit: int = 50) -> list[dict] | dict:
    """Most recent fills (newest first) with price, commission and realized P&L."""
    return _safe(lambda: (broker().process(), broker().history(limit))[1])


@mcp.tool()
def configure_rules(daily_loss_limit: float | None = None, max_drawdown: float | None = None,
                    max_contracts_per_symbol: float | None = None) -> dict:
    """Change the prop-firm style risk rules (use 0 to disable one). Match these to your prop firm's rules."""
    changes = {k: v for k, v in {"daily_loss_limit": daily_loss_limit, "max_drawdown": max_drawdown,
                                 "max_contracts_per_symbol": max_contracts_per_symbol}.items() if v is not None}
    return _safe(lambda: broker().configure(**changes))


@mcp.tool()
def export_tradingview() -> dict:
    """Write the TradingView Pine script "Claude Paper Trades" (trades with times, trade log,
    results table, balance panel, VWAP) from the account. The user pastes it into the saved
    script in TradingView's Pine Editor (desktop shortcut "Paper Trades on TradingView")."""
    from paper_broker import tradingview
    def run():
        broker().process()
        return {"files": tradingview.export(),
                "how_to_view": "Desktop shortcut 'Paper Trades on TradingView', or: TradingView > Pine Editor > "
                               "'Claude Paper Trades' > Ctrl+A, Ctrl+V, Ctrl+S. Use an MNQ1! or MES1! chart."}
    return _safe(run)


@mcp.tool()
def apply_rules_preset(name: str, reset_account: bool = False) -> dict:
    """Load a prop-firm rule set, e.g. "mff_rapid_50k" (MyFundedFutures Rapid 50K evaluation).
    reset_account=true also wipes history and starts fresh — only when the user asked for that."""
    return _safe(lambda: broker().apply_preset(name, reset_account))


@mcp.tool()
def reset_account(confirm: bool = False, starting_cash: float | None = None) -> dict:
    """Wipe all positions, orders and history and start over. Only with confirm=true, after the user asks for it."""
    if not confirm:
        return {"error": "Not reset. This erases the whole paper account history; call again with confirm=true "
                         "only if the user explicitly asked for a reset."}
    return _safe(lambda: broker().reset(starting_cash))


if __name__ == "__main__":
    mcp.run("stdio")
