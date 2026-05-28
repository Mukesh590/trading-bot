"""
Strategy configuration for the Short Strangle Income Bot.

Edit values here to tune the strategy. Trading logic modules import from
this file — no magic numbers should appear anywhere else in the codebase.
"""

# ── Watchlists ─────────────────────────────────────────────────────────────────
# CCS tickers run the call credit spread + put strangle (original strategy).
# CSP tickers run the wheel strategy: sell cash-secured put → if assigned,
# sell covered calls until shares are called away.
CCS_TICKERS = ['AMZN', 'META']
CSP_TICKERS = ['MSFT', 'AAPL']
WATCH_LIST  = CCS_TICKERS + CSP_TICKERS   # kept for logging convenience

# ── Call credit spread + put strangle construction (CCS_TICKERS) ───────────────
# How far out-of-the-money (OTM) to sell each leg, expressed as a fraction of
# the current stock price.
CALL_OTM_PCT      = 0.025   # Sell call ~2.5 % above spot
PUT_OTM_PCT       = 0.025   # Sell put  ~2.5 % below spot
CALL_SPREAD_WIDTH = 5       # Buy a call this many strikes above the short call to cap risk

# Days-to-expiration (DTE) target window.  Contracts must expire between
# MIN_DTE and MAX_DTE days from today.  The 30-45 day range captures the
# steepest part of theta decay while giving the position room to breathe.
MIN_DTE = 30
MAX_DTE = 45

# Maximum number of open strangles at any given time (across all tickers).
# Limits capital at risk and keeps margin usage manageable.
MAX_STRANGLES = 3

# ── Profit / loss management ───────────────────────────────────────────────────
# Target exit: close when we have captured this fraction of the max profit.
#   Max profit = total credit received at open.
#   At 50 % profit, the combined option value has fallen to 50 % of its entry price.
#   At 70 % profit, it has fallen to 30 % of its entry price (more aggressive hold).
PROFIT_TAKE_MIN_PCT = 0.50   # Close position when 50 % of max profit is in hand
PROFIT_TAKE_MAX_PCT = 0.70   # Stretch target — accept any close between 50-70 %

# Stop-loss per leg: close the WHOLE strangle if either individual leg's current
# market value exceeds its entry credit by this multiple.
# Example: sold a call spread for $0.50 -> stop triggers if it is now worth
# more than $1.50 (credit * (1 + 2.0) = 3x entry), meaning you have lost 2x
# the collected premium on that leg.  2x-3x premium is the industry standard
# for short-premium strategies held 30-45 DTE.
# WARNING: values < 1.0 fire almost immediately from normal intraday fluctuation.
STOP_LOSS_PCT = 2.0          # Stop when leg value exceeds 3x entry credit (lost 2x premium)

# Minimum calendar days a position must be held before stop-loss is evaluated.
# Prevents same-day whipsaw closes driven by bid-ask noise right after entry.
MIN_HOLD_DAYS = 1

# After any close (stop or profit), block re-entering the same ticker for this
# many hours.  Prevents the open->stop->reopen loop that generates 24+ trades.
TICKER_COOLDOWN_HRS = 24

# ── Cash-secured put construction (CSP_TICKERS — wheel strategy) ───────────────
# Deeper OTM than the strangle put: gives more cushion and lowers the effective
# cost basis if assigned.
CSP_OTM_PCT = 0.05          # Sell put 5 % below spot

# ── Covered call construction (only after CSP assignment) ──────────────────────
CC_OTM_PCT_MIN = 0.03       # Strike must be at least 3 % above current spot
CC_OTM_PCT_MAX = 0.05       # Target strike ~5 % above current spot
CC_MIN_DTE     = 5          # Weekly options: at least 5 days to expiration
CC_MAX_DTE     = 9          # Cap at 9 days to stay in the weekly window

# ── VIX filters ────────────────────────────────────────────────────────────────
# High-volatility environments make short premium strategies riskier.
VIX_NO_TRADE  = 40.0   # Do NOT open new strangles when VIX is at or above this level
VIX_HALF_SIZE = 30.0   # Trade at half the normal contract count when VIX is this high

# ── Earnings avoidance ─────────────────────────────────────────────────────────
# Earnings announcements cause large gap moves that can blow through our strikes.
# Skip any ticker whose next earnings date falls within this many calendar days.
EARNINGS_BUFFER_DAYS = 7

# ── Position sizing ────────────────────────────────────────────────────────────
# Each contract controls 100 shares of the underlying.
NORMAL_CONTRACTS  = 2   # Standard qty per leg (VIX < VIX_HALF_SIZE)
REDUCED_CONTRACTS = 1   # Half-size qty per leg (VIX >= VIX_HALF_SIZE)

# ── Scheduler ──────────────────────────────────────────────────────────────────
CHECK_INTERVAL_MIN  = 30   # Run the main strategy loop every N minutes
CLOSE_BUFFER_MIN    = 30   # Stop opening new strangles this many minutes before 4 PM close

# ── Output files ───────────────────────────────────────────────────────────────
TRADE_LOG_FILE = 'trades.csv'    # CSV file for all trade records
BOT_LOG_FILE   = 'bot.log'       # Rotating text log for debugging
