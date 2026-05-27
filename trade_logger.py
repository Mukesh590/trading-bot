"""
Append every OPEN / CLOSE event to a CSV file for post-trade analysis.

CSV columns
───────────
  timestamp     UTC ISO-8601 timestamp of the event
  action        OPEN or CLOSE
  ticker        Underlying stock (e.g. AAPL)
  leg           CALL or PUT
  symbol        Full OCC option symbol (e.g. AAPL240119C00195000)
  strike        Strike price in dollars
  expiry        Expiration date (YYYY-MM-DD)
  contracts     Number of contracts traded (each = 100 shares)
  entry_credit  Per-share credit received when the leg was sold to open
  exit_debit    Per-share debit paid to buy back the leg (blank for OPEN rows)
  pnl           Realized P&L in dollars for this leg (blank for OPEN rows)
  vix           VIX level at the time the strangle was originally opened
  notes         Free-text reason for closing (e.g. PROFIT_TAKE_52.3%, STOP_LOSS_CALL)
"""

import csv
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

_FIELDS = [
    'timestamp', 'action', 'ticker', 'leg',
    'symbol', 'strike', 'expiry', 'contracts',
    'entry_credit', 'exit_debit', 'pnl', 'vix', 'notes',
]


def _ensure_header() -> None:
    """Write the CSV header row the first time the log file is created."""
    if not os.path.exists(config.TRADE_LOG_FILE):
        with open(config.TRADE_LOG_FILE, 'w', newline='') as fh:
            csv.DictWriter(fh, fieldnames=_FIELDS).writeheader()


def log_trade(
    action: str,
    ticker: str,
    leg: str,
    symbol: str,
    strike: float,
    expiry: str,
    contracts: int,
    entry_credit: float,
    exit_debit: Optional[float] = None,
    vix: Optional[float] = None,
    notes: str = '',
) -> None:
    """
    Append one trade record to the CSV log.

    P&L formula (per leg):
        pnl = (entry_credit - exit_debit) * contracts * 100

    A positive pnl means the option lost value after we sold it (a profit for
    a short-options strategy).  A negative pnl means we paid more to buy it
    back than we received when we sold it (a loss).
    """
    _ensure_header()

    pnl: Optional[float] = None
    if exit_debit is not None:
        pnl = round((entry_credit - exit_debit) * contracts * 100, 2)

    row = {
        'timestamp':    datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'action':       action,
        'ticker':       ticker,
        'leg':          leg,
        'symbol':       symbol,
        'strike':       strike,
        'expiry':       expiry,
        'contracts':    contracts,
        'entry_credit': entry_credit,
        'exit_debit':   '' if exit_debit is None else exit_debit,
        'pnl':          '' if pnl is None else pnl,
        'vix':          '' if vix is None else vix,
        'notes':        notes,
    }

    with open(config.TRADE_LOG_FILE, 'a', newline='') as fh:
        csv.DictWriter(fh, fieldnames=_FIELDS).writerow(row)

    logger.info(
        "Logged %s %s %-30s  credit=%.4f  debit=%s  pnl=%s",
        action, leg, symbol, entry_credit,
        f"{exit_debit:.4f}" if exit_debit is not None else '—',
        f"${pnl:.2f}" if pnl is not None else '—',
    )
