"""
Nightshade Seed Engine - risk.py  v3  (Layer 3)
Position sizing, SL/TP calculation, and all protective guards.

Changes vs v2:
  - MAX_DAILY_TRADES = 3 enforced (was: only loss-count circuit breaker)
  - trades_today counter increments on every APPROVED trade (win or loss)
  - RISK_PCT default changed to 1.0 (was: 1.5)
  - MAX_DAILY_LOSSES = 2 preserved — fires at 2 losses regardless of trade count
  - Circuit breaker still fires if 2 losses hit before 3-trade limit
  - Per-symbol SL distance limits updated for USDJPY (JPY pairs have
    different pip scale — 1 pip = 0.01 not 0.0001)
  - record_trade_loss() and record_trade_win() preserved for caller use
  - No mt5.initialize() / mt5.shutdown() — connection owned by main.py
"""

import MetaTrader5 as mt5
import json
import os
import datetime
import logging

log = logging.getLogger("nightshade")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

MAX_DAILY_TRADES         = 3     # hard cap on total trades per UTC day across all pairs
MAX_DAILY_LOSSES         = 2     # circuit breaker fires after this many losing trades
                                  # At 1% risk: 2 losses = -2% drawdown (safe below 5% limit)

CIRCUIT_BREAKER_ACTIVE_KEY = "circuit_breaker_active"
STATE_FILE                 = "daily_state.json"

# Maximum SL distance in price terms per symbol.
# Rejects trades when ATR-based SL is abnormally wide (news spike protection).
# USDJPY: 1 pip = 0.01 (2-decimal broker) so limits are 100x larger than USD pairs.
MAX_SL_DISTANCE = {
    "EURUSD": 0.010,   # 100 pips max
    "GBPUSD": 0.012,   # 120 pips max (GBP is more volatile)
    "USDJPY": 1.00,    # 100 pips max (JPY pip = 0.01)
    "AUDUSD": 0.010,   # 100 pips max
}
DEFAULT_MAX_SL_DISTANCE = 0.012  # fallback for any unlisted symbol

# ---------------------------------------------------------------------------
# DAILY STATE
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def load_daily_state() -> dict:
    """
    Loads daily state from disk. Resets automatically at UTC midnight.

    State keys:
      date                     : YYYY-MM-DD (UTC)
      start_equity             : equity at first evaluation of the day
      losses_today             : count of trades closed at a loss
      trades_today             : count of ALL trades taken today (win or loss)
      circuit_breaker_active   : bool — blocks new trades if True
    """
    today = _today_str()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == today:
                return state
        except (json.JSONDecodeError, KeyError):
            pass  # corrupted file — reset

    state = {
        "date":                      today,
        "start_equity":              None,
        "losses_today":              0,
        "trades_today":              0,
        CIRCUIT_BREAKER_ACTIVE_KEY:  False,
    }
    _save_daily_state(state)
    return state


def _save_daily_state(state: dict) -> None:
    """Atomic write using temp-file-then-rename."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def record_trade_opened() -> None:
    """
    Call this immediately after a trade is confirmed opened.
    Increments trades_today. Does NOT affect the loss counter.
    """
    state = load_daily_state()
    state["trades_today"] += 1
    log.info(
        f"Trade opened. Trades today: {state['trades_today']} / {MAX_DAILY_TRADES}."
    )
    _save_daily_state(state)


def record_trade_loss() -> None:
    """
    Call this after a trade closes at a loss (SL hit).
    Increments loss counter and activates circuit breaker if limit reached.
    Note: trades_today was already incremented when the trade opened.
    """
    state = load_daily_state()
    state["losses_today"] += 1
    log.info(
        f"Loss recorded. Losses today: {state['losses_today']} / {MAX_DAILY_LOSSES}."
    )
    if state["losses_today"] >= MAX_DAILY_LOSSES:
        state[CIRCUIT_BREAKER_ACTIVE_KEY] = True
        log.warning(
            f"CIRCUIT BREAKER ACTIVATED. "
            f"{state['losses_today']} losses hit the daily limit of {MAX_DAILY_LOSSES}. "
            f"No new trades until UTC midnight."
        )
    _save_daily_state(state)


def record_trade_win() -> None:
    """
    Call this after a trade closes profitably.
    Logged for audit; does not affect circuit breaker.
    """
    state = load_daily_state()
    log.info(
        f"Win recorded. Losses today: {state['losses_today']} / {MAX_DAILY_LOSSES}. "
        f"Trades today: {state['trades_today']} / {MAX_DAILY_TRADES}."
    )

# ---------------------------------------------------------------------------
# PIP VALUE CALCULATOR
# ---------------------------------------------------------------------------

def _get_pip_value_per_lot(symbol: str, account_currency: str) -> float:
    """
    Computes the monetary value of 1 pip per 1.0 standard lot
    in the account's currency, using live MT5 contract spec.

    For 5-decimal brokers (EURUSD, GBPUSD, AUDUSD): 1 pip = 10 points.
    For 3-decimal brokers (USDJPY): 1 pip = 10 points of 0.001 = 0.01.
    """
    sym = mt5.symbol_info(symbol)
    if sym is None:
        log.warning(f"Cannot read symbol info for {symbol}. Defaulting pip value to 10.0.")
        return 10.0

    tick_value = sym.trade_tick_value   # account currency per tick per lot
    tick_size  = sym.trade_tick_size    # price per tick

    if tick_size == 0:
        log.warning(f"tick_size is 0 for {symbol}. Defaulting pip value to 10.0.")
        return 10.0

    point     = sym.point
    pip_size  = point * 10
    pip_value = tick_value * (pip_size / tick_size)

    log.info(
        f"Pip value | {symbol}: {pip_value:.4f} {account_currency}/lot."
    )
    return pip_value

# ---------------------------------------------------------------------------
# MAIN RISK EVALUATION
# ---------------------------------------------------------------------------

def evaluate_risk(
    signal_type:   str,
    current_price: float,
    atr_val:       float,
    risk_pct:      float = 1.0,
    rr_ratio:      float = 1.5,
    symbol:        str   = "EURUSD",
) -> dict:
    """
    Evaluates whether a trade should be taken and computes exact parameters.

    Returns dict with is_approved=True and full order params if approved.
    Returns dict with is_approved=False and reject_reason if rejected.
    Never returns None.

    Guards applied in order:
      1. Signal validity
      2. ATR validity
      3. Circuit breaker (2-loss limit)
      4. Daily trade count (3-trade limit)
      5. SL distance sanity (max pips guard)
      6. Minimum lot size (account too small check)

    Does NOT call mt5.initialize() / mt5.shutdown().
    """

    def reject(reason: str) -> dict:
        log.info(f"Risk REJECT [{symbol}]: {reason}")
        return {"is_approved": False, "reject_reason": reason}

    # --- 1. Basic validation ---
    if signal_type not in ["BUY", "SELL"]:
        return reject(f"Invalid signal type: {signal_type}")

    if atr_val <= 0 or atr_val != atr_val:  # atr_val != atr_val catches NaN
        return reject(f"Invalid ATR value: {atr_val}")

    # --- 2. Circuit breaker ---
    state = load_daily_state()
    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY):
        return reject(
            f"Circuit breaker active ({state['losses_today']} losses today). "
            f"Resumes at UTC midnight."
        )

    # --- 3. Daily trade count limit ---
    trades_today = state.get("trades_today", 0)
    if trades_today >= MAX_DAILY_TRADES:
        return reject(
            f"Daily trade limit reached: {trades_today}/{MAX_DAILY_TRADES}. "
            f"No more trades today."
        )

    # --- 4. MT5 account and symbol data ---
    account = mt5.account_info()
    sym     = mt5.symbol_info(symbol)

    if account is None:
        return reject("mt5.account_info() returned None.")
    if sym is None:
        return reject(f"mt5.symbol_info({symbol}) returned None.")

    equity           = account.equity
    account_currency = account.currency
    point            = sym.point
    digits           = sym.digits

    # --- 5. Record start-of-day equity on first evaluation ---
    if state["start_equity"] is None:
        state["start_equity"] = equity
        _save_daily_state(state)
        log.info(
            f"Start-of-day equity: {equity:.2f} {account_currency}."
        )

    # --- 6. SL distance ---
    sl_distance = atr_val * 1.5
    max_sl      = MAX_SL_DISTANCE.get(symbol, DEFAULT_MAX_SL_DISTANCE)

    if sl_distance > max_sl:
        return reject(
            f"SL distance {sl_distance:.5f} > max {max_sl:.5f}. "
            f"Market too volatile (news spike?)."
        )

    sl_pips = sl_distance / (point * 10)

    if sl_pips < 1.0:
        return reject(
            f"SL distance {sl_pips:.2f} pips too small. "
            f"Spread would consume the stop."
        )

    # --- 7. Position sizing ---
    risk_amount       = equity * (risk_pct / 100.0)
    pip_value_per_lot = _get_pip_value_per_lot(symbol, account_currency)

    if pip_value_per_lot <= 0:
        return reject("Cannot compute pip value.")

    raw_lot  = risk_amount / (sl_pips * pip_value_per_lot)
    step     = sym.volume_step
    lot_size = (raw_lot // step) * step          # always round DOWN
    lot_size = max(sym.volume_min, min(sym.volume_max, lot_size))
    lot_size = round(lot_size, 2)

    if lot_size < sym.volume_min:
        return reject(
            f"Computed lot {lot_size} < broker minimum {sym.volume_min}. "
            f"Account equity {equity:.2f} too low."
        )

    # --- 8. SL / TP prices ---
    if signal_type == "BUY":
        sl_price = current_price - sl_distance
        tp_price = current_price + (sl_distance * rr_ratio)
    else:
        sl_price = current_price + sl_distance
        tp_price = current_price - (sl_distance * rr_ratio)

    sl_price = round(sl_price, digits)
    tp_price = round(tp_price, digits)

    log.info(
        f"Risk APPROVED [{symbol}] | {signal_type} {lot_size} lots | "
        f"Entry: {current_price:.{digits}f} | "
        f"SL: {sl_price:.{digits}f} | TP: {tp_price:.{digits}f} | "
        f"Risk: {risk_amount:.2f} {account_currency} ({risk_pct}%) | "
        f"SL: {sl_pips:.1f} pips | "
        f"Trades today after this: {trades_today + 1}/{MAX_DAILY_TRADES}"
    )

    return {
        "is_approved":   True,
        "symbol":        symbol,
        "signal":        signal_type,
        "equity":        equity,
        "lot_size":      lot_size,
        "entry_price":   current_price,
        "sl_price":      sl_price,
        "tp_price":      tp_price,
        "risk_amount":   round(risk_amount, 2),
        "sl_pips":       round(sl_pips, 1),
        "reject_reason": None,
    }
