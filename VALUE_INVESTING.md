# Value investing fork

This fork turns TradingAgents from a short-term trading desk into a long-term **quality value** investor. It combines:

- **Good Stocks Cheap / Buffett-Munger**: a good business, run by good management, at a cheap price.
- **Greenblatt's Magic Formula**: high return on capital (EBIT / (net working capital + net fixed assets)) plus a high earnings yield (EBIT / EV).
- **Graham**: margin of safety against a conservative intrinsic value estimate.
- **Piotroski F-Score / Altman Z-Score**: financial health checks against value traps and distress.
- **GARP (PEG)**: for growing companies, price relative to growth.
- **Momentum as a timing filter only**: avoid buying in a steep downtrend; never buy because of momentum.

Only prompt text changed. The graph, tools, state keys, output section labels (Rating / Recommendation / Action / Executive Summary / Investment Thesis / Rationale / Strategic Actions / Entry Price / Stop Loss / Position Sizing) and the five rating words are untouched, so parsers, schemas and reports work as before.

## What each agent now does

| Agent | Before | Now |
|---|---|---|
| Fundamentals analyst | Past-week fundamentals summary | 5-10 years of annual statements (plus quarterly). Answers *Good business?* (ROIC, ROE, margin stability, FCF vs net income, moat), *Good management?* (buybacks vs dilution, insider activity, SBC), *Cheap?* (EBIT/EV, FCF yield, P/E, EV/EBIT, P/B, intrinsic value range, margin of safety, PEG). Safety checks: F-Score (each point), Z-Score, debt/equity, interest coverage, current ratio, accounting red flags. Ends with a scorecard table. |
| News analyst | Past-week news and macro for traders | 6-12 months of news: management integrity, competitive position, and *why the stock is cheap* (temporary vs permanent). Macro only where it changes long-term earnings power. |
| Market analyst | Picks up to 8 technical indicators | **Trend checker only.** 50/200-day SMA, 12-1 month return, distance from 52-week high/low. Classifies **UPTREND / BASING / DOWNTREND / FREEFALL**. No trade levels, no buy ideas. |
| Bull researcher | Growth and positive indicators | **Value Bull**: good business + good management + price well below intrinsic value; why the cheapness is temporary; 3-5 year compounding. No momentum or social-sentiment arguments. |
| Bear researcher | Risks and negative indicators | **Value-Trap Hunter**: cheap for a reason, or not actually cheap (declining returns, fading moat, distress, dilution, accounting flags, optimistic valuation, FREEFALL/DOWNTREND). |
| Research Manager | Weighs the stronger side | Three tests (business, management, cheap). Buy needs all three plus ≥~30% margin of safety and no distress or accounting flag; Overweight at ~15-30% or in a DOWNTREND; never Buy in FREEFALL. 1-3 year horizon. Strategic Actions = target size, add-below price, thesis breakers. |
| Trader | Entry/stop from ATR and support/resistance | Low-turnover executor. Entry = price that still leaves a margin of safety; stop loss only at a thesis-breaking price; size ≤ ~10% per stock; no tranches or options. Uses the market report only for current price and trend status. |
| Risk analysts (×3) | Personalities unchanged | Each also judges risk as **permanent loss of capital** (business deterioration, overpaying, leverage) over a 1-3 year hold, not short-term volatility. |
| Portfolio Manager | Weighs the risk debate | Same three-test rules; prefers doing nothing, and changes a previous rating only when facts change, not price. The Investment Thesis lists business and management quality, intrinsic value and margin of safety, F/Z-Scores, trend status, main risk, and the sell trigger. |

Both managers keep the rule that conflicting arguments alone are not a reason to Hold (#1321); otherwise every debate would end in Hold.

## Config

- `holding_period_days`: **5 → 126** trading days (about six months). Past decisions are scored by the memory log over a value-investing horizon instead of a week, so a decision only gets its reflection about six months later.

## How to run

Use the market, news and fundamentals analysts, and skip sentiment (social media mood is noise for this strategy):

```bash
tradingagents --ticker AAPL --date 2026-10-09 --analysts market,news,fundamentals --save --no-show
```

or in Python:

```python
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

ta = TradingAgentsGraph(selected_analysts=("market", "news", "fundamentals"), config=DEFAULT_CONFIG.copy())
final_state, rating = ta.propagate("AAPL", "2026-10-09")
```

A deep-thinking model matters more here than usual: the fundamentals analyst computes ratios across many years of statements.
