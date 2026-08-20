"""
Nightshade Seed Engine - pull_mt5.py  v3
Standalone diagnostic. Run before starting main.py each session.
Checks all four trading pairs.

Usage:
    python pull_mt5.py
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import json
import os
import datetime

SYMBOLS      = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME    = mt5.TIMEFRAME_M15
FETCH_COUNT  = 120
STATE_FILE   = "daily_state.json"
MAGIC_NUMBER = 20260818

# Strategy parameters — keep in sync with main.py
BB_PERIOD       = 20
BB_STD_MULT     = 2.5
ATR_PERIOD      = 14
ATR_BASELINE    = 50
ATR_REGIME_MULT = 1.2
RISK_PCT        = 1.0
RR_RATIO        = 1.5


def sep(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


# ---------------------------------------------------------------------------
# 1. MT5 Connection
# ---------------------------------------------------------------------------

sep("1. MT5 CONNECTION")

if not mt5.initialize():
    code, msg = mt5.last_error()
    print(f"FAIL: MT5 init failed. Code {code}: {msg}")
    print("Is MT5 terminal open with AutoTrading enabled?")
    raise SystemExit(1)

terminal = mt5.terminal_info()
account  = mt5.account_info()

if terminal is None or account is None:
    print("FAIL: Cannot read terminal/account info.")
    mt5.shutdown()
    raise SystemExit(1)

print(f"  Terminal:  {terminal.name} | Build: {terminal.build}")
print(f"  Connected: {terminal.connected}")
print(f"  AutoTrade: {terminal.trade_allowed}")

if not terminal.trade_allowed:
    print(
        "\n  ACTION REQUIRED: Enable AutoTrading.\n"
        "  1. Click 'AutoTrading' button in MT5 toolbar (turns green).\n"
        "  2. Tools > Options > Expert Advisors > Allow algorithmic trading.\n"
        "  3. Re-run this script to confirm."
    )

# ---------------------------------------------------------------------------
# 2. Account Details
# ---------------------------------------------------------------------------

sep("2. ACCOUNT DETAILS")
print(f"  Login:     {account.login}")
print(f"  Broker:    {account.company}")
print(f"  Server:    {account.server}")
print(f"  Currency:  {account.currency}")
print(f"  Balance:   {account.balance:.2f} {account.currency}")
print(f"  Equity:    {account.equity:.2f} {account.currency}")
print(f"  Leverage:  1:{account.leverage}")
mode_str = "DEMO" if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "LIVE"
print(f"  Mode:      {mode_str}")
if mode_str == "LIVE":
    print("  WARNING: This is a LIVE account. Verify everything on DEMO first.")

# ---------------------------------------------------------------------------
# 3. Symbol Check — all four pairs
# ---------------------------------------------------------------------------

sep("3. SYMBOL CHECK — ALL FOUR PAIRS")

symbol_ok = True
for sym_name in SYMBOLS:
    sym = mt5.symbol_info(sym_name)
    if sym is None:
        print(f"  FAIL: {sym_name} not found. Check broker's exact symbol name.")
        symbol_ok = False
        continue

    if not sym.visible:
        mt5.symbol_select(sym_name, True)

    tick = mt5.symbol_info_tick(sym_name)
    pip_size  = sym.point * 10
    pip_value = sym.trade_tick_value * (pip_size / sym.trade_tick_size) \
                if sym.trade_tick_size > 0 else 0.0
    spread_pips = (tick.ask - tick.bid) / (sym.point * 10) if tick else 0.0

    status = "PASS" if tick else "WARN"
    print(
        f"  {status}: {sym_name} | "
        f"Bid: {tick.bid:.{sym.digits}f} | Ask: {tick.ask:.{sym.digits}f} | "
        f"Spread: {spread_pips:.1f} pips | "
        f"Pip val: {pip_value:.4f} {account.currency}/lot | "
        f"Min lot: {sym.volume_min}"
    )
    if spread_pips > 5.0:
        print(f"         WARNING: {sym_name} spread is wide ({spread_pips:.1f} pips). "
              f"Avoid trading now.")

if not symbol_ok:
    print("\n  One or more symbols missing. Fix before running main.py.")

# ---------------------------------------------------------------------------
# 4. Candle Data and Indicators — all four pairs
# ---------------------------------------------------------------------------

sep("4. CANDLE DATA & INDICATORS — ALL FOUR PAIRS")

for sym_name in SYMBOLS:
    rates = mt5.copy_rates_from_pos(sym_name, TIMEFRAME, 0, FETCH_COUNT)
    sym   = mt5.symbol_info(sym_name)
    if rates is None or len(rates) == 0 or sym is None:
        print(f"  FAIL: {sym_name} — cannot fetch candle data.")
        continue

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # Z-Score
    df["sma"]     = df["close"].rolling(BB_PERIOD).mean()
    df["std"]     = df["close"].rolling(BB_PERIOD).std()
    df["z_score"] = (df["close"] - df["sma"]) / df["std"]

    # ATR
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["tr"]           = np.maximum(hl, np.maximum(hc, lc))
    df["atr"]          = df["tr"].rolling(ATR_PERIOD).mean()
    df["atr_baseline"] = df["atr"].rolling(ATR_BASELINE).mean()
    df["regime_ok"]    = df["atr"] < (df["atr_baseline"] * ATR_REGIME_MULT)

    # Signal
    df["signal"] = 0
    df.loc[df["regime_ok"] & (df["z_score"] < -BB_STD_MULT), "signal"] =  1
    df.loc[df["regime_ok"] & (df["z_score"] >  BB_STD_MULT), "signal"] = -1

    c       = df.iloc[-2]
    sig_str = "BUY" if c["signal"] == 1 else ("SELL" if c["signal"] == -1 else "HOLD")

    # NaN check
    nan_cols = [col for col in ["sma", "atr", "atr_baseline", "z_score"]
                if pd.isna(c[col])]
    nan_warn = f" | NaN in {nan_cols} — fetch more candles!" if nan_cols else ""

    print(
        f"  {sym_name} | Candle: {c['time']} | "
        f"Close: {c['close']:.{sym.digits}f} | "
        f"Z: {c['z_score']:.2f} | "
        f"ATR: {c['atr']:.5f} | "
        f"Regime OK: {c['regime_ok']} | "
        f"Signal: {sig_str}{nan_warn}"
    )

    # Position sizing simulation
    equity     = account.equity
    risk_amt   = equity * (RISK_PCT / 100.0)
    pip_size   = sym.point * 10
    pip_value  = (sym.trade_tick_value * (pip_size / sym.trade_tick_size)
                  if sym.trade_tick_size > 0 else 10.0)
    sl_dist    = float(c["atr"]) * 1.5 if not pd.isna(c["atr"]) else 0
    sl_pips    = sl_dist / (sym.point * 10) if sl_dist > 0 else 0
    raw_lot    = risk_amt / (sl_pips * pip_value) if sl_pips > 0 else 0
    step       = sym.volume_step
    lot_size   = (raw_lot // step) * step if raw_lot > 0 else 0
    lot_size   = max(sym.volume_min, min(sym.volume_max, lot_size)) if lot_size > 0 else 0
    lot_size   = round(lot_size, 2)

    print(
        f"         Sizing sim | Risk: {risk_amt:.2f} | "
        f"SL: {sl_pips:.1f} pips | Lot: {lot_size}"
    )

# ---------------------------------------------------------------------------
# 5. Daily State File
# ---------------------------------------------------------------------------

sep("5. DAILY STATE FILE")

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        stale = state.get("date") != today
        print(f"  Date:            {state.get('date')} {'(STALE - resets on first cycle)' if stale else '(TODAY)'}")
        print(f"  Start equity:    {state.get('start_equity')}")
        print(f"  Trades today:    {state.get('trades_today', 0)} / 3")
        print(f"  Losses today:    {state.get('losses_today', 0)} / 2")
        print(f"  Circuit breaker: {state.get('circuit_breaker_active')}")
        if state.get("circuit_breaker_active"):
            print("  WARNING: Circuit breaker ACTIVE. Bot will not trade today.")
        if state.get("trades_today", 0) >= 3:
            print("  WARNING: Daily trade limit (3) already reached. No more trades today.")
    except Exception as e:
        print(f"  WARNING: Cannot read state file: {e}")
else:
    print("  No state file. Created automatically on first cycle.")

# ---------------------------------------------------------------------------
# 6. Open Positions — all pairs
# ---------------------------------------------------------------------------

sep("6. OPEN POSITIONS — ALL PAIRS")

all_positions = mt5.positions_get()
our_positions = [p for p in all_positions if p.magic == MAGIC_NUMBER] \
                if all_positions else []

if not our_positions:
    print("  No open positions for this bot.")
else:
    for p in our_positions:
        direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        sym       = mt5.symbol_info(p.symbol)
        digits    = sym.digits if sym else 5
        print(
            f"  {p.symbol} | {direction} {p.volume} lots | "
            f"Open: {p.price_open:.{digits}f} | "
            f"Current: {p.price_current:.{digits}f} | "
            f"SL: {p.sl:.{digits}f} | TP: {p.tp:.{digits}f} | "
            f"P&L: {p.profit:.2f} {account.currency} | "
            f"Ticket: #{p.ticket}"
        )

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

mt5.shutdown()

sep("DIAGNOSTIC COMPLETE")
print("  MT5 connection closed.")
print("  If all checks show PASS/WARN (not FAIL), run: python main.py")
print()
