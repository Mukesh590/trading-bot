"""
Call Credit Spread + Cash-Secured Put Income Bot
=================================================
Opens a defined-risk call credit spread and a cash-secured put on large-cap stocks
to collect time-decay (theta) premium, then exits when a profit target or stop-loss
level is reached.

Strategy rules
--------------
  Tickers        : CCS on META, SPY, QQQ;  CSP/wheel on MSFT, AAPL
  Call leg       : Sell call ≈ 2.5 % above spot; buy call CALL_SPREAD_WIDTH strikes
                   higher on the same expiry to cap upside risk (credit spread)
  Put  leg       : Sell put  ≈ 2.5 % below current spot (cash-secured)
  DTE window     : 30 – 45 days to expiration
  Max open       : 3 concurrent positions GLOBALLY (CCS + CSP + CC combined)
  Profit target  : Close when >= 50 % of max profit is captured (up to 70 % ideal)
  Stop loss      : Close entire position if call spread value or put value exceeds
                   entry credit * 1.10
  VIX gate       : No new trades when VIX >= 40; cap to 1 open position when VIX >= 30
  Earnings gate  : Skip a ticker if earnings fall within the next 7 days
  Schedule       : Every 30 minutes during regular market hours (9:30 – 16:00 ET)

Setup
-----
  1. pip install -r requirements.txt
  2. cp .env.example .env  ->  fill in your Alpaca paper-trading API keys
  3. Enable options trading in your Alpaca paper account settings
  4. python strangle_bot.py

Known limitation
----------------
  The in-memory position tracker (open_strangles) is lost on restart.
  If the bot crashes with open positions, those positions will not be
  automatically managed on the next start — close them manually in Alpaca.
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

import schedule
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient

import config
import market_data
import options_helper
import trade_logger

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.BOT_LOG_FILE),
    ],
)
logger = logging.getLogger('strangle_bot')

load_dotenv()


# ── Alpaca client initialisation ───────────────────────────────────────────────

def _init_clients() -> Tuple[TradingClient, StockHistoricalDataClient, OptionHistoricalDataClient]:
    """
    Build Alpaca API clients from environment variables.
    Exits immediately if credentials are missing — nothing else will work without them.
    """
    api_key    = os.getenv('ALPACA_API_KEY')
    api_secret = os.getenv('ALPACA_API_SECRET')

    if not api_key or not api_secret:
        logger.critical(
            "ALPACA_API_KEY or ALPACA_API_SECRET is not set. "
            "Copy .env.example to .env and fill in your paper-trading credentials."
        )
        sys.exit(1)

    # paper=True routes all orders to the paper trading environment
    trading    = TradingClient(api_key=api_key, secret_key=api_secret, paper=True)
    stock_data = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
    opt_data   = OptionHistoricalDataClient(api_key=api_key, secret_key=api_secret)

    account = trading.get_account()
    logger.info(
        "Connected — paper account #%s  equity=$%.2f  buying_power=$%.2f",
        account.account_number,
        float(account.equity),
        float(account.buying_power),
    )
    return trading, stock_data, opt_data


trading_client, stock_data_client, option_data_client = _init_clients()


# ── In-memory position tracker ─────────────────────────────────────────────────
# Maps a unique position ID -> dict with all data needed to manage the position.
#
# Schema:
#   ticker              str      underlying stock symbol
#   short_call_symbol   str      OCC symbol — short leg of call credit spread
#   long_call_symbol    str      OCC symbol — long (hedge) leg of call credit spread
#   put_symbol          str      OCC symbol — cash-secured put
#   short_call_strike   float    short call strike price
#   long_call_strike    float    long call strike price
#   put_strike          float    put strike price
#   short_call_expiry   str      short call expiration date (YYYY-MM-DD)
#   long_call_expiry    str      long call expiration date (YYYY-MM-DD)
#   put_expiry          str      put expiration date (YYYY-MM-DD)
#   short_call_credit   float    per-share credit received for the short call
#   long_call_debit     float    per-share debit paid for the long call
#   call_spread_credit  float    net credit for the call spread (short - long)
#   put_credit          float    per-share credit received for the put
#   contracts           int      number of contracts per leg (each = 100 shares)
#   vix_at_entry        float    VIX level when the position was opened
#   opened_at           datetime UTC timestamp when the position was opened
open_strangles: Dict[str, dict] = {}
_id_counter = 0

# Per-ticker re-entry block: maps ticker -> UTC datetime before which no new
# strangle may be opened on that ticker.  Set on every close (profit or stop).
_ticker_cooldowns: Dict[str, datetime] = {}

# ── CSP / wheel position trackers ─────────────────────────────────────────────
# open_csps: csp_id -> dict
#   ticker, put_symbol, put_strike, put_expiry, put_credit, contracts,
#   vix_at_entry, opened_at, effective_cost_basis, collateral_reserved
open_csps: Dict[str, dict] = {}

# assigned_shares: ticker -> dict (at most one assignment per ticker at a time)
#   ticker, shares, effective_cost_basis, assigned_at, source_csp_id
assigned_shares: Dict[str, dict] = {}

# open_covered_calls: cc_id -> dict
#   ticker, call_symbol, call_strike, call_expiry, call_credit, contracts,
#   opened_at, effective_cost_basis
open_covered_calls: Dict[str, dict] = {}


def _get_buying_power() -> float:
    """Return current account buying power; returns 0.0 on error."""
    try:
        return float(trading_client.get_account().buying_power)
    except Exception as exc:
        logger.error("Could not fetch buying power: %s", exc)
        return 0.0


def _total_open_positions() -> int:
    """
    Total number of open positions counted GLOBALLY across every strategy:
    call credit spreads (CCS) + cash-secured puts (CSP) + covered calls (CC).

    This is the single source of truth for the MAX_STRANGLES capacity check, so
    that, e.g., two CSPs on MSFT/AAPL plus one CCS already fills all three slots
    and no fourth position of any type can be opened.
    """
    return len(open_strangles) + len(open_csps) + len(open_covered_calls)


def _prune_stale_cooldowns() -> None:
    """
    Drop cooldown entries for tickers no longer in any watchlist.

    After removing a ticker (e.g. AMZN) from the watchlists, any lingering
    cooldown entry for it is meaningless and should not be carried around.
    """
    stale = [t for t in _ticker_cooldowns if t not in config.WATCH_LIST]
    for ticker in stale:
        del _ticker_cooldowns[ticker]
        logger.info("Cleared stale cooldown for de-listed ticker %s", ticker)


def _new_strangle_id(ticker: str) -> str:
    global _id_counter
    _id_counter += 1
    ts = datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')
    return f"{ticker}_{ts}_{_id_counter}"


# ── Order execution ────────────────────────────────────────────────────────────

def _sell_to_open(symbol: str, qty: int) -> bool:
    """
    Submit a market sell-to-open order for an option contract.
    Returns True if Alpaca accepted the order, False otherwise.
    """
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        logger.info("SELL-TO-OPEN  %-35s  qty=%d  order_id=%s", symbol, qty, order.id)
        return True
    except Exception as exc:
        logger.error("SELL-TO-OPEN failed  %s: %s", symbol, exc)
        return False


def _buy_to_close(symbol: str, qty: int) -> bool:
    """
    Submit a market buy-to-close order to exit a short option position.
    Returns True if Alpaca accepted the order, False otherwise.
    """
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        logger.info("BUY-TO-CLOSE  %-35s  qty=%d  order_id=%s", symbol, qty, order.id)
        return True
    except Exception as exc:
        logger.error("BUY-TO-CLOSE failed  %s: %s", symbol, exc)
        return False


def _buy_to_open(symbol: str, qty: int) -> bool:
    """
    Submit a market buy-to-open order for a long option leg (e.g. the call spread hedge).
    Returns True if Alpaca accepted the order, False otherwise.
    """
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        logger.info("BUY-TO-OPEN   %-35s  qty=%d  order_id=%s", symbol, qty, order.id)
        return True
    except Exception as exc:
        logger.error("BUY-TO-OPEN failed  %s: %s", symbol, exc)
        return False


def _sell_to_close(symbol: str, qty: int) -> bool:
    """
    Submit a market sell-to-close order to exit a long option position.
    Returns True if Alpaca accepted the order, False otherwise.
    """
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        logger.info("SELL-TO-CLOSE %-35s  qty=%d  order_id=%s", symbol, qty, order.id)
        return True
    except Exception as exc:
        logger.error("SELL-TO-CLOSE failed  %s: %s", symbol, exc)
        return False


# ── Position reconciliation ────────────────────────────────────────────────────

def _reconcile_positions() -> None:
    """
    Remove strangles from the in-memory tracker if both legs are no longer
    in the Alpaca account (expired, assigned, or closed outside this bot).

    This prevents the bot from trying to manage ghost positions and keeps the
    strangle count accurate for the capacity check.
    """
    if not open_strangles:
        return
    try:
        live_symbols = {p.symbol for p in trading_client.get_all_positions()}
    except Exception as exc:
        logger.error("Could not fetch positions for reconciliation: %s", exc)
        return

    for sid in list(open_strangles.keys()):
        s = open_strangles[sid]
        short_call_live = s['short_call_symbol'] in live_symbols
        long_call_live  = s['long_call_symbol']  in live_symbols
        put_live        = s['put_symbol']         in live_symbols

        if not short_call_live and not long_call_live and not put_live:
            logger.warning(
                "Position %s (%s) is no longer in account — removing from tracker",
                sid, s['ticker'],
            )
            del open_strangles[sid]
        else:
            missing = [
                sym for sym, live in [
                    (s['short_call_symbol'], short_call_live),
                    (s['long_call_symbol'],  long_call_live),
                    (s['put_symbol'],        put_live),
                ] if not live
            ]
            if missing:
                logger.warning(
                    "Position %s (%s): leg(s) %s missing from account — "
                    "may have been assigned or closed manually",
                    sid, s['ticker'], missing,
                )


# ── Strangle open ──────────────────────────────────────────────────────────────

def open_strangle(ticker: str, vix: float) -> bool:
    """
    Attempt to open a call credit spread + cash-secured put on `ticker`.

    Flow:
      1. Fetch the current stock price.
      2. Find the short call, long call (hedge), and put contracts in the 30–45 DTE window.
      3. Get bid-ask midpoints for all three legs.
      4. Verify the call spread yields a positive net credit.
      5. Set contract quantity (always NORMAL_CONTRACTS; elevated VIX limits the
         number of open positions, not the contract count — see run_cycle).
      6. Submit three orders: buy the long call FIRST (and confirm it), then sell
         the short call and the put.  This keeps the short call covered at all
         times.  If any order fails, roll back all submitted legs.
      7. Record the position in memory and write to the CSV log.

    Returns True when all three legs are submitted successfully.
    """
    # Step 1 — stock price
    price = market_data.get_current_price(ticker, stock_data_client)
    if price is None:
        logger.warning("%s: skipping — could not fetch current price", ticker)
        return False

    # Step 2 — find contracts
    triple = options_helper.find_strangle_contracts(ticker, price, trading_client)
    if triple is None:
        return False
    short_call, long_call, put_contract = triple

    # Step 3 — price all three legs
    short_call_credit = options_helper.get_option_midprice(short_call.symbol,    option_data_client)
    long_call_debit   = options_helper.get_option_midprice(long_call.symbol,     option_data_client)
    put_credit        = options_helper.get_option_midprice(put_contract.symbol,  option_data_client)

    if not short_call_credit or not long_call_debit or not put_credit:
        logger.warning(
            "%s: unusable quotes — short_call=%s  long_call=%s  put=%s — skipping",
            ticker, short_call_credit, long_call_debit, put_credit,
        )
        return False

    # Step 4 — verify the spread has a positive net credit
    call_spread_credit = round(short_call_credit - long_call_debit, 4)
    if call_spread_credit <= 0:
        logger.warning(
            "%s: call spread yields no credit (short=%.4f  long=%.4f) — skipping",
            ticker, short_call_credit, long_call_debit,
        )
        return False

    # Step 5 — position size (always full size; elevated VIX limits the NUMBER
    # of open positions instead of shrinking contract count — see run_cycle)
    qty = config.NORMAL_CONTRACTS

    # Step 6 — submit all three legs.
    # ORDER MATTERS: buy the long (hedge) call FIRST and confirm it was accepted
    # before selling the short call.  This guarantees the short call is never
    # left uncovered, which is what triggers the broker's "not eligible to trade
    # uncovered option contracts" rejection.  Abort the short call if the long
    # call did not go through.
    long_call_ok = _buy_to_open(long_call.symbol, qty)
    if not long_call_ok:
        logger.error(
            "%s: long call did not submit — refusing to sell an uncovered short call",
            ticker,
        )
        return False
    short_call_ok = _sell_to_open(short_call.symbol,   qty)
    put_ok        = _sell_to_open(put_contract.symbol, qty)

    if not (short_call_ok and long_call_ok and put_ok):
        logger.error("%s: partial fill — rolling back submitted leg(s) for safety", ticker)
        if short_call_ok:
            _buy_to_close(short_call.symbol,  qty)
        if long_call_ok:
            _sell_to_close(long_call.symbol,  qty)
        if put_ok:
            _buy_to_close(put_contract.symbol, qty)
        return False

    # Step 7 — record
    sid = _new_strangle_id(ticker)
    open_strangles[sid] = {
        'ticker':             ticker,
        'short_call_symbol':  short_call.symbol,
        'long_call_symbol':   long_call.symbol,
        'put_symbol':         put_contract.symbol,
        'short_call_strike':  float(short_call.strike_price),
        'long_call_strike':   float(long_call.strike_price),
        'put_strike':         float(put_contract.strike_price),
        'short_call_expiry':  str(short_call.expiration_date),
        'long_call_expiry':   str(long_call.expiration_date),
        'put_expiry':         str(put_contract.expiration_date),
        'short_call_credit':  short_call_credit,
        'long_call_debit':    long_call_debit,
        'call_spread_credit': call_spread_credit,
        'put_credit':         put_credit,
        'contracts':          qty,
        'vix_at_entry':       vix,
        'opened_at':          datetime.now(tz=timezone.utc),
    }

    # Log as CALL_SPREAD (net credit) + PUT to the CSV
    trade_logger.log_trade(
        action='OPEN', ticker=ticker, leg='CALL_SPREAD',
        symbol=short_call.symbol,
        strike=float(short_call.strike_price),
        expiry=str(short_call.expiration_date),
        contracts=qty, entry_credit=call_spread_credit, vix=vix,
        notes=f"long={long_call.symbol}@{long_call_debit:.4f}",
    )
    trade_logger.log_trade(
        action='OPEN', ticker=ticker, leg='PUT',
        symbol=put_contract.symbol,
        strike=float(put_contract.strike_price),
        expiry=str(put_contract.expiration_date),
        contracts=qty, entry_credit=put_credit, vix=vix,
    )

    net_credit_dollars = (call_spread_credit + put_credit) * qty * 100
    logger.info(
        "Opened %s  %s  qty=%d  "
        "spread=%s/+%s net=%.2f  put=%s@%.2f  total_credit=$%.2f",
        sid, ticker, qty,
        short_call.symbol, long_call.symbol, call_spread_credit,
        put_contract.symbol, put_credit,
        net_credit_dollars,
    )
    return True


# ── Strangle close ─────────────────────────────────────────────────────────────

def _close_strangle(
    sid: str,
    spread_debit: float,
    put_debit: float,
    reason: str,
) -> None:
    """
    Close all three legs of a position, log the result, and remove it from the tracker.

    spread_debit  — net cost to close the call spread (short_call_price - long_call_price).
                    Buy back the short call and sell the long call.
    put_debit     — market price to buy back the put.
    """
    s   = open_strangles[sid]
    qty = s['contracts']

    _buy_to_close(s['short_call_symbol'],  qty)
    _sell_to_close(s['long_call_symbol'],  qty)
    _buy_to_close(s['put_symbol'],         qty)

    trade_logger.log_trade(
        action='CLOSE', ticker=s['ticker'], leg='CALL_SPREAD',
        symbol=s['short_call_symbol'],
        strike=s['short_call_strike'],
        expiry=s['short_call_expiry'],
        contracts=qty, entry_credit=s['call_spread_credit'],
        exit_debit=spread_debit, vix=s['vix_at_entry'], notes=reason,
    )
    trade_logger.log_trade(
        action='CLOSE', ticker=s['ticker'], leg='PUT',
        symbol=s['put_symbol'],
        strike=s['put_strike'],
        expiry=s['put_expiry'],
        contracts=qty, entry_credit=s['put_credit'],
        exit_debit=put_debit, vix=s['vix_at_entry'], notes=reason,
    )

    spread_pnl = (s['call_spread_credit'] - spread_debit) * qty * 100
    put_pnl    = (s['put_credit']          - put_debit)   * qty * 100
    logger.info(
        "Closed %s  reason=%-25s  spread_pnl=$%.2f  put_pnl=$%.2f  total=$%.2f",
        sid, reason, spread_pnl, put_pnl, spread_pnl + put_pnl,
    )

    cooldown_until = datetime.now(tz=timezone.utc) + timedelta(hours=config.TICKER_COOLDOWN_HRS)
    _ticker_cooldowns[s['ticker']] = cooldown_until
    logger.info(
        "%s: cooldown set — no new entry until %s UTC",
        s['ticker'], cooldown_until.strftime('%Y-%m-%d %H:%M'),
    )

    del open_strangles[sid]


# ── Position management ────────────────────────────────────────────────────────

def manage_positions() -> None:
    """
    Check every open position against profit-take and stop-loss thresholds.

    Profit target logic:
      total_credit  = call_spread_credit + put_credit
      total_current = spread_value_now   + put_price
        where spread_value_now = short_call_price - long_call_price
      Profit captured = (total_credit - total_current) / total_credit
      Close when profit_captured >= PROFIT_TAKE_MIN_PCT (50 %)

    Stop-loss logic:
      Call side — close if the spread value exceeds call_spread_credit * (1 + STOP_LOSS_PCT).
      Put  side — close if put_price exceeds put_credit * (1 + STOP_LOSS_PCT).
      Either trigger closes the full position (all three legs).
    """
    _reconcile_positions()

    for sid in list(open_strangles.keys()):
        s = open_strangles[sid]

        short_call_price = options_helper.get_option_midprice(s['short_call_symbol'], option_data_client)
        long_call_price  = options_helper.get_option_midprice(s['long_call_symbol'],  option_data_client)
        put_price        = options_helper.get_option_midprice(s['put_symbol'],        option_data_client)

        if short_call_price is None or long_call_price is None or put_price is None:
            logger.warning("%s: could not price all legs this cycle — skipping", sid)
            continue

        # Do not evaluate the stop-loss until the position has been held long
        # enough to rule out bid-ask noise triggering an immediate exit.
        hold_days = (datetime.now(tz=timezone.utc) - s['opened_at']).days
        if hold_days < config.MIN_HOLD_DAYS:
            logger.info(
                "%s (%s): held %d day(s) — stop-loss evaluation starts after day %d",
                sid, s['ticker'], hold_days, config.MIN_HOLD_DAYS,
            )
            continue

        # Net value of the call spread (cannot be negative — the long call caps it)
        spread_value_now = max(round(short_call_price - long_call_price, 4), 0.0)

        total_credit  = s['call_spread_credit'] + s['put_credit']
        total_current = spread_value_now + put_price
        profit_pct    = (total_credit - total_current) / total_credit

        spread_stop = spread_value_now > s['call_spread_credit'] * (1 + config.STOP_LOSS_PCT)
        put_stop    = put_price        > s['put_credit']         * (1 + config.STOP_LOSS_PCT)

        logger.debug(
            "%s  spread=%.4f/%.4f(stop=%s)  put=%.4f/%.4f(stop=%s)  profit=%.1f%%",
            sid,
            spread_value_now, s['call_spread_credit'], 'Y' if spread_stop else 'n',
            put_price,        s['put_credit'],          'Y' if put_stop    else 'n',
            profit_pct * 100,
        )

        if spread_stop or put_stop:
            triggered_leg = 'CALL_SPREAD' if spread_stop else 'PUT'
            _close_strangle(sid, spread_value_now, put_price, f"STOP_LOSS_{triggered_leg}")

        elif profit_pct >= config.PROFIT_TAKE_MIN_PCT:
            _close_strangle(sid, spread_value_now, put_price, f"PROFIT_TAKE_{profit_pct:.1%}")


# ── CSP open / close ──────────────────────────────────────────────────────────

def open_csp(ticker: str, vix: float) -> bool:
    """
    Sell a cash-secured put on `ticker` at CSP_OTM_PCT (5%) below spot.

    Buying-power check is performed twice:
      1. Against the approximate strike (spot * 0.95) before hitting the API.
      2. Against the actual strike of the contract returned by the chain search.
    Either failure aborts cleanly without submitting any order.
    """
    price = market_data.get_current_price(ticker, stock_data_client)
    if price is None:
        logger.warning("%s CSP: skipping — could not fetch price", ticker)
        return False

    qty = config.NORMAL_CONTRACTS
    approx_strike = round(price * (1 - config.CSP_OTM_PCT), 2)
    required = approx_strike * 100 * qty

    bp = _get_buying_power()
    if bp < required:
        logger.warning(
            "%s CSP: insufficient buying power — need $%.2f, have $%.2f",
            ticker, required, bp,
        )
        return False

    put_contract = options_helper.find_csp_contract(ticker, price, trading_client)
    if put_contract is None:
        return False

    actual_strike = float(put_contract.strike_price)
    actual_required = actual_strike * 100 * qty
    if bp < actual_required:
        logger.warning(
            "%s CSP: actual strike %.2f needs $%.2f collateral, only $%.2f available",
            ticker, actual_strike, actual_required, bp,
        )
        return False

    put_credit = options_helper.get_option_midprice(put_contract.symbol, option_data_client)
    if not put_credit:
        logger.warning("%s CSP: unusable quote — skipping", ticker)
        return False

    if not _sell_to_open(put_contract.symbol, qty):
        return False

    effective_cost_basis = round(actual_strike - put_credit, 4)
    csp_id = f"CSP_{ticker}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    open_csps[csp_id] = {
        'ticker':               ticker,
        'put_symbol':           put_contract.symbol,
        'put_strike':           actual_strike,
        'put_expiry':           str(put_contract.expiration_date),
        'put_credit':           put_credit,
        'contracts':            qty,
        'vix_at_entry':         vix,
        'opened_at':            datetime.now(tz=timezone.utc),
        'effective_cost_basis': effective_cost_basis,
        'collateral_reserved':  actual_required,
    }

    trade_logger.log_trade(
        action='OPEN', ticker=ticker, leg='CSP',
        symbol=put_contract.symbol,
        strike=actual_strike,
        expiry=str(put_contract.expiration_date),
        contracts=qty, entry_credit=put_credit, vix=vix,
        notes=f"cost_basis={effective_cost_basis:.4f} collateral={actual_required:.2f}",
    )
    logger.info(
        "Opened %s  %s  qty=%d  strike=%.2f  credit=%.4f  "
        "cost_basis=%.4f  collateral=$%.2f",
        csp_id, ticker, qty, actual_strike, put_credit,
        effective_cost_basis, actual_required,
    )
    return True


def _close_csp(csp_id: str, put_debit: float, reason: str) -> None:
    """Buy back the short put and remove the CSP from the tracker."""
    s   = open_csps[csp_id]
    qty = s['contracts']

    _buy_to_close(s['put_symbol'], qty)

    trade_logger.log_trade(
        action='CLOSE', ticker=s['ticker'], leg='CSP',
        symbol=s['put_symbol'],
        strike=s['put_strike'],
        expiry=s['put_expiry'],
        contracts=qty, entry_credit=s['put_credit'],
        exit_debit=put_debit, vix=s['vix_at_entry'], notes=reason,
    )
    pnl = (s['put_credit'] - put_debit) * qty * 100
    logger.info("Closed %s  reason=%-25s  pnl=$%.2f", csp_id, reason, pnl)

    cooldown_until = datetime.now(tz=timezone.utc) + timedelta(hours=config.TICKER_COOLDOWN_HRS)
    _ticker_cooldowns[s['ticker']] = cooldown_until
    logger.info("%s: cooldown set until %s UTC",
                s['ticker'], cooldown_until.strftime('%Y-%m-%d %H:%M'))

    del open_csps[csp_id]


def _reconcile_csps() -> None:
    """
    Detect CSP assignment and externally-closed CSPs by comparing the in-memory
    tracker against live Alpaca positions.

    If the put symbol is gone from the account AND the underlying stock now appears
    as a position, we record the assignment in `assigned_shares` and remove the CSP
    from the tracker.  If the put is gone with no stock, the position was either
    closed externally or expired worthless; we remove it from the tracker and log.
    """
    if not open_csps:
        return
    try:
        live = {p.symbol: p for p in trading_client.get_all_positions()}
    except Exception as exc:
        logger.error("Could not fetch positions for CSP reconciliation: %s", exc)
        return

    for csp_id in list(open_csps.keys()):
        s = open_csps[csp_id]
        if s['put_symbol'] in live:
            continue  # still open — nothing to do

        ticker = s['ticker']
        if ticker in live:
            # Shares delivered — assignment
            shares = s['contracts'] * 100
            cost_basis = s['effective_cost_basis']
            logger.info(
                "ASSIGNMENT detected: %s  %d shares  cost_basis=%.4f",
                ticker, shares, cost_basis,
            )
            if ticker not in assigned_shares:
                assigned_shares[ticker] = {
                    'ticker':               ticker,
                    'shares':               shares,
                    'effective_cost_basis': cost_basis,
                    'assigned_at':          datetime.now(tz=timezone.utc),
                    'source_csp_id':        csp_id,
                }
                trade_logger.log_trade(
                    action='ASSIGNED', ticker=ticker, leg='CSP',
                    symbol=s['put_symbol'],
                    strike=s['put_strike'],
                    expiry=s['put_expiry'],
                    contracts=s['contracts'],
                    entry_credit=s['put_credit'],
                    notes=f"cost_basis={cost_basis:.4f} shares={shares}",
                )
        else:
            logger.warning(
                "CSP %s (%s): put gone from account with no shares — "
                "expired worthless or closed externally; removing from tracker",
                csp_id, ticker,
            )

        del open_csps[csp_id]


def manage_csps() -> None:
    """
    Check open CSPs for profit-take and stop-loss; detect assignments via reconciliation.

    Profit target: same 50 % threshold as the CCS strategy.
    Stop-loss:     same 2x-premium threshold (STOP_LOSS_PCT = 2.0).
    Min hold:      stop-loss evaluation deferred until MIN_HOLD_DAYS.
    """
    _reconcile_csps()

    for csp_id in list(open_csps.keys()):
        s = open_csps[csp_id]

        put_price = options_helper.get_option_midprice(s['put_symbol'], option_data_client)
        if put_price is None:
            logger.warning("%s: could not price CSP this cycle — skipping", csp_id)
            continue

        hold_days = (datetime.now(tz=timezone.utc) - s['opened_at']).days
        if hold_days < config.MIN_HOLD_DAYS:
            logger.info(
                "%s (%s): held %d day(s) — stop-loss evaluation starts after day %d",
                csp_id, s['ticker'], hold_days, config.MIN_HOLD_DAYS,
            )
            continue

        profit_pct = (s['put_credit'] - put_price) / s['put_credit']
        put_stop   = put_price > s['put_credit'] * (1 + config.STOP_LOSS_PCT)

        logger.debug(
            "%s  put=%.4f/%.4f(stop=%s)  profit=%.1f%%",
            csp_id, put_price, s['put_credit'], 'Y' if put_stop else 'n',
            profit_pct * 100,
        )

        if put_stop:
            _close_csp(csp_id, put_price, 'STOP_LOSS_CSP')
        elif profit_pct >= config.PROFIT_TAKE_MIN_PCT:
            _close_csp(csp_id, put_price, f"PROFIT_TAKE_{profit_pct:.1%}")


# ── Covered call open / close ──────────────────────────────────────────────────

def open_covered_call(ticker: str) -> bool:
    """
    Sell a covered call against assigned shares of `ticker`.

    Constraints enforced here:
      • Requires an entry in `assigned_shares[ticker]` — aborts if missing.
      • Strike must be >= effective_cost_basis (enforced inside find_covered_call_contract).
      • No buying-power check needed — covered calls are collateralised by the shares.
    """
    if ticker not in assigned_shares:
        logger.error(
            "open_covered_call(%s): called but no assigned shares found — skipping", ticker,
        )
        return False

    asgn      = assigned_shares[ticker]
    shares    = asgn['shares']
    cost_basis = asgn['effective_cost_basis']
    contracts = shares // 100
    if contracts == 0:
        logger.warning("%s: fewer than 100 assigned shares — cannot write CC", ticker)
        return False

    price = market_data.get_current_price(ticker, stock_data_client)
    if price is None:
        logger.warning("%s CC: skipping — could not fetch price", ticker)
        return False

    call_contract = options_helper.find_covered_call_contract(
        ticker, price, cost_basis, trading_client,
    )
    if call_contract is None:
        return False

    call_credit = options_helper.get_option_midprice(call_contract.symbol, option_data_client)
    if not call_credit:
        logger.warning("%s CC: unusable quote — skipping", ticker)
        return False

    if not _sell_to_open(call_contract.symbol, contracts):
        return False

    cc_id = f"CC_{ticker}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    open_covered_calls[cc_id] = {
        'ticker':               ticker,
        'call_symbol':          call_contract.symbol,
        'call_strike':          float(call_contract.strike_price),
        'call_expiry':          str(call_contract.expiration_date),
        'call_credit':          call_credit,
        'contracts':            contracts,
        'opened_at':            datetime.now(tz=timezone.utc),
        'effective_cost_basis': cost_basis,
    }

    trade_logger.log_trade(
        action='OPEN', ticker=ticker, leg='CC',
        symbol=call_contract.symbol,
        strike=float(call_contract.strike_price),
        expiry=str(call_contract.expiration_date),
        contracts=contracts, entry_credit=call_credit,
        notes=f"cost_basis={cost_basis:.4f} shares={shares}",
    )
    logger.info(
        "Opened %s  %s  qty=%d  strike=%.2f  credit=%.4f  cost_basis=%.4f",
        cc_id, ticker, contracts,
        float(call_contract.strike_price), call_credit, cost_basis,
    )
    return True


def _close_covered_call(cc_id: str, call_debit: float, reason: str) -> None:
    """Buy back the short call and remove the CC from the tracker."""
    s   = open_covered_calls[cc_id]
    qty = s['contracts']

    _buy_to_close(s['call_symbol'], qty)

    trade_logger.log_trade(
        action='CLOSE', ticker=s['ticker'], leg='CC',
        symbol=s['call_symbol'],
        strike=s['call_strike'],
        expiry=s['call_expiry'],
        contracts=qty, entry_credit=s['call_credit'],
        exit_debit=call_debit, notes=reason,
    )
    pnl = (s['call_credit'] - call_debit) * qty * 100
    logger.info("Closed %s  reason=%-25s  pnl=$%.2f", cc_id, reason, pnl)

    del open_covered_calls[cc_id]


def _reconcile_covered_calls() -> None:
    """
    Detect covered call assignment (shares called away) by comparing the tracker
    against live Alpaca positions.

    If the call symbol is gone AND the underlying stock is also gone, the shares
    were called away.  We log the wheel completion and remove `assigned_shares[ticker]`.
    If the call is gone but shares remain, the call expired or was closed externally.
    """
    if not open_covered_calls:
        return
    try:
        live = {p.symbol: p for p in trading_client.get_all_positions()}
    except Exception as exc:
        logger.error("Could not fetch positions for CC reconciliation: %s", exc)
        return

    for cc_id in list(open_covered_calls.keys()):
        s = open_covered_calls[cc_id]
        if s['call_symbol'] in live:
            continue  # still open

        ticker = s['ticker']
        if ticker not in live:
            # Shares called away — full wheel cycle complete
            logger.info(
                "CC ASSIGNMENT: %s shares called away at strike=%.2f — wheel complete",
                ticker, s['call_strike'],
            )
            if ticker in assigned_shares:
                asgn = assigned_shares[ticker]
                net_cb = asgn['effective_cost_basis']
                locked_gain = (s['call_strike'] - net_cb) * s['contracts'] * 100
                logger.info(
                    "%s: wheel locked in gain ≈ $%.2f  "
                    "(call_strike=%.2f - cost_basis=%.4f) x %d shares",
                    ticker, locked_gain,
                    s['call_strike'], net_cb, s['contracts'] * 100,
                )
                del assigned_shares[ticker]

            trade_logger.log_trade(
                action='ASSIGNED', ticker=ticker, leg='CC',
                symbol=s['call_symbol'],
                strike=s['call_strike'],
                expiry=s['call_expiry'],
                contracts=s['contracts'],
                entry_credit=s['call_credit'],
                notes='CC_ASSIGNED_wheel_complete',
            )
        else:
            logger.info(
                "CC %s (%s): call gone but shares remain — expired worthless or closed externally",
                cc_id, ticker,
            )

        del open_covered_calls[cc_id]


def manage_covered_calls() -> None:
    """
    Check open covered calls for profit-take and stop-loss.

    Stop-loss on a CC means buying back the short call when it has run against
    us — the shares are still held, so a new CC can be sold next cycle once the
    cooldown period ends.  No per-ticker cooldown is set here because we still own
    the shares and want to keep selling calls against them.
    """
    _reconcile_covered_calls()

    for cc_id in list(open_covered_calls.keys()):
        s = open_covered_calls[cc_id]

        call_price = options_helper.get_option_midprice(s['call_symbol'], option_data_client)
        if call_price is None:
            logger.warning("%s: could not price CC this cycle — skipping", cc_id)
            continue

        hold_days = (datetime.now(tz=timezone.utc) - s['opened_at']).days
        if hold_days < config.MIN_HOLD_DAYS:
            logger.info(
                "%s (%s): held %d day(s) — stop-loss evaluation starts after day %d",
                cc_id, s['ticker'], hold_days, config.MIN_HOLD_DAYS,
            )
            continue

        profit_pct = (s['call_credit'] - call_price) / s['call_credit']
        call_stop  = call_price > s['call_credit'] * (1 + config.STOP_LOSS_PCT)

        logger.debug(
            "%s  call=%.4f/%.4f(stop=%s)  profit=%.1f%%",
            cc_id, call_price, s['call_credit'], 'Y' if call_stop else 'n',
            profit_pct * 100,
        )

        if call_stop:
            _close_covered_call(cc_id, call_price, 'STOP_LOSS_CC')
        elif profit_pct >= config.PROFIT_TAKE_MIN_PCT:
            _close_covered_call(cc_id, call_price, f"PROFIT_TAKE_{profit_pct:.1%}")


# ── Main strategy cycle ────────────────────────────────────────────────────────

def run_cycle() -> None:
    """
    Execute one full strategy iteration across all three sub-strategies.

    Order of operations:
      1. Verify the market is open.
      2. Fetch VIX — skip new entries if too high; always manage existing.
      3. Snapshot open tickers BEFORE management calls so a position closed
         this cycle cannot be reopened in the same cycle.
      4. Manage CCS strangles, CSPs, and covered calls.
      5. Open new positions (CCS on META/SPY/QQQ, then CSPs on MSFT/AAPL, then
         covered calls on assigned shares) while the GLOBAL open-position count
         stays under the cap.  The cap is MAX_STRANGLES normally, or
         VIX_ELEVATED_MAX_POSITIONS when VIX is elevated (>= VIX_ELEVATED).
         Every position type — CCS, CSP and CC — counts toward this single cap,
         so the total can never exceed it.
    """
    if not market_data.is_market_open():
        logger.debug("Market closed — no action this cycle")
        return

    mins_left = market_data.minutes_to_close()
    logger.info(
        "=== Strategy cycle  (%d min to close | "
        "CCS=%d/%d  CSP=%d  CC=%d  assigned=%d) ===",
        mins_left,
        len(open_strangles), config.MAX_STRANGLES,
        len(open_csps),
        len(open_covered_calls),
        len(assigned_shares),
    )

    # ── VIX gate ───────────────────────────────────────────────────────────────
    vix = market_data.get_vix_level()
    if vix is None:
        logger.warning("VIX unavailable — managing existing positions only this cycle")
        manage_positions()
        manage_csps()
        manage_covered_calls()
        return

    logger.info("VIX = %.2f", vix)

    # Snapshot BEFORE any manage call — prevents same-cycle close+reopen.
    tickers_with_strangles    = {s['ticker'] for s in open_strangles.values()}
    tickers_with_csps         = {s['ticker'] for s in open_csps.values()}
    tickers_with_covered_calls = {s['ticker'] for s in open_covered_calls.values()}

    if vix >= config.VIX_NO_TRADE:
        logger.info(
            "VIX %.2f >= no-trade threshold (%.0f) — "
            "no new entries; managing existing positions only",
            vix, config.VIX_NO_TRADE,
        )
        manage_positions()
        manage_csps()
        manage_covered_calls()
        return

    manage_positions()
    manage_csps()
    manage_covered_calls()

    # ── Stop opening new positions near market close ────────────────────────────
    if mins_left <= config.CLOSE_BUFFER_MIN:
        logger.info(
            "Within %d minutes of market close — no new entries this cycle",
            config.CLOSE_BUFFER_MIN,
        )
        return

    now_utc = datetime.now(tz=timezone.utc)

    # ── Global position cap ────────────────────────────────────────────────────
    # A SINGLE cap governs how many positions of ANY type may be open at once.
    # When VIX is elevated (>= VIX_ELEVATED but < VIX_NO_TRADE) we allow only
    # VIX_ELEVATED_MAX_POSITIONS open at once — one position, full size — rather
    # than trading reduced contract counts across several.
    if vix >= config.VIX_ELEVATED:
        position_cap = config.VIX_ELEVATED_MAX_POSITIONS
        logger.info(
            "VIX %.2f >= elevated threshold (%.0f) — capping total open positions at %d",
            vix, config.VIX_ELEVATED, position_cap,
        )
    else:
        position_cap = config.MAX_STRANGLES

    def _at_capacity() -> bool:
        """True when no further positions of any type may be opened this cycle."""
        if _total_open_positions() >= position_cap:
            logger.info(
                "At global position cap (%d/%d open: CCS=%d CSP=%d CC=%d) — no new entries",
                _total_open_positions(), position_cap,
                len(open_strangles), len(open_csps), len(open_covered_calls),
            )
            return True
        return False

    # ── New CCS strangles (META, SPY, QQQ) ────────────────────────────────────
    for ticker in config.CCS_TICKERS:
        if _at_capacity():
            break
        if ticker in tickers_with_strangles:
            logger.info("%s: CCS already open — skipping", ticker)
            continue
        cooldown_until = _ticker_cooldowns.get(ticker)
        if cooldown_until and now_utc < cooldown_until:
            logger.info("%s: CCS cooldown until %s UTC — skipping",
                        ticker, cooldown_until.strftime('%Y-%m-%d %H:%M'))
            continue
        if market_data.is_near_earnings(ticker, config.EARNINGS_BUFFER_DAYS):
            logger.info("%s: near earnings — skipping CCS", ticker)
            continue
        logger.info("Attempting CCS on %s (VIX=%.2f)", ticker, vix)
        open_strangle(ticker, vix)

    # ── New CSPs (MSFT, AAPL) ─────────────────────────────────────────────────
    # CSPs count toward the same global cap as CCS, so an open CSP on MSFT/AAPL
    # consumes a slot that a CCS would otherwise use (and vice versa).
    for ticker in config.CSP_TICKERS:
        if _at_capacity():
            break
        if ticker in tickers_with_csps:
            logger.info("%s: CSP already open — skipping", ticker)
            continue
        cooldown_until = _ticker_cooldowns.get(ticker)
        if cooldown_until and now_utc < cooldown_until:
            logger.info("%s: CSP cooldown until %s UTC — skipping",
                        ticker, cooldown_until.strftime('%Y-%m-%d %H:%M'))
            continue
        if market_data.is_near_earnings(ticker, config.EARNINGS_BUFFER_DAYS):
            logger.info("%s: near earnings — skipping CSP", ticker)
            continue
        logger.info("Attempting CSP on %s (VIX=%.2f)", ticker, vix)
        open_csp(ticker, vix)

    # ── Covered calls on assigned shares ──────────────────────────────────────
    # Covered calls also count toward the global cap.  Note the wheel is
    # self-balancing here: a CSP that gets assigned is removed from open_csps
    # (freeing its slot) before its covered call is written, so monetising
    # assigned shares does not by itself push the total over the cap.
    for ticker in list(assigned_shares.keys()):
        if _at_capacity():
            break
        if ticker in tickers_with_covered_calls:
            logger.info("%s: CC already open — skipping", ticker)
            continue
        logger.info(
            "Attempting CC on assigned %s (cost_basis=%.4f)",
            ticker, assigned_shares[ticker]['effective_cost_basis'],
        )
        open_covered_call(ticker)

    logger.info(
        "=== Cycle end | total=%d/%d (CCS=%d CSP=%d CC=%d)  assigned=%d ===",
        _total_open_positions(), position_cap,
        len(open_strangles), len(open_csps), len(open_covered_calls),
        len(assigned_shares),
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 70)
    logger.info("Wheel Strategy Bot — PAPER TRADING")
    logger.info("  --- Call Credit Spread strategy (CCS) ---")
    logger.info("  Tickers      : %s", ', '.join(config.CCS_TICKERS))
    logger.info("  DTE window   : %d-%d days", config.MIN_DTE, config.MAX_DTE)
    logger.info("  Call spread  : Sell %.1f%% OTM; buy %d strikes higher",
                config.CALL_OTM_PCT * 100, config.CALL_SPREAD_WIDTH)
    logger.info("  Put leg      : Sell %.1f%% OTM", config.PUT_OTM_PCT * 100)
    logger.info("  Max open     : %d (GLOBAL — CCS + CSP + CC combined)", config.MAX_STRANGLES)
    logger.info("  --- Cash-Secured Put + Wheel strategy (CSP) ---")
    logger.info("  Tickers      : %s", ', '.join(config.CSP_TICKERS))
    logger.info("  CSP strike   : %.1f%% OTM  |  DTE %d-%d days",
                config.CSP_OTM_PCT * 100, config.MIN_DTE, config.MAX_DTE)
    logger.info("  CC strike    : %.1f%%-%.1f%% OTM  |  DTE %d-%d days (weekly)",
                config.CC_OTM_PCT_MIN * 100, config.CC_OTM_PCT_MAX * 100,
                config.CC_MIN_DTE, config.CC_MAX_DTE)
    logger.info("  --- Shared parameters ---")
    logger.info("  Profit take  : %.0f%%-%.0f%%  |  Stop loss : %.0fx premium",
                config.PROFIT_TAKE_MIN_PCT * 100, config.PROFIT_TAKE_MAX_PCT * 100,
                config.STOP_LOSS_PCT)
    logger.info("  Min hold     : %d day(s)  |  Cooldown : %d hrs after any close",
                config.MIN_HOLD_DAYS, config.TICKER_COOLDOWN_HRS)
    logger.info("  VIX gates    : no-trade >= %.0f  |  elevated (max %d position) >= %.0f",
                config.VIX_NO_TRADE, config.VIX_ELEVATED_MAX_POSITIONS, config.VIX_ELEVATED)
    logger.info("  Trade log    : %s", config.TRADE_LOG_FILE)
    logger.info("=" * 70)

    # Drop any cooldowns left over for tickers no longer in the watchlists
    # (e.g. AMZN after it was replaced by SPY/QQQ).
    _prune_stale_cooldowns()

    # Run one cycle immediately on startup so we don't wait CHECK_INTERVAL_MIN
    run_cycle()

    # Schedule recurring cycles during market hours
    schedule.every(config.CHECK_INTERVAL_MIN).minutes.do(run_cycle)
    logger.info("Scheduler active — running every %d minutes", config.CHECK_INTERVAL_MIN)

    # Keep the process alive; the scheduler wakes up every 15 seconds
    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == '__main__':
    main()
