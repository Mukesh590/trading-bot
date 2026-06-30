# VALIDATION SUITE
# Real minute bars from Alpaca
# Validates all our key findings with real data

import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from math import log, sqrt, exp
from scipy.stats import norm
from scipy.optimize import brentq
import warnings
warnings.filterwarnings('ignore')

# ── ALPACA CREDENTIALS ────────────────────────────────────────
# Paste your keys here
ALPACA_KEY    = "PK372JXIHFTRGTBTYGHMYJNMZS"
ALPACA_SECRET = "2QgdZAChPk9e2f71CnfwVzhW4JcvszHKNWiVexPqeAw2"

# Use paper trading base URL for data (same data access)
DATA_URL = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "accept":              "application/json",
}

# ── PULL MINUTE BARS ──────────────────────────────────────────
def pull_bars(symbol, start, end, timeframe="1Min"):
    """Pull minute bars from Alpaca in chunks"""
    print(f"Pulling {symbol} {timeframe} bars "
          f"{start} to {end}...")
    
    all_bars = []
    url      = (f"{DATA_URL}/v2/stocks/{symbol}/bars")
    params   = {
        "timeframe": timeframe,
        "start":     start,
        "end":       end,
        "limit":     10000,
        "feed":      "sip",
        "adjustment":"split",
    }
    
    page = 0
    next_token = None
    
    while True:
        if next_token:
            params["page_token"] = next_token
        
        resp = requests.get(url, headers=HEADERS,
                            params=params, timeout=30)
        
        if resp.status_code != 200:
            print(f"  Error {resp.status_code}: "
                  f"{resp.text[:100]}")
            break
        
        data = resp.json()
        bars = data.get("bars", [])
        
        if not bars:
            break
        
        all_bars.extend(bars)
        page += 1
        
        next_token = data.get("next_page_token")
        if not next_token:
            break
        
        if page % 10 == 0:
            print(f"  Page {page}: "
                  f"{len(all_bars):,} bars so far...")
    
    if not all_bars:
        print(f"  No bars returned for {symbol}")
        return pd.DataFrame()
    
    df = pd.DataFrame(all_bars)
    df["t"] = pd.to_datetime(df["t"])
    df = df.set_index("t").sort_index()
    df = df.rename(columns={
        "o": "open",  "h": "high",
        "l": "low",   "c": "close",
        "v": "volume","vw":"vwap",
    })
    
    # convert to Eastern time
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("America/New_York")
    
    print(f"  Done: {len(df):,} bars "
          f"({df.index[0]} to {df.index[-1]})")
    return df

# ── PULL DATA ─────────────────────────────────────────────────
print("=" * 60)
print("PULLING REAL MINUTE BARS FROM ALPACA")
print("=" * 60)

# 3 years of minute bars
END   = datetime.now().strftime("%Y-%m-%d")
START = (datetime.now() - timedelta(days=365*3)
         ).strftime("%Y-%m-%d")

spy_min = pull_bars("SPY", START, END)
qqq_min = pull_bars("QQQ", START, END)

# also pull daily for ML features
spy_day = pull_bars("SPY", "2010-01-01", END, "1Day")
qqq_day = pull_bars("QQQ", "2010-01-01", END, "1Day")

# BTC overnight
btc_day = pull_bars("BTCUSD", "2018-01-01", END, "1Day")

print(f"\nData summary:")
print(f"  SPY minute: {len(spy_min):,} bars")
print(f"  QQQ minute: {len(qqq_min):,} bars")
print(f"  SPY daily:  {len(spy_day):,} days")
print(f"  BTC daily:  {len(btc_day):,} days")

# save to CSV so we don't re-pull
spy_min.to_csv("spy_minute.csv")
qqq_min.to_csv("qqq_minute.csv")
spy_day.to_csv("spy_daily.csv")
qqq_day.to_csv("qqq_daily.csv")
btc_day.to_csv("btc_daily.csv")
print("\nSaved all data to CSV files")

# ── BLACK SCHOLES ─────────────────────────────────────────────
def bs_call(S, K, T, r, iv):
    if T <= 0 or iv <= 0: return max(S-K, 0)
    d1 = (log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    return S*norm.cdf(d1)-K*exp(-r*T)*norm.cdf(d1-iv*sqrt(T))

def bs_put(S, K, T, r, iv):
    if T <= 0 or iv <= 0: return max(K-S, 0)
    d1 = (log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    d2 = d1-iv*sqrt(T)
    return K*exp(-r*T)*norm.cdf(-d2)-S*norm.cdf(-d1)

def bs_delta(S, K, T, r, iv):
    if T <= 0 or iv <= 0: return 0.5
    d1 = (log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    return float(norm.cdf(d1))

def bs_gamma(S, K, T, r, iv):
    if T <= 0 or iv <= 0: return 0
    d1 = (log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    return float(norm.pdf(d1)/(S*iv*sqrt(T)))

def bs_theta_per_min(S, K, T, r, iv):
    """Theta per minute"""
    if T <= 0 or iv <= 0: return 0
    d1 = (log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    d2 = d1-iv*sqrt(T)
    t1 = -S*norm.pdf(d1)*iv/(2*sqrt(T))
    theta_day = (t1-r*K*exp(-r*T)*norm.cdf(d2))/252
    return theta_day / 390  # per minute

r_rate = 0.05

# ── VALIDATION 1: INTRADAY SCENARIO CONSTANTS ─────────────────
print("\n" + "="*60)
print("VALIDATION 1: INTRADAY SCENARIO CONSTANTS")
print("98% Rule + High/Low First with REAL minute data")
print("="*60)

def analyze_day_structure(df_min, symbol="SPY"):
    """Analyze High First vs Low First with real minute bars"""
    records = []
    
    # group by date
    dates = df_min.index.normalize().unique()
    
    for date in dates:
        try:
            # market hours only
            day = df_min[df_min.index.normalize()==date]
            day = day.between_time("09:30","16:00")
            if len(day) < 60: continue
            
            # open = first bar
            day_open = float(day["open"].iloc[0])
            if day_open <= 0: continue
            
            # high, low, close
            day_high  = float(day["high"].max())
            day_low   = float(day["low"].min())
            day_close = float(day["close"].iloc[-1])
            
            # scenario
            ha = day_high > day_open
            lb = day_low  < day_open
            if ha and lb:       sc = "A"
            elif ha and not lb: sc = "B"
            elif not ha and lb: sc = "C"
            else:               sc = "D"
            
            # when was high/low set (minute of day)
            high_time = day["high"].idxmax()
            low_time  = day["low"].idxmin()
            high_min  = (high_time.hour*60 +
                         high_time.minute)
            low_min   = (low_time.hour*60 +
                         low_time.minute)
            high_first = high_min < low_min
            
            # returns
            dr  = (day_close-day_open)/day_open*100
            du  = (day_high -day_open)/day_open*100
            dd  = (day_low  -day_open)/day_open*100
            rng = (day_high -day_low) /day_open*100
            
            # OR range (first 30 min)
            or_bars = day.between_time("09:30","10:00")
            orh = float(or_bars["high"].max())
            orl = float(or_bars["low"].min())
            orr = (orh-orl)/day_open*100
            
            # VWAP at various times
            day["cum_vol"] = day["volume"].cumsum()
            day["cum_pv"]  = (day["vwap"] *
                              day["volume"]).cumsum()
            day["vwap_running"] = (day["cum_pv"] /
                                   day["cum_vol"])
            
            # snapshots at key times
            snaps = {}
            for t_str, t_lbl in [
                ("09:45","snap_945"),
                ("10:00","snap_1000"),
                ("10:15","snap_1015"),
                ("10:30","snap_1030"),
                ("11:00","snap_1100"),
            ]:
                t_bars = day.between_time(
                    t_str, t_str)
                if len(t_bars) == 0:
                    # get closest bar
                    h,m = int(t_str.split(":")[0]), \
                          int(t_str.split(":")[1])
                    target = date.replace(
                        hour=h, minute=m,
                        tzinfo=day.index[0].tzinfo)
                    idx = day.index.get_indexer(
                        [target], method="nearest")[0]
                    t_bars = day.iloc[idx:idx+1]
                
                if len(t_bars) > 0:
                    cl  = float(t_bars["close"].iloc[-1])
                    vw  = float(t_bars["vwap_running"
                                       ].iloc[-1])
                    ret = (cl-day_open)/day_open*100
                    snaps[t_lbl] = {
                        "ret":    ret,
                        "above_vwap": cl > vw,
                        "above_or":   cl > orh,
                        "below_or":   cl < orl,
                        "inside_or":  orl <= cl <= orh,
                        "vwap_dist":  (cl-vw)/vw*100,
                    }
            
            records.append({
                "date":       date.date(),
                "dow":        date.dayofweek,
                "year":       date.year,
                "symbol":     symbol,
                "open":       day_open,
                "high":       day_high,
                "low":        day_low,
                "close":      day_close,
                "sc":         sc,
                "dr":         dr,
                "du":         du,
                "dd":         dd,
                "rng":        rng,
                "orr":        orr,
                "high_first": high_first,
                "high_min":   high_min,
                "low_min":    low_min,
                **{f"{k}_{kk}": vv
                   for k,v in snaps.items()
                   for kk,vv in v.items()},
            })
        except Exception as e:
            continue
    
    return pd.DataFrame(records)

print("\nAnalyzing SPY day structure...")
spy_struct = analyze_day_structure(spy_min, "SPY")
print(f"  SPY: {len(spy_struct)} days analyzed")

print("Analyzing QQQ day structure...")
qqq_struct = analyze_day_structure(qqq_min, "QQQ")
print(f"  QQQ: {len(qqq_struct)} days analyzed")

struct = pd.concat([spy_struct, qqq_struct],
                   ignore_index=True)

# ── PRINT SCENARIO FREQUENCIES ────────────────────────────────
print("\n====== SCENARIO FREQUENCIES (REAL DATA) ======")
for sym in ["SPY","QQQ"]:
    sub = struct[struct["symbol"]==sym]
    n   = len(sub)
    print(f"\n  {sym} (n={n}):")
    for sc in ["A","B","C","D"]:
        cnt = (sub["sc"]==sc).sum()
        print(f"    Sc{sc}: {cnt} ({cnt/n*100:.1f}%)")

# ── HIGH FIRST vs LOW FIRST ───────────────────────────────────
print("\n====== HIGH FIRST vs LOW FIRST (REAL DATA) ======")
for sym in ["SPY","QQQ"]:
    sub = struct[struct["symbol"]==sym]
    hf  = sub[sub["high_first"]==True]
    lf  = sub[sub["high_first"]==False]
    print(f"\n  {sym}:")
    print(f"    HighFirst: {len(hf)} "
          f"({len(hf)/len(sub)*100:.1f}%) "
          f"avgRet={hf['dr'].mean():+.3f}%")
    print(f"    LowFirst:  {len(lf)} "
          f"({len(lf)/len(sub)*100:.1f}%) "
          f"avgRet={lf['dr'].mean():+.3f}%")

# ── SNAPSHOT ANALYSIS ─────────────────────────────────────────
print("\n====== SNAPSHOT PREDICTORS (REAL DATA) ======")
print("Replicating Run 1B findings with real minute bars")

snap_cols = [
    ("snap_945_above_vwap",  "AboveVWAP@9:45"),
    ("snap_1000_above_or",   "AboveOR@10:00"),
    ("snap_1000_below_or",   "BelowOR@10:00"),
    ("snap_1015_above_or",   "AboveOR@10:15"),
    ("snap_1030_above_or",   "AboveOR@10:30"),
    ("snap_1100_above_or",   "AboveOR@11:00"),
    ("snap_1100_below_or",   "BelowOR@11:00"),
]

spy_s = struct[struct["symbol"]=="SPY"]
print(f"\n  {'Signal':<20} {'N':>5} {'LF%':>7} "
      f"{'HF%':>7} {'DayPos%':>9} {'AvgRet%':>9}")
print("  "+"-"*58)

for col, lbl in snap_cols:
    if col not in spy_s.columns: continue
    grp = spy_s[spy_s[col]==True]
    if len(grp) < 10: continue
    lf  = (grp["high_first"]==False).mean()*100
    hf  = (grp["high_first"]==True).mean()*100
    pos = (grp["dr"]>0).mean()*100
    ret = grp["dr"].mean()
    print(f"  {lbl:<20} {len(grp):>5} "
          f"{lf:>6.1f}% {hf:>6.1f}% "
          f"{pos:>8.1f}% {ret:>8.3f}%")

# ── VALIDATION 2: THETA CAPTURE WITH REAL PRICES ─────────────
print("\n" + "="*60)
print("VALIDATION 2: THETA CAPTURE WITH REAL MINUTE DATA")
print("Simulate option P&L tick by tick through the day")
print("="*60)

def simulate_theta_capture(df_min, struct_df,
                            vix_df=None,
                            ml_threshold=0.70):
    """
    Simulate buying 0DTE ITM call at 9:45 AM
    and closing at various times
    Using real minute bars for underlying path
    """
    import yfinance as yf
    
    # get VIX for IV assumption
    if vix_df is None:
        print("  Pulling VIX for IV...")
        vix_raw = yf.download("^VIX",
                               start=df_min.index[0]
                               .strftime("%Y-%m-%d"),
                               end=df_min.index[-1]
                               .strftime("%Y-%m-%d"),
                               progress=False)
        vix_df = vix_raw["Close"].squeeze()
        vix_df.index = pd.to_datetime(
            vix_df.index).tz_localize(None)
    
    results = []
    trading_day_minutes = 390  # 6.5 hours
    
    # use Low First proxy from struct
    # (du > abs(dd) = low first)
    lf_proxy = struct_df[
        struct_df["symbol"]=="SPY"
    ].copy()
    lf_proxy["lf_prob"] = np.where(
        lf_proxy["du"] > lf_proxy["dd"].abs(),
        0.75, 0.25)
    
    # try to merge with ML probs if available
    # otherwise use proxy
    signal_days = lf_proxy[
        lf_proxy["lf_prob"] >= ml_threshold
    ]["date"].values
    
    print(f"  Signal days: {len(signal_days)}")
    
    for date in signal_days:
        try:
            date_ts = pd.Timestamp(date)
            
            # get this day's minute bars
            day = df_min[
                df_min.index.normalize().date ==
                date
            ] if hasattr(df_min.index,
                         'normalize') else \
                df_min[df_min.index.date == date]
            
            # filter to market hours
            day = day.between_time("09:30","16:00")
            if len(day) < 60: continue
            
            S_open = float(day["open"].iloc[0])
            if S_open <= 0: continue
            
            # get VIX for this day
            date_norm = pd.Timestamp(date).normalize()
            if hasattr(vix_df.index, 'tz') and \
               vix_df.index.tz is not None:
                vix_today = float(
                    vix_df.reindex([date_norm],
                                   method="ffill"
                                   ).values[0]) / 100
            else:
                try:
                    vix_today = float(
                        vix_df.loc[date_norm]) / 100
                except:
                    vix_today = 0.18
            
            if vix_today <= 0: vix_today = 0.18
            
            # entry at 9:45 AM
            entry_bars = day.between_time(
                "09:45","09:46")
            if len(entry_bars) == 0:
                entry_bars = day.iloc[15:16]
            if len(entry_bars) == 0: continue
            
            S_entry = float(
                entry_bars["close"].iloc[-1])
            
            # ITM call: strike 0.5% below spot
            K_itm = S_entry * 0.995
            K_atm = S_entry
            
            # time to expiry at entry
            # 9:45 AM = 375 min remaining in day
            # T in years = mins_remaining / (390*252)
            mins_at_entry = 375
            T_entry = mins_at_entry / (390*252)
            
            # entry premium
            iv_entry = vix_today * 1.05
            c_itm_entry = bs_call(
                S_entry, K_itm, T_entry,
                r_rate, iv_entry)
            c_atm_entry = bs_call(
                S_entry, K_atm, T_entry,
                r_rate, iv_entry)
            
            if c_itm_entry <= 0.01: continue
            if c_atm_entry <= 0.01: continue
            
            # simulate minute by minute
            entry_idx = day.index.get_loc(
                entry_bars.index[-1])
            after_entry = day.iloc[entry_idx+1:]
            
            # track P&L at each minute
            itm_pnls  = []
            atm_pnls  = []
            times_min = []
            
            for i, (ts, bar) in enumerate(
                after_entry.iterrows()
            ):
                S_now = float(bar["close"])
                
                # time remaining
                mins_elapsed = i + 1
                mins_remain  = max(
                    mins_at_entry - mins_elapsed, 1)
                T_now = mins_remain / (390*252)
                
                # IV slight drift (crush during day)
                iv_crush = 1.0 - (
                    mins_elapsed / mins_at_entry * 0.15)
                iv_now = iv_entry * max(iv_crush, 0.85)
                
                # current premium
                c_itm_now = bs_call(
                    S_now, K_itm, T_now,
                    r_rate, iv_now)
                c_atm_now = bs_call(
                    S_now, K_atm, T_now,
                    r_rate, iv_now)
                
                # P&L %
                itm_pnl = (c_itm_now - c_itm_entry) \
                          / c_itm_entry * 100
                atm_pnl = (c_atm_now - c_atm_entry) \
                          / c_atm_entry * 100
                
                itm_pnls.append(itm_pnl)
                atm_pnls.append(atm_pnl)
                
                # time label
                hr  = ts.hour
                mn  = ts.minute
                times_min.append(hr*60+mn)
            
            if not itm_pnls: continue
            
            # extract at specific exit times
            def get_pnl_at(target_min, pnls, times):
                diffs = [abs(t-target_min)
                         for t in times]
                if not diffs: return None
                idx = diffs.index(min(diffs))
                return pnls[idx]
            
            results.append({
                "date":       date,
                "S_open":     S_open,
                "S_entry":    S_entry,
                "vix":        vix_today*100,
                "K_itm":      K_itm,
                "K_atm":      K_atm,
                "c_itm_entry":c_itm_entry,
                "c_atm_entry":c_atm_entry,
                # ITM exits
                "itm_30min":  get_pnl_at(
                    10*60+15, itm_pnls, times_min),
                "itm_60min":  get_pnl_at(
                    10*60+45, itm_pnls, times_min),
                "itm_90min":  get_pnl_at(
                    11*60+15, itm_pnls, times_min),
                "itm_120min": get_pnl_at(
                    11*60+45, itm_pnls, times_min),
                "itm_eod":    itm_pnls[-1] \
                              if itm_pnls else None,
                # ATM exits
                "atm_30min":  get_pnl_at(
                    10*60+15, atm_pnls, times_min),
                "atm_60min":  get_pnl_at(
                    10*60+45, atm_pnls, times_min),
                "atm_90min":  get_pnl_at(
                    11*60+15, atm_pnls, times_min),
                "atm_120min": get_pnl_at(
                    11*60+45, atm_pnls, times_min),
                "atm_eod":    atm_pnls[-1] \
                              if atm_pnls else None,
                # max P&L during day
                "itm_max":    max(itm_pnls)
                              if itm_pnls else None,
                "atm_max":    max(atm_pnls)
                              if atm_pnls else None,
                # time of max P&L
                "itm_max_min":times_min[
                    itm_pnls.index(max(itm_pnls))]
                              if itm_pnls else None,
            })
        
        except Exception as e:
            continue
    
    return pd.DataFrame(results)

print("\nRunning theta capture simulation...")
theta_results = simulate_theta_capture(
    spy_min, struct, ml_threshold=0.50)
print(f"  Simulated: {len(theta_results)} signal days")

if len(theta_results) > 10:
    print("\n====== THETA CAPTURE RESULTS (REAL DATA) ======")
    print(f"{'Exit':<12} {'ITM_EV%':>9} {'ITM_Pos%':>9} "
          f"{'ATM_EV%':>9} {'ATM_Pos%':>9}")
    print("-"*48)
    
    for col_itm, col_atm, lbl in [
        ("itm_30min","atm_30min","30min"),
        ("itm_60min","atm_60min","60min"),
        ("itm_90min","atm_90min","90min"),
        ("itm_120min","atm_120min","120min"),
        ("itm_eod","atm_eod","EOD"),
    ]:
        itm = theta_results[col_itm].dropna()
        atm = theta_results[col_atm].dropna()
        if len(itm) == 0: continue
        print(f"{lbl:<12} "
              f"{itm.mean():>8.1f}% "
              f"{(itm>0).mean()*100:>8.1f}% "
              f"{atm.mean():>8.1f}% "
              f"{(atm>0).mean()*100:>8.1f}%")
    
    # max P&L analysis
    print(f"\n  Max ITM P&L during day:")
    mx = theta_results["itm_max"].dropna()
    print(f"    Mean max: {mx.mean():.1f}%")
    print(f"    Median:   {mx.median():.1f}%")
    print(f"    p25:      {mx.quantile(.25):.1f}%")
    print(f"    p75:      {mx.quantile(.75):.1f}%")
    print(f"    >50%:     "
          f"{(mx>50).mean()*100:.1f}% of days")
    print(f"    >100%:    "
          f"{(mx>100).mean()*100:.1f}% of days")
    
    # when does max P&L occur
    mx_time = theta_results["itm_max_min"].dropna()
    print(f"\n  When does max P&L occur?")
    for lo,hi,lbl in [
        (570,600,"9:30-10:00"),
        (600,660,"10:00-11:00"),
        (660,720,"11:00-12:00"),
        (720,840,"12:00-14:00"),
        (840,960,"14:00-16:00"),
    ]:
        cnt = ((mx_time>=lo)&(mx_time<hi)).sum()
        pct = cnt/len(mx_time)*100
        print(f"    {lbl}: {cnt} ({pct:.1f}%)")
    
    # profit target analysis
    print(f"\n  +50% profit target analysis:")
    itm_eod = theta_results["itm_eod"].dropna()
    itm_max = theta_results["itm_max"].dropna()
    
    # if max > 50% at any point, take 50%
    # otherwise take EOD
    pt50_pnls = []
    for _, row in theta_results.dropna(
        subset=["itm_max","itm_eod"]
    ).iterrows():
        if float(row["itm_max"]) >= 50:
            pt50_pnls.append(50.0)
        else:
            pt50_pnls.append(float(row["itm_eod"]))
    
    if pt50_pnls:
        print(f"    +50%PT EV:  "
              f"{np.mean(pt50_pnls):.1f}%")
        print(f"    +50%PT Pos: "
              f"{np.mean(np.array(pt50_pnls)>0)*100:.1f}%")
        print(f"    vs EOD EV:  "
              f"{itm_eod.mean():.1f}%")
        print(f"    vs EOD Pos: "
              f"{(itm_eod>0).mean()*100:.1f}%")

# ── VALIDATION 3: DUAL LAYER EQUITY+OPTIONS ───────────────────
print("\n" + "="*60)
print("VALIDATION 3: DUAL LAYER STRATEGY")
print("2% options + 98% equity on signal days")
print("="*60)

if len(theta_results) > 10 and len(spy_struct) > 10:
    
    portfolio = 100000.0
    equity_alloc = 0.98
    options_alloc = 0.02
    
    equity_curve  = [portfolio]
    options_pnls  = []
    combined_pnls = []
    dates_used    = []
    
    spy_daily_idx = spy_struct.set_index("date")
    
    for _, row in theta_results.iterrows():
        date = row["date"]
        if date not in spy_daily_idx.index:
            continue
        
        spy_day_data = spy_daily_idx.loc[date]
        spy_ret = float(spy_day_data["dr"]) / 100
        
        # equity layer: long SPY
        eq_pnl = portfolio * equity_alloc * spy_ret
        
        # options layer: ITM call EOD
        itm_eod = row.get("itm_eod", None)
        if itm_eod is not None and not pd.isna(itm_eod):
            opt_pnl = (portfolio * options_alloc *
                       float(itm_eod) / 100)
        else:
            opt_pnl = 0
        
        total_pnl = eq_pnl + opt_pnl
        portfolio += total_pnl
        
        equity_curve.append(portfolio)
        options_pnls.append(opt_pnl)
        combined_pnls.append(total_pnl/
                              equity_curve[-2]*100)
        dates_used.append(date)
    
    if combined_pnls:
        print(f"\n  Signal days traded: {len(dates_used)}")
        print(f"  Final portfolio: ${portfolio:,.0f}")
        print(f"  Total return: "
              f"{(portfolio/100000-1)*100:+.1f}%")
        print(f"\n  Combined P&L distribution:")
        cpnls = np.array(combined_pnls)
        print(f"    Mean:   {cpnls.mean():+.3f}%")
        print(f"    Median: {np.median(cpnls):+.3f}%")
        print(f"    Pos%:   "
              f"{(cpnls>0).mean()*100:.1f}%")
        print(f"    p10:    {np.percentile(cpnls,10):+.3f}%")
        print(f"    p90:    {np.percentile(cpnls,90):+.3f}%")
        print(f"\n  Equity layer avg: "
              f"{np.mean([p/100000*100 for p in options_pnls]):+.3f}%")
        print(f"  Options layer avg: "
              f"{np.mean(options_pnls)/100000*100:+.3f}%")
        
        # max drawdown
        eq_arr = np.array(equity_curve)
        peak   = np.maximum.accumulate(eq_arr)
        dd     = (eq_arr - peak) / peak * 100
        print(f"\n  Max drawdown: {dd.min():.1f}%")
        print(f"  Win rate:     "
              f"{(cpnls>0).mean()*100:.1f}%")

# ── VALIDATION 4: BID-ASK FRICTION ───────────────────────────
print("\n" + "="*60)
print("VALIDATION 4: BID-ASK SPREAD IMPACT")
print("How much does friction reduce EV?")
print("="*60)

if len(theta_results) > 10:
    # typical bid-ask spreads
    spreads = {
        "0DTE ATM  ($0.24 prem)":  0.05,
        "0DTE ITM  ($0.50 prem)":  0.04,
        "1DTE ATM  ($2.00 prem)":  0.08,
        "2DTE ATM  ($3.50 prem)":  0.10,
    }
    
    base_ev_itm = theta_results[
        "itm_120min"].dropna().mean()
    base_ev_atm = theta_results[
        "atm_120min"].dropna().mean()
    
    print(f"\n  Base EV (no friction):")
    print(f"    ITM 120min: {base_ev_itm:.1f}%")
    print(f"    ATM 120min: {base_ev_atm:.1f}%")
    print(f"\n  {'Config':<28} {'Spread$':>8} "
          f"{'Friction%':>11} {'NetEV%':>9}")
    print("  "+"-"*58)
    
    for cfg, spread in spreads.items():
        if "ITM" in cfg:
            prem = 0.50
            base = base_ev_itm
        else:
            prem = 2.00
            base = base_ev_atm
        
        # friction = spread / premium * 100
        friction_pct = spread / prem * 100
        net_ev = base - friction_pct
        
        print(f"  {cfg:<28} ${spread:>6.2f} "
              f"{friction_pct:>10.1f}% "
              f"{net_ev:>8.1f}%")

# ── VALIDATION 5: WALK FORWARD TEST ──────────────────────────
print("\n" + "="*60)
print("VALIDATION 5: WALK FORWARD TEST")
print("Does Low First signal hold across all years?")
print("="*60)

if len(spy_struct) > 100:
    print(f"\n  {'Year':<6} {'N':>5} {'LF%':>7} "
          f"{'HF%':>7} {'LF_DayPos%':>12} "
          f"{'HF_DayPos%':>12}")
    print("  "+"-"*50)
    
    for yr in sorted(spy_struct["year"].unique()):
        sub = spy_struct[spy_struct["year"]==yr]
        lf  = sub[sub["high_first"]==False]
        hf  = sub[sub["high_first"]==True]
        if len(sub) < 10: continue
        
        lf_pct = len(lf)/len(sub)*100
        hf_pct = len(hf)/len(sub)*100
        lf_pos = (lf["dr"]>0).mean()*100 \
                 if len(lf)>0 else 0
        hf_pos = (hf["dr"]>0).mean()*100 \
                 if len(hf)>0 else 0
        
        print(f"  {yr:<6} {len(sub):>5} "
              f"{lf_pct:>6.1f}% "
              f"{hf_pct:>6.1f}% "
              f"{lf_pos:>11.1f}% "
              f"{hf_pos:>11.1f}%")

# ── SAVE RESULTS ──────────────────────────────────────────────
print("\n" + "="*60)
print("SAVING RESULTS")
print("="*60)

struct.to_csv("day_structure_real.csv", index=False)
print("  Saved day_structure_real.csv")

if len(theta_results) > 0:
    theta_results.to_csv("theta_results_real.csv",
                          index=False)
    print("  Saved theta_results_real.csv")

print("\n====== VALIDATION COMPLETE ======")
print("Review results above.")
print("If numbers match our research → build the system.")
print("If numbers differ → investigate why before building.")