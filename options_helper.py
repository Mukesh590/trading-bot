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
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
) -> List:
    """
    Return a list of active option contracts for one side of the chain.

    min_strike / max_strike narrow the search to a dollar window around the
    target strike.  Without these filters the Alpaca API returns the first N
    contracts sorted by strike ascending.  For high-priced ETFs (SPY ~$590,
    QQQ ~$510) this can mean the 2.5%-OTM short call target (~$605) falls
    outside the returned page — the bot then selects the highest available
    strike as "nearest," which may be $20-30 below spot rather than above.

    Handles both list and response-object return types across alpaca-py versions.
    """
    kwargs: dict = dict(
        underlying_symbols=[ticker],
        expiration_date_gte=min_exp,
        expiration_date_lte=max_exp,
        type=contract_type,
        status='active',
    )
    if min_strike is not None:
        kwargs['strike_price_gte'] = str(round(min_strike, 2))
    if max_strike is not None:
        kwargs['strike_price_lte'] = str(round(max_strike, 2))

    req = GetOptionContractsRequest(**kwargs)
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
    Return the hedge (long) leg of the call credit spread: the call on the same
    expiration date whose strike is closest to CALL_SPREAD_WIDTH_DOLLARS above
    the short-call strike.

    Using a dollar width rather than a fixed strike count makes the spread width
    consistent across underlyings with different strike spacing (SPY/QQQ at $1,
    META at $5).  Any strike strictly above the short call qualifies, so this
    only returns None when there is no higher strike at all in the chain.
    """
    same_expiry = [c for c in calls if c.expiration_date == short_call.expiration_date]
    short_strike = float(short_call.strike_price)
    target_strike = short_strike + config.CALL_SPREAD_WIDTH_DOLLARS
    higher = [c for c in same_expiry if float(c.strike_price) > short_strike]
    if not higher:
        return None
    return min(higher, key=lambda c: abs(float(c.strike_price) - target_strike))


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
      • Long  call        = strike closest to CALL_SPREAD_WIDTH_DOLLARS above the
                            short call, same expiration (caps the upside risk)
      • Put   target      = current_price * (1 - PUT_OTM_PCT)
      • All contracts must expire between MIN_DTE and MAX_DTE days from today

    Returns (short_call, long_call, put_contract) or None if no suitable set exists.
    """
    call_target = round(current_price * (1 + config.CALL_OTM_PCT), 2)
    put_target  = round(current_price * (1 - config.PUT_OTM_PCT),  2)
    min_exp, max_exp = _expiry_window()

    # Strike search windows — keep the API query tight so the correct strikes
    # are always within the returned page even for high-priced ETFs.
    buf = config.STRIKE_SEARCH_BUFFER
    call_min = call_target - buf
    call_max = call_target + config.CALL_SPREAD_WIDTH_DOLLARS + buf
    put_min  = put_target  - buf
    put_max  = put_target  + buf

    logger.info(
        "%s  spot=%.2f  short_call_target=%.2f [%.2f-%.2f]  "
        "put_target=%.2f [%.2f-%.2f]  exp=[%s -> %s]",
        ticker, current_price,
        call_target, call_min, call_max,
        put_target,  put_min,  put_max,
        min_exp, max_exp,
    )

    try:
        calls = _fetch_chain(ticker, 'call', min_exp, max_exp, trading_client,
                             min_strike=call_min, max_strike=call_max)
        puts  = _fetch_chain(ticker, 'put',  min_exp, max_exp, trading_client,
                             min_strike=put_min,  max_strike=put_max)
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
            "%s: could not find any call strike above %.2f (~$%d-wide target) — "
            "chain may not have higher strikes in this DTE window",
            ticker, float(short_call.strike_price), config.CALL_SPREAD_WIDTH_DOLLARS,
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


def find_csp_contract(
    ticker: str,
    current_price: float,
    trading_client: TradingClient,
) -> Optional[object]:
    """
    Find the put contract for a cash-secured put at CSP_OTM_PCT below spot,
    in the standard 30-45 DTE window.
    Returns the single best contract or None.
    """
    put_target = round(current_price * (1 - config.CSP_OTM_PCT), 2)
    min_exp, max_exp = _expiry_window()
    buf = config.STRIKE_SEARCH_BUFFER

    logger.info(
        "%s CSP: spot=%.2f  put_target=%.2f [%.2f-%.2f]  exp=[%s -> %s]",
        ticker, current_price, put_target,
        put_target - buf, put_target + buf, min_exp, max_exp,
    )

    try:
        puts = _fetch_chain(ticker, 'put', min_exp, max_exp, trading_client,
                            min_strike=put_target - buf, max_strike=put_target + buf)
    except Exception as exc:
        logger.error("CSP chain fetch failed for %s: %s", ticker, exc)
        return None

    if not puts:
        logger.warning("%s: empty put chain for CSP — options may not be enabled", ticker)
        return None

    contract = _nearest_strike(puts, put_target)
    if contract:
        logger.info("%s CSP selected -> %s  strike=%.2f  exp=%s",
                    ticker, contract.symbol, float(contract.strike_price),
                    contract.expiration_date)
    return contract


def find_put_for_expiry(
    ticker: str,
    current_price: float,
    expiry,                       # 'YYYY-MM-DD' string or datetime.date
    trading_client: TradingClient,
) -> Optional[object]:
    """
    Find a put at PUT_OTM_PCT below spot on a SPECIFIC expiration date.

    Used to backfill the put leg of a spread-only strangle so the put shares the
    call spread's expiration, completing a proper strangle.  Returns the best
    contract on that expiry or None.
    """
    put_target = round(current_price * (1 - config.PUT_OTM_PCT), 2)
    exp_date = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry))
    buf = config.STRIKE_SEARCH_BUFFER

    logger.info(
        "%s PUT backfill: spot=%.2f  put_target=%.2f [%.2f-%.2f]  exp=%s",
        ticker, current_price, put_target,
        put_target - buf, put_target + buf, exp_date,
    )

    try:
        puts = _fetch_chain(ticker, 'put', exp_date, exp_date, trading_client,
                            min_strike=put_target - buf, max_strike=put_target + buf)
    except Exception as exc:
        logger.error("Put backfill chain fetch failed for %s: %s", ticker, exc)
        return None

    if not puts:
        logger.warning("%s: no puts found on %s for backfill", ticker, exp_date)
        return None

    contract = _nearest_strike(puts, put_target)
    if contract:
        logger.info(
            "%s PUT backfill selected -> %s  strike=%.2f  exp=%s",
            ticker, contract.symbol, float(contract.strike_price),
            contract.expiration_date,
        )
    return contract


def _cc_expiry_window() -> Tuple[date, date]:
    """Return (min_expiry, max_expiry) for weekly covered calls."""
    today = date.today()
    return today + timedelta(days=config.CC_MIN_DTE), today + timedelta(days=config.CC_MAX_DTE)


def find_covered_call_contract(
    ticker: str,
    current_price: float,
    cost_basis: float,
    trading_client: TradingClient,
) -> Optional[object]:
    """
    Find a call contract for a covered call in the weekly window (CC_MIN_DTE–CC_MAX_DTE).

    Selection rules:
      • Strike >= current_price * (1 + CC_OTM_PCT_MIN)   — at least 3 % OTM
      • Strike >= cost_basis                              — never sell below breakeven
      • Among eligible strikes, pick the one closest to current_price * (1 + CC_OTM_PCT_MAX)

    Returns the best contract or None if no eligible strike exists.
    """
    call_target  = round(current_price * (1 + config.CC_OTM_PCT_MAX), 2)
    call_min_otm = round(current_price * (1 + config.CC_OTM_PCT_MIN), 2)
    min_exp, max_exp = _cc_expiry_window()
    buf = config.STRIKE_SEARCH_BUFFER

    # The strike must clear both the OTM floor and the cost-basis floor.
    min_eligible_strike = max(call_min_otm, cost_basis)

    logger.info(
        "%s CC: spot=%.2f  min_strike=%.2f (otm_floor=%.2f cost_basis=%.2f)  "
        "target=%.2f  exp=[%s -> %s]",
        ticker, current_price, min_eligible_strike, call_min_otm, cost_basis,
        call_target, min_exp, max_exp,
    )

    try:
        calls = _fetch_chain(ticker, 'call', min_exp, max_exp, trading_client,
                             min_strike=min_eligible_strike - buf,
                             max_strike=call_target + buf)
    except Exception as exc:
        logger.error("CC chain fetch failed for %s: %s", ticker, exc)
        return None

    if not calls:
        logger.warning("%s: empty call chain in weekly window", ticker)
        return None

    candidates = [c for c in calls if float(c.strike_price) >= min_eligible_strike]
    if not candidates:
        logger.warning(
            "%s: no call strikes >= %.2f in weekly window (cost_basis=%.2f)",
            ticker, min_eligible_strike, cost_basis,
        )
        return None

    contract = min(candidates, key=lambda c: abs(float(c.strike_price) - call_target))
    logger.info(
        "%s CC selected -> %s  strike=%.2f  exp=%s",
        ticker, contract.symbol, float(contract.strike_price), contract.expiration_date,
    )
    return contract


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
