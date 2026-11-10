---
name: paper-trade
description: Run the swing hedge fund on tickers/index futures and place its decisions as PAPER orders through the paper-broker MCP (simulated money, live prices). Also reports the paper account.
disable-model-invocation: true
allowed-tools: Bash(*) Read Write Agent mcp__paper-broker__*
argument-hint: TICKER1[,TICKER2,...] | status | RUN_ID
---

# Paper Trade — AI Hedge Fund → paper-broker

Simulated account only (`paper_broker/paper_broker.db`, $50,000 by default). Never connect to, or place orders on, a real brokerage or prop firm account from this skill.

## Step 0 — Parse arguments

- `status` → skip to Step 4 (report only).
- A run id like `20261001_093000` → skip to Step 2 using that existing run.
- Otherwise a comma-separated list of symbols. Index requests map to tradable symbols before analysis:
  - Nasdaq / NDX / "the Nasdaq" → analyse `NQ=F`, trade `MNQ` (micro)
  - S&P 500 / SPX → analyse `ES=F`, trade `MES`
  - Dow → `YM=F` / `MYM`; Russell 2000 → `RTY=F` / `M2K`
  - ETFs (`QQQ`, `SPY`, `DIA`, `IWM`) and stocks trade as shares.
  Use full-size contracts (NQ/ES/YM/RTY) only if the user explicitly asks.

## Step 1 — Check the account, then run the analysis

1. Call `mcp__paper-broker__get_account`. If `trading_locked` is set, tell the user why and stop — only exits are allowed.
2. Run the full swing workflow in `.claude/skills/swing/SKILL.md` for the symbols (all of its steps, including web research and the Portfolio Manager). Keep its `RUN_ID`.

## Step 2 — Turn decisions into paper orders

Read `runs/{RUN_ID}/decisions.json`. For each ticker:

| PM action | Paper order |
|---|---|
| `hold` | nothing |
| `buy` | `side="buy"` |
| `short` | `side="sell"` (opens a short) |
| `sell` | `close_position` (exit a long) |
| `cover` | `close_position` (exit a short) |

For `buy` / `short`:
- **Order type:** `limit` at `entry_price`. If the current quote (`get_quote`) is already within `entry_tolerance_pct` of the entry, use `market` instead.
- **Brackets:** always pass `take_profit = target_price` and `stop_loss = stop_loss`. Never send an entry without a stop.
- **Size — stocks/ETFs:** the PM's `quantity`.
- **Size — futures:** ignore the PM's share count. Contracts = floor(risk_dollars / (|entry − stop| × multiplier)), where risk_dollars = min(1% of account equity, the PM's `account_risk_pct` × equity). Multipliers: MNQ $2, MES $5, MYM $0.50, M2K $5 (NQ $20, ES $50). If that is 0 contracts, skip the trade and say why.
- **note:** `"{RUN_ID}: {first sentence of the PM reasoning}"`.
- If the broker rejects an order, report the exact error; don't retry with different numbers unless the fix is obvious (e.g. rounding).

## Step 3 — Existing positions

Before placing anything, compare with `get_account().positions`. Don't add to a position in the same direction; the PM already saw paper positions via aggregate.

## Step 4 — Report

Call `get_account` and show, in plain English:
- Orders placed this run (symbol, side, size, entry, target, stop) with their TradingView chart links
- Account: equity, today's P&L, total P&L, open positions, open orders
- Risk rule status (daily loss limit, max drawdown, lock state)

Remind the user this is simulated money; fills use live prices (index futures quotes from Yahoo are ~10 minutes delayed).
