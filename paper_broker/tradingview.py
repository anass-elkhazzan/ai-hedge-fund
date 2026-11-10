"""Export the paper account to TradingView as ONE Pine Script indicator.

TradingView doesn't let outside programs place trades in its paper account, so
instead we draw ours on its charts. One indicator ("Claude Paper Trades") does it
all, because TradingView's free plan allows only 2 indicators per chart:
  - on the price chart (force_overlay): every trade's entry and exit with its time
    (ET) and result, open positions with stop/target, a results table, a trade log,
    and an optional session VWAP with ±1 standard-deviation bands (so it can replace
    a separate VWAP indicator)
  - in its own panel: the account balance after every fill, with the start,
    the profit target and the max-loss floor
Paste the file into TradingView's Pine Editor and click "Add to chart".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from paper_broker.broker import DEFAULT_DB, HERE, resolve

OUT_DIR = HERE.parent / "tradingview"
SCRIPT_NAME = "Claude Paper Trades.pine"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _et(iso: str, fmt: str = "%a %d %b %I:%M %p") -> str:
    """Trade time in New York time, as people read it on the chart."""
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(iso).astimezone(ZoneInfo("America/New_York")).strftime(fmt).replace(" 0", " ").lstrip("0")


def round_trips(db: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Pair fills into closed trades (FIFO per symbol). Returns (closed, open legs)."""
    open_legs: dict[str, list[dict]] = {}
    closed = []
    for f in db.execute("SELECT * FROM fills ORDER BY id"):
        sym = f["symbol"]
        signed = f["qty"] if f["side"] == "buy" else -f["qty"]
        legs = open_legs.setdefault(sym, [])
        remaining = signed
        while remaining and legs and (legs[0]["qty"] > 0) != (remaining > 0):
            leg = legs[0]
            take = min(abs(remaining), abs(leg["qty"]))
            mult = resolve(sym).multiplier
            direction = 1 if leg["qty"] > 0 else -1
            closed.append({
                "symbol": sym, "side": "long" if direction > 0 else "short", "qty": take,
                "t_in": leg["ts"], "p_in": leg["price"], "t_out": f["ts"], "p_out": f["price"],
                "pnl": (f["price"] - leg["price"]) * take * direction * mult,
            })
            leg["qty"] -= take * direction
            remaining += take * direction
            if leg["qty"] == 0:
                legs.pop(0)
        if remaining:
            legs.append({"qty": remaining, "price": f["price"], "ts": f["ts"]})
    still_open = [{"symbol": s, **leg} for s, legs in open_legs.items() for leg in legs]
    return closed, still_open


def _arr(values, fmt) -> str:
    return "array.from(" + ", ".join(fmt(v) for v in values) + ")"


def _num(v: float) -> str:
    # always a float literal: Pine's array.from() won't mix ints and floats
    text = f"{float(v):.4f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _str(v: str) -> str:
    return '"' + v.replace('"', "'") + '"'


def build_script(db_path: Path | str = DEFAULT_DB, title_suffix: str = "") -> str:
    db = sqlite3.connect(str(db_path), timeout=30)
    db.row_factory = sqlite3.Row
    acct = db.execute("SELECT * FROM account WHERE id = 1").fetchone()
    closed, open_legs = round_trips(db)
    exits = {o["symbol"]: o for o in db.execute(
        "SELECT symbol, MAX(CASE WHEN type='stop' THEN stop_price END) AS stop, "
        "MAX(CASE WHEN type='limit' THEN limit_price END) AS target FROM orders "
        "WHERE status = 'working' AND parent_id IS NOT NULL GROUP BY symbol")}
    fills = db.execute("SELECT ts, realized_pnl, commission FROM fills ORDER BY id").fetchall()
    db.close()

    from paper_broker.broker import _load_config, DEFAULT_CONFIG
    cfg = {**DEFAULT_CONFIG, **_load_config()}
    start = acct["starting_cash"]
    balance = start + acct["realized_pnl"]
    wins = [t for t in closed if t["pnl"] > 0]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rule_set = cfg["rule_set"]
    target = cfg["profit_target"]
    floor = acct["floor"]

    L = [
        "//@version=6",
        f'indicator("Claude Paper Trades{title_suffix}", overlay = false, precision = 0, max_labels_count = 500, max_lines_count = 500)',
        f"// Generated {stamp} from the paper account ({rule_set}). Simulated money only.",
        "// Trades for the symbol on this chart (MNQ, MES...) are drawn on the price chart; the panel shows the balance.",
        "",
        "showVwap = input.bool(true, \"Show session VWAP with ±1σ bands\")",
        "[vw, vwUp, vwLo] = ta.vwap(hlc3, timeframe.change(\"1D\"), 1.0)",
        "plot(showVwap ? vw : na, \"VWAP\", color = color.new(color.blue, 0), force_overlay = true)",
        "plot(showVwap ? vwUp : na, \"VWAP +1σ\", color = color.new(color.green, 30), force_overlay = true)",
        "plot(showVwap ? vwLo : na, \"VWAP -1σ\", color = color.new(color.green, 30), force_overlay = true)",
        "",
    ]

    # ---- balance curve in the indicator's own panel ----
    pts_t = [_ms(acct["created_at"])] + [_ms(f["ts"]) for f in fills]
    pts_v = [start]
    for f in fills:
        pts_v.append(pts_v[-1] + f["realized_pnl"] - f["commission"])
    L += [
        f"var pts_t = {_arr(pts_t, str)}",
        f"var pts_v = {_arr(pts_v, _num)}",
        "var int idx = 0",
        "while idx + 1 < array.size(pts_t) and array.get(pts_t, idx + 1) <= time_close",
        "    idx += 1",
        "equity = time_close >= array.get(pts_t, 0) ? array.get(pts_v, idx) : na",
        "plot(equity, \"Balance\", color = color.teal, linewidth = 2, style = plot.style_stepline)",
        f"hline({_num(start)}, \"Start\", color = color.gray, linestyle = hline.style_dotted)",
    ]
    if target:
        L.append(f"hline({_num(start + target)}, \"Profit target\", color = color.green, linestyle = hline.style_dashed)")
    if floor is not None:
        L.append(f"hline({_num(floor)}, \"Max-loss floor (now)\", color = color.red, linestyle = hline.style_dashed)")
    L.append("")

    # ---- closed trades on the price chart ----
    if closed:
        L += [
            f"var sym_c = {_arr([t['symbol'] for t in closed], _str)}",
            f"var side_c = {_arr([t['side'] for t in closed], _str)}",
            f"var qty_c = {_arr([t['qty'] for t in closed], _num)}",
            f"var tin_c = {_arr([_ms(t['t_in']) for t in closed], str)}",
            f"var pin_c = {_arr([t['p_in'] for t in closed], _num)}",
            f"var tout_c = {_arr([_ms(t['t_out']) for t in closed], str)}",
            f"var pout_c = {_arr([t['p_out'] for t in closed], _num)}",
            f"var pnl_c = {_arr([t['pnl'] for t in closed], _num)}",
            f"var when_in = {_arr([_et(t['t_in'], '%I:%M %p ET') for t in closed], _str)}",
            f"var when_out = {_arr([_et(t['t_out'], '%I:%M %p ET') for t in closed], _str)}",
            "",
            "if barstate.islast",
            "    for i = 0 to array.size(sym_c) - 1",
            "        if array.get(sym_c, i) == syminfo.root",
            "            pnl = array.get(pnl_c, i)",
            "            longTrade = array.get(side_c, i) == \"long\"",
            "            col = pnl >= 0 ? color.new(color.green, 0) : color.new(color.red, 0)",
            "            line.new(array.get(tin_c, i), array.get(pin_c, i), array.get(tout_c, i), array.get(pout_c, i), xloc = xloc.bar_time, color = col, width = 2, style = line.style_dashed, force_overlay = true)",
            "            label.new(array.get(tin_c, i), array.get(pin_c, i), (longTrade ? \"BUY \" : \"SELL \") + str.tostring(array.get(qty_c, i)) + \" @ \" + str.tostring(array.get(pin_c, i)) + \"\\n\" + array.get(when_in, i), xloc = xloc.bar_time, style = longTrade ? label.style_label_up : label.style_label_down, color = longTrade ? color.new(color.blue, 0) : color.new(color.orange, 0), textcolor = color.white, size = size.small, force_overlay = true)",
            "            label.new(array.get(tout_c, i), array.get(pout_c, i), (pnl >= 0 ? \"+$\" : \"-$\") + str.tostring(math.abs(pnl), \"#,##0\") + \"\\n\" + array.get(when_out, i), xloc = xloc.bar_time, style = longTrade ? label.style_label_down : label.style_label_up, color = col, textcolor = color.white, size = size.small, force_overlay = true)",
            "",
        ]

    # ---- open positions with their stop and target ----
    if open_legs:
        L += [
            f"var sym_o = {_arr([o['symbol'] for o in open_legs], _str)}",
            f"var qty_o = {_arr([o['qty'] for o in open_legs], _num)}",
            f"var tin_o = {_arr([_ms(o['ts']) for o in open_legs], str)}",
            f"var pin_o = {_arr([o['price'] for o in open_legs], _num)}",
            f"var stop_o = {_arr([(exits.get(o['symbol']) or {}).get('stop') or 0 for o in open_legs], _num)}",
            f"var tgt_o = {_arr([(exits.get(o['symbol']) or {}).get('target') or 0 for o in open_legs], _num)}",
            f"var when_o = {_arr([_et(o['ts'], '%I:%M %p ET') for o in open_legs], _str)}",
            "",
            "if barstate.islast",
            "    for i = 0 to array.size(sym_o) - 1",
            "        if array.get(sym_o, i) == syminfo.root",
            "            t0 = array.get(tin_o, i)",
            "            label.new(t0, array.get(pin_o, i), \"OPEN \" + (array.get(qty_o, i) > 0 ? \"LONG \" : \"SHORT \") + str.tostring(math.abs(array.get(qty_o, i))) + \" @ \" + str.tostring(array.get(pin_o, i)) + \"\\n\" + array.get(when_o, i), xloc = xloc.bar_time, color = color.new(color.purple, 0), textcolor = color.white, size = size.small, force_overlay = true)",
            "            line.new(t0, array.get(pin_o, i), time, array.get(pin_o, i), xloc = xloc.bar_time, extend = extend.right, color = color.new(color.purple, 0), force_overlay = true)",
            "            if array.get(stop_o, i) > 0",
            "                line.new(t0, array.get(stop_o, i), time, array.get(stop_o, i), xloc = xloc.bar_time, extend = extend.right, color = color.new(color.red, 0), style = line.style_dotted, force_overlay = true)",
            "            if array.get(tgt_o, i) > 0",
            "                line.new(t0, array.get(tgt_o, i), time, array.get(tgt_o, i), xloc = xloc.bar_time, extend = extend.right, color = color.new(color.green, 0), style = line.style_dotted, force_overlay = true)",
            "",
        ]

    # ---- results table (top right of the price chart) ----
    rows = [
        ("Account", rule_set),
        ("Balance", f"${balance:,.2f}"),
        ("Total P&L", f"{'+' if balance >= start else '-'}${abs(balance - start):,.2f}"),
        ("Profit target", f"${target:,.0f} ({max(0.0, balance - start) / target:.0%} done)" if target else "none"),
        ("Max-loss floor", f"${floor:,.2f} (room ${balance - floor:,.2f})" if floor is not None else "none"),
        ("Closed trades", f"{len(closed)} ({len(wins)} wins)" + (f", win rate {len(wins) / len(closed):.0%}" if closed else "")),
        ("Status", acct["breached"] or "trading allowed"),
        ("Updated", stamp),
    ]
    L += [
        f"var tbl = table.new(position.top_right, 2, {len(rows)}, bgcolor = color.new(color.black, 15), border_width = 1, force_overlay = true)",
        "if barstate.islast",
    ]
    for r, (k, v) in enumerate(rows):
        L.append(f"    table.cell(tbl, 0, {r}, {_str(k)}, text_color = color.silver, text_size = size.small, text_halign = text.align_left)")
        L.append(f"    table.cell(tbl, 1, {r}, {_str(v)}, text_color = color.white, text_size = size.small, text_halign = text.align_left)")

    # ---- trade log: the 12 most recent closed trades, newest first ----
    recent = closed[-12:][::-1]
    if recent:
        header = ["Opened (ET)", "Symbol", "Side", "Qty", "Entry", "Exit", "Closed (ET)", "P&L"]
        L += [
            "",
            f"var log = table.new(position.bottom_right, {len(header)}, {len(recent) + 1}, bgcolor = color.new(color.black, 15), border_width = 1, force_overlay = true)",
            "if barstate.islast",
        ]
        for c, h in enumerate(header):
            L.append(f"    table.cell(log, {c}, 0, {_str(h)}, text_color = color.silver, text_size = size.tiny)")
        for r, t in enumerate(recent, start=1):
            cells = [_et(t["t_in"]), t["symbol"], t["side"], f"{t['qty']:g}", f"{t['p_in']:,.2f}",
                     f"{t['p_out']:,.2f}", _et(t["t_out"], "%I:%M %p"),
                     f"{'+' if t['pnl'] >= 0 else '-'}${abs(t['pnl']):,.0f}"]
            for c, v in enumerate(cells):
                color = ("color.lime" if t["pnl"] >= 0 else "color.red") if c == len(cells) - 1 else "color.white"
                L.append(f"    table.cell(log, {c}, {r}, {_str(v)}, text_color = {color}, text_size = size.tiny)")
    return "\n".join(L) + "\n"


def export(db_path: Path | str = DEFAULT_DB, out_dir: Path | str = OUT_DIR, title_suffix: str = "") -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in ("Claude Paper Equity.pine",):  # merged into the one script
        (out / old).unlink(missing_ok=True)
    path = out / SCRIPT_NAME
    path.write_text(build_script(db_path, title_suffix), encoding="utf-8")
    return [str(path)]


if __name__ == "__main__":
    for p in export():
        print(p)
