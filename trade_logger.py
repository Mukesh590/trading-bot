"""
Append every OPEN / CLOSE event to a CSV file for post-trade analysis.

CSV columns
───────────
  timestamp     UTC ISO-8601 timestamp of the event
  action        OPEN or CLOSE
  ticker        Underlying stock (e.g. AAPL)
  leg           Which leg this row is: SHORT_CALL, LONG_CALL, PUT, CSP, CC
  symbol        Full OCC option symbol (e.g. AAPL240119C00195000)
  strike        Strike price in dollars
  expiry        Expiration date (YYYY-MM-DD)
  contracts     Number of contracts traded (each = 100 shares)
  entry_credit  Per-share price at open.  For SHORT legs this is the credit
                received; for LONG legs (side=LONG) it is the debit PAID.
  exit_debit    Per-share price at close.  For SHORT legs this is the debit paid
                to buy back; for LONG legs it is the credit received on sale.
                Blank for OPEN rows.
  pnl           Realized P&L in dollars for this leg (blank for OPEN rows)
  vix           VIX level at the time the position was originally opened
  notes         Free-text reason for closing (e.g. PROFIT_TAKE_52.3%, STOP_LOSS_CALL)
  position_type Strategy structure this leg belongs to: CALL_CREDIT_SPREAD,
                CASH_SECURED_PUT, or COVERED_CALL.  Lets a dashboard label a
                position by structure instead of guessing from the legs (so a
                short call paired with its long call is shown as a credit spread,
                never a "naked call").
  side          SHORT (sold to open) or LONG (bought to open).  Drives the P&L
                sign: SHORT profits when the option loses value, LONG when it
                gains.
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
    'position_type', 'side',
]

# Map a leg label to its strategy structure, used both for new rows when a
# caller omits position_type and to back-fill the column when migrating an
# older log that predates it.
# 'CALL' covers versions that logged the call side as a single unlabelled row
# before the SHORT_CALL / LONG_CALL split.  This bot never has naked calls, so
# any standalone 'CALL' row is always part of a credit spread.
_LEG_TO_POSITION_TYPE = {
    'CALL_SPREAD': 'CALL_CREDIT_SPREAD',   # legacy single-row call spread
    'SHORT_CALL':  'CALL_CREDIT_SPREAD',
    'LONG_CALL':   'CALL_CREDIT_SPREAD',
    'CALL':        'CALL_CREDIT_SPREAD',   # legacy: any call in this bot is a CCS
    'PUT':         'CASH_SECURED_PUT',
    'CSP':         'CASH_SECURED_PUT',
    'CC':          'COVERED_CALL',
}


def _position_type_for(leg: str) -> str:
    return _LEG_TO_POSITION_TYPE.get(leg, '')


def _migrate_log(old_header: list) -> None:
    """
    Upgrade a trade log written under an older column set to the current schema.

    Reads every existing row by its old header, fills any newly-added columns
    (position_type, side) with sensible values inferred from the leg, and
    rewrites the file with the current header.  History is preserved; only the
    schema changes.  Performed atomically via a temp file + replace.
    """
    with open(config.TRADE_LOG_FILE, 'r', newline='') as fh:
        rows = list(csv.DictReader(fh))

    for r in rows:
        if not r.get('position_type'):
            r['position_type'] = _position_type_for(r.get('leg', ''))
        if not r.get('side'):
            r['side'] = 'LONG' if r.get('leg') == 'LONG_CALL' else 'SHORT'

    tmp_path = config.TRADE_LOG_FILE + '.tmp'
    with open(tmp_path, 'w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, '') for k in _FIELDS})
    os.replace(tmp_path, config.TRADE_LOG_FILE)

    logger.info(
        "Migrated trade log from %d-column to %d-column schema "
        "(added: %s)",
        len(old_header), len(_FIELDS),
        ', '.join(c for c in _FIELDS if c not in old_header),
    )


def _ensure_header() -> None:
    """
    Make sure the CSV exists with the current header, migrating it if it was
    written under an older schema.
    """
    if not os.path.exists(config.TRADE_LOG_FILE):
        with open(config.TRADE_LOG_FILE, 'w', newline='') as fh:
            csv.DictWriter(fh, fieldnames=_FIELDS).writeheader()
        return

    with open(config.TRADE_LOG_FILE, 'r', newline='') as fh:
        header = next(csv.reader(fh), None)

    if header == _FIELDS:
        return
    if header is None:
        # File exists but is empty — just write the header.
        with open(config.TRADE_LOG_FILE, 'w', newline='') as fh:
            csv.DictWriter(fh, fieldnames=_FIELDS).writeheader()
        return

    _migrate_log(header)


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
    position_type: str = '',
    side: str = 'SHORT',
) -> None:
    """
    Append one trade record to the CSV log.

    P&L formula (per leg), where entry/exit are per-share prices:
        SHORT leg:  pnl = (entry_credit - exit_debit) * contracts * 100
        LONG  leg:  pnl = (exit_debit - entry_credit) * contracts * 100

    For a SHORT leg a positive pnl means the option lost value after we sold it
    (a profit).  For a LONG leg a positive pnl means the option gained value
    after we bought it.  Summing both call legs of a credit spread reproduces
    the spread's net P&L.

    `position_type` defaults to the structure implied by `leg` when not given.
    """
    _ensure_header()

    if not position_type:
        position_type = _position_type_for(leg)

    pnl: Optional[float] = None
    if exit_debit is not None:
        if side == 'LONG':
            pnl = round((exit_debit - entry_credit) * contracts * 100, 2)
        else:
            pnl = round((entry_credit - exit_debit) * contracts * 100, 2)

    row = {
        'timestamp':     datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'action':        action,
        'ticker':        ticker,
        'leg':           leg,
        'symbol':        symbol,
        'strike':        strike,
        'expiry':        expiry,
        'contracts':     contracts,
        'entry_credit':  entry_credit,
        'exit_debit':    '' if exit_debit is None else exit_debit,
        'pnl':           '' if pnl is None else pnl,
        'vix':           '' if vix is None else vix,
        'notes':         notes,
        'position_type': position_type,
        'side':          side,
    }

    with open(config.TRADE_LOG_FILE, 'a', newline='') as fh:
        csv.DictWriter(fh, fieldnames=_FIELDS).writerow(row)

    logger.info(
        "Logged %s %s %-9s %-30s  credit=%.4f  debit=%s  pnl=%s",
        action, position_type, leg, symbol, entry_credit,
        f"{exit_debit:.4f}" if exit_debit is not None else '—',
        f"${pnl:.2f}" if pnl is not None else '—',
    )
