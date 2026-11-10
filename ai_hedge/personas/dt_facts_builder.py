"""
Build per-agent facts bundles for Day Trade mode from raw data + intraday data.

Each bundle contains the intraday indicators, daily context, pre-market data,
recent news, gap analysis, and previous day levels that the DT strategy
subagent needs to produce its signal.

Usage:
    python -m ai_hedge.personas.dt_facts_builder --run-id <id> --tickers AAPL,MSFT
"""
from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any

import numpy as np
import pandas as pd
from datetime import time as _time

from ai_hedge.data.api import (
    get_intraday_prices,
    get_premarket_data,
    intraday_to_df,
    prices_to_df,
)
from ai_hedge.data.indicators import (
    compute_daily_indicators,
    compute_intraday_indicators,
)
from ai_hedge.data.models import CompanyNews, Price


# ── JSON serialisation helpers ────────────────────────────────────────────────

def _safe_val(v: Any) -> Any:
    """Coerce numpy scalars and math special values to JSON-safe Python types."""
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _safe_serialize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_serialize(i) for i in obj]
    if isinstance(obj, pd.Series):
        return _safe_serialize(obj.tolist())
    if isinstance(obj, pd.DataFrame):
        return _safe_serialize(obj.to_dict("records"))
    if hasattr(obj, "model_dump"):
        return _safe_serialize(obj.model_dump())
    return _safe_val(obj)


# ── raw data loading ──────────────────────────────────────────────────────────

def _load_raw(run_id: str, ticker: str) -> dict:
    path = os.path.join("runs", run_id, "raw", f"{ticker}.json")
    with open(path) as f:
        return json.load(f)


# ── gap analysis helper ──────────────────────────────────────────────────────

def _compute_gap_analysis(
    prev_close: float | None,
    current_open: float | None,
    premarket: dict | None,
) -> dict:
    """Compute gap size, direction, and classification hints."""
    if prev_close is None or current_open is None:
        return {"gap_pct": None, "gap_direction": "unknown", "gap_size_category": "unknown"}

    gap_pct = (current_open - prev_close) / prev_close * 100.0

    if abs(gap_pct) < 0.3:
        direction = "flat"
        category = "no_gap"
    elif gap_pct > 0:
        direction = "up"
        category = "large" if gap_pct > 3.0 else ("medium" if gap_pct > 1.5 else "small")
    else:
        direction = "down"
        category = "large" if gap_pct < -3.0 else ("medium" if gap_pct < -1.5 else "small")

    result = {
        "gap_pct": round(gap_pct, 3),
        "gap_direction": direction,
        "gap_size_category": category,
        "prev_close": prev_close,
        "current_open": current_open,
    }

    if premarket:
        result["premarket_high"] = premarket.get("high", premarket.get("overnight_high"))
        result["premarket_low"] = premarket.get("low", premarket.get("overnight_low"))
        result["premarket_volume"] = premarket.get("volume", premarket.get("overnight_volume"))

    return result


# ── previous day levels ──────────────────────────────────────────────────────

def _compute_prev_day_levels(raw: dict) -> dict:
    """Extract previous day high, low, close, open from daily price data."""
    prices = raw.get("prices", [])
    if not prices:
        return {}

    # Prices are sorted most recent first typically
    try:
        price_objs = [Price(**p) if isinstance(p, dict) else p for p in prices]
        # Sort by date descending
        price_objs.sort(key=lambda p: p.time, reverse=True)
        if len(price_objs) >= 1:
            prev = price_objs[0]
            result = {
                "prev_day_high": prev.high,
                "prev_day_low": prev.low,
                "prev_day_close": prev.close,
                "prev_day_open": prev.open,
                "prev_day_volume": prev.volume,
                "prev_day_date": prev.time,
            }
            if len(price_objs) >= 2:
                two_days_ago = price_objs[1]
                result["two_days_ago_close"] = two_days_ago.close
            return result
    except Exception:
        pass
    return {}


# ── per-agent facts builder ─────────────────────────────────────────────────

DT_AGENTS = [
    "dt_vwap_trader",
    "dt_momentum_scalper",
    "dt_mean_reversion",
    "dt_breakout_hunter",
    "dt_gap_analyst",
    "dt_volume_profiler",
    "dt_pattern_reader",
    "dt_stat_arb",
    "dt_news_catalyst",
]


def _build_common_facts(
    ticker: str,
    raw: dict,
    intraday_indicators: dict,
    daily_indicators: dict,
    premarket: dict | None,
    gap_analysis: dict,
    prev_day_levels: dict,
    news_items: list[dict],
) -> dict:
    """Build the common facts bundle shared by all DT agents."""
    return {
        "ticker": ticker,
        "intraday_indicators": intraday_indicators,
        "daily_context": daily_indicators,
        "premarket": premarket,
        "gap_analysis": gap_analysis,
        "prev_day_levels": prev_day_levels,
        "recent_news": news_items[:20],
    }


def _build_agent_facts(
    agent_name: str,
    common: dict,
) -> dict:
    """Build agent-specific facts by adding agent-relevant emphasis to common data."""
    facts = dict(common)
    facts["agent"] = agent_name

    # Each agent gets the full data — the prompt tells it what to focus on.
    # We tag which data sections are most relevant for quick reference.
    relevance_map = {
        "dt_vwap_trader": ["intraday_indicators.vwap", "intraday_indicators.volume"],
        "dt_momentum_scalper": ["intraday_indicators.ema", "intraday_indicators.rsi", "intraday_indicators.macd"],
        "dt_mean_reversion": ["intraday_indicators.bollinger", "intraday_indicators.rsi", "intraday_indicators.z_score"],
        "dt_breakout_hunter": ["gap_analysis", "prev_day_levels", "premarket", "intraday_indicators.volume"],
        "dt_gap_analyst": ["gap_analysis", "premarket", "prev_day_levels", "recent_news"],
        "dt_volume_profiler": ["intraday_indicators.obv", "intraday_indicators.volume", "intraday_indicators.ad_line"],
        "dt_pattern_reader": ["intraday_indicators", "prev_day_levels", "gap_analysis"],
        "dt_stat_arb": ["intraday_indicators.hurst", "intraday_indicators.autocorrelation", "intraday_indicators.returns_distribution"],
        "dt_news_catalyst": ["recent_news", "gap_analysis", "premarket"],
    }

    facts["primary_data_sections"] = relevance_map.get(agent_name, [])
    return facts


# ── main orchestrator ──────────────────────────────────────────────────────────

def _prev_rth_day_levels(rth: pd.DataFrame) -> dict:
    """Previous New York cash-session levels for futures, from 09:30-16:00 bars.

    A futures "daily" bar runs 18:00-17:00 and doesn't match the session the
    day-trade strategies reason about. "Previous day" is the latest complete
    session (last bar at 15:55 or later) before the session being planned.
    """
    if rth.empty:
        return {}
    days = sorted(set(rth.index.date))
    latest = rth[rth.index.date == days[-1]]
    complete = latest.index.max().time() >= _time(15, 55)
    prev_idx = -1 if complete else -2
    if len(days) < abs(prev_idx):
        return {}
    prev_day = days[prev_idx]
    d = rth[rth.index.date == prev_day]
    result = {
        "prev_day_high": round(float(d["high"].max()), 2),
        "prev_day_low": round(float(d["low"].min()), 2),
        "prev_day_close": round(float(d["close"].iloc[-1]), 2),
        "prev_day_open": round(float(d["open"].iloc[0]), 2),
        "prev_day_volume": int(d["volume"].sum()),
        "prev_day_date": prev_day.isoformat(),
        "session": "New York cash session 09:30-16:00 ET",
    }
    if len(days) >= abs(prev_idx) + 1:
        before = rth[rth.index.date == days[prev_idx - 1]]
        result["two_days_ago_close"] = round(float(before["close"].iloc[-1]), 2)
    return result


def _return_statistics(df: pd.DataFrame, sessions: int = 5) -> dict:
    """Hurst exponent, autocorrelation and return distribution of 5-minute bars.

    Uses the last few sessions so the statistics describe the current regime.
    """
    out: dict = {}
    try:
        days = sorted(set(df.index.date))[-sessions:]
        recent = df[np.isin(df.index.date, days)]
        close = recent["close"].astype(float)
        rets = close.pct_change().dropna()
        if len(rets) < 50:
            return {}
        logp = np.log(close.values)
        lags = range(2, 21)
        tau = [np.std(logp[lag:] - logp[:-lag]) for lag in lags]
        hurst = float(np.polyfit(np.log(list(lags)), np.log(tau), 1)[0])
        out["hurst"] = {
            "value": round(hurst, 3),
            "regime": "trending" if hurst > 0.55 else "mean_reverting" if hurst < 0.45 else "random_walk",
            "bars_used": int(len(close)),
            "sessions": len(days),
        }
        out["autocorrelation"] = {
            "lag_1": round(float(rets.autocorr(1)), 3),
            "lag_2": round(float(rets.autocorr(2)), 3),
            "lag_3": round(float(rets.autocorr(3)), 3),
            "interpretation": "positive = momentum persists, negative = moves tend to reverse",
        }
        std = float(rets.std())
        out["returns_distribution"] = {
            "mean_pct": round(float(rets.mean()) * 100, 4),
            "std_pct": round(std * 100, 4),
            "skew": round(float(rets.skew()), 3),
            "excess_kurtosis": round(float(rets.kurt()), 3),
            "pct_bars_beyond_2_std": round(float((rets.abs() > 2 * std).mean()) * 100, 2),
            "fat_tails": bool(rets.kurt() > 3),
        }
    except Exception as exc:
        out["return_statistics_error"] = str(exc)[:120]
    return out


def _split_futures_sessions(df: pd.DataFrame) -> tuple[pd.DataFrame, dict | None]:
    """Return (New York cash-session bars only, overnight session summary).

    Bars are timestamped in exchange time (America/New_York). The overnight
    session for the latest day runs from 16:00 ET the previous trading day to 09:30.
    """
    times = df.index.time
    rth_mask = (times >= _time(9, 30)) & (times < _time(16, 0))
    rth = df[rth_mask]
    if rth.empty:
        return df, None
    last_day = rth.index.date.max()
    complete = rth.index.max().time() >= _time(15, 55)
    if complete:
        start = pd.Timestamp.combine(last_day, _time(16, 0))
        end = df.index.max() + pd.Timedelta(minutes=5)
    else:
        prev_days = sorted({d for d in rth.index.date if d < last_day})
        start = pd.Timestamp.combine(prev_days[-1], _time(16, 0)) if prev_days else df.index.min()
        end = pd.Timestamp.combine(last_day, _time(9, 30))
    on = df[(df.index >= start) & (df.index < end)]
    overnight = None
    if not on.empty:
        overnight = {
            "session": ("overnight Globex since today's 16:00 ET close (next session not open yet)" if complete
                        else "overnight Globex (prev 16:00 to 09:30 ET)"),
            "overnight_high": round(float(on["high"].max()), 2),
            "overnight_low": round(float(on["low"].min()), 2),
            "overnight_last": round(float(on["close"].iloc[-1]), 2),
            "overnight_volume": int(on["volume"].sum()),
        }
    # before 09:30 today there are no cash-session bars yet for today; the
    # analysis then describes the previous session, which the prompt should know
    return rth, overnight


def build_dt_facts(run_id: str, tickers: list[str]) -> None:
    """Build day trade facts bundles for all DT agents × tickers."""
    facts_dir = os.path.join("runs", run_id, "facts")
    os.makedirs(facts_dir, exist_ok=True)

    for ticker in tickers:
        print(f"\nBuilding day trade facts for {ticker}...")

        # Load raw daily data from prepare step
        raw = _load_raw(run_id, ticker)

        # Previous day levels from daily data
        prev_day_levels = _compute_prev_day_levels(raw)

        # Fetch intraday prices
        try:
            intraday_prices = get_intraday_prices(ticker, period="1mo")
            intraday_df = intraday_to_df(intraday_prices) if intraday_prices else pd.DataFrame()
        except Exception as exc:
            print(f"  [WARN] Could not fetch intraday prices for {ticker}: {exc}")
            intraday_df = pd.DataFrame()

        # Index futures trade ~23h/day. The strategies (opening range, gap, VWAP) are
        # defined on the New York cash session, so analyse 09:30-16:00 ET bars and
        # report the overnight (Globex) session separately.
        overnight = None
        if ticker.upper().endswith("=F") and not intraday_df.empty:
            intraday_df, overnight = _split_futures_sessions(intraday_df)
            rth_levels = _prev_rth_day_levels(intraday_df)
            if rth_levels:
                prev_day_levels = rth_levels

        # Compute intraday indicators
        if not intraday_df.empty:
            intraday_indicators = compute_intraday_indicators(intraday_df)
            intraday_indicators.update(_return_statistics(intraday_df))
            if ticker.upper().endswith("=F"):
                # Futures bars are ~10 min delayed; give agents the near-live estimate too
                from ai_hedge.data.live_estimate import live_estimate
                est = live_estimate(ticker)
                if est:
                    intraday_indicators["live_price_estimate"] = {
                        "price": round(est["price"], 2),
                        "as_of_utc": est["as_of"].isoformat(timespec="seconds"),
                        "delayed_futures_price": est["futures_delayed_price"],
                        "note": (f"current_price/bars are ~10 min delayed; this estimate uses {est['proxy']} "
                                 "(typical error ~1 NQ point). Use it for where price is NOW."),
                    }
        else:
            intraday_indicators = {}
            print(f"  [WARN] No intraday data for {ticker}, indicators will be empty")

        # Compute daily indicators for context
        daily_prices = raw.get("prices", [])
        if daily_prices:
            try:
                price_objs = [Price(**p) if isinstance(p, dict) else p for p in daily_prices]
                daily_df = prices_to_df(price_objs)
                daily_indicators = compute_daily_indicators(daily_df)
            except Exception as exc:
                daily_indicators = {}
                print(f"  [WARN] Could not compute daily indicators: {exc}")
        else:
            daily_indicators = {}

        # Get pre-market data (for futures: the overnight Globex session)
        if overnight is not None:
            premarket = overnight
        else:
            try:
                premarket = get_premarket_data(ticker)
            except Exception as exc:
                premarket = None
                print(f"  [WARN] Could not fetch pre-market data for {ticker}: {exc}")

        if overnight is not None and prev_day_levels.get("session"):
            cur = intraday_indicators.get("current_price")
            intraday_indicators["prev_day"] = {
                "high": prev_day_levels["prev_day_high"], "low": prev_day_levels["prev_day_low"],
                "close": prev_day_levels["prev_day_close"], "date": prev_day_levels["prev_day_date"],
                "above_prev_high": bool(cur and cur > prev_day_levels["prev_day_high"]),
                "below_prev_low": bool(cur and cur < prev_day_levels["prev_day_low"]),
            }
            if isinstance(daily_indicators, dict):
                daily_indicators["note"] = ("Futures daily bars run 18:00-17:00 ET and include overnight trading; "
                                            "use prev_day_levels / intraday_indicators for cash-session levels.")

        # Gap analysis
        prev_close = prev_day_levels.get("prev_day_close")
        current_open = None
        if not intraday_df.empty:
            # today's first bar, not the first bar of the whole month of history
            latest = intraday_df.index.date == intraday_df.index.date.max()
            current_open = float(intraday_df[latest]["open"].iloc[0])
            if (prev_day_levels.get("prev_day_date") == str(intraday_df.index.date.max())
                    and overnight and overnight.get("overnight_last")):
                # planning after the close: "gap" = overnight price vs today's close
                current_open = overnight["overnight_last"]
        gap_analysis = _compute_gap_analysis(prev_close, current_open, premarket)

        # Recent news
        news_raw = raw.get("company_news", [])
        news_items = []
        for n in news_raw[:20]:
            if isinstance(n, dict):
                news_items.append({
                    "headline": n.get("title", n.get("headline", "")),
                    "sentiment": n.get("sentiment"),
                    "date": n.get("date"),
                })
            else:
                try:
                    obj = CompanyNews(**n) if isinstance(n, dict) else n
                    news_items.append({
                        "headline": obj.title,
                        "sentiment": obj.sentiment,
                        "date": obj.date,
                    })
                except Exception:
                    pass

        # Build common facts
        common = _build_common_facts(
            ticker=ticker,
            raw=raw,
            intraday_indicators=intraday_indicators,
            daily_indicators=daily_indicators,
            premarket=premarket,
            gap_analysis=gap_analysis,
            prev_day_levels=prev_day_levels,
            news_items=news_items,
        )

        # Write per-agent facts
        for agent in DT_AGENTS:
            try:
                facts = _build_agent_facts(agent, common)
                out_path = os.path.join(facts_dir, f"{agent}__{ticker}.json")
                with open(out_path, "w") as f:
                    json.dump(_safe_serialize(facts), f, indent=2, default=str)
                print(f"  wrote facts/{agent}__{ticker}.json")
            except Exception as exc:
                print(f"  [ERROR] {agent}/{ticker}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Build day trade facts bundles from raw + intraday data.")
    parser.add_argument("--run-id", required=True, help="Run identifier, e.g. 20240101_120000")
    parser.add_argument("--tickers", required=True, help="Comma-separated ticker symbols")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",")]
    build_dt_facts(args.run_id, tickers)
    print("\nDay trade facts built successfully.")


if __name__ == "__main__":
    main()
