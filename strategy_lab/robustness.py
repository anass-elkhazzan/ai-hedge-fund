"""Robustness checks for the VWAP-reversion candidate (python -m strategy_lab.robustness).

1. Parameter sensitivity: every k/buf pair on the full period (a real edge should not
   live on one lucky setting).
2. Cost stress: slippage of 1, 2 and 3 ticks per side.
3. Risk sizing: $150 / $200 / $300 per trade.
4. Fill resolution: replay the same signals with exits checked on 1-minute bars
   (last ~29 days, the most Yahoo keeps) instead of 5-minute bars.
"""

from __future__ import annotations

import json
import logging

import pandas as pd
import yfinance as yf

from strategy_lab import data
from strategy_lab.sim import Costs, HouseRules, monte_carlo, run_trade, simulate_day, stats
from strategy_lab.strategies import vwap_reversion

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def summarize(days):
    s, m = stats(days), monte_carlo(days, runs=3_000)
    return {"trades": s["trades"], "win%": round((s["win_rate"] or 0) * 100), "exp$": round(s["expectancy"]),
            "PF": round(s["profit_factor"], 2) if s["profit_factor"] else None, "total$": round(s["total_pnl"]),
            "maxDD$": round(s["max_drawdown"]), "pass%": round(m["p_pass"] * 100), "fail%": round(m["p_fail"] * 100),
            "days_to_pass": m["median_days_to_pass"]}


def one_minute_bars(sym: str) -> pd.DataFrame:
    t = yf.Ticker(data.SPECS[sym][0])
    frames = []
    end = pd.Timestamp.now(tz="UTC")
    for _ in range(4):  # 4 × 7 days
        start = end - pd.Timedelta(days=7)
        df = t.history(start=start, end=end, interval="1m")
        if not df.empty:
            frames.append(df)
        end = start
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated()]
    df.index = df.index.tz_convert("America/New_York")
    return df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].between_time("09:30", "15:59")


def main():
    out = {}
    for sym in data.SPECS:
        _, dpp, tick, comm = data.SPECS[sym]
        sessions = data.sessions(data.load(sym))
        base = Costs(dpp, tick, comm)
        rules = HouseRules()
        res = {"sensitivity": {}, "slippage": {}, "risk": {}}
        for k in (1.25, 1.5, 1.75, 2.0, 2.5):
            for buf in (0.25, 0.5, 0.75, 1.0):
                days = [simulate_day(d, vwap_reversion(d, k=k, buf=buf), base, rules) for d in sessions]
                res["sensitivity"][f"k={k} buf={buf}"] = summarize(days)
        best = dict(k=1.5, buf=0.5 if sym == "MNQ" else 0.25)
        for ticks in (1, 2, 3):
            c = Costs(dpp, tick * ticks, comm)
            days = [simulate_day(d, vwap_reversion(d, **best), c, rules) for d in sessions]
            res["slippage"][f"{ticks} tick(s)"] = summarize(days)
        for risk in (150, 200, 300):
            r = HouseRules(risk_per_trade=risk)
            days = [simulate_day(d, vwap_reversion(d, **best), base, r) for d in sessions]
            res["risk"][f"${risk}"] = summarize(days)
        # 1-minute fill check: signals from 5-minute bars, exits walked on 1-minute bars
        m1 = one_minute_bars(sym)
        same, diff, pts5, pts1 = 0, 0, 0.0, 0.0
        for d in sessions:
            day1 = m1[m1.index.date == d.index[0].date()]
            if len(day1) < 300:
                continue
            for (i, side, stop, target, rr) in vwap_reversion(d, **best):
                t5 = run_trade(d, i, side, stop, target, rr, base)
                if not t5:
                    continue
                entry_time = d.index[i + 1]
                j = int(day1.index.searchsorted(entry_time))
                t1 = run_trade(day1, j - 1, side, stop, target, rr, base) if j > 0 else None
                if not t1:
                    continue
                pts5 += t5["pts"]; pts1 += t1["pts"]
                same += t5["why"] == t1["why"]; diff += t5["why"] != t1["why"]
        res["one_minute_check"] = {"trades_compared": same + diff, "same_outcome": same, "different_outcome": diff,
                                   "points_5m": round(pts5, 2), "points_1m": round(pts1, 2)}
        out[sym] = res
        print(sym, json.dumps(res["one_minute_check"]))
        print("  slippage", json.dumps(res["slippage"]))
        print("  risk", json.dumps(res["risk"]))
        good = sum(1 for v in res["sensitivity"].values() if v["pass%"] >= 50 and v["fail%"] <= 20)
        print(f"  sensitivity: {good}/{len(res['sensitivity'])} settings with pass>=50% and fail<=20%")
        for k_, v in res["sensitivity"].items():
            print("   ", k_, v)
    (data.CACHE.parent / "results" / "robustness.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
