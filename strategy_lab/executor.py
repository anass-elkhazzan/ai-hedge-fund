"""Execution desk: runs the VWAP-reversion strategy live on the paper account (no AI in the loop).

Runs 9:45-15:56 ET on trading days (Windows task "HedgeFund Executor"). Every ~20 s it:
  1. reads today's MNQ 5-minute bars (≈10 min delayed) → session VWAP and σ
  2. reads the near-live MNQ price P (delayed futures × QQQ move; ~1 pt error)
  3. at each 5-minute boundary computes z = (P − VWAP)/σ and looks for the signal:
        short when z_prev ≥ k and z < k ;  long when z_prev ≤ −k and z > −k
  4. checks the investment committee's plan for today (AI agents, strategy_lab/plans/<date>.json),
     the house rules and the account, sizes the trade and places a bracket order:
        stop   = session extreme ± buf·σ          target = VWAP
        contracts = floor( risk$ × size_multiplier / (|P − stop| × $2) ), max 15
  5. at 15:55 closes anything still open.
Tested settings (strategy lab, 23 Jul–30 Sep 2026): k = 1.5, buf = 0.5, risk $300 → see Obsidian "Strategy lab".
Run by hand: python -m strategy_lab.executor [--dry-run]
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from ai_hedge.data.live_estimate import live_estimate
from paper_broker import market_data
from paper_broker.broker import BrokerError, PaperBroker
from strategy_lab.indicators import session_vwap

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
PLANS = ROOT / "plans"
LOGS = ROOT.parent / "logs"

CONFIG = {
    "symbol": "MNQ", "data_symbol": "MNQ=F", "dollars_per_point": 2.0,
    "k": 1.5, "buf": 0.5, "risk": 300.0, "max_contracts": 15, "max_trades": 2,
    "first_signal": "10:00", "last_entry": "15:00", "flatten": "15:55", "stop_running": "15:56",
    "daily_stop": -500.0, "daily_profit_lock": 1200.0,
}
DEFAULT_PLAN = {"trade": True, "size_multiplier": 0.5, "direction": "both", "blackouts": [],
                "reason": "No committee plan found for today: trading at half size by default."}


def log(msg: str, day: str) -> None:
    LOGS.mkdir(exist_ok=True)
    line = f"{datetime.now(NY).strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOGS / f"executor_{day}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_plan(day: str) -> dict:
    p = PLANS / f"{day}.json"
    try:
        return {**DEFAULT_PLAN, **json.loads(p.read_text(encoding="utf-8")), "_source": str(p)}
    except (OSError, ValueError):
        return {**DEFAULT_PLAN, "_source": "default"}


def in_blackout(plan: dict, hhmm: str) -> str | None:
    for w in plan.get("blackouts", []):
        if w.get("from", "99:99") <= hhmm <= w.get("to", "00:00"):
            return w.get("why", "news window")
    return None


def todays_bars():
    df = yf.Ticker(CONFIG["data_symbol"]).history(period="1d", interval="5m")
    if df is None or df.empty:
        return None
    df.index = df.index.tz_convert(NY)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].between_time("09:30", "15:55")
    return df if len(df) else None


def main(dry_run: bool = False) -> None:
    now = datetime.now(NY)
    day = now.date().isoformat()
    if now.weekday() >= 5:
        log("weekend: nothing to do", day)
        return
    broker = PaperBroker(market_data.quote, market_data.bars_since)
    log(f"executor started (dry_run={dry_run}) config={json.dumps(CONFIG)}", day)
    z_prev, last_boundary, trades_today, flattened = None, None, 0, False
    hi_since, lo_since = None, None
    while True:
        now = datetime.now(NY)
        hhmm = now.strftime("%H:%M")
        if hhmm >= CONFIG["stop_running"]:
            break
        if hhmm >= CONFIG["flatten"] and not flattened:
            if not dry_run:
                try:
                    acct = broker.account()
                    if any(p["symbol"] == CONFIG["symbol"] for p in acct["positions"]):
                        broker.close_position(CONFIG["symbol"], note="executor: end-of-day flat")
                        log("15:55 flatten: position closed", day)
                except BrokerError as e:
                    log(f"flatten error: {e}", day)
            flattened = True
        if hhmm < "09:45":
            time.sleep(20)
            continue
        try:
            est = live_estimate(CONFIG["data_symbol"])
            bars = todays_bars()
        except Exception as e:  # network hiccup: try again shortly
            log(f"data error: {e}", day)
            time.sleep(20)
            continue
        if not est or bars is None or len(bars) < 3:
            time.sleep(20)
            continue
        p = est["price"]
        hi_since = p if hi_since is None else max(hi_since, p)
        lo_since = p if lo_since is None else min(lo_since, p)
        boundary = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        if boundary == last_boundary:
            time.sleep(20)
            continue
        last_boundary = boundary
        v = session_vwap(bars)
        vwap, sd = float(v["vwap"].iloc[-1]), float(v["vwap_sd"].iloc[-1])
        if sd <= 0:
            continue
        z = (p - vwap) / sd
        session_hi = max(float(bars["high"].max()), hi_since)
        session_lo = min(float(bars["low"].min()), lo_since)
        log(f"P={p:.2f} VWAP={vwap:.2f} sd={sd:.2f} z={z:+.2f} (z_prev={'n/a' if z_prev is None else f'{z_prev:+.2f}'})", day)
        signal = None
        k = CONFIG["k"]
        if z_prev is not None and CONFIG["first_signal"] <= hhmm <= CONFIG["last_entry"]:
            if z_prev >= k and z < k:
                signal = ("sell", session_hi + CONFIG["buf"] * sd)
            elif z_prev <= -k and z > -k:
                signal = ("buy", session_lo - CONFIG["buf"] * sd)
        z_prev = z
        if not signal:
            continue
        side, stop = signal
        plan = load_plan(day)
        why_not = None
        if not plan.get("trade", True):
            why_not = f"committee says no trading today ({plan.get('reason', '')})"
        elif plan.get("direction") == "long_only" and side == "sell" or plan.get("direction") == "short_only" and side == "buy":
            why_not = f"committee allows {plan['direction']} only"
        elif in_blackout(plan, hhmm):
            why_not = f"blackout: {in_blackout(plan, hhmm)}"
        elif trades_today >= CONFIG["max_trades"]:
            why_not = "max trades for today reached"
        acct = broker.account()
        if not why_not:
            if acct["trading_locked"]:
                why_not = f"account locked: {acct['trading_locked']}"
            elif any(pp["symbol"] == CONFIG["symbol"] for pp in acct["positions"]) or acct["open_orders"]:
                why_not = "already in a position"
            elif acct["today_pnl"] <= CONFIG["daily_stop"] or acct["today_pnl"] >= CONFIG["daily_profit_lock"]:
                why_not = f"daily limit reached (today ${acct['today_pnl']:.0f})"
        risk_pts = abs(p - stop)
        n = math.floor(CONFIG["risk"] * float(plan.get("size_multiplier", 1.0)) / (risk_pts * CONFIG["dollars_per_point"]))
        n = min(n, CONFIG["max_contracts"])
        if not why_not and n <= 0:
            why_not = f"stop too far ({risk_pts:.1f} pts) for the risk budget"
        desc = f"SIGNAL {side.upper()} z {z:+.2f} entry~{p:.2f} stop {stop:.2f} target VWAP {vwap:.2f} size {n}"
        if why_not:
            log(f"{desc} -> SKIPPED: {why_not}", day)
            continue
        if dry_run:
            log(f"{desc} -> DRY RUN (no order)", day)
            trades_today += 1
            continue
        try:
            o = broker.place_order(CONFIG["symbol"], side, n, "market", take_profit=round(vwap * 4) / 4,
                                   stop_loss=round(stop * 4) / 4,
                                   note=f"VWAP reversion z={z:+.2f}; plan x{plan.get('size_multiplier')}; {plan.get('reason', '')[:80]}")
            trades_today += 1
            log(f"{desc} -> ORDER {o['status']} fill {o.get('fill_price')}", day)
        except BrokerError as e:
            log(f"{desc} -> REJECTED: {e}", day)
    log("executor stopped for the day", day)


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
