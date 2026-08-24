"""
Nightshade Seed Engine - statistics.py  v4
Standalone market snapshot. NOT imported by main.py.
Shows current indicator state, signal status, and streak for all four pairs.

Usage:
    python statistics.py
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import json
import os
import datetime

SYMBOLS            = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME          = mt5.TIMEFRAME_M15
FETCH_COUNT        = 120
STATE_FILE         = "daily_state.json"

BB_PERIOD          = 20
BB_STD_MULT        = 2.5
ATR_PERIOD         = 14
ATR_BASELINE       = 50
ATR_REGIME_MULT    = 1.2
RISK_PCT           = 1.0
CONSECUTIVE_LIMIT  = 3
MAX_DAILY_TRADES   = 3

# ---------------------------------------------------------------------------
# Initialize
# ---------------------------------------------------------------------------

if not mt5.initialize():
    print("MT5 Initialization Failed:", mt5.last_error())
    quit()

account = mt5.account_info()

print("=" * 65)
print("NIGHTSHADE v4 — LAYER 2 STRATEGY SNAPSHOT")
print(f"Time (UTC): {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Account:    {account.login} | "
      f"Equity: {account.equity:.2f} {account.currency}")
print("=" * 65)

# ---------------------------------------------------------------------------
# Daily State Summary
# ---------------------------------------------------------------------------

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        today  = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        streak = state.get("consecutive_losses", 0)
        trades = state.get("trades_today", 0)
        cb     = state.get("circuit_breaker_active", False)
        last   = state.get("last_trade_result", "none yet")
        bar    = "█" * streak + "░" * (CONSECUTIVE_LIMIT - streak)

        print(f"\nDAILY STATUS  |  "
              f"Trades: {trades}/{MAX_DAILY_TRADES}  |  "
              f"Streak: [{bar}] {streak}/{CONSECUTIVE_LIMIT} consecutive losses  |  "
              f"Last: {last}  |  "
              f"CB: {'ACTIVE' if cb else 'OFF'}")
        if cb:
            print("  *** CIRCUIT BREAKER ACTIVE — bot will not trade today ***")
    except Exception:
        print("\nDAILY STATUS  |  State file unreadable.")
else:
    print("\nDAILY STATUS  |  No state file — first run of the day.")

print()

# ---------------------------------------------------------------------------
# Per-Symbol Analysis
# ---------------------------------------------------------------------------

for sym_name in SYMBOLS:
    sym = mt5.symbol_info(sym_name)
    if sym is None:
        print(f"\n{sym_name}: NOT FOUND on this broker.")
        continue

    rates = mt5.copy_rates_from_pos(sym_name, TIMEFRAME, 0, FETCH_COUNT)
    if rates is None or len(rates) == 0:
        print(f"\n{sym_name}: Cannot fetch candle data.")
        continue

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # --- Indicators ---
    df["sma"]          = df["close"].rolling(BB_PERIOD).mean()
    df["std"]          = df["close"].rolling(BB_PERIOD).std()
    df["upper_band"]   = df["sma"] + df["std"] * BB_STD_MULT
    df["lower_band"]   = df["sma"] - df["std"] * BB_STD_MULT
    df["z_score"]      = (df["close"] - df["sma"]) / df["std"]

    hl                 = df["high"] - df["low"]
    hc                 = (df["high"] - df["close"].shift()).abs()
    lc                 = (df["low"]  - df["close"].shift()).abs()
    df["tr"]           = np.maximum(hl, np.maximum(hc, lc))
    df["atr"]          = df["tr"].rolling(ATR_PERIOD).mean()
    df["atr_baseline"] = df["atr"].rolling(ATR_BASELINE).mean()

    # ATR regime filter — PRESERVED
    df["regime_ok"] = df["atr"] < (df["atr_baseline"] * ATR_REGIME_MULT)

    # Signals — only when regime_ok AND Z-score crosses ±BB_STD_MULT
    df["signal"] = 0
    df.loc[df["regime_ok"] & (df["z_score"] < -BB_STD_MULT), "signal"] =  1
    df.loc[df["regime_ok"] & (df["z_score"] >  BB_STD_MULT), "signal"] = -1

    c       = df.iloc[-2]   # last COMPLETED candle
    digits  = sym.digits
    sig_str = "BUY" if c["signal"] == 1 else ("SELL" if c["signal"] == -1 else "HOLD")

    # NaN guard
    has_nan = any(pd.isna(c[col]) for col in
                  ["sma", "std", "z_score", "atr", "atr_baseline"])

    atr_ratio = (c["atr"] / c["atr_baseline"]
                 if not pd.isna(c["atr"]) and not pd.isna(c["atr_baseline"])
                    and c["atr_baseline"] > 0
                 else 0.0)

    print(f"{'─' * 65}")
    print(f"  {sym_name}")
    print(f"  Candle time:      {c['time']}")
    print(f"  Close:            {c['close']:.{digits}f}")
    print(f"  SMA({BB_PERIOD}):           {c['sma']:.{digits}f}")
    print(f"  Upper band:       {c['upper_band']:.{digits}f}  (+{BB_STD_MULT}σ)")
    print(f"  Lower band:       {c['lower_band']:.{digits}f}  (-{BB_STD_MULT}σ)")
    print(f"  Z-Score:          {c['z_score']:.4f}  "
          f"{'← ABOVE THRESHOLD' if abs(c['z_score']) > BB_STD_MULT else ''}")
    print(f"  ATR({ATR_PERIOD}):            {c['atr']:.5f}")
    print(f"  ATR Baseline({ATR_BASELINE}):  {c['atr_baseline']:.5f}")
    print(f"  ATR ratio:        {atr_ratio:.3f}x  (limit: {ATR_REGIME_MULT}x)")
    regime_note = "← trading ALLOWED" if c["regime_ok"] else "← BLOCKED by regime filter"
    print(f"  Regime OK:        {c['regime_ok']}  {regime_note}")
    print(f"  Signal:           {c['signal']} ({sig_str})")

    if has_nan:
        print(f"  WARNING:          NaN detected — need more candle history.")

    # --- Position sizing at 1% risk ---
    equity   = account.equity
    risk_amt = equity * (RISK_PCT / 100.0)
    atr_val  = float(c["atr"]) if not pd.isna(c["atr"]) else 0
    sl_dist  = atr_val * 1.5
    sl_pips  = sl_dist / (sym.point * 10) if sl_dist > 0 else 0
    pip_size = sym.point * 10
    pip_val  = (sym.trade_tick_value * (pip_size / sym.trade_tick_size)
                if sym.trade_tick_size > 0 else 10.0)
    raw_lot  = risk_amt / (sl_pips * pip_val) if sl_pips > 0 and pip_val > 0 else 0
    step     = sym.volume_step
    lot      = round(max(sym.volume_min, min(sym.volume_max,
               (raw_lot // step) * step)), 2) if raw_lot > 0 else 0

    print(
        f"\n  Position sizing ({RISK_PCT}% risk) | "
        f"Risk: {risk_amt:.2f} | SL: {sl_pips:.1f} pips | Lot: {lot}"
    )

    # --- Recent signal history ---
    recent = df.iloc[-12:-2]
    fired  = recent[recent["signal"] != 0][["time", "close", "z_score",
                                             "regime_ok", "signal"]]
    if not fired.empty:
        print(f"\n  Recent signals (last 10 candles):")
        for _, row in fired.iterrows():
            s = "BUY" if row["signal"] == 1 else "SELL"
            blocked = "" if row["regime_ok"] else " [BLOCKED by regime]"
            print(
                f"    {row['time']} | {s} | "
                f"Close: {row['close']:.{digits}f} | "
                f"Z: {row['z_score']:.2f}{blocked}"
            )
    else:
        print(f"\n  No signals in the last 10 completed candles.")

print(f"\n{'=' * 65}")

mt5.shutdown()
print("SNAPSHOT COMPLETE. MT5 closed.")
print(
    f"Circuit breaker rule: fires after {CONSECUTIVE_LIMIT} CONSECUTIVE losses.\n"
    f"A WIN resets the streak. Loss-Loss-Win-Loss = streak of 1."
)
