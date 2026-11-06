"""Command line: uv run python -m value_pipeline <command>. Nothing here places trades."""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from value_pipeline import (
    backtest,
    calibration,
    daily,
    market_data,
    momentum,
    operations as ops,
    portfolio as pfmod,
    pullback,
    review,
    screener,
    superinvestors,
    trend,
    watch,
)
from value_pipeline.config import Paths, load_config, parking_ticker

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


trade_line = ops.trade_line          # kept importable from here


def cmd_buy(args, paths: Paths, cfg) -> None:
    try:
        held, pf = ops.record_buy(paths, args.ticker, args.shares, args.price, args.fees, on=args.date,
                                  note=args.note, fx=args.fx)
    except pfmod.TradeError as exc:
        raise SystemExit(str(exc)) from exc
    console.print(ops.last_fill_line(paths))
    console.print(f"Now {held.shares:g} sh, avg {held.avg_price:,.2f}. Cash {pf.cash:,.2f}")


def cmd_sell(args, paths: Paths, cfg) -> None:
    try:
        gain, pf = ops.record_sell(paths, args.ticker, args.shares, args.price, args.fees, on=args.date,
                                   note=args.note, fx=args.fx)
    except pfmod.TradeError as exc:
        raise SystemExit(str(exc)) from exc
    ticker = args.ticker.strip().upper()
    console.print(ops.last_fill_line(paths))
    left = pf.position(ticker)
    console.print(f"Realised {gain:+,.2f}. "
                  f"{'Now ' + format(left.shares, 'g') + ' sh' if left else 'Position closed'}. Cash {pf.cash:,.2f}")


def cmd_status(args, paths: Paths, cfg) -> None:
    view = ops.portfolio_view(paths, parking_ticker=parking_ticker(cfg))
    total = view["total"]
    table = Table(title=f"Portfolio {date.today().isoformat()} ({view['currency']})")
    for col in ("Ticker", "Shares", "Avg", "Price", "Value", "Gain", "Weight", "Rating", "Days"):
        table.add_column(col, justify="left" if col in ("Ticker", "Rating") else "right")
    for r in view["positions"]:
        table.add_row(r["ticker"], f"{r['shares']:g}", f"{r['avg_price']:,.2f}",
                      (f"{r['price']:,.2f}" + (f" {r['currency']}" if r["currency"] not in (None, "USD") else ""))
                      if r["price"] else "no price", f"{r['value']:,.2f}",
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
    console.print("Screening Nordnet's markets (first run fetches every new ticker, roughly 1.5 to 3 hours)...")
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


def cmd_review(args, paths: Paths, cfg, discover: bool = False) -> None:
    if args.candidates is not None:
        cfg["review"]["candidates"] = args.candidates
    only = args.tickers.split(",") if getattr(args, "tickers", None) else None
    out = review.run(paths, cfg, yes=args.yes, dry_run=args.dry_run, only=only, discover=discover)
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


def cmd_daily(args, paths: Paths, cfg) -> int:
    _, out, code = daily.run(paths, cfg, dry_run=args.dry_run, notify=not args.no_notify,
                             max_reviews=args.max)
    console.print(Markdown(out.read_text(encoding="utf-8")))
    console.print(f"\nSaved {out}")
    return code


def cmd_calibration(args, paths: Paths, cfg) -> None:
    rs = calibration.rows(paths)   # fetches closes it has not cached yet
    console.print(Markdown(calibration.markdown(rs, calibration.score(rs), args.horizon)))


def _ints(text: str) -> list[int]:
    try:
        return [int(x) for x in text.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a comma list of whole years: {text}") from exc


def _indexes(text: str) -> list[str]:
    names = list(dict.fromkeys(x.strip().lower() for x in text.split(",") if x.strip()))
    unknown = [n for n in names if n not in market_data.MARKETS]
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"not a comma list of known index lists: {text}")
    return names


def cmd_backtest(args, paths: Paths, cfg) -> None:
    cfg = copy.deepcopy(cfg)
    bt = cfg["backtest"]
    if args.first_year is not None:
        bt["first_year"] = args.first_year
    if args.last_year is not None:
        bt["last_year"] = args.last_year
    if args.holds:
        bt["holds"] = args.holds
    cfg["universe"]["indexes"] = args.indexes
    tickers = [t.strip().upper() for t in (args.tickers or "").split(",") if t.strip()] or None
    console.print(f"Replaying the lane rules on past cohorts of {', '.join(args.indexes)} (the first "
                  "run fetches every ticker's EDGAR statements and Yahoo prices; 20-40 minutes. Later "
                  "runs read the caches, and the four cohorts a year make them about four times longer than "
                  "one a year)...")

    def progress(i: int, n: int, ticker: str) -> None:
        if i % 50 == 0 or i == n:
            console.print(f"{i}/{n} tickers ({ticker})")

    result = backtest.run(paths, cfg, tickers=tickers, refresh=args.refresh,
                          fetch=not args.no_fetch, progress=progress, leavers=args.leavers)
    md, csv = backtest.write(paths, result, result["generated"])
    console.print(Markdown(backtest.verdict(result)))
    console.print(Markdown(backtest.candidates(result)))
    if section := backtest.small_company(result):
        console.print(Markdown(section))
    console.print(f"\nSaved {md} and {csv}")


def _iso_day(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text}") from exc


def cmd_trend_backtest(args, paths: Paths, cfg) -> None:
    cfg = copy.deepcopy(cfg)
    if args.start:
        cfg.setdefault("trend", {})["start"] = args.start
    console.print("Replaying the trend rules on daily closes of today's S&P 500 and S&P 400 members: first "
                  "the lane backtest's cohorts (for the quality universe), then the closes. With cached data "
                  "the lane step's four cohorts a year make it about four times longer than one cohort a "
                  "year did; nothing trades.")

    def progress(i: int, n: int, ticker: str) -> None:
        if i % 100 == 0 or i == n:
            console.print(f"{i}/{n} tickers ({ticker})")

    result = trend.run(paths, cfg, fetch=not args.no_fetch, progress=progress)
    md = trend.write(paths, result)
    console.print(Markdown(trend.render_report(result)))
    console.print(f"\nSaved {md} (and the .html and _trades.csv beside it)", soft_wrap=True)


def cmd_momentum_backtest(args, paths: Paths, cfg) -> None:
    console.print("Replaying the 12-1 momentum rule on daily closes of today's S&P 500 members, over a "
                  "recent period and an earlier hold-out. With the caches this takes a few minutes. "
                  "Nothing trades and no agent is called.")

    def progress(i: int, n: int, ticker: str) -> None:
        if i % 100 == 0 or i == n:
            console.print(f"{i}/{n} tickers ({ticker})")

    result = momentum.run(paths, cfg, fetch=not args.no_fetch, progress=progress)
    md = momentum.write(paths, result)
    console.print(Markdown(momentum.render_report(result)))
    console.print(f"\nSaved {md} (and the .html and _trades.csv beside it)", soft_wrap=True)


def cmd_pullback_backtest(args, paths: Paths, cfg) -> None:
    out, result = pullback.run(paths, args.picks)
    console.print(pullback.verdict(result)["sentence"])
    console.print(f"\nSaved {out}", soft_wrap=True)


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


def cmd_themes(args, paths: Paths, cfg) -> None:
    """The theme scan alone: ideas, the inflection candidates the numbers confirm, the report."""
    from value_pipeline import themes

    names = cfg["lanes"]["themes"]
    if not names:
        raise SystemExit("No themes: name some under lanes.themes in value_pipeline/config.yaml.")
    owned = {p.ticker for p in pfmod.load(paths).positions} if paths.portfolio.exists() else set()
    today = date.today().isoformat()
    rcfg = cfg["review"]
    console.print(f"Scanning {len(names)} theme(s) (one quick model call each)...")
    llm = review.make_graph(rcfg["analysts"], rcfg.get("statement_vendor")).quick_thinking_llm
    ideas = themes.scan(llm, names, today)
    kept, skipped = themes.candidates(paths, cfg, ideas, owned)
    extra = themes.scare_lines(screener.latest_scares(paths))
    out = themes.write_report(paths, today, ideas, kept, skipped, extra=extra)
    console.print(Markdown(out.read_text(encoding="utf-8")))
    console.print(f"\nSaved {out}")


def cmd_run_all(args, paths: Paths, cfg) -> None:
    cmd_screen(args, paths, cfg)
    cmd_review(args, paths, cfg, discover=True)


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
        s.add_argument("--fees", type=float, default=0.0, help="in dollars")
        s.add_argument("--fx", type=float, default=None,
                       help="dollars per quoted unit actually got (default: today's market rate)")
        s.add_argument("--date", type=_date, default=today)
        s.add_argument("--note", default="")
        s.set_defaults(func=func)

    s = sub.add_parser("status", help="holdings, value, gain and return vs SPY")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("watch", help="free weekly check: prices vs values, FREEFALL, buy range (no agents)")
    s.add_argument("--no-notify", action="store_true", help="don't show a macOS notification")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("daily", help="weekday check: re-run the agents on names whose triggers fired")
    s.add_argument("--dry-run", action="store_true", help="list the triggers, re-run nothing")
    s.add_argument("--no-notify", action="store_true", help="don't show a macOS notification")
    s.add_argument("--max", type=int, help="most reviews to run today (default daily.max_reviews)")
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("superinvestors", help="refresh which tracked value investors hold what (13F filings)")
    s.add_argument("--force", action="store_true", help="refresh even if the data is under a week old")
    s.set_defaults(func=cmd_superinvestors)

    s = sub.add_parser("themes", help="scan the themes in lanes.themes for inflection candidates (no review)")
    s.set_defaults(func=cmd_themes)

    s = sub.add_parser("calibration", help="past decisions against the prices that followed (no agents)")
    s.add_argument("--horizon", type=int, choices=calibration.HORIZONS, default=180,
                   help="days after the review for the table's return (default 180)")
    s.set_defaults(func=cmd_calibration)

    s = sub.add_parser("backtest", help="the lane rules replayed on past cohorts against SPY (no agents)")
    s.add_argument("--from", dest="first_year", type=int, help="first cohort year (default backtest.first_year)")
    s.add_argument("--to", dest="last_year", type=int, help="last cohort year (default backtest.last_year)")
    s.add_argument("--holds", type=_ints, help="holds in years, comma-separated (default 1,3)")
    s.add_argument("--tickers", help="only these, comma-separated (a smoke run)")
    s.add_argument("--indexes", type=_indexes, default=list(backtest.DEFAULT_INDEXES),
                   help="index lists, comma-separated (default sp500,sp400; sp500 alone is the "
                        "survivorship check, saved as backtest_<date>_sp500.md)")
    s.add_argument("--refresh", action="store_true", help="refetch the cached statements and prices")
    s.add_argument("--no-fetch", action="store_true", help="read the statement and price caches only")
    s.add_argument("--leavers", action="store_true",
                   help="also replay the names that left the S&P 500 until their leave date "
                        "(saved as backtest_<date>_leavers.md)")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("trend-backtest",
                       help="Turtle-style breakouts and 12-1 momentum replayed on daily closes (no agents)")
    s.add_argument("--from", dest="start", type=_iso_day, help="first day, YYYY-MM-DD (default trend.start)")
    s.add_argument("--no-fetch", action="store_true", help="read the statement and price caches only")
    s.set_defaults(func=cmd_trend_backtest)

    s = sub.add_parser("momentum-backtest",
                       help="12-1 momentum on today's S&P 500 members, two periods, fixed verdict (no agents)")
    s.add_argument("--no-fetch", action="store_true", help="read the statement and price caches only")
    s.set_defaults(func=cmd_momentum_backtest)

    s = sub.add_parser("pullback-backtest",
                       help="what waiting for a dip would have done to the backtest's picks, fixed verdict (no agents)")
    s.add_argument("--picks", type=Path, help="a backtest picks CSV (default the newest default-universe one)")
    s.set_defaults(func=cmd_pullback_backtest)

    s = sub.add_parser("web", help="open the local website (this Mac only)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="don't open the browser")
    s.set_defaults(func=cmd_web)

    for name, func in (("screen", cmd_screen), ("review", cmd_review), ("run-all", cmd_run_all)):
        s = sub.add_parser(name, help={"screen": "rank the S&P 500 + 400 by the Magic Formula",
                                       "review": "run the agents on holdings + top candidates; write ACTIONS.md",
                                       "run-all": "screen, scan the themes, then review"}[name])
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
        code = args.func(args, paths, load_config())
    except (pfmod.TradeError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    if code:
        raise SystemExit(code)
