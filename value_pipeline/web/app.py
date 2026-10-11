"""The local website: FastAPI routes over the pipeline's own functions. It never trades."""

from __future__ import annotations

import secrets
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from value_pipeline import benchmark, history, market_data, operations as ops
from value_pipeline.config import CONFIG_PATH, Paths
from value_pipeline.web import charts, views

TEMPLATES = Path(__file__).parent / "templates"


class FormError(ValueError):
    """A form value that cannot be used; the message is shown to the user."""


def parse_number(text, field: str) -> float:
    try:
        return float(str(text).strip())
    except ValueError:
        raise FormError(f"{field}: '{text}' is not a number (use a dot for decimals, e.g. 1.5)") from None


def _markdown(text: str) -> str:
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def create_app(paths: Paths, *, config_path: Path = CONFIG_PATH, price_fn=market_data.latest_price,
               series_fn=history.fetch_value_series, benchmark_fn=benchmark.compare, runner=None) -> FastAPI:
    app = FastAPI(title="Value pipeline", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.paths, app.state.config_path = paths, config_path
    app.state.price_fn, app.state.series_fn, app.state.benchmark_fn = price_fn, series_fn, benchmark_fn
    app.state.runner = runner
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

    return app
