"""Strategy lab: test rule-based strategies against the MyFundedFutures Rapid 50K evaluation.

Research process (as a quant desk would):
  1. In-sample (first 2/3 of sessions): try each parameter set, pick the one with the best
     Monte Carlo pass probability (ties: higher expectancy).
  2. Out-of-sample (last 1/3, never used for picking): report how that choice actually did.
  3. Full period: rolling real-sequence evaluations + Monte Carlo, for context.
Usage:  python -m strategy_lab.run_lab [--refresh]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from strategy_lab import data
from strategy_lab.sim import Costs, HouseRules, monte_carlo, rolling_evaluations, simulate_day, stats
from strategy_lab.strategies import GRIDS, STRATEGIES

OUT = Path(__file__).resolve().parent / "results"


def run_days(sessions, fn, params, costs, rules):
    return [simulate_day(day, fn(day, **params), costs, rules) for day in sessions]


def main(refresh: bool = False) -> dict:
    OUT.mkdir(exist_ok=True)
    rules = HouseRules()
    report = {"generated": datetime.now().isoformat(timespec="minutes"), "house_rules": rules.__dict__, "results": []}
    for sym in data.SPECS:
        _, dpp, tick, comm = data.SPECS[sym]
        costs = Costs(dpp, tick, comm)
        sessions = data.sessions(data.load(sym, refresh=refresh))
        split = int(len(sessions) * 2 / 3)
        is_s, oos_s = sessions[:split], sessions[split:]
        period = {"from": str(sessions[0].index[0].date()), "to": str(sessions[-1].index[0].date()),
                  "in_sample_to": str(is_s[-1].index[0].date()), "sessions": len(sessions)}
        for name, fn in STRATEGIES.items():
            best = None
            for params in GRIDS[name]:
                days = run_days(is_s, fn, params, costs, rules)
                mc = monte_carlo(days, runs=2_000)
                st = stats(days)
                key = (mc.get("p_pass", 0), st["expectancy"])
                if best is None or key > best[0]:
                    best = (key, params, mc, st)
            _, params, is_mc, is_st = best
            oos_days = run_days(oos_s, fn, params, costs, rules)
            all_days = run_days(sessions, fn, params, costs, rules)
            row = {
                "symbol": sym, "strategy": name, "params": params, "period": period,
                "in_sample": {"stats": is_st, "monte_carlo": is_mc},
                "out_of_sample": {"stats": stats(oos_days), "monte_carlo": monte_carlo(oos_days, runs=5_000)},
                "full": {"stats": stats(all_days), "monte_carlo": monte_carlo(all_days, runs=5_000),
                         "rolling": rolling_evaluations(all_days)},
                "trades": [{k: t[k] for k in ("entry_time", "exit_time", "side", "entry", "exit", "contracts", "pnl", "why")}
                           for d in all_days for t in d["trades"]],
                "daily": [{"date": d["date"], "pnl": round(d["pnl"], 2), "worst": round(d["worst"], 2)} for d in all_days],
            }
            report["results"].append(row)
            o = row["out_of_sample"]
            print(f"{sym:4} {name:15} {json.dumps(params):55} IS pass {is_mc['p_pass']:.0%} | "
                  f"OOS pass {o['monte_carlo'].get('p_pass', 0):.0%} fail {o['monte_carlo'].get('p_fail', 0):.0%} "
                  f"exp ${o['stats']['expectancy']:.0f} PF {o['stats']['profit_factor']} trades {o['stats']['trades']}")
    (OUT / "lab_results.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
