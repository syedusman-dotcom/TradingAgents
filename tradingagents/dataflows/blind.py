"""The price-free fundamentals the Blind Valuer reads (independent arm only).

Built from ``blind_inputs`` (the same Yahoo statements as the verified value metrics), it shows
the accounts and the figures the worksheet derives from them alone, and nothing that carries
today's price: no price, market cap, enterprise value, yield, multiple, consensus, formula value,
and no buyback cash (cash spent over shares retired is the price paid). The share count is shown.
"""

from __future__ import annotations

from tradingagents.dataflows.value_metrics import (
    accounting_checks,
    base_rate_block,
    div,
    num,
    piotroski,
    record,
    roic_incl_goodwill,
)

# (heading, field) of the money columns, in the statement currency's millions.
_MONEY = (
    ("Revenue", "revenue"), ("Gross profit", "gross_profit"), ("EBIT", "ebit"),
    ("Net income", "net_income"), ("Operating cash flow", "operating_cash_flow"),
    ("Capex", "capex"), ("Free cash flow", "free_cash_flow"), ("Dividends paid", "dividends_paid"),
)


def _m(x) -> str:
    x = num(x)
    return "n/a" if x is None else f"{x / 1e6:,.0f}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _net_debt(y: dict):
    debt, cash = num(y.get("total_debt")), num(y.get("cash"))
    if debt is None and cash is None:
        return None
    return (debt or 0.0) - (cash or 0.0)


def _check_basis(inputs: dict) -> None:
    if not inputs.get("same_basis"):
        raise ValueError("the quote and the statements count different shares (an ADR)")
    if inputs.get("cross") is None:
        raise ValueError(f"no exchange rate from {inputs.get('reported')} statements to the quote")
    if not num(inputs.get("statement_shares")):
        raise ValueError("the statements give no share count")


def blind_fundamentals_text(inputs: dict) -> str:
    """The accounts of ``inputs`` (``blind_inputs``) as prompt text, free of anything priced.
    Raises ValueError (the reason) when per-share figures could not be put in the quote unit.

    Limit: the revenue CAGR uses the Yahoo statements only (``record`` without the worksheet's SEC
    revenue history), so its span and figure can differ from the worksheet's."""
    _check_basis(inputs)
    years, cur = inputs["years"], inputs["reported"]
    sector = f" ({inputs['sector']})" if inputs.get("sector") else ""
    lines = [
        f"# Accounts of {inputs['name']}{sector}",
        "",
        f"Statement currency: {cur}. Money figures are in {cur} millions; give every per-share "
        f"figure in {cur} per share, on the latest share count below. The program converts your "
        "per-share figures to the trading unit afterwards.",
        "",
        "## By fiscal year (newest first)",
        "",
        "| Fiscal year end | " + " | ".join(h for h, _ in _MONEY)
        + " | Shares outstanding (M) | Net debt | ROIC incl. goodwill | Gross margin | EBIT margin "
        "| FCF margin |",
        "|" + "---|" * (len(_MONEY) + 7),
    ]
    for y in years:
        cells = [str(y.get("fiscal_year_end"))] + [_m(y.get(f)) for _, f in _MONEY]
        shares = num(y.get("shares"))
        cells += ["n/a" if shares is None else f"{shares / 1e6:,.1f}", _m(_net_debt(y)),
                  _pct(roic_incl_goodwill(y)), _pct(div(y.get("gross_profit"), y.get("revenue"))),
                  _pct(div(y.get("ebit"), y.get("revenue"))),
                  _pct(div(y.get("free_cash_flow"), y.get("revenue")))]
        lines.append("| " + " | ".join(cells) + " |")

    rec = record(years)
    span = rec["revenue_cagr_years"]
    fcf_span = rec["fcf_cagr_years"]
    lines += [
        "",
        f"Shares outstanding (latest, the per-share basis): {num(inputs['statement_shares']) / 1e6:,.1f}M",
        "",
        "## Growth and reinvestment",
        "",
        f"- Revenue CAGR: {_pct(rec['revenue_cagr'])}" + (f" over {span} years" if span else ""),
        f"- Free cash flow CAGR: {_pct(rec['fcf_cagr'])}" + (f" over {fcf_span} years" if fcf_span else ""),
        f"- Reinvestment rate (share of the last three years' owner earnings kept): "
        f"{_pct(rec['reinvestment_rate'])}",
        f"- Sustainable growth (reinvestment rate x five-year mean ROIC): {_pct(rec['sustainable_growth'])}",
        "",
        "## Accounting checks",
        "",
    ]
    if len(years) > 1:
        fs = piotroski(years[0], years[1])
        lines.append(f"- Piotroski F-Score: {fs['score']}/9 ({fs['missing']} points not computable)")
    else:
        lines.append("- Piotroski F-Score: not computable (one year of statements)")
    checks = accounting_checks(years)
    m = checks["beneish_m"]
    lines.append(f"- Beneish M-score: {'n/a' if m is None else f'{m:.2f}'} (above -1.78 is the "
                 "likely-manipulator band)")
    lines.append("- Flags: " + ("; ".join(checks["flags"]) if checks["flags"] else "none"))
    lines += [
        "",
        f"## Discount rate\n\n{inputs['discount_rate']:.1%} (the 10-year Treasury rate plus 4.5 "
        "points, never below 9%)",
        "",
        "## Base rates for the company's size",
        "",
        base_rate_block(inputs.get("revenue_usd")) or "Base rates: not available (no revenue in dollars).",
    ]
    return "\n".join(lines)
