"""Command line: uv run python -m value_pipeline <command>. Nothing here places trades."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from value_pipeline import benchmark, market_data, portfolio as pfmod, review, screener, watch
from value_pipeline.config import Paths, load_config

console = Console()


def _date(text: str) -> str:
    return date.fromisoformat(text).isoformat()


def cmd_init(args, paths: Paths, cfg) -> None:
    if paths.portfolio.exists() and not args.force:
        raise SystemExit(f"{paths.portfolio} already exists; pass --force to start over (a backup is kept).")
    pf = pfmod.Portfolio(cash=0.0, currency=args.currency)
    pfmod.apply_deposit(pf, args.cash, on=args.date)
    pfmod.save(paths, pf)
    console.print(f"Started a portfolio with {args.cash:,.2f} {args.currency} cash in {paths.root}")


def cmd_deposit(args, paths: Paths, cfg) -> None:
    pf = pfmod.load(paths)
    pfmod.apply_deposit(pf, args.amount, on=args.date)
    pfmod.save(paths, pf)
    console.print(f"Cash is now {pf.cash:,.2f} {pf.currency}")


def cmd_buy(args, paths: Paths, cfg) -> None:
    pf = pfmod.load(paths)
    ticker = args.ticker.upper()
    found = review.latest_result(paths, ticker) if pf.position(ticker) is None else None
    held = pfmod.apply_buy(pf, ticker, args.shares, args.price, args.fees, on=args.date,
                           thesis=(found or {}).get("thesis", ""),
                           intrinsic_value=(found or {}).get("intrinsic_value"))
    pfmod.save(paths, pf)
    pfmod.append_trade(paths, {"date": args.date, "ticker": ticker, "action": "BUY", "shares": args.shares,
                               "price": args.price, "fees": args.fees, "note": args.note})
    console.print(f"Bought {args.shares:g} {ticker} @ {args.price:,.2f}. Now {held.shares:g} sh, "
                  f"avg {held.avg_price:,.2f}. Cash {pf.cash:,.2f}")


def cmd_sell(args, paths: Paths, cfg) -> None:
    pf = pfmod.load(paths)
    ticker = args.ticker.upper()
    gain = pfmod.apply_sell(pf, ticker, args.shares, args.price, args.fees)
    pfmod.save(paths, pf)
    pfmod.append_trade(paths, {"date": args.date, "ticker": ticker, "action": "SELL", "shares": args.shares,
                               "price": args.price, "fees": args.fees, "note": args.note})
    left = pf.position(ticker)
    console.print(f"Sold {args.shares:g} {ticker} @ {args.price:,.2f} (realised {gain:+,.2f}). "
                  f"{'Now ' + format(left.shares, 'g') + ' sh' if left else 'Position closed'}. Cash {pf.cash:,.2f}")


def cmd_status(args, paths: Paths, cfg) -> None:
    pf = pfmod.load(paths)
    today = date.today()
    rows, invested_value = [], 0.0
    for p in pf.positions:
        price = market_data.latest_price(p.ticker)
        value = p.shares * (price if price is not None else p.avg_price)
        invested_value += value
        rows.append((p, price, value))
    total = pf.cash + invested_value

    table = Table(title=f"Portfolio {today.isoformat()} ({pf.currency})")
    for col in ("Ticker", "Shares", "Avg", "Price", "Value", "Gain", "Weight", "Rating", "Days"):
        table.add_column(col, justify="left" if col in ("Ticker", "Rating") else "right")
    for p, price, value in rows:
        gain = f"{price / p.avg_price - 1:+.1%}" if price else "–"
        days = (today - date.fromisoformat(p.first_bought)).days
        table.add_row(p.ticker, f"{p.shares:g}", f"{p.avg_price:,.2f}", f"{price:,.2f}" if price else "no price",
                      f"{value:,.2f}", gain, f"{value / total:.1%}" if total else "–", p.last_rating or "–", str(days))
    table.add_row("Cash", "", "", "", f"{pf.cash:,.2f}", "", f"{pf.cash / total:.1%}" if total else "–", "", "")
    console.print(table)
    console.print(f"Total value: [bold]{total:,.2f}[/bold] {pf.currency}")
    cmp = benchmark.compare(total, pf.deposits, pfmod.read_trades(paths))
    if cmp:
        console.print(f"Since {cmp['since']}: portfolio {cmp['portfolio_return']:+.1%} vs SPY {cmp['spy_return']:+.1%} "
                      f"(same cash flows; {cmp['invested']:,.2f} put in)")


def cmd_screen(args, paths: Paths, cfg) -> None:
    owned = {p.ticker for p in pfmod.load(paths).positions} if paths.portfolio.exists() else set()
    if args.top:
        cfg["screen"]["top_n"] = args.top
    console.print("Screening the S&P 500 + 400 (first run of the week fetches every ticker; this takes a while)...")
    res = screener.run(paths, cfg, owned)
    console.print(f"Screened {res['screened']}, skipped {len(res['skipped'])} (missing data), "
                  f"excluded {len(res['excluded'])} financials/REITs/utilities, "
                  f"{len(res['ranked'])} passed the filters. Saved {res['path']}")
    table = Table(title=f"Top {len(res['top'])} (Magic Formula, not owned)")
    for col in ("#", "Ticker", "Sector", "EBIT/EV (avg)", "ROIC (avg)", "FCF yield", "F", "Z", "Value MoS", "Trend"):
        table.add_column(col)
    for r in res["top"]:
        mos = r.get("mos_formula")
        table.add_row(str(r["mf_position"]), r["ticker"], str(r.get("sector", "")), f"{r['earnings_yield_avg']:.1%}",
                      f"{r['roic_avg']:.0%}", f"{r['fcf_yield']:.1%}" if r.get("fcf_yield") is not None else "–",
                      str(r["fscore"]), f"{r['altman_z']:.1f}", "–" if mos is None else f"{mos:.0%}",
                      r.get("trend") or "–")
    console.print(table)
    if res["knives"]:
        console.print("Watch, falling knives (FREEFALL): " + ", ".join(r["ticker"] for r in res["knives"][:10]))


def cmd_review(args, paths: Paths, cfg) -> None:
    if args.candidates is not None:
        cfg["review"]["candidates"] = args.candidates
    out = review.run(paths, cfg, yes=args.yes, dry_run=args.dry_run)
    console.print(Markdown(out.read_text(encoding="utf-8")))
    console.print(f"\nSaved {out} and {out.with_suffix('.html')}")


def cmd_watch(args, paths: Paths, cfg) -> None:
    found, out = watch.run(paths, cfg, notify=not args.no_notify)
    console.print(Markdown(out.read_text(encoding="utf-8")))
    console.print(f"\nSaved {out}")


def cmd_run_all(args, paths: Paths, cfg) -> None:
    cmd_screen(args, paths, cfg)
    cmd_review(args, paths, cfg)


def build_parser() -> argparse.ArgumentParser:
    today = date.today().isoformat()
    p = argparse.ArgumentParser(prog="value_pipeline", description=__doc__)
    p.add_argument("--data-dir", help="where holdings, screens and reports live (default value_pipeline/data)")
    p.add_argument("-v", "--verbose", action="store_true", help="log every skipped ticker")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="start a portfolio with some cash")
    s.add_argument("--cash", type=float, required=True)
    s.add_argument("--currency", default="USD")
    s.add_argument("--date", type=_date, default=today)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("deposit", help="add cash (negative to withdraw)")
    s.add_argument("amount", type=float)
    s.add_argument("--date", type=_date, default=today)
    s.set_defaults(func=cmd_deposit)

    for name, func in (("buy", cmd_buy), ("sell", cmd_sell)):
        s = sub.add_parser(name, help=f"record a {name} you placed at your broker")
        s.add_argument("ticker")
        s.add_argument("shares", type=float)
        s.add_argument("price", type=float)
        s.add_argument("--fees", type=float, default=0.0)
        s.add_argument("--date", type=_date, default=today)
        s.add_argument("--note", default="")
        s.set_defaults(func=func)

    s = sub.add_parser("status", help="holdings, value, gain and return vs SPY")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("watch", help="free weekly check: prices vs values, FREEFALL, buy range (no agents)")
    s.add_argument("--no-notify", action="store_true", help="don't show a macOS notification")
    s.set_defaults(func=cmd_watch)

    for name, func in (("screen", cmd_screen), ("review", cmd_review), ("run-all", cmd_run_all)):
        s = sub.add_parser(name, help={"screen": "rank the S&P 500 + 400 by the Magic Formula",
                                       "review": "run the agents on holdings + top candidates; write ACTIONS.md",
                                       "run-all": "screen, then review"}[name])
        if name in ("screen", "run-all"):
            s.add_argument("--top", type=int, help="how many top candidates to list")
        if name in ("review", "run-all"):
            s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
            s.add_argument("--dry-run", action="store_true", help="fake ratings, no agent calls, no portfolio update")
            s.add_argument("--candidates", type=int, help="how many screen candidates to analyse")
        s.set_defaults(func=func)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    paths = Paths(args.data_dir)
    try:
        args.func(args, paths, load_config())
    except (pfmod.TradeError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
