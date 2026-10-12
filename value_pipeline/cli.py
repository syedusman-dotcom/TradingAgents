"""Command line: uv run python -m value_pipeline <command>. Nothing here places trades."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from value_pipeline import (
    operations as ops,
    portfolio as pfmod,
    review,
    screener,
    superinvestors,
    watch,
)
from value_pipeline.config import Paths, load_config

console = Console()


def _date(text: str) -> str:
    return date.fromisoformat(text).isoformat()


def cmd_init(args, paths: Paths, cfg) -> None:
    try:
        ops.init_portfolio(paths, args.cash, args.currency, on=args.date, force=args.force)
    except ops.PortfolioExists as exc:
        raise SystemExit(str(exc)) from exc
    console.print(f"Started a portfolio with {args.cash:,.2f} {args.currency} cash in {paths.root}")


def cmd_deposit(args, paths: Paths, cfg) -> None:
    pf = ops.record_deposit(paths, args.amount, on=args.date)
    console.print(f"Cash is now {pf.cash:,.2f} {pf.currency}")


def cmd_buy(args, paths: Paths, cfg) -> None:
    held, pf = ops.record_buy(paths, args.ticker, args.shares, args.price, args.fees, on=args.date, note=args.note)
    console.print(f"Bought {args.shares:g} {held.ticker} @ {args.price:,.2f}. Now {held.shares:g} sh, "
                  f"avg {held.avg_price:,.2f}. Cash {pf.cash:,.2f}")


def cmd_sell(args, paths: Paths, cfg) -> None:
    gain, pf = ops.record_sell(paths, args.ticker, args.shares, args.price, args.fees, on=args.date, note=args.note)
    ticker = args.ticker.strip().upper()
    left = pf.position(ticker)
    console.print(f"Sold {args.shares:g} {ticker} @ {args.price:,.2f} (realised {gain:+,.2f}). "
                  f"{'Now ' + format(left.shares, 'g') + ' sh' if left else 'Position closed'}. Cash {pf.cash:,.2f}")


def cmd_status(args, paths: Paths, cfg) -> None:
    view = ops.portfolio_view(paths)
    total = view["total"]
    table = Table(title=f"Portfolio {date.today().isoformat()} ({view['currency']})")
    for col in ("Ticker", "Shares", "Avg", "Price", "Value", "Gain", "Weight", "Rating", "Days"):
        table.add_column(col, justify="left" if col in ("Ticker", "Rating") else "right")
    for r in view["positions"]:
        table.add_row(r["ticker"], f"{r['shares']:g}", f"{r['avg_price']:,.2f}",
                      f"{r['price']:,.2f}" if r["price"] else "no price", f"{r['value']:,.2f}",
                      f"{r['gain']:+.1%}" if r["gain"] is not None else "–",
                      f"{r['weight']:.1%}" if r["weight"] is not None else "–", r["last_rating"] or "–",
                      str(r["days_held"]))
    table.add_row("Cash", "", "", "", f"{view['cash']:,.2f}", "",
                  f"{view['cash'] / total:.1%}" if total else "–", "", "")
    console.print(table)
    console.print(f"Total value: [bold]{total:,.2f}[/bold] {view['currency']}")
    cmp = view["comparison"]
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
    only = args.tickers.split(",") if getattr(args, "tickers", None) else None
    out = review.run(paths, cfg, yes=args.yes, dry_run=args.dry_run, only=only)
    console.print(Markdown(out.read_text(encoding="utf-8")))
    console.print(f"\nSaved {out} and {out.with_suffix('.html')}")


def cmd_superinvestors(args, paths: Paths, cfg) -> None:
    console.print("Reading the latest 13F of each tracked investor (the first run maps every holding; a few minutes)...")
    data = superinvestors.refresh_if_stale(paths, cfg["superinvestors"], force=args.force)
    for inv in data["investors"]:
        console.print(f"{inv['name']}: {inv['holdings']} holdings, quarter ending {inv['period']} (filed {inv['filed']})")
    for err in data.get("errors", []):
        console.print(f"[red]{err}[/red]")
    shared = sorted(((len(h), t) for t, h in data["holdings"].items() if len(h) > 1), reverse=True)[:15]
    if shared:
        console.print("Held by more than one: " + ", ".join(f"{t} ({n})" for n, t in shared))


def cmd_watch(args, paths: Paths, cfg) -> None:
    found, out = watch.run(paths, cfg, notify=not args.no_notify)
    console.print(Markdown(out.read_text(encoding="utf-8")))
    console.print(f"\nSaved {out}")


def cmd_web(args, paths: Paths, cfg) -> None:
    import threading
    import webbrowser

    import uvicorn

    from value_pipeline.web import create_app

    url = f"http://127.0.0.1:{args.port}"
    console.print(f"Value pipeline website at {url} (this Mac only). Stop with Ctrl+C; "
                  "that also stops a screen or review still running from the Run page.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(create_app(paths), host="127.0.0.1", port=args.port, log_level="warning")


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

    s = sub.add_parser("superinvestors", help="refresh which tracked value investors hold what (13F filings)")
    s.add_argument("--force", action="store_true", help="refresh even if the data is under a week old")
    s.set_defaults(func=cmd_superinvestors)

    s = sub.add_parser("web", help="open the local website (this Mac only)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="don't open the browser")
    s.set_defaults(func=cmd_web)

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
            if name == "review":
                s.add_argument("--tickers", help="analyse only these, comma-separated (e.g. HOG,INGR,FOX)")
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
