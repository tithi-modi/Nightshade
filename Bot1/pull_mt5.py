# pull_mt5.py
# Nightshade Seed Engine - diagnostics.

import MetaTrader5 as mt5
import pandas as pd
import datetime
import json
import os
from risk import *   # all constants
import risk

def sep(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

if not mt5.initialize():
    print("MT5 init failed")
    exit()

account = mt5.account_info()
if account is None:
    print("No account")
    mt5.shutdown()
    exit()

sep("1. CONNECTION")
print(f"Terminal: {mt5.terminal_info().name}")
print(f"Connected: {mt5.terminal_info().connected}")
print(f"AutoTrading: {mt5.terminal_info().trade_allowed}")

sep("2. ACCOUNT")
print(f"Login: {account.login}, Server: {account.server}")
print(f"Balance: {account.balance:.2f} {account.currency}")
print(f"Equity: {account.equity:.2f}")
print(f"Margin free: {account.margin_free:.2f}")
print(f"Leverage: 1:{account.leverage}")

sep("3. SYMBOLS")
for sym in SYMBOLS:
    s = mt5.symbol_info(sym)
    if s is None:
        print(f"FAIL: {sym} not found")
        continue
    tick = mt5.symbol_info_tick(sym)
    if tick:
        spread = (tick.ask - tick.bid) / (s.point * (10 ** (s.digits - 1)))
        print(f"{sym}: Bid {tick.bid:.{s.digits}f} Ask {tick.ask:.{s.digits}f} Spread {spread:.1f} pips")
    else:
        print(f"{sym}: no tick")

sep("4. INDICATORS")
for sym in SYMBOLS:
    rates = mt5.copy_rates_from_pos(sym, TIMEFRAME, 0, 150)
    if rates is None:
        print(f"{sym}: no data")
        continue
    df = pd.DataFrame(rates)
    df = risk.compute_indicators(df, BB_PERIOD, ATR_PERIOD, ATR_BASELINE, BB_STD_MULT, ATR_REGIME_MULT)
    c = df.iloc[-1]
    sig = "BUY" if c["signal"]==1 else "SELL" if c["signal"]==-1 else "HOLD"
    print(f"{sym} | Close {c['close']:.{5}f} | Z {c['z_score']:.2f} | Regime {c['regime_ok']} | Signal {sig}")

sep("5. DAILY STATE")
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
    print(f"Date: {state.get('date')}")
    print(f"Trades: {state.get('trades_today',0)}/{MAX_DAILY_TRADES}")
    print(f"Consecutive losses: {state.get('consecutive_losses',0)}/{CONSECUTIVE_LOSS_LIMIT}")
    print(f"Breaker: {'ACTIVE' if state.get('circuit_breaker_active',False) else 'OFF'}")

sep("6. OPEN POSITIONS")
positions = mt5.positions_get()
if positions:
    for p in [p for p in positions if p.magic == MAGIC_NUMBER]:
        print(f"{p.symbol} {p.type} vol {p.volume} open {p.price_open:.5f} SL {p.sl:.5f} TP {p.tp:.5f} PnL {p.profit:.2f}")
else:
    print("No positions")

mt5.shutdown()
print("\nDiagnostic complete.")