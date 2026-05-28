"""
Market data utilities: stock prices, VIX level, earnings dates, market hours.

Data sources:
  - Stock prices  -> Alpaca market data API (same API key, no extra cost)
  - VIX level     -> Yahoo Finance via yfinance  (Alpaca does not carry index data)
  - Earnings dates -> Yahoo Finance via yfinance  (free, no key required)
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional

import pytz
import yfinance as yf
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

import config

logger = logging.getLogger(__name__)

_EASTERN = pytz.timezone('America/New_York')
_OPEN_H,  _OPEN_M  = 9,  30
_CLOSE_H, _CLOSE_M = 16,  0


# ── Stock price ────────────────────────────────────────────────────────────────

def get_current_price(ticker: str, data_client: StockHistoricalDataClient) -> Optional[float]:
    """
    Return the latest bid-ask midpoint for a stock via Alpaca market data.
    Falls back to the ask price alone if the bid is zero (thin markets).
    Returns None if the request fails or the data is unusable.
    """
    try:
        resp = data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=[ticker])
        )
        q = resp[ticker]
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        if ask > 0:
            return round(ask, 2)
        logger.warning("%s: quote has no usable prices (bid=%.2f ask=%.2f)", ticker, bid, ask)
    except Exception as exc:
        logger.error("Price fetch failed for %s: %s", ticker, exc)
    return None


# ── VIX level ──────────────────────────────────────────────────────────────────

def get_vix_level() -> Optional[float]:
    """
    Fetch the most recent VIX closing value from Yahoo Finance (^VIX).

    Alpaca does not provide index / volatility data, so yfinance is used as an
    always-available free source.  The value may be the prior day's close when
    called before 9:30 AM ET, which is fine for our pre-trade gate check.
    """
    try:
        hist = yf.Ticker('^VIX').history(period='2d')
        if not hist.empty:
            return round(float(hist['Close'].iloc[-1]), 2)
        logger.warning("VIX history returned empty DataFrame")
    except Exception as exc:
        logger.error("VIX fetch failed: %s", exc)
    return None


# ── Earnings calendar ──────────────────────────────────────────────────────────

def get_next_earnings_date(ticker: str) -> Optional[date]:
    """
    Return the next scheduled earnings date for a ticker, or None if unknown.

    yfinance.Ticker.calendar returns a dict whose 'Earnings Date' key contains
    a list of Timestamp objects.  We filter for dates >= today and return the
    nearest one.  Older yfinance versions returned a DataFrame; both are handled.
    """
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return None

        # Normalise to a list of date-like objects regardless of yfinance version
        if isinstance(cal, dict):
            raw_dates = cal.get('Earnings Date', [])
        elif hasattr(cal, 'get'):
            raw_dates = cal.get('Earnings Date', [])
        else:
            return None

        today = date.today()
        future = []
        for d in raw_dates:
            if d is None:
                continue
            d_date = d.date() if hasattr(d, 'date') else d
            if d_date >= today:
                future.append(d_date)

        return min(future) if future else None
    except Exception as exc:
        logger.warning("Earnings fetch failed for %s: %s", ticker, exc)
        return None


def is_near_earnings(ticker: str, buffer_days: int) -> bool:
    """
    Return True if the ticker has earnings within `buffer_days` calendar days.

    When the earnings date cannot be determined we err on the side of caution
    and return True so the ticker is skipped — better to miss a trade than to
    hold a short strangle through a gap-move earnings announcement.

    ETFs (e.g. SPY, QQQ) hold baskets of stocks and never report earnings, so
    they are never "near earnings" — return False so they remain tradeable.
    """
    if ticker in config.ETF_TICKERS:
        return False

    next_date = get_next_earnings_date(ticker)
    if next_date is None:
        logger.warning("%s: earnings date unknown — skipping to be safe", ticker)
        return True
    days_away = (next_date - date.today()).days
    logger.info("%s: next earnings in %d day(s) (%s)", ticker, days_away, next_date)
    return days_away <= buffer_days


# ── Market hours ───────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Return True if US equity markets are currently open (Mon–Fri, 9:30–16:00 ET)."""
    now = datetime.now(tz=_EASTERN)
    if now.weekday() >= 5:   # Saturday = 5, Sunday = 6
        return False
    open_dt  = now.replace(hour=_OPEN_H,  minute=_OPEN_M,  second=0, microsecond=0)
    close_dt = now.replace(hour=_CLOSE_H, minute=_CLOSE_M, second=0, microsecond=0)
    return open_dt <= now < close_dt


def minutes_to_close() -> int:
    """
    Return the number of minutes remaining until 4:00 PM ET.
    Returns a negative value when the market is already closed.
    """
    now   = datetime.now(tz=_EASTERN)
    close = now.replace(hour=_CLOSE_H, minute=_CLOSE_M, second=0, microsecond=0)
    return int((close - now).total_seconds() / 60)
