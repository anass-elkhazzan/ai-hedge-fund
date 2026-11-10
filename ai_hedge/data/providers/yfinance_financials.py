"""yfinance financial statements, shaped like SEC EDGAR entries.

Used to fill gaps when a company's EDGAR companyfacts are thin or use tags we
don't map (e.g. XOM). Entries look like get_line_item_history() output:
{start, end, val, form, accn}. Flow items get a start date (quarter = ~91
days, annual = ~364 days); balance-sheet items have no start.
"""

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# our key -> (statement, yfinance row names in priority order, take absolute value)
_INCOME = "income"
_BALANCE = "balance"
_CASHFLOW = "cashflow"
YF_ROWS = {
    "revenue_fb": (_INCOME, ["Total Revenue", "Operating Revenue"], False),
    "gross_profit": (_INCOME, ["Gross Profit"], False),
    "operating_income": (_INCOME, ["Operating Income"], False),
    "net_income": (_INCOME, ["Net Income", "Net Income Common Stockholders"], False),
    "earnings_per_share": (_INCOME, ["Basic EPS"], False),
    "earnings_per_share_fb": (_INCOME, ["Diluted EPS"], False),
    "research_and_development": (_INCOME, ["Research And Development"], False),
    "interest_expense": (_INCOME, ["Interest Expense", "Interest Expense Non Operating"], True),
    "depreciation_and_amortization": (_CASHFLOW, ["Depreciation Amortization Depletion", "Depreciation And Amortization"], False),
    "operating_expense": (_INCOME, ["Operating Expense", "Total Expenses"], False),
    "total_assets": (_BALANCE, ["Total Assets"], False),
    "current_assets": (_BALANCE, ["Current Assets"], False),
    "total_liabilities": (_BALANCE, ["Total Liabilities Net Minority Interest"], False),
    "current_liabilities": (_BALANCE, ["Current Liabilities"], False),
    "cash_and_equivalents": (_BALANCE, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"], False),
    "long_term_debt": (_BALANCE, ["Long Term Debt"], False),
    "long_term_debt_current": (_BALANCE, ["Current Debt"], False),
    "shareholders_equity": (_BALANCE, ["Stockholders Equity", "Common Stock Equity"], False),
    "outstanding_shares": (_BALANCE, ["Ordinary Shares Number", "Share Issued"], False),
    "goodwill": (_BALANCE, ["Goodwill"], False),
    "intangible_assets": (_BALANCE, ["Other Intangible Assets"], False),
    "inventory": (_BALANCE, ["Inventory"], False),
    "accounts_receivable": (_BALANCE, ["Accounts Receivable", "Receivables"], False),
    "capital_expenditure": (_CASHFLOW, ["Capital Expenditure", "Purchase Of PPE"], True),
    "operating_cash_flow": (_CASHFLOW, ["Operating Cash Flow"], False),
    "dividends": (_CASHFLOW, ["Cash Dividends Paid", "Common Stock Dividend Paid"], True),
    "stock_issuance": (_CASHFLOW, ["Common Stock Issuance"], True),
    "stock_repurchase": (_CASHFLOW, ["Repurchase Of Capital Stock", "Common Stock Payments"], True),
}

_cache: dict[str, dict[str, list[dict]]] = {}


def _frames(ticker: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(ticker)
    out = {}
    for name, q_attr, a_attr in (
        (_INCOME, "quarterly_income_stmt", "income_stmt"),
        (_BALANCE, "quarterly_balance_sheet", "balance_sheet"),
        (_CASHFLOW, "quarterly_cashflow", "cashflow"),
    ):
        for kind, attr in (("10-Q", q_attr), ("10-K", a_attr)):
            try:
                out[(name, kind)] = getattr(t, attr)
            except Exception as e:  # network / parsing errors: just skip that frame
                logger.debug("yfinance %s %s failed: %s", ticker, attr, e)
                out[(name, kind)] = None
    return out


def get_statement_series(ticker: str) -> dict[str, list[dict]]:
    """Return {our_key: [entry, ...]} sorted by end date. Empty dict on failure."""
    if ticker in _cache:
        return _cache[ticker]
    try:
        frames = _frames(ticker)
    except Exception as e:
        logger.debug("yfinance statements unavailable for %s: %s", ticker, e)
        _cache[ticker] = {}
        return {}

    result: dict[str, list[dict]] = {}
    for key, (stmt, rows, absolute) in YF_ROWS.items():
        entries = []
        for kind in ("10-Q", "10-K"):
            df = frames.get((stmt, kind))
            if df is None or getattr(df, "empty", True):
                continue
            row = next((r for r in rows if r in df.index), None)
            if row is None:
                continue
            for col, val in df.loc[row].items():
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    continue
                end_dt = col.to_pydatetime() if hasattr(col, "to_pydatetime") else datetime.fromisoformat(str(col)[:10])
                end = end_dt.strftime("%Y-%m-%d")
                entry = {"end": end, "val": abs(float(val)) if absolute else float(val), "form": kind, "accn": "yfinance"}
                if stmt != _BALANCE:
                    days = 91 if kind == "10-Q" else 364
                    entry["start"] = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")
                else:
                    entry["start"] = ""
                entries.append(entry)
        if entries:
            entries.sort(key=lambda x: x["end"])
            result[key] = entries
    _cache[ticker] = result
    return result
