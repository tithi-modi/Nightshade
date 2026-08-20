"""
Nightshade Seed Engine - statistics.py  v3
Standalone analysis script. NOT imported by main.py.
Prints current indicator state for all four pairs.
Run manually to inspect market conditions before a session.

Usage:
    python statistics.py
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

SYMBOLS      = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME    = mt5.TIMEFRAME_M15
FETCH_COUNT  = 120

# Strategy parameters — keep in sync with main.py
BB_PERIOD       = 20
BB_STD_MULT     = 2.5
ATR_PERIOD      = 14
ATR_BASELINE    = 50
ATR_REGIME_MULT = 1.2

# ---------------------------------------------------------------------------
# Initialize
# ---------------------------------------------------------------------------

if not mt5.initialize():
    print("MT5 Initialization Failed:", mt5.last_error())
    quit()

account = mt5.account_info()
print("=" * 60)
print("NIGHTSHADE — LAYER 2 STRATEGY ENGINE SNAPSHOT")
print(f"Account: {account.login} | Equity: {account.equity:.2f} {account.currency}")
print("=" * 60)

# ---------------------------------------------------------------------------
# Evaluate each symbol
# ---------------------------------------------------------------------------

for sym_name in SYMBOLS:
    sym = mt5.symbol_info(sym_name)
    if sym is None:
        print(f"\n{sym_name}: NOT FOUND on this broker.")
        continue

    rates = mt5.copy_rates_from_pos(sym_name, TIMEFRAME, 0, FETCH_COUNT)
    if rates is None or len(rates) == 0:
        print(f"\n{sym_name}: Failed to fetch candle data.")
        continue

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # --- Z-Score / Bollinger Band ---
    df["sma"]        = df["close"].rolling(BB_PERIOD).mean()
    df["std"]        = df["close"].rolling(BB_PERIOD).std()
    df["upper_band"] = df["sma"] + (df["std"] * BB_STD_MULT)
    df["lower_band"] = df["sma"] - (df["std"] * BB_STD_MULT)
    df["z_score"]    = (df["close"] - df["sma"]) / df["std"]

    # --- ATR Regime Filter ---
    # PRESERVED: The regime filter is the key volatility guard.
    # Trading only allowed when ATR(14) < ATR_BASELINE(50) * 1.2
    # This blocks entries during news spikes and abnormal vol.
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["tr"]           = np.maximum(hl, np.maximum(hc, lc))
    df["atr"]          = df["tr"].rolling(ATR_PERIOD).mean()
    df["atr_baseline"] = df["atr"].rolling(ATR_BASELINE).mean()
    df["regime_ok"]    = df["atr"] < (df["atr_baseline"] * ATR_REGIME_MULT)

    # --- Signals ---
    # Only fires when BOTH regime_ok AND Z-score crosses threshold.
    df["signal"] = 0
    df.loc[df["regime_ok"] & (df["z_score"] < -BB_STD_MULT), "signal"] =  1  # BUY
    df.loc[df["regime_ok"] & (df["z_score"] >  BB_STD_MULT), "signal"] = -1  # SELL

    # Evaluate last COMPLETED candle (iloc[-2] avoids repainting)
    c       = df.iloc[-2]
    sig_str = "BUY" if c["signal"] == 1 else ("SELL" if c["signal"] == -1 else "HOLD")
    digits  = sym.digits

    # --- NaN guard ---
    has_nan = any(pd.isna(c[col]) for col in
                  ["sma", "std", "z_score", "atr", "atr_baseline"])

    print(f"\n{'─' * 60}")
    print(f"  Pair:            {sym_name}")
    print(f"  Candle time:     {c['time']}")
    print(f"  Close price:     {c['close']:.{digits}f}")
    print(f"  SMA({BB_PERIOD}):         {c['sma']:.{digits}f}")
    print(f"  Upper band:      {c['upper_band']:.{digits}f}")
    print(f"  Lower band:      {c['lower_band']:.{digits}f}")
    print(f"  Z-Score:         {c['z_score']:.4f}")
    print(f"  ATR({ATR_PERIOD}):          {c['atr']:.5f}")
    print(f"  ATR Baseline({ATR_BASELINE}): {c['atr_baseline']:.5f}")
    print(f"  ATR / Baseline:  {(c['atr'] / c['atr_baseline']):.3f}x "
          f"(limit: {ATR_REGIME_MULT}x)")
    print(f"  Regime OK:       {c['regime_ok']}  "
          f"{'← trading allowed' if c['regime_ok'] else '← BLOCKED by regime filter'}")
    print(f"  Signal:          {c['signal']} ({sig_str})")
    if has_nan:
        print(f"  WARNING:         NaN values detected. "
              f"Increase FETCH_COUNT or wait for more history.")

    # --- Recent signal history (last 10 completed candles) ---
    recent = df.iloc[-12:-2]   # 10 completed candles before the latest
    recent_signals = recent[recent["signal"] != 0][["time", "close", "z_score", "signal"]]
    if not recent_signals.empty:
        print(f"\n  Recent signals (last 10 candles):")
        for _, row in recent_signals.iterrows():
            s = "BUY" if row["signal"] == 1 else "SELL"
            print(
                f"    {row['time']} | {s} | "
                f"Close: {row['close']:.{digits}f} | "
                f"Z: {row['z_score']:.2f}"
            )
    else:
        print(f"\n  No signals in the last 10 completed candles.")

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

mt5.shutdown()

print(f"\n{'=' * 60}")
print("SNAPSHOT COMPLETE. MT5 connection closed.")
print("=" * 60)
