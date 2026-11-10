from ai_hedge.data.providers.sec_edgar_provider import build_quarter_series, compute_ttm, get_latest_annual


def _e(start, end, val, form="10-Q", accn="0001"):
    return {"start": start, "end": end, "val": val, "form": form, "accn": accn}


# Apple-style fiscal year: Q1-Q3 in 10-Qs, Q4 only inside the 10-K annual figure
FY = [
    _e("2024-09-29", "2024-12-28", 124.0),
    _e("2024-12-29", "2025-03-29", 95.0),
    _e("2024-09-29", "2025-03-29", 219.0),  # 6-month YTD
    _e("2025-03-30", "2025-06-28", 94.0),
    _e("2024-09-29", "2025-06-28", 313.0),  # 9-month YTD
    _e("2024-09-29", "2025-09-27", 416.0, form="10-K"),
    _e("2025-06-29", "2025-09-27", 102.0, form="10-K"),  # 3-month Q4 figure carried in the 10-K
]


def test_ttm_includes_fiscal_q4_from_10k():
    assert compute_ttm(FY) == 124.0 + 95.0 + 94.0 + 102.0


def test_q4_derived_from_annual_minus_ytd_when_not_reported():
    series = [v for v in FY if not (v["form"] == "10-K" and v["start"] == "2025-06-29")]
    quarters = build_quarter_series(series)
    assert quarters[-1]["end"] == "2025-09-27"
    assert quarters[-1]["val"] == 416.0 - 313.0


def test_cash_flow_quarters_derived_from_ytd_only_filings():
    ytd_only = [
        _e("2024-09-29", "2024-12-28", 30.0),
        _e("2024-09-29", "2025-03-29", 55.0),
        _e("2024-09-29", "2025-06-28", 80.0),
        _e("2024-09-29", "2025-09-27", 110.0, form="10-K"),
    ]
    assert [q["val"] for q in build_quarter_series(ytd_only)] == [30.0, 25.0, 25.0, 30.0]
    assert compute_ttm(ytd_only) == 110.0


def test_ttm_refuses_non_contiguous_quarters():
    gappy = [
        _e("2024-09-29", "2024-12-28", 1.0),
        _e("2024-12-29", "2025-03-29", 1.0),
        _e("2025-03-30", "2025-06-28", 1.0),
        _e("2025-09-28", "2025-12-27", 1.0),  # fiscal Q4 missing
    ]
    assert compute_ttm(gappy) is None


def test_supplementary_quarter_does_not_duplicate_filed_quarter():
    series = FY + [_e("2025-04-01", "2025-06-30", 999.0, accn="yfinance")]
    assert compute_ttm(series) == 124.0 + 95.0 + 94.0 + 102.0


def test_latest_annual_ignores_quarter_figures_in_10k():
    assert get_latest_annual(FY) == 416.0
