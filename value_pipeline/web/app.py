"""The local website: FastAPI routes over the pipeline's own functions. It never trades."""

from __future__ import annotations

import math
import secrets
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote_plus

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from value_pipeline import (
    benchmark,
    history,
    market_data,
    operations as ops,
    portfolio as pfmod,
    review as reviewmod,
)
from value_pipeline.config import CONFIG_PATH, Paths, load_config, validate_config
from value_pipeline.web import charts, views
from value_pipeline.web.jobs import JobBusy, JobRunner

TEMPLATES = Path(__file__).parent / "templates"


class FormError(ValueError):
    """A form value that cannot be used; the message is shown to the user."""


def parse_number(text, field: str) -> float:
    try:
        value = float(str(text).strip())
    except ValueError:
        raise FormError(f"{field}: '{text}' is not a number (use a dot for decimals, e.g. 1.5)") from None
    if not math.isfinite(value):
        raise FormError(f"{field}: '{text}' is not a usable number")
    return value


def _markdown(text: str) -> str:
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def _default_job_funcs() -> dict:
    """What each Run-page button runs: the same functions as the commands."""
    from value_pipeline import screener, watch

    def owned(paths: Paths) -> set[str]:
        return {p.ticker for p in pfmod.load(paths).positions} if paths.portfolio.exists() else set()

    return {
        "screen": lambda paths, cfg, **kw: screener.run(paths, cfg, owned(paths)),
        "review": lambda paths, cfg, only=None, dry_run=False: reviewmod.run(
            paths, cfg, yes=True, dry_run=dry_run, only=only),
        "watch": lambda paths, cfg, **kw: watch.run(paths, cfg, notify=False),
    }


def create_app(paths: Paths, *, config_path: Path = CONFIG_PATH, price_fn=market_data.latest_price,
               series_fn=history.fetch_value_series, benchmark_fn=benchmark.compare, runner=None,
               job_funcs: dict | None = None, allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")) -> FastAPI:
    app = FastAPI(title="Value pipeline", docs_url=None, redoc_url=None, openapi_url=None)
    # A hostile page can point its own domain at 127.0.0.1 (DNS rebinding) and then read and
    # post as if it were this site; it cannot make the browser send Host: 127.0.0.1.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.state.paths, app.state.config_path = paths, config_path
    app.state.price_fn, app.state.series_fn, app.state.benchmark_fn = price_fn, series_fn, benchmark_fn
    app.state.runner = runner or JobRunner(paths.root / "logs")
    app.state.job_funcs = job_funcs or _default_job_funcs()
    app.state.token = secrets.token_urlsafe(24)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["markdown"] = _markdown

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.update(request=request, token=app.state.token, msg=request.query_params.get("msg"))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    async def checked_form(request: Request):
        """The posted form, if it carries this run's token: another site open in the browser cannot post here."""
        form = await request.form()
        if not secrets.compare_digest(str(form.get("token", "")), app.state.token):
            raise HTTPException(403, "Missing or wrong form token. Reload the page and try again.")
        return form

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if not paths.portfolio.exists():
            return render(request, "start.html")
        view = ops.portfolio_view(paths, price_fn=app.state.price_fn, benchmark_fn=app.state.benchmark_fn)
        series = app.state.series_fn(paths)
        chart = charts.line_chart([s["date"] for s in series],
                                  {"Portfolio": [s["portfolio"] for s in series],
                                   "SPY, same cash flows": [s["spy"] for s in series]})
        return render(request, "dashboard.html", view=view, chart=chart, watch=views.latest_watch(paths),
                      next_review=views.next_review_date(date.today()))

    @app.post("/start")
    async def start(request: Request):
        form = await checked_form(request)
        try:
            cash = parse_number(form.get("cash", ""), "Cash")
            ops.init_portfolio(paths, cash, str(form.get("currency") or "USD").strip().upper(),
                               on=str(form.get("date") or "") or None)
        except (FormError, ops.PortfolioExists, ValueError) as exc:
            return render(request, "start.html", status=400, error=str(exc))
        return RedirectResponse("/?msg=Portfolio+started", status_code=303)

    def trade_page(request: Request, status: int = 200, error: str | None = None,
                   values: dict | None = None, posted: str | None = None):
        if not paths.portfolio.exists():
            return render(request, "start.html")
        values = values or {}
        pf = pfmod.load(paths)
        ticker = (request.query_params.get("ticker") or values.get("ticker") or "").strip().upper()
        info = None
        if ticker:
            info = {"ticker": ticker, "price": app.state.price_fn(ticker),
                    "result": reviewmod.latest_result(paths, ticker), "position": pf.position(ticker)}
        return render(request, "trade.html", status=status, error=error, pf=pf, info=info,
                      values=values, posted=posted)

    @app.get("/trade", response_class=HTMLResponse)
    def trade(request: Request):
        return trade_page(request)

    @app.post("/trade/{kind}")
    async def record(request: Request, kind: str):
        form = await checked_form(request)
        values = {k: str(v) for k, v in form.items() if k != "token"}
        on = values.get("date") or None
        try:
            if on:
                on = date.fromisoformat(on).isoformat()
            if kind in ("buy", "sell"):
                ticker = values.get("ticker", "").strip().upper()
                if not ticker:
                    raise FormError("Ticker is required")
                shares = parse_number(values.get("shares", ""), "Shares")
                price = parse_number(values.get("price", ""), "Price")
                fees = parse_number(values.get("fees") or "0", "Fees")
                note = values.get("note", "")
                if kind == "buy":
                    held, pf = ops.record_buy(paths, ticker, shares, price, fees, on=on, note=note)
                    msg = f"Recorded: bought {shares:g} {ticker} @ {price:,.2f}. Now {held.shares:g} sh; cash {pf.cash:,.2f}"
                else:
                    gain, pf = ops.record_sell(paths, ticker, shares, price, fees, on=on, note=note)
                    msg = f"Recorded: sold {shares:g} {ticker} @ {price:,.2f} (realised {gain:+,.2f}); cash {pf.cash:,.2f}"
            elif kind == "deposit":
                pf = ops.record_deposit(paths, parse_number(values.get("amount", ""), "Amount"), on=on)
                msg = f"Recorded. Cash is now {pf.cash:,.2f}"
            else:
                return HTMLResponse("Unknown trade type", status_code=404)
        except ValueError as exc:      # FormError, TradeError and a malformed date are all ValueErrors
            return trade_page(request, status=400, error=str(exc), values=values, posted=kind)
        return RedirectResponse(f"/trades?msg={quote_plus(msg)}", status_code=303)

    @app.get("/trades", response_class=HTMLResponse)
    def trades(request: Request):
        if not paths.portfolio.exists():
            return render(request, "start.html")
        rows = pfmod.read_trades(paths)
        entries = [{**t, "kind": t["action"], "gain": g, "amount": None}
                   for t, g in zip(rows, ops.realised_gains(rows), strict=True)]
        entries += [{"date": d["date"], "kind": "DEPOSIT" if float(d["amount"]) > 0 else "WITHDRAW",
                     "ticker": "", "shares": "", "price": "", "fees": "", "note": "",
                     "amount": abs(float(d["amount"])), "gain": None} for d in pfmod.load(paths).deposits]
        entries.sort(key=lambda e: e["date"], reverse=True)
        return render(request, "trades.html", entries=entries, undo=ops.undo_info(paths))

    @app.post("/trades/undo")
    async def undo(request: Request):
        await checked_form(request)
        try:
            msg = f"Undone: {ops.undo_last(paths)}"
        except ops.UndoUnavailable as exc:
            msg = str(exc)
        return RedirectResponse(f"/trades?msg={quote_plus(msg)}", status_code=303)

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request):
        return render(request, "reviews.html", reviews=views.reviews_index(paths))

    @app.get("/reports/{rel:path}")
    def report_file(rel: str):
        target = views.safe_report_path(paths, rel)
        if target is None:
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(target)

    @app.get("/stock/{ticker}", response_class=HTMLResponse)
    def stock(request: Request, ticker: str):
        data = views.stock_history(paths, ticker)
        valued = [r for r in reversed(data["reviews"]) if r.get("intrinsic_value")]
        chart = charts.line_chart([r["folder"] for r in valued],
                                  {"Intrinsic value": [r["intrinsic_value"] for r in valued]})
        return render(request, "stock.html", data=data, chart=chart, price=app.state.price_fn(data["ticker"]))

    @app.get("/screen", response_class=HTMLResponse)
    def screen(request: Request):
        return render(request, "screen.html", screen=views.screen_table(paths))

    def tickers_from(form) -> list[str] | None:
        raw = str(form.get("tickers") or "")
        names = [t.strip().upper() for t in raw.replace(" ", ",").split(",") if t.strip()]
        return names or None

    @app.get("/run", response_class=HTMLResponse)
    def run_page(request: Request):
        return render(request, "run.html", current=app.state.runner.current(), jobs=app.state.runner.history())

    @app.get("/run/status")
    def run_status():
        from fastapi.responses import JSONResponse

        cur = app.state.runner.current()
        log = app.state.runner.log_text(cur["name"]) if cur else None
        return JSONResponse({"current": cur, "log_tail": (log or "")[-6000:]})

    @app.get("/run/log/{name}", response_class=HTMLResponse)
    def run_log(request: Request, name: str):
        text = app.state.runner.log_text(name)
        if text is None:
            return HTMLResponse("Not found", status_code=404)
        return render(request, "run.html", current=None, jobs=app.state.runner.history(), log=text)

    @app.post("/run/preview", response_class=HTMLResponse)
    async def run_preview(request: Request):
        form = await checked_form(request)
        if not paths.portfolio.exists():
            return render(request, "start.html")
        only = tickers_from(form)
        plan = reviewmod.preview(paths, load_config(app.state.config_path), only)
        return render(request, "run_preview.html", plan=plan, tickers=",".join(only or []),
                      dry_run=form.get("dry_run") == "yes")

    @app.post("/run/start")
    async def run_start(request: Request):
        form = await checked_form(request)
        kind = str(form.get("kind"))
        func = app.state.job_funcs.get(kind)
        if func is None:
            return HTMLResponse("Unknown job", status_code=404)
        cfg = load_config(app.state.config_path)
        kw = {}
        if kind == "review":
            if form.get("confirm") != "yes":
                return RedirectResponse("/run?msg=Preview+and+confirm+the+review+first", status_code=303)
            only, dry = tickers_from(form), form.get("dry_run") == "yes"
            plan = reviewmod.preview(paths, cfg, only)
            if plan["blocked"] and not dry:
                msg = (f"Estimated cost ${plan['estimate']:,.2f} exceeds max_cost_per_review "
                       f"${plan['limit']:,.2f}; nothing was started")
                return RedirectResponse(f"/run?msg={quote_plus(msg)}", status_code=303)
            kw = {"only": only, "dry_run": dry}
        try:
            app.state.runner.start(kind, lambda: func(paths, cfg, **kw))
        except JobBusy as exc:
            return RedirectResponse(f"/run?msg={quote_plus(str(exc))}", status_code=303)
        return RedirectResponse(f"/run?msg={quote_plus(kind.capitalize() + ' started')}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        cfg_path = app.state.config_path
        text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
        return render(request, "settings.html", text=text, path=str(cfg_path))

    @app.post("/settings")
    async def save_settings(request: Request):
        form = await checked_form(request)
        text = str(form.get("text", "")).replace("\r\n", "\n")
        cfg_path = app.state.config_path
        try:
            parsed = yaml.safe_load(text) if text.strip() else {}
            if not isinstance(parsed, dict):
                raise ValueError("the file must be a set of 'section: values' entries")
            check = cfg_path.with_suffix(".yaml.check")
            check.write_text(text, encoding="utf-8")
            try:
                validate_config(load_config(check))
            finally:
                check.unlink(missing_ok=True)
        except (yaml.YAMLError, ValueError, TypeError) as exc:
            return render(request, "settings.html", status=400, text=text, path=str(cfg_path),
                          error=f"Settings not saved: not valid ({exc})")
        if cfg_path.exists():
            paths.backups.mkdir(parents=True, exist_ok=True)
            (paths.backups / f"config_{datetime.now():%Y%m%d_%H%M%S_%f}.yaml").write_text(
                cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        cfg_path.write_text(text, encoding="utf-8")
        return RedirectResponse("/settings?msg=Settings+saved", status_code=303)

    return app
