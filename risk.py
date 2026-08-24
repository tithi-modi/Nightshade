"""
Nightshade Seed Engine - risk.py  v4  (Layer 3)

Changes vs v3:
  - Circuit breaker now triggers on 3 CONSECUTIVE losing trades,
    not 2 total losses. A win resets the consecutive counter to zero.
  - State file gains: consecutive_losses (int), last_trade_result (str)
  - record_trade_loss() increments consecutive_losses; fires breaker at 3
  - record_trade_win() resets consecutive_losses to 0
  - MAX_DAILY_LOSSES removed — replaced by CONSECUTIVE_LOSS_LIMIT = 3
  - All other logic unchanged: 1% risk, 3-trade daily cap, live pip value,
    per-symbol SL distance limits, no mt5.initialize/shutdown
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

CONSECUTIVE_LOSS_LIMIT     = 3    # circuit breaker fires after this many
                                   # consecutive losses with no win in between
MAX_DAILY_TRADES           = 3    # hard cap on total trades per UTC day
CIRCUIT_BREAKER_ACTIVE_KEY = "circuit_breaker_active"
STATE_FILE                 = "daily_state.json"

# Maximum SL distance in price terms per symbol.
# Rejects when ATR-based SL is abnormally wide (news spike protection).
# USDJPY: pip = 0.01, so limits are 100x larger than USD pairs.
MAX_SL_DISTANCE = {
    "EURUSD": 0.010,
    "GBPUSD": 0.012,
    "USDJPY": 1.00,
    "AUDUSD": 0.010,
}
DEFAULT_MAX_SL_DISTANCE = 0.012

# ---------------------------------------------------------------------------
# DAILY STATE
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def load_daily_state() -> dict:
    """
    Loads daily state from disk. Auto-resets at UTC midnight.

    State keys:
      date                     : YYYY-MM-DD (UTC)
      start_equity             : equity at first evaluation today
      trades_today             : total trades opened today
      consecutive_losses       : current streak of consecutive losses
                                 resets to 0 on any winning trade
      last_trade_result        : "win", "loss", or None
      circuit_breaker_active   : True blocks all new trades until midnight
    """
    today = _today_str()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == today:
                return state
        except (json.JSONDecodeError, KeyError):
            pass

    # Fresh day
    state = {
        "date":                      today,
        "start_equity":              None,
        "trades_today":              0,
        "consecutive_losses":        0,
        "last_trade_result":         None,
        CIRCUIT_BREAKER_ACTIVE_KEY:  False,
    }
    _save_daily_state(state)
    return state


def _save_daily_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def record_trade_opened() -> None:
    """
    Call immediately after a fill is confirmed.
    Increments trades_today only.
    """
    state = load_daily_state()
    state["trades_today"] += 1
    log.info(
        f"Trade opened. "
        f"Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}. "
        f"Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}."
    )
    _save_daily_state(state)


def record_trade_loss() -> None:
    """
    Call after a trade closes at a loss (stop loss hit).
    Increments consecutive_losses.
    Fires circuit breaker if CONSECUTIVE_LOSS_LIMIT is reached.
    Does NOT reset on this call — only a win resets the streak.

    Example sequence and counter:
      Loss  → consecutive = 1   bot continues
      Loss  → consecutive = 2   bot continues
      Win   → consecutive = 0   bot continues  (reset by record_trade_win)
      Loss  → consecutive = 1   bot continues
      Loss  → consecutive = 2   bot continues
      Loss  → consecutive = 3   CIRCUIT BREAKER FIRES
    """
    state = load_daily_state()
    state["consecutive_losses"] += 1
    state["last_trade_result"]   = "loss"

    log.info(
        f"Loss recorded. "
        f"Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}."
    )

    if state["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
        state[CIRCUIT_BREAKER_ACTIVE_KEY] = True
        log.warning(
            f"CIRCUIT BREAKER ACTIVATED. "
            f"{state['consecutive_losses']} consecutive losses reached the limit of "
            f"{CONSECUTIVE_LOSS_LIMIT}. No new trades until UTC midnight."
        )

    _save_daily_state(state)


def record_trade_win() -> None:
    """
    Call after a trade closes profitably (dynamic TP or static TP hit).
    Resets consecutive_losses to zero — this is the key behaviour.
    Loss, Loss, Win → counter resets → bot can take 3 more losses before stopping.
    """
    state = load_daily_state()
    prev_streak             = state["consecutive_losses"]
    state["consecutive_losses"] = 0
    state["last_trade_result"]  = "win"

    log.info(
        f"Win recorded. Consecutive loss streak reset from {prev_streak} to 0. "
        f"Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}."
    )
    _save_daily_state(state)


def get_streak_status() -> str:
    """Returns a human-readable streak status string for logging."""
    state = load_daily_state()
    cb    = state.get(CIRCUIT_BREAKER_ACTIVE_KEY, False)
    streak = state.get("consecutive_losses", 0)
    trades = state.get("trades_today", 0)
    if cb:
        return f"CIRCUIT BREAKER ACTIVE ({streak} consecutive losses)"
    return (
        f"Streak: {streak}/{CONSECUTIVE_LOSS_LIMIT} consecutive losses | "
        f"Trades: {trades}/{MAX_DAILY_TRADES}"
    )

# ---------------------------------------------------------------------------
# PIP VALUE CALCULATOR
# ---------------------------------------------------------------------------

def _get_pip_value_per_lot(symbol: str, account_currency: str) -> float:
    """
    Monetary value of 1 pip per 1.0 standard lot in account currency.
    Uses live MT5 contract spec — correct for all pairs including JPY.
    """
    sym = mt5.symbol_info(symbol)
    if sym is None:
        log.warning(f"Cannot read symbol info for {symbol}. Defaulting to 10.0.")
        return 10.0

    tick_value = sym.trade_tick_value
    tick_size  = sym.trade_tick_size

    if tick_size == 0:
        log.warning(f"tick_size is 0 for {symbol}. Defaulting to 10.0.")
        return 10.0

    pip_value = tick_value * ((sym.point * 10) / tick_size)
    log.info(f"Pip value | {symbol}: {pip_value:.4f} {account_currency}/lot.")
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

    Guards applied in order:
      1. Signal and ATR validity
      2. Consecutive loss circuit breaker
      3. Daily trade count cap
      4. SL distance sanity check
      5. Position sizing and minimum lot check

    Returns dict with is_approved=True and full order params, or
    is_approved=False with reject_reason. Never returns None.
    Does NOT call mt5.initialize() or mt5.shutdown().
    """

    def reject(reason: str) -> dict:
        log.info(f"Risk REJECT [{symbol}]: {reason}")
        return {"is_approved": False, "reject_reason": reason}

    # --- 1. Basic validation ---
    if signal_type not in ["BUY", "SELL"]:
        return reject(f"Invalid signal type: {signal_type}")
    if atr_val <= 0 or atr_val != atr_val:
        return reject(f"Invalid ATR value: {atr_val}")

    # --- 2. Consecutive loss circuit breaker ---
    state = load_daily_state()
    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY):
        streak = state.get("consecutive_losses", 0)
        return reject(
            f"Circuit breaker active: {streak} consecutive losses. "
            f"Resets at UTC midnight."
        )

    # --- 3. Daily trade count cap ---
    trades_today = state.get("trades_today", 0)
    if trades_today >= MAX_DAILY_TRADES:
        return reject(
            f"Daily trade limit: {trades_today}/{MAX_DAILY_TRADES} reached."
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
    digits           = sym.digits

    # Record start-of-day equity
    if state["start_equity"] is None:
        state["start_equity"] = equity
        _save_daily_state(state)
        log.info(f"Start-of-day equity: {equity:.2f} {account_currency}.")

    # --- 5. SL distance ---
    sl_distance = atr_val * 1.5
    max_sl      = MAX_SL_DISTANCE.get(symbol, DEFAULT_MAX_SL_DISTANCE)
    if sl_distance > max_sl:
        return reject(
            f"SL distance {sl_distance:.5f} > max {max_sl:.5f}. "
            f"Market too volatile."
        )

    sl_pips = sl_distance / (sym.point * 10)
    if sl_pips < 1.0:
        return reject(f"SL {sl_pips:.2f} pips too small — spread risk.")

    # --- 6. Position sizing ---
    risk_amount       = equity * (risk_pct / 100.0)
    pip_value_per_lot = _get_pip_value_per_lot(symbol, account_currency)
    if pip_value_per_lot <= 0:
        return reject("Cannot compute pip value.")

    step     = sym.volume_step
    raw_lot  = risk_amount / (sl_pips * pip_value_per_lot)
    lot_size = (raw_lot // step) * step
    lot_size = max(sym.volume_min, min(sym.volume_max, lot_size))
    lot_size = round(lot_size, 2)

    if lot_size < sym.volume_min:
        return reject(
            f"Lot {lot_size} < broker min {sym.volume_min}. "
            f"Equity {equity:.2f} too low."
        )

    # --- 7. SL / TP prices ---
    if signal_type == "BUY":
        sl_price = round(current_price - sl_distance, digits)
        tp_price = round(current_price + sl_distance * rr_ratio, digits)
    else:
        sl_price = round(current_price + sl_distance, digits)
        tp_price = round(current_price - sl_distance * rr_ratio, digits)

    log.info(
        f"Risk APPROVED [{symbol}] | {signal_type} {lot_size} lots | "
        f"Entry: {current_price:.{digits}f} | "
        f"SL: {sl_price:.{digits}f} | TP: {tp_price:.{digits}f} | "
        f"Risk: {risk_amount:.2f} {account_currency} ({risk_pct}%) | "
        f"SL pips: {sl_pips:.1f} | "
        f"{get_streak_status()}"
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