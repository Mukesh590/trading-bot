"""
Call Credit Spread + Cash-Secured Put Income Bot
=================================================
Opens a defined-risk call credit spread and a cash-secured put on large-cap stocks
to collect time-decay (theta) premium, then exits when a profit target or stop-loss
level is reached.

Strategy rules
--------------
  Tickers        : MSFT, AAPL, AMZN, META
  Call leg       : Sell call ≈ 2.5 % above spot; buy call CALL_SPREAD_WIDTH strikes
                   higher on the same expiry to cap upside risk (credit spread)
  Put  leg       : Sell put  ≈ 2.5 % below current spot (cash-secured)
  DTE window     : 30 – 45 days to expiration
  Max open       : 3 concurrent positions (one per ticker)
  Profit target  : Close when >= 50 % of max profit is captured (up to 70 % ideal)
  Stop loss      : Close entire position if call spread value or put value exceeds
                   entry credit * 1.10
  VIX gate       : No new trades when VIX >= 40; half position size when VIX >= 30
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
      5. Set contract quantity (halved when VIX >= VIX_HALF_SIZE).
      6. Submit three orders (sell short call, buy long call, sell put).
         If any order fails, roll back all submitted legs to avoid partial exposure.
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

    # Step 5 — position size
    qty = config.REDUCED_CONTRACTS if vix >= config.VIX_HALF_SIZE else config.NORMAL_CONTRACTS

    # Step 6 — submit all three legs
    short_call_ok = _sell_to_open(short_call.symbol,   qty)
    long_call_ok  = _buy_to_open(long_call.symbol,     qty)
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


# ── Main strategy cycle ────────────────────────────────────────────────────────

def run_cycle() -> None:
    """
    Execute one full strategy iteration.

    Order of operations:
      1. Verify the market is currently open.
      2. Fetch VIX — skip new entries if too high; manage existing either way.
      3. Manage open positions (profit targets and stops).
      4. Open new strangles if capacity allows and conditions are met.
         • One strangle per ticker maximum.
         • Respect the MAX_STRANGLES global cap.
         • Skip tickers near earnings.
         • Stop new entries CLOSE_BUFFER_MIN minutes before market close.
    """
    if not market_data.is_market_open():
        logger.debug("Market closed — no action this cycle")
        return

    mins_left = market_data.minutes_to_close()
    logger.info("=== Strategy cycle  (%d min to close,  open=%d/%d) ===",
                mins_left, len(open_strangles), config.MAX_STRANGLES)

    # ── VIX gate ───────────────────────────────────────────────────────────────
    vix = market_data.get_vix_level()
    if vix is None:
        logger.warning("VIX unavailable — managing existing positions only this cycle")
        manage_positions()
        return

    logger.info("VIX = %.2f", vix)

    if vix >= config.VIX_NO_TRADE:
        logger.info(
            "VIX %.2f is at or above the no-trade threshold (%.0f) — "
            "no new entries; managing existing positions only",
            vix, config.VIX_NO_TRADE,
        )
        manage_positions()
        return

    # ── Manage open positions ──────────────────────────────────────────────────
    # Snapshot tickers BEFORE manage_positions() so that a position closed by
    # the stop-loss this cycle cannot be reopened in the same cycle.
    tickers_with_positions = {s['ticker'] for s in open_strangles.values()}
    manage_positions()

    # ── New entries ────────────────────────────────────────────────────────────
    # Stop opening positions in the last CLOSE_BUFFER_MIN minutes of the session;
    # we still continue to manage existing ones above.
    if mins_left <= config.CLOSE_BUFFER_MIN:
        logger.info(
            "Within %d minutes of market close — no new entries this cycle",
            config.CLOSE_BUFFER_MIN,
        )
        return

    available_slots = config.MAX_STRANGLES - len(open_strangles)
    if available_slots <= 0:
        logger.info("At capacity (%d/%d strangles) — no new entries",
                    len(open_strangles), config.MAX_STRANGLES)
        return

    logger.info("%d slot(s) open for new strangles", available_slots)

    now_utc = datetime.now(tz=timezone.utc)

    for ticker in config.WATCH_LIST:
        if len(open_strangles) >= config.MAX_STRANGLES:
            break

        if ticker in tickers_with_positions:
            logger.info("%s: already has an open strangle — skipping", ticker)
            continue

        # ── Cooldown check ─────────────────────────────────────────────────────
        cooldown_until = _ticker_cooldowns.get(ticker)
        if cooldown_until and now_utc < cooldown_until:
            logger.info(
                "%s: cooldown active until %s UTC — skipping",
                ticker, cooldown_until.strftime('%Y-%m-%d %H:%M'),
            )
            continue

        # ── Earnings check ─────────────────────────────────────────────────────
        # Never hold a short strangle through an earnings announcement — the
        # implied volatility crush before earnings looks good for sellers, but
        # the gap risk on the announcement day can far exceed the premium collected.
        if market_data.is_near_earnings(ticker, config.EARNINGS_BUFFER_DAYS):
            logger.info("%s: skipped — earnings within %d days", ticker, config.EARNINGS_BUFFER_DAYS)
            continue

        logger.info("Attempting to open strangle on %s (VIX=%.2f)", ticker, vix)
        open_strangle(ticker, vix)

    logger.info("=== Cycle end — open strangles: %d/%d ===",
                len(open_strangles), config.MAX_STRANGLES)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 65)
    logger.info("Call Credit Spread + Cash-Secured Put Bot — PAPER TRADING")
    logger.info("  Watchlist    : %s", ', '.join(config.WATCH_LIST))
    logger.info("  DTE window   : %d – %d days", config.MIN_DTE, config.MAX_DTE)
    logger.info("  Call leg     : Sell %.1f%% OTM call; buy %d strikes higher (spread)",
                config.CALL_OTM_PCT * 100, config.CALL_SPREAD_WIDTH)
    logger.info("  Put leg      : Sell %.1f%% OTM put (cash-secured)", config.PUT_OTM_PCT * 100)
    logger.info("  Max positions: %d", config.MAX_STRANGLES)
    logger.info("  Profit take  : %.0f%% – %.0f%% of max profit",
                config.PROFIT_TAKE_MIN_PCT * 100, config.PROFIT_TAKE_MAX_PCT * 100)
    logger.info("  Stop loss    : %.0f%% of entry credit per component", config.STOP_LOSS_PCT * 100)
    logger.info("  VIX gates    : no-trade >= %.0f  |  half-size >= %.0f",
                config.VIX_NO_TRADE, config.VIX_HALF_SIZE)
    logger.info("  Trade log    : %s", config.TRADE_LOG_FILE)
    logger.info("=" * 65)

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
