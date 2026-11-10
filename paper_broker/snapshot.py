"""Read-only view of the paper account for the analysis pipeline (no network calls).

Returns positions/orders in the same shape as tracker.db's helpers, so
ai_hedge.runner.aggregate can feed them to the Portfolio Manager.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_broker.broker import DEFAULT_DB, resolve


def available(db_path: Path | str = DEFAULT_DB) -> bool:
    return Path(db_path).exists()


def portfolio(db_path: Path | str = DEFAULT_DB, recent_days: int = 7) -> dict:
    db = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        acct = db.execute("SELECT * FROM account WHERE id = 1").fetchone()
        positions = []
        for p in db.execute("SELECT * FROM positions WHERE qty != 0"):
            inst = resolve(p["symbol"])
            positions.append({
                "ticker": p["symbol"], "direction": "long" if p["qty"] > 0 else "short",
                "quantity": abs(p["qty"]), "entry_fill_price": p["avg_price"],
                "is_future": inst.is_future, "contract_multiplier": inst.multiplier,
                # Futures tie up margin, not their notional value
                "exposure_usd": 0.0 if inst.is_future else abs(p["qty"]) * p["avg_price"],
            })
        pending = [{
            "ticker": o["symbol"], "direction": "long" if o["side"] == "buy" else "short",
            "quantity": o["qty"], "entry_price": o["limit_price"] or o["stop_price"] or 0.0,
            "order_type": o["type"], "order_id": o["id"],
        } for o in db.execute("SELECT * FROM orders WHERE status = 'working' AND parent_id IS NULL")
            if not resolve(o["symbol"]).is_future]
        since = (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat()
        recent = [dict(f) for f in db.execute(
            "SELECT symbol AS ticker, side, qty AS quantity, price, realized_pnl AS pnl, ts AS closed_at "
            "FROM fills WHERE realized_pnl != 0 AND ts >= ? ORDER BY id DESC", (since,))]
        return {
            "account_value": acct["starting_cash"] + acct["realized_pnl"],
            "trading_locked": acct["breached"],
            "open_positions": positions, "pending_orders": pending, "recent_closed": recent,
        }
    finally:
        db.close()
