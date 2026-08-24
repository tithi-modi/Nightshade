"""
Nightshade Seed Engine - pull_mt5.py  v5
Standalone diagnostic. Run before every session.

Changes vs v4 (per GitHub issue from tech review):
  P0-3  Standard deviation now uses population stdev (ddof=0) to match
        the spec, not pandas' default sample stdev (ddof=1).
  P0-4  Diagnostics now show margin / margin_free and the full current
        state schema (including last_evaluated candle count and
        circuit-breaker note), matching risk.py v5's daily_state.json.

Usage:
    python pull_mt5.py
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import json
import os
import datetime
import risk

SYMBOLS      = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME    = mt5.TIMEFRAME_M15
FETCH_COUNT  = 120
STATE_FILE   = risk.STATE_FILE
MAGIC_NUMBER = 20260818

BB_PERIOD          = 20
BB_STD_MULT        = 2.5
ATR_PERIOD         = 14
ATR_BASELINE       = 50
ATR_REGIME_MULT    = 1.2
RISK_PCT           = 1.0
RR_RATIO           = 1.5
CONSECUTIVE_LIMIT  = 3
MAX_DAILY_TRADES   = 3


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

print(f"  Terminal:    {terminal.name} | Build: {terminal.build}")
print(f"  Connected:   {terminal.connected}")
print(f"  AutoTrade:   {terminal.trade_allowed}")

if not terminal.trade_allowed:
    print(
        "\n  ACTION REQUIRED:\n"
        "  1. Click 'AutoTrading' in the MT5 toolbar — must turn GREEN.\n"
        "  2. Tools > Options > Expert Advisors > Allow algorithmic trading.\n"
        "  3. Re-run this script."
    )

# ---------------------------------------------------------------------------
# 2. Account Details
# ---------------------------------------------------------------------------

sep("2. ACCOUNT DETAILS")
print(f"  Login:        {account.login}")
print(f"  Broker:       {account.company}")
print(f"  Server:       {account.server}")
print(f"  Currency:     {account.currency}")
print(f"  Balance:      {account.balance:.2f} {account.currency}")
print(f"  Equity:       {account.equity:.2f} {account.currency}")
print(f"  Margin used:  {account.margin:.2f} {account.currency}")
print(f"  Margin free:  {account.margin_free:.2f} {account.currency}")
if account.margin > 0:
    print(f"  Margin level: {account.margin_level:.1f}%")
print(f"  Leverage:     1:{account.leverage}")
mode = "DEMO" if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "LIVE"
print(f"  Mode:         {mode}")
if mode == "LIVE":
    print("  WARNING: LIVE account. Do not run until all P0 issues are fixed and tested on demo.")

# ---------------------------------------------------------------------------
# 3. Symbol Check
# ---------------------------------------------------------------------------

sep("3. SYMBOL CHECK — ALL FOUR PAIRS")

for sym_name in SYMBOLS:
    sym = mt5.symbol_info(sym_name)
    if sym is None:
        print(f"  FAIL: {sym_name} not found. Check broker symbol name.")
        continue
    if not sym.visible:
        mt5.symbol_select(sym_name, True)

    tick       = mt5.symbol_info_tick(sym_name)
    pip_size   = sym.point * 10
    pip_value  = (sym.trade_tick_value * (pip_size / sym.trade_tick_size)
                  if sym.trade_tick_size > 0 else 0.0)
    spread_pip = ((tick.ask - tick.bid) / (sym.point * 10)) if tick else 0.0
    status     = "PASS" if tick else "WARN"

    if tick is None:
        print(f"  WARN: {sym_name} | no current tick; cannot report executable spread.")
        continue
    print(
        f"  {status}: {sym_name} | "
        f"Bid: {tick.bid:.{sym.digits}f} | Ask: {tick.ask:.{sym.digits}f} | "
        f"Spread: {spread_pip:.1f} pips | "
        f"Pip val: {pip_value:.4f} {account.currency}/lot | "
        f"Min lot: {sym.volume_min}"
    )
    if spread_pip > 5.0:
        print(f"         WARNING: Wide spread ({spread_pip:.1f} pips). Avoid trading.")

# ---------------------------------------------------------------------------
# 4. Candle Data & Indicators
# ---------------------------------------------------------------------------

sep("4. CANDLE DATA & INDICATORS — ALL FOUR PAIRS")

for sym_name in SYMBOLS:
    rates = mt5.copy_rates_from_pos(sym_name, TIMEFRAME, 0, FETCH_COUNT)
    sym   = mt5.symbol_info(sym_name)
    if rates is None or len(rates) == 0 or sym is None:
        print(f"  FAIL: {sym_name} — cannot fetch candles.")
        continue
    if len(rates) < FETCH_COUNT:
        print(f"  WARN: {sym_name} — only {len(rates)}/{FETCH_COUNT} candles returned.")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    df = risk.compute_indicators(df, BB_PERIOD, ATR_PERIOD, ATR_BASELINE, BB_STD_MULT, ATR_REGIME_MULT)

    c       = df.iloc[-2]
    sig_str = "BUY" if c["signal"] == 1 else ("SELL" if c["signal"] == -1 else "HOLD")

    nan_cols = [col for col in ["sma", "atr", "atr_baseline", "z_score"] if pd.isna(c[col])]
    nan_note = f" | NaN in {nan_cols}!" if nan_cols else ""

    print(
        f"  {sym_name} | {c['time']} | "
        f"Close: {c['close']:.{sym.digits}f} | Z: {c['z_score']:.2f} | "
        f"ATR: {c['atr']:.5f} | Regime: {c['regime_ok']} | Signal: {sig_str}{nan_note}"
    )

    equity    = account.equity
    risk_amt  = equity * (RISK_PCT / 100.0)
    atr_val   = float(c["atr"]) if not pd.isna(c["atr"]) else 0
    sl_dist   = atr_val * 1.5
    sl_pips   = sl_dist / (sym.point * 10) if sl_dist > 0 else 0
    pip_size  = sym.point * 10
    pip_val   = (sym.trade_tick_value * (pip_size / sym.trade_tick_size)
                 if sym.trade_tick_size > 0 else 0.0)
    raw_lot   = risk_amt / (sl_pips * pip_val) if sl_pips > 0 and pip_val > 0 else 0
    step      = sym.volume_step
    lot       = round((raw_lot // step) * step, 2) if raw_lot > 0 else 0
    lot_note  = ""
    if 0 < lot < sym.volume_min:
        lot_note = f" | REJECT (below broker min {sym.volume_min} — would over-risk if bumped up)"
        lot = 0

    print(f"         Sizing | Risk: {risk_amt:.2f} | SL: {sl_pips:.1f} pips | Lot: {lot}{lot_note}")

# ---------------------------------------------------------------------------
# 5. Daily State — consecutive loss streak
# ---------------------------------------------------------------------------

sep("5. DAILY STATE — CONSECUTIVE LOSS CIRCUIT BREAKER")

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        stale = state.get("date") != today

        print(f"  Date:               {state.get('date')} {'(STALE — resets on first cycle)' if stale else '(TODAY)'}")
        print(f"  Start equity:       {state.get('start_equity')}")
        print(f"  Trades today:       {state.get('trades_today', 0)} / {MAX_DAILY_TRADES}")

        streak = state.get("consecutive_losses", 0)
        last   = state.get("last_trade_result", None)
        cb     = state.get("circuit_breaker_active", False)

        streak_bar = "█" * streak + "░" * max(0, CONSECUTIVE_LIMIT - streak)
        print(f"  Consecutive losses: {streak} / {CONSECUTIVE_LIMIT}  [{streak_bar}]")
        print(f"  Last trade result:  {last if last else 'None yet today'}")
        print(f"  Circuit breaker:    {'ACTIVE — no trades today' if cb else 'OFF'}")

        last_eval = state.get("last_evaluated", {})
        if last_eval:
            print(f"  Last evaluated candles: {last_eval}")

        print(
            "\n  NOTE: this file is a CACHE. main.py reconciles authoritative state "
            "from MT5 trade history on every startup — this display may lag until "
            "the bot next runs."
        )

        if cb:
            print(
                f"\n  NOTE: Circuit breaker fires after {CONSECUTIVE_LIMIT} CONSECUTIVE losses.\n"
                f"  A win resets the streak to 0. Loss-Win-Loss = streak of 1, NOT 2."
            )
        if state.get("trades_today", 0) >= MAX_DAILY_TRADES:
            print(f"\n  NOTE: Daily trade limit ({MAX_DAILY_TRADES}) already reached.")

    except Exception as e:
        print(f"  WARNING: Cannot read state file: {e}")
else:
    print("  No state file found. Created automatically on first cycle.")
    print(
        f"  Circuit breaker fires after {CONSECUTIVE_LIMIT} consecutive losses.\n"
        f"  A winning trade resets the streak — Loss Loss Win Loss = streak of 1."
    )

# ---------------------------------------------------------------------------
# 6. Open Positions
# ---------------------------------------------------------------------------

sep("6. OPEN POSITIONS — ALL PAIRS")

all_pos = mt5.positions_get()
if all_pos is None:
    print("  FAIL: positions_get() returned None — cannot confirm open positions. Treat as unknown, not zero.")
    our_pos = []
else:
    our_pos = [p for p in all_pos if p.magic == MAGIC_NUMBER]

if all_pos is not None and not our_pos:
    print("  No open positions for this bot.")
else:
    for p in our_pos:
        direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        sym       = mt5.symbol_info(p.symbol)
        digits    = sym.digits if sym else 5
        print(
            f"  {p.symbol} | {direction} {p.volume} lots | "
            f"Open: {p.price_open:.{digits}f} | SL: {p.sl:.{digits}f} | TP: {p.tp:.{digits}f} | "
            f"P&L: {p.profit:.2f} {account.currency} | Ticket: #{p.ticket}"
        )

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

mt5.shutdown()
sep("DIAGNOSTIC COMPLETE")
print("  MT5 closed.")
print("  All PASS/WARN = ready. Run: python main.py")
print()
