"""
Options chain helpers: find suitable contracts and fetch live quotes.

All Alpaca options API interaction lives here.  The main bot calls only
find_strangle_contracts() and get_option_midprice() — it never touches the
raw API request/response types directly.

find_strangle_contracts() returns a 3-tuple:
    (short_call, long_call, put)
The call side forms a credit spread (sell short_call, buy long_call);
the put side is a cash-secured put (sell put).
"""

import logging
from datetime import date, timedelta
from typing import List, Optional, Tuple

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest

import config

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _expiry_window() -> Tuple[date, date]:
    """Return (min_expiry, max_expiry) based on the configured DTE range."""
    today = date.today()
    return today + timedelta(days=config.MIN_DTE), today + timedelta(days=config.MAX_DTE)


def _fetch_chain(
    ticker: str,
    contract_type: str,   # 'call' or 'put'
    min_exp: date,
    max_exp: date,
    trading_client: TradingClient,
) -> List:
    """
    Return a list of active option contracts for one side of the chain.

    Handles both list and response-object return types across alpaca-py versions.
    """
    req = GetOptionContractsRequest(
        underlying_symbols=[ticker],
        expiration_date_gte=min_exp,
        expiration_date_lte=max_exp,
        type=contract_type,
        status='active',
    )
    resp = trading_client.get_option_contracts(req)

    # alpaca-py may wrap results in a response object or return them directly
    if hasattr(resp, 'option_contracts'):
        return resp.option_contracts or []
    if isinstance(resp, list):
        return resp
    return []


def _nearest_strike(contracts: list, target_strike: float) -> Optional[object]:
    """Return the contract whose strike price is closest to target_strike."""
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(float(c.strike_price) - target_strike))


def _find_long_call(calls: list, short_call: object) -> Optional[object]:
    """
    Return the call that is CALL_SPREAD_WIDTH strikes above short_call on the
    same expiration date — this is the hedge (long) leg of the call credit spread.
    """
    same_expiry = [c for c in calls if c.expiration_date == short_call.expiration_date]
    short_strike = float(short_call.strike_price)
    higher = sorted(
        [c for c in same_expiry if float(c.strike_price) > short_strike],
        key=lambda c: float(c.strike_price),
    )
    if len(higher) < config.CALL_SPREAD_WIDTH:
        return None
    return higher[config.CALL_SPREAD_WIDTH - 1]


# ── Public API ─────────────────────────────────────────────────────────────────

def find_strangle_contracts(
    ticker: str,
    current_price: float,
    trading_client: TradingClient,
) -> Optional[Tuple[object, object, object]]:
    """
    Find the (short_call, long_call, put) contracts for a call credit spread
    combined with a cash-secured put.

    Selection logic:
      • Short call target = current_price * (1 + CALL_OTM_PCT)
      • Long  call        = CALL_SPREAD_WIDTH strikes above the short call,
                            same expiration (caps the upside risk)
      • Put   target      = current_price * (1 - PUT_OTM_PCT)
      • All contracts must expire between MIN_DTE and MAX_DTE days from today

    Returns (short_call, long_call, put_contract) or None if no suitable set exists.
    """
    call_target = round(current_price * (1 + config.CALL_OTM_PCT), 2)
    put_target  = round(current_price * (1 - config.PUT_OTM_PCT),  2)
    min_exp, max_exp = _expiry_window()

    logger.info(
        "%s  spot=%.2f  short_call_target=%.2f  put_target=%.2f  exp=[%s -> %s]",
        ticker, current_price, call_target, put_target, min_exp, max_exp,
    )

    try:
        calls = _fetch_chain(ticker, 'call', min_exp, max_exp, trading_client)
        puts  = _fetch_chain(ticker, 'put',  min_exp, max_exp, trading_client)
    except Exception as exc:
        logger.error("Contract fetch failed for %s: %s", ticker, exc)
        return None

    if not calls or not puts:
        logger.warning(
            "%s: empty chain returned (calls=%d puts=%d) — "
            "options may not be enabled or the market is closed",
            ticker, len(calls), len(puts),
        )
        return None

    short_call   = _nearest_strike(calls, call_target)
    put_contract = _nearest_strike(puts,  put_target)

    if short_call is None or put_contract is None:
        logger.warning("%s: could not find short call or put in the chain", ticker)
        return None

    long_call = _find_long_call(calls, short_call)
    if long_call is None:
        logger.warning(
            "%s: could not find a long call %d strikes above %.2f — "
            "chain may not have enough strikes in this DTE window",
            ticker, config.CALL_SPREAD_WIDTH, float(short_call.strike_price),
        )
        return None

    logger.info(
        "%s selected -> SHORT_CALL %s %.2f  LONG_CALL %s %.2f  exp=%s  |  PUT %s %.2f  exp=%s",
        ticker,
        short_call.symbol,  float(short_call.strike_price),
        long_call.symbol,   float(long_call.strike_price),
        short_call.expiration_date,
        put_contract.symbol, float(put_contract.strike_price),
        put_contract.expiration_date,
    )
    return short_call, long_call, put_contract


def get_option_midprice(
    symbol: str,
    data_client: OptionHistoricalDataClient,
) -> Optional[float]:
    """
    Return the bid-ask midpoint for an option contract (per share, not per contract).

    Returns None if the quote is unavailable or both bid and ask are zero.
    Falls back to the ask price alone when the bid is zero (wide/illiquid markets).
    """
    try:
        resp = data_client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=[symbol])
        )
        q = resp[symbol]
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        if ask > 0:
            return round(ask, 4)
        logger.warning("%s: option quote has no prices (bid=%.4f ask=%.4f)", symbol, bid, ask)
    except Exception as exc:
        logger.error("Option quote failed for %s: %s", symbol, exc)
    return None
