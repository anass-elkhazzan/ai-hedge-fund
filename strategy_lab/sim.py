"""Trade simulation, position sizing and prop-firm evaluation math.

Fills (per trade, on 5-minute bars, no look-ahead):
    entry  = O_{i+1} + s·side           (next bar's open plus slippage s = 1 tick against us)
    risk   R = |entry − stop| (points);  target = entry + side·m·R when set from a reward multiple
    each later bar: stop hit if L ≤ stop (long) / H ≥ stop (short); target hit if H ≥ target / L ≤ target
    both in one bar → assume the stop (worst case);  still open at 15:55 → exit at that bar's close
Sizing (fixed dollar risk):
    contracts = floor( risk$ / (R · $/pt) ),  capped;  skip the trade if that is 0
Trade P&L in dollars:
    P&L = side·(exit − entry)·contracts·$/pt − 2·commission·contracts
Worst point of a trade (for the intraday max-loss check):
    MAE = side·(worst price − entry)·contracts·$/pt   (≤ 0)

Evaluation (MyFundedFutures Rapid 50K):
    balance B_d after day d;  high-water mark of end-of-day balances H_d = max(H_{d−1}, B_d)
    floor F_d = min(H_d − 2000, 50 100)   (trails at end of day, stops at 50 100)
    FAIL if equity ≤ F at any point;  PASS when B − 50 000 ≥ 3 000, best day ≤ 50% of total
    profit (consistency) and ≥ 2 trading days.
Statistics:
    win rate p = wins / trades;  expectancy E = p·W̄ − (1−p)·L̄  ($ per trade)
    profit factor PF = Σ wins / |Σ losses|
    daily Sharpe (annualised) = mean(daily P&L) / sd(daily P&L) · √252
    Monte Carlo pass probability: resample whole days with replacement, run the evaluation
    N times, P(pass) = passes / N
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START = 50_000.0
TARGET = 3_000.0
MAX_LOSS = 2_000.0
LOCK = 50_100.0


@dataclass
class Costs:
    dollars_per_point: float
    tick: float
    commission: float


@dataclass
class HouseRules:
    risk_per_trade: float = 200.0
    max_contracts: int = 15
    daily_stop: float = -500.0
    daily_profit_lock: float = 1_200.0
    max_trades_per_day: int = 2


def run_trade(day: pd.DataFrame, i: int, side: int, stop: float, target: float | None, rr: float | None,
              costs: Costs) -> dict | None:
    """Simulate one setup signalled at bar i. Returns points-based result or None if no entry."""
    if i + 1 >= len(day):
        return None
    entry = float(day["open"].iloc[i + 1]) + side * costs.tick
    risk = (entry - stop) * side
    if risk <= costs.tick:  # stop already crossed or absurdly tight
        return None
    if target is None:
        target = entry + side * rr * risk
    elif (target - entry) * side <= 0:  # target already reached by the entry price
        return None
    worst = entry
    for j in range(i + 1, len(day)):
        h, l = float(day["high"].iloc[j]), float(day["low"].iloc[j])
        worst = min(worst, l) if side == 1 else max(worst, h)
        stop_hit = l <= stop if side == 1 else h >= stop
        tgt_hit = h >= target if side == 1 else l <= target
        if stop_hit:
            exit_px, why = stop - side * costs.tick, "stop"  # stop fills one tick worse
            worst = min(worst, exit_px) if side == 1 else max(worst, exit_px)
            break
        if tgt_hit:
            exit_px, why = target, "target"
            break
    else:
        exit_px, why, j = float(day["close"].iloc[-1]) - side * costs.tick, "close", len(day) - 1
    return {"i_entry": i + 1, "i_exit": j, "side": side, "entry": entry, "stop": stop, "target": target,
            "exit": exit_px, "why": why, "risk_pts": risk, "pts": (exit_px - entry) * side,
            "mae_pts": (worst - entry) * side}


def simulate_day(day: pd.DataFrame, setups: list[tuple], costs: Costs, rules: HouseRules) -> dict:
    """Apply house rules to one session's setups (one position at a time)."""
    trades, pnl, worst_equity, busy_until = [], 0.0, 0.0, -1
    for (i, side, stop, target, rr) in setups:
        if len(trades) >= rules.max_trades_per_day or pnl <= rules.daily_stop or pnl >= rules.daily_profit_lock:
            break
        if i < busy_until:
            continue
        t = run_trade(day, i, side, stop, target, rr, costs)
        if not t:
            continue
        n = min(rules.max_contracts, math.floor(rules.risk_per_trade / (t["risk_pts"] * costs.dollars_per_point)))
        if n <= 0:
            continue
        fees = 2 * costs.commission * n
        t.update(contracts=n, pnl=t["pts"] * n * costs.dollars_per_point - fees,
                 mae=t["mae_pts"] * n * costs.dollars_per_point - fees / 2,
                 entry_time=str(day.index[t["i_entry"]]), exit_time=str(day.index[t["i_exit"]]))
        worst_equity = min(worst_equity, pnl + t["mae"])
        pnl += t["pnl"]
        worst_equity = min(worst_equity, pnl)
        busy_until = t["i_exit"] + 1
        trades.append(t)
    return {"date": str(day.index[0].date()), "pnl": pnl, "worst": worst_equity, "trades": trades}


def run_evaluation(days: list[dict], max_days: int | None = None) -> dict:
    """Play one MyFundedFutures evaluation through a sequence of simulated days."""
    bal, high, floor = START, START, START - MAX_LOSS
    daily, traded = [], 0
    for n, d in enumerate(days[:max_days] if max_days else days, start=1):
        if bal + d["worst"] <= floor:
            return {"result": "fail", "days": n, "balance": floor}
        bal += d["pnl"]
        if d["trades"]:
            traded += 1
            daily.append(d["pnl"])
        high = max(high, bal)
        floor = min(high - MAX_LOSS, LOCK)
        profit = bal - START
        if profit >= TARGET and traded >= 2 and max(daily) <= 0.5 * profit:
            return {"result": "pass", "days": n, "balance": bal}
    return {"result": "unfinished", "days": len(days), "balance": bal}


def rolling_evaluations(days: list[dict]) -> dict:
    """Start an evaluation on every day and play it forward on the real sequence."""
    res = [run_evaluation(days[s:]) for s in range(len(days))]
    done = [r for r in res if r["result"] != "unfinished"]
    passes = [r for r in done if r["result"] == "pass"]
    return {"starts": len(res), "finished": len(done), "passed": len(passes),
            "failed": len(done) - len(passes),
            "pass_rate_finished": len(passes) / len(done) if done else None,
            "median_days_to_pass": float(np.median([r["days"] for r in passes])) if passes else None}


def monte_carlo(days: list[dict], runs: int = 5_000, horizon: int = 60, seed: int = 7) -> dict:
    """Resample whole days (keeps each day's intraday worst point) and replay the evaluation."""
    if not days:
        return {}
    rng = random.Random(seed)
    out = {"pass": 0, "fail": 0, "unfinished": 0}
    pass_days = []
    for _ in range(runs):
        r = run_evaluation([rng.choice(days) for _ in range(horizon)])
        out[r["result"]] += 1
        if r["result"] == "pass":
            pass_days.append(r["days"])
    return {"p_pass": out["pass"] / runs, "p_fail": out["fail"] / runs, "p_unfinished": out["unfinished"] / runs,
            "median_days_to_pass": float(np.median(pass_days)) if pass_days else None, "runs": runs}


def stats(days: list[dict]) -> dict:
    trades = [t for d in days for t in d["trades"]]
    pnl = np.array([t["pnl"] for t in trades]) if trades else np.array([])
    daily = np.array([d["pnl"] for d in days])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    eq = np.cumsum(daily)
    return {
        "trades": int(len(pnl)), "days": len(days), "days_traded": int(sum(1 for d in days if d["trades"])),
        "win_rate": float(len(wins) / len(pnl)) if len(pnl) else None,
        "avg_win": float(wins.mean()) if len(wins) else 0.0, "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnl.mean()) if len(pnl) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
        "total_pnl": float(daily.sum()),
        "max_drawdown": float((eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min()) if len(eq) else 0.0,
        "sharpe": float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else None,
        "best_day": float(daily.max()) if len(daily) else 0.0, "worst_day": float(daily.min()) if len(daily) else 0.0,
    }
