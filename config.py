"""
Strategy configuration for the Short Strangle Income Bot.

Edit values here to tune the strategy. Trading logic modules import from
this file — no magic numbers should appear anywhere else in the codebase.

Academic basis for key parameters (papers in project root):
  [FOLTICE2021]  ssrn-3786342  – Foltice, "Revisiting Covered Calls and Protective Puts"
                                  SPY data 1993-2020, N=331 months.  By put-call parity,
                                  CC at X% OTM ≡ CSP at X% OTM.
  [ISRAELOV2014] ssrn-2444993  – Israelov & Nielsen, "Covered Call Strategies: One Fact
                                  and Eight Myths" (AQR, 2014).
  [ISRAELOV2015] ssrn-2444999  – Israelov & Nielsen, "Covered Calls Uncovered"
                                  (FAJ Nov/Dec 2015, AQR).
  [WRONG PAPER]  ssrn-191668   – Collin-Dufresne, Goldstein & Martin (1999),
                                  "Determinants of Credit Spread Changes" — a fixed-income
                                  paper about corporate bond credit spreads.  It has NO
                                  findings applicable to equity options or CSP strike
                                  selection.  If you intended a CSP-specific study here,
                                  replace this file.

Theoretical performance expectations (from academic evidence):
  Win rate (positive monthly P&L) at 3% OTM:  ~71% per month   [FOLTICE2021, Exhibit 1]
  CAPM alpha above buy-and-hold:               ~0.59%/month     [FOLTICE2021, Exhibit 1]
  Short-volatility component Sharpe ratio:      ~1.0 annualised  [ISRAELOV2015, Table 1]
  Expected annual alpha vs passive equity:      ~7%              [FOLTICE2021]

Strategy contradictions vs academic literature (do not fix silently):
  1. CC_MIN/MAX_DTE (weekly 5-9 DTE): Myth 4 in [ISRAELOV2014] explicitly states that
     shorter-dated options produce higher cash flow but NOT higher risk-adjusted returns.
     Papers validate only 30-DTE monthly options.  Updated to 25-35 DTE below.
  2. VIX gates reduce trading when VIX is high: [ISRAELOV2015] Table 5 shows the short-
     volatility Sharpe ratio is ~1.0 across all regimes, including the bear period
     2002-2008.  High VIX means richer options → BETTER selling environment for index
     options.  Individual stocks add gap/earnings risk, so VIX_NO_TRADE=40 is retained
     as a backstop, but VIX_ELEVATED lowered from 30 → 25.
  3. ssrn-191668 (credit spread paper): Contains no usable CSP parameters.
"""

# ── Watchlists ─────────────────────────────────────────────────────────────────
# CCS tickers run the call credit spread + put strangle (original strategy).
# CSP tickers run the wheel strategy: sell cash-secured put → if assigned,
# sell covered calls until shares are called away.
#
# SPY and QQQ replace AMZN here: AMZN repeatedly failed with "account not
# eligible to trade uncovered option contracts", and these broad ETFs have
# deeper, tighter options chains that work with our account level.  Their
# percentage-based strike selection (see options_helper) is price-agnostic, so
# the same OTM % and spread-width rules apply cleanly to ETF prices.
CCS_TICKERS = ['META', 'SPY', 'QQQ']
CSP_TICKERS = ['MSFT', 'AAPL']
WATCH_LIST  = CCS_TICKERS + CSP_TICKERS   # kept for logging convenience

# ETFs do not report earnings, so the earnings-avoidance gate must not skip
# them (an unknown earnings date would otherwise be treated as "too close").
ETF_TICKERS = ['SPY', 'QQQ']

# ── Call credit spread + put strangle construction (CCS_TICKERS) ───────────────
# How far out-of-the-money (OTM) to sell each leg, expressed as a fraction of
# the current stock price.
CALL_OTM_PCT      = 0.025   # Sell call ~2.5 % above spot
PUT_OTM_PCT       = 0.025   # Sell put  ~2.5 % below spot

# Width of the call credit spread, in DOLLARS above the short-call strike (not a
# fixed number of strikes).  A dollar width works across underlyings regardless
# of their strike spacing: SPY/QQQ use $1 increments while META uses $5, so a
# fixed "5 strikes" gave a $5 spread on the ETFs but $25 on META.  We pick the
# strike closest to (short_strike + CALL_SPREAD_WIDTH_DOLLARS) for every ticker.
CALL_SPREAD_WIDTH_DOLLARS = 25

# Days-to-expiration (DTE) target window.  Contracts must expire between
# MIN_DTE and MAX_DTE days from today.  The 30-45 day range captures the
# steepest part of theta decay while giving the position room to breathe.
MIN_DTE = 30
MAX_DTE = 45

# When a strangle opens with only the call spread (the put was skipped because
# buying power was insufficient), the bot tries to "backfill" the matching put
# leg on a later cycle once buying power frees up — but only while the call
# spread still has at least this many days to expiration, so the added put still
# earns worthwhile premium.  Set to 0 to disable the hold-window guard.
PUT_BACKFILL_MIN_DTE = 21

# Maximum number of open positions at any given time, counted across the two
# premium-selling entry strategies: call credit spreads (CCS) + cash-secured
# puts (CSP) combined must never exceed this number.  Covered calls are EXEMPT —
# they monetise shares we were already assigned and are always written.  Limits
# capital at risk and keeps margin usage manageable.
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
# [FOLTICE2021] Exhibit 1: 3% OTM CC (≡ 3% OTM CSP by put-call parity) produces
# the highest statistically significant CAPM alpha (0.59%/month, p<0.01) over 331
# months and the highest Sharpe after transaction costs.  5% OTM alpha is only
# 0.49%/month (p<0.05, barely significant) and falls further post-transaction costs.
# Trade-off: 3% OTM means more frequent assignment — acceptable for the wheel.
CSP_OTM_PCT = 0.03          # Sell put 3 % below spot  [was 0.05; see FOLTICE2021]

# ── Covered call construction (only after CSP assignment) ──────────────────────
# Strike: [ISRAELOV2014] Exhibit 4 shows ATM provides the highest volatility risk
# premium (VRP) per unit of leverage.  [ISRAELOV2015] Table 1 & 3 shows 2% OTM has
# near-identical short-volatility Sharpe (~1.0) to ATM.  Tightening from 3-5% OTM
# toward 2-3% OTM captures more VRP while still staying OTM to avoid early call-away.
# [FOLTICE2021] Exhibit 1: 1-2% OTM has highest Sharpe (0.361).
#
# DTE: [ISRAELOV2014] Myth 4: "selling options more often per year [shorter DTE] does
# NOT unequivocally translate into higher net profits."  Both [ISRAELOV2014] and
# [ISRAELOV2015] validate only 30-DTE (1-month) options.  Changing from weekly (5-9 DTE)
# to monthly (25-35 DTE) eliminates uncompensated risk from high-frequency theta rollover.
CC_OTM_PCT_MIN = 0.02       # Strike at least 2 % above spot  [was 0.03; ISRAELOV2014/2015]
CC_OTM_PCT_MAX = 0.03       # Target ~3 % above spot           [was 0.05; FOLTICE2021]
CC_MIN_DTE     = 25         # Monthly options: at least 25 DTE [was 5; ISRAELOV2014 Myth 4]
CC_MAX_DTE     = 35         # Cap at 35 DTE for monthly window [was 9; ISRAELOV2014]

# ── VIX filters ────────────────────────────────────────────────────────────────
# Academic nuance: [ISRAELOV2015] Table 5 shows the short-volatility Sharpe ratio is
# ~1.49 in bull markets and ~0.40 even in the 2002-2008 bear period — always positive.
# [ISRAELOV2014] conclusion: "a good stand-alone strategy when implied volatilities are
# high relative to expectations."  High VIX = richer options → BETTER selling environment
# for index vol.  The literature does NOT support halting selling at elevated VIX.
#
# However: the papers study S&P 500 index options.  Individual stocks (MSFT, AAPL, META)
# carry jump risk from earnings and news.  VIX_NO_TRADE=40 is kept as a tail-risk guard.
# VIX_ELEVATED is lowered 30→25: [ISRAELOV2015] shows moderate VIX regimes are also
# profitable — we were leaving premium on the table by being too conservative at 30.
VIX_NO_TRADE     = 40.0   # Do NOT open new positions when VIX is at or above this level
VIX_ELEVATED     = 25.0   # Cap open positions when VIX ∈ [25, 40)  [was 30; ISRAELOV2015]
VIX_ELEVATED_MAX_POSITIONS = 1   # Open only ONE position while VIX is elevated, rather
                                 # than trading reduced size across several — simpler logic.

# ── Earnings avoidance ─────────────────────────────────────────────────────────
# Earnings announcements cause large gap moves that can blow through our strikes.
# Skip any ticker whose next earnings date falls within this many calendar days.
EARNINGS_BUFFER_DAYS = 7

# ── Position sizing ────────────────────────────────────────────────────────────
# Each contract controls 100 shares of the underlying.  We always trade the same
# contract count per leg; elevated VIX is handled by limiting the NUMBER of open
# positions (see VIX_ELEVATED_MAX_POSITIONS), not by shrinking contract size.
NORMAL_CONTRACTS  = 2   # Standard qty per leg

# ── Scheduler ──────────────────────────────────────────────────────────────────
CHECK_INTERVAL_MIN  = 30   # Run the main strategy loop every N minutes
CLOSE_BUFFER_MIN    = 30   # Stop opening new strangles this many minutes before 4 PM close

# ── Strike search window ───────────────────────────────────────────────────────
# Dollar buffer around each target strike when querying the Alpaca options chain.
# The API returns a paginated, bounded set of contracts.  Without a strike filter,
# high-priced ETFs (SPY, QQQ) return only the lower portion of the chain — so the
# bot finds a strike near $584 instead of the 2.5%-OTM target near $605.
# Setting this to $15 covers the search from (target-15) to (target+width+15),
# which captures the short AND long legs for $1-spaced ETF chains while staying
# well inside the page limit.
STRIKE_SEARCH_BUFFER = 15

# ── Output files ───────────────────────────────────────────────────────────────
TRADE_LOG_FILE = 'trades.csv'    # CSV file for all trade records
BOT_LOG_FILE   = 'bot.log'       # Rotating text log for debugging
