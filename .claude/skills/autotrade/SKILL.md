---
name: autotrade
description: Unattended session of the AI investment committee for the MyFundedFutures-rules paper account. Run by Windows Task Scheduler with one argument - open, midday or close.
disable-model-invocation: true
allowed-tools: Bash(*) Read Write Edit Glob Grep Agent WebSearch WebFetch mcp__paper-broker__*
argument-hint: open | midday | close
---

# Autotrade — the AI investment committee

You are running **unattended**. Nobody will answer questions: never ask, never wait for input. Make the decision the playbook gives, act, log it, and finish.

This is the **paper-broker** account only (simulated money). Never place orders anywhere else, and never change the account's rules or reset it.

Evaluation period: Thursday 2026-10-01 through Friday 2026-10-30. Week 1 (1-9 Oct) was the shakedown; the same account and rules continue for three more weeks (12-30 Oct) as a continuous MyFundedFutures-style evaluation (target +$3,000). If today (New York date) is outside that range, write one line to the log saying so and stop.

## How the desk is organised (since 1 Oct 2026)
Like a real trading firm, roles are split:
| Desk | Who | Job |
|---|---|---|
| Research | strategy lab (`strategy_lab/`) | Found and stress-tested the strategy: **MNQ VWAP reversion** (fade a move back inside ±1.5σ of VWAP; target VWAP; stop beyond the session extreme). 50-day test: 72% wins, profit factor 1.83, 79% simulated pass rate |
| **Investment committee** | **you + the AI agents** | Decide each day **whether** the strategy may trade, **which direction**, **what size**, and **which news windows to avoid**. Written to the plan file |
| Execution | `strategy_lab/executor.py` (Windows task, 9:45–15:56) | Watches the market every 20 s and places the trades in seconds, following your plan |
| Risk | paper broker + house rules | MyFundedFutures rules, $300 risk per trade × size multiplier, max 2 trades/day, −$500 daily stop, +$1,200 daily profit lock |

**You do not place entry orders yourself.** The executor does. You may close a position or move a stop (risk management).

## The plan file
Path: `<project-root>\strategy_lab\plans\YYYY-MM-DD.json` (New York date). Format:
```json
{"trade": true, "size_multiplier": 1.0, "direction": "both",
 "blackouts": [{"from": "09:55", "to": "10:15", "why": "ISM manufacturing 10:00"}],
 "regime": "range", "reason": "one-line why", "decided_by": "committee open 2026-10-02 09:58", "run_id": "..."}
```
- `size_multiplier`: 0 (no trades), 0.5 or 1.0. `direction`: `both`, `long_only` or `short_only`.
- Times are New York time, `HH:MM`. If there's no plan file, the executor trades at half size.

## Committee rules (how to turn the agents' views into the plan)
Mean reversion makes money on **range / rotation days** and loses on **strong trend days** and **news shocks**. So:
1. **Scheduled high-impact news today** (FOMC decision or minutes, CPI, PCE, payrolls, ISM, retail sales, Fed chair speech; find them with web research): add a blackout from 5 minutes before to 20 minutes after each release time. **FOMC decision day: `trade: false`** (or blackout from 13:45 to the close).
2. **Strong trend expected** (head trader confidence ≥ 60 in one direction, OR at least 6 of 9 strategy agents agree on direction, OR an overnight gap > 1% on a real catalyst): mean-reversion fades against the trend are dangerous. Set `direction` to fade **with** the trend only (trend up → `long_only`, which buys dips back to VWAP) and `size_multiplier` 0.5.
3. **Range / mixed day** (agents split, chop regime, Hurst < 0.5): `trade: true`, `direction: both`, `size_multiplier: 1.0`.
4. **Extreme event risk** (market-wide shock in the news, circuit breakers, VIX spike headlines): `trade: false`.
5. When unsure, prefer `size_multiplier: 0.5` over `trade: false`. Standing aside every day teaches nothing.

## Session: `open` (about 9:50 AM ET)
1. `mcp__paper-broker__get_account`. If trading is locked or the evaluation is PASSED, write `{"trade": false, ...}` with that reason, journal it, and stop.
1b. Read the top of `<notes-vault>\Learning log.md` (max ~30 lines, auto-written nightly): it shows what each earlier filter (long_only, blackouts, daily cap) would have earned or saved. Use it when choosing direction and size, but don't over-fit to a handful of signals.
2. Run the day-trade workflow in `.claude/skills/daytrade/SKILL.md` for `NQ=F,ES=F` (all steps through the Portfolio Manager). Keep the `RUN_ID`. The web research must list today's and tomorrow's scheduled economic releases with times. **Stale data warning:** at 09:33 the intraday VWAP, z-score and gap blocks in the facts come from the PREVIOUS session (they are not today's); tell every agent to use the premarket gap and overnight range instead, and do not quote those VWAP levels as current. If `aggregate.py` fails, fix the import or report it; do not skip it silently.
3. Apply the committee rules to the agents' output (head trader, strategy signals, web research, Hurst/regime statistics) and **write the plan file**. Then validate it parses:
   `python -c "import json,sys; json.load(open(sys.argv[1]))" "<project-root>\strategy_lab\plans\YYYY-MM-DD.json"`
4. Write the journal entry: the plan and why (one or two sentences per decision).

## Session: `midday` (about 12:35 PM ET)
1. `get_account` and read today's executor log `<project-root>\logs\executor_YYYY-MM-DD.log`.
2. **If a position is open** and it's at least +1R in profit, move the stop to the entry price with `update_bracket`.
3. **Review the plan**: if the morning was a strong one-way trend against the fades (two stopped-out fades in the same direction, or price > 2.5σ from VWAP for an hour), set `direction` to the trend side only or `trade: false` for the afternoon. If an afternoon release is scheduled, make sure it's in `blackouts`. Rewrite the plan file with `decided_by` updated.
4. Write the journal entry.

## Session: `close` (about 3:50 PM ET)
1. `get_account`. The executor flattens at 15:55 and the broker force-closes at 4:10 PM; if a position is still open at this point, leave it for the executor unless it has no stop (then close it).
2. `trade_history` and the executor log for today.
3. Write the journal entry for the day: the committee's plan, every signal the executor saw (taken or skipped, and why), each trade's result, the day's P&L, and one lesson. Note whether the committee's plan helped (for example, a blackout or direction filter that avoided a loss) or hurt (blocked a winner).
4. Update the scoreboard note (below). Write negative amounts with a plain hyphen-minus (-$134.51), never the typographic minus; scripts/reconcile.py parses the Scoreboard.
5. Do NOT export TradingView yourself: the scheduled task "HedgeFund PostClose" (16:12, `scripts/post_close.py`) exports it, writes the final broker numbers into today's note and updates the Learning log after the 15:55 flatten. In the scoreboard keep one line: "To see today's trades on TradingView, double-click the desktop shortcut 'Paper Trades on TradingView' (Pine Editor > Ctrl+A, Ctrl+V, Ctrl+S)." Don't try to drive the TradingView app yourself in an unattended session.
6. Your close note is written at 15:50, BEFORE the 15:55 flatten. If a position is still open, say so and write its unrealized figure as unrealized; never present it as final. PostClose adds the final numbers.
7. If today is a **Friday** (2026-10-16, 10-23) write that week's report; if today is **2026-10-30** write the final evaluation report instead (below).

## Journal (Obsidian vault)
Folder: `<notes-vault>\Paper Trading Journal\`
- **Daily note** `YYYY-MM-DD.md` (New York date). Append a section per session: `## Open`, `## Midday`, `## Close`. Include the time (ET), account equity, today's P&L, floor and room to floor, the committee plan (with RUN_ID) and the executor's trades: symbol, side, contracts, entry, stop, target, result. Plain English. Keep each section short.
- **Scoreboard** `Scoreboard.md`: rewrite each close with a table of days (date, plan, trades, P&L, equity), total P&L vs the $3,000 target, best day vs the consistency rule, room to the max-loss floor, and evaluation status. Link each day's note with `[[YYYY-MM-DD]]` and link back to `[[Paper trading]]` and `[[Strategy lab]]`.
- **Week report** `Week report YYYY-MM-DD to YYYY-MM-DD.md` (Friday closes; the 1-9 Oct report already exists; use `scripts/daily_report.py` for the stats instead of re-reading logs, and base trade results on the broker, not on your 15:50 view). The **final evaluation report** (30 Oct) covers all four weeks. Contents: final result vs the MyFundedFutures rules (would it have passed?), every trade, win rate, average win and loss, how live results compare with the strategy lab's backtest (72% wins, PF 1.83), whether the committee filter added value, biggest mistake, what to change before a real evaluation.

## Log
Append one line per session to `logs/autotrade.log`: `ISO time | session | action taken | equity`.

If anything fails (data fetch, an agent, the broker), log the error, write it in the journal, still write a plan file (fall back to `{"trade": true, "size_multiplier": 0.5, "direction": "both", "blackouts": [], "reason": "committee failed: <error>"}`), make sure no position is left without a stop, and finish.
