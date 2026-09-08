# statistics.py
# Nightshade Seed Engine - market snapshot.

import MetaTrader5 as mt5
import pandas as pd
import datetime
import json
import os
from risk import *   # all constants
import risk

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

if not mt5.initialize():
    print("MT5 init failed")
    exit()

account = mt5.account_info()
if account is None:
    print("No account")
    mt5.shutdown()
    exit()

print("="*65)
print("NIGHTSHADE v11 — SNAPSHOT")
print(f"Time: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Account: {account.login} | Equity: {account.equity:.2f} {account.currency}")
print("="*65)

if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
    print(f"Trades today: {state.get('trades_today',0)}/{MAX_DAILY_TRADES}")
    print(f"Consecutive losses: {state.get('consecutive_losses',0)}/{CONSECUTIVE_LOSS_LIMIT}")
    print(f"Breaker: {'ACTIVE' if state.get('circuit_breaker_active',False) else 'OFF'}")

print("\n--- INDICATORS ---")
for sym in SYMBOLS:
    s = mt5.symbol_info(sym)
    if s is None:
        continue
    rates = mt5.copy_rates_from_pos(sym, TIMEFRAME, 0, 150)
    if rates is None:
        continue
    df = pd.DataFrame(rates)
    df = risk.compute_indicators(df, BB_PERIOD, ATR_PERIOD, ATR_BASELINE, BB_STD_MULT, ATR_REGIME_MULT)
    c = df.iloc[-1]
    sig = "BUY" if c["signal"]==1 else "SELL" if c["signal"]==-1 else "HOLD"
    print(f"{sym}: Close {c['close']:.{s.digits}f} | Z {c['z_score']:.2f} | ATR {c['atr']:.5f} | Regime {c['regime_ok']} | Signal {sig}")

mt5.shutdown()
print("Snapshot complete.")