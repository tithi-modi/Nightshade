"""
Nightshade Seed Engine - risk.py  v5  (Layer 3)

Changes vs v4 (per GitHub issue from tech review):
  P0-1  Consecutive-loss logic was already correct in v4 (win resets to 0,
        loss increments, breaker fires at CONSECUTIVE_LOSS_LIMIT). Left in
        place. Added a startup note: with a 3-consecutive-loss breaker AND
        a 3-trade/day cap, the breaker can in practice only fire on a day
        with zero wins. This is intentional per the spec but is logged
        explicitly now so it isn't a silent surprise.
  P0-2  Win/loss is no longer decided by a floating-P&L snapshot. That
        logic now lives in main.py's position monitor, which must use
        realized deal P&L from MT5 history. This module now exposes
        reconcile_state_from_history() so daily_state.json can be rebuilt
        from MT5's own trade history on every startup (item 14) instead of
        being trusted blindly.
  P0-5  record_trade... unchanged, but evaluate_risk() no longer clamps a
        too-small lot UP to the broker minimum. If the risk-correct lot is
        below volume_min, the trade is rejected outright (never take more
        risk than configured just to satisfy a broker floor).
  P1-10 New check_portfolio_exposure() — aggregate USD exposure and
        correlated-position limits, called by main.py before execution.
  P1-13 New _broker_estimated_loss() uses mt5.order_calc_profit() (or
        order_calc_margin() for margin) where available, falling back to
        the manual pip-value formula only if the broker call fails. This
        prefers MT5's own math over hand-rolled pip-value logic.
  P0-6  Helper is_position_open() added: distinguishes "no position" from
        "MT5 error" by checking for None explicitly (fail closed).
  All other v4 behaviour preserved: 1% risk, 3-trade daily cap, per-symbol
  SL distance limits, no mt5.initialize/shutdown here.
"""

"""
Nightshade Seed Engine - risk.py  v6  (Layer 3)

Changes vs v5:
  - Added In-Flight Risk Tracking: _IN_FLIGHT_RISK_AMOUNT and _IN_FLIGHT_TRADES accumulators.
  - Added Risk Lifecycle Handlers: add_in_flight_risk() and clear_in_flight_risk().
  - Added get_total_portfolio_risk(): Explicit calculation including active positions and in-flight risk.
  - Added Currency Exposure Matrix: parse_currencies() helper and currency matrix tracking in exposure checks.
  - Updated check_portfolio_exposure(): Incorporates in-flight risk and pending trade exposure checks.
"""

"""
Nightshade Seed Engine - risk.py  v7  (Layer 3)

Changes vs v6:
  - Updated MAX_DAILY_TRADES limit from 3 to 5 while preserving CONSECUTIVE_LOSS_LIMIT at 3.
  - Added calculate_ratchet_sl() function to evaluate unrealized R-multiple profit and return target ratchet Stop Loss levels.
"""

"""
Nightshade Seed Engine - risk.py  v8  (Layer 3)

Changes vs v7:
  - Added Peak Giveback & Circuit Breaker constants (GIVEBACK_ACTIVATION_EUR, GIVEBACK_TIERS, HARD_GIVEBACK_CAP_PCT, DECLINE_NOISE_TOLERANCE).
  - Added Time-Decay TP schedule constants (TIME_DECAY_TP_R).
  - Added calculate_time_decay_tp() for time-decay profit target recalculations.
  - Added evaluate_hard_giveback_cap(), evaluate_giveback_exit(), and evaluate_decline_to_zero() pure exit evaluation functions.
"""

"""
Nightshade Seed Engine - risk.py  v9  (Layer 3)

Changes vs v8:
  - Added calculate_dynamic_rr() helper to dynamically derive entry R:R from swing structure (clamped 1.2R–3.0R).
  - Updated evaluate_risk() to accept swing levels (swing_high, swing_low) and tp_price_override.
  - Added HARD_PROFIT_LOCK_EUR = 70.0 constant and evaluate_profit_lock_exit() priority #0 exit guard.
  - Added BREAKEVEN_FEE_BUFFER_PIPS = 1.0 constant and updated calculate_ratchet_sl() with monetary & fee-inclusive breakeven triggers.
  - Added price_for_profit() helper to compute explicit SL prices for monetary target floors.
"""

import MetaTrader5 as mt5
import json
import os
import tempfile
import datetime
import logging
import math
from pathlib import Path

log = logging.getLogger("nightshade")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

CONSECUTIVE_LOSS_LIMIT     = 3    # circuit breaker fires after this many
                                   # consecutive losses with no win in between
MAX_DAILY_TRADES           = 5    # hard cap on total trades per UTC day
CIRCUIT_BREAKER_ACTIVE_KEY = "circuit_breaker_active"
BASE_DIR                   = Path(__file__).resolve().parent
STATE_FILE                 = BASE_DIR / "daily_state.json"

# --- Hard Profit Lock & Breakeven Buffer ---
HARD_PROFIT_LOCK_EUR      = 70.0  # Minimum absolute cash-out target floor
BREAKEVEN_FEE_BUFFER_PIPS = 1.0   # Extra pips added to breakeven SL to cover spread/commission

# --- Peak Giveback & Circuit Breaker Constants ---
GIVEBACK_ACTIVATION_EUR = 50.0
GIVEBACK_TIERS = [
    (100.0, 0.40),         # Peak €50 - €100  -> allow 40% give-back
    (200.0, 0.25),         # Peak €100 - €200 -> allow 25% give-back
    (float("inf"), 0.10),  # Peak > €200      -> allow 10% give-back
]
HARD_GIVEBACK_CAP_PCT = 0.50  # Hard circuit breaker: trigger if PnL retraces >= 50% AND < €0
DECLINE_NOISE_TOLERANCE = 0.05 # €0.05 noise tolerance for trend detection

# --- Time-Decay TP Schedules ---
TIME_DECAY_TP_R = [
    (15 * 60, 1.5),        # 0–15 min: 1.5R target
    (30 * 60, 1.0),        # 15–30 min: 1.0R target
    (45 * 60, 0.5),        # 30–45 min: 0.5R target
    (float("inf"), 0.05),  # 45+ min: 0.05R (breakeven + fee cover)
]

# Maximum SL distance in price terms per symbol.
MAX_SL_DISTANCE = {
    "EURUSD": 0.010,
    "GBPUSD": 0.012,
    "USDJPY": 1.00,
    "AUDUSD": 0.010,
}
DEFAULT_MAX_SL_DISTANCE = 0.012

# --- Portfolio / correlated-exposure limits --------------------------------
MAX_CONCURRENT_POSITIONS       = 3     # across all symbols combined
MAX_SAME_DIRECTION_USD_TRADES  = 2     # trades that are net-short-USD (or net-long-USD) at the same time
MAX_TOTAL_OPEN_RISK_PCT        = 2.0   # max sum of open + in-flight risk % before new trade is blocked

# Symbols where BUY = long USD (USD is the base currency). For all other
# traded symbols, BUY = short USD (USD is the quote currency).
USD_BASE_SYMBOLS = {"USDJPY"}

# --- In-Flight Risk Accumulators ------------------------------------------
_IN_FLIGHT_RISK_AMOUNT = 0.0
_IN_FLIGHT_TRADES = []  # list of dicts: [{"symbol": str, "signal_type": str, "risk_amount": float}]


def parse_currencies(symbol: str) -> tuple:
    """Parses base and quote currencies from a standard 6-character forex pair (e.g. AUDUSD -> AUD, USD)."""
    if isinstance(symbol, str) and len(symbol) == 6:
        return symbol[:3], symbol[3:]
    return symbol, ""


def add_in_flight_risk(symbol_or_amount, signal_type: str = "BUY", risk_amount: float = None) -> None:
    """Registers pending trade risk prior to broker transmission."""
    global _IN_FLIGHT_RISK_AMOUNT, _IN_FLIGHT_TRADES
    if isinstance(symbol_or_amount, (int, float)):
        amount = float(symbol_or_amount)
        sym = "UNKNOWN"
        sig = "BUY"
    else:
        sym = str(symbol_or_amount)
        sig = str(signal_type)
        amount = float(risk_amount) if risk_amount is not None else 0.0

    _IN_FLIGHT_RISK_AMOUNT += amount
    _IN_FLIGHT_TRADES.append({"symbol": sym, "signal_type": sig, "risk_amount": amount})
    log.info(f"In-flight risk added: {amount:.2f} [{sym} {sig}]. Total in-flight: {_IN_FLIGHT_RISK_AMOUNT:.2f}")


def clear_in_flight_risk(symbol_or_amount, signal_type: str = None, risk_amount: float = None) -> None:
    """Clears pending trade risk after execution fill or order rejection."""
    global _IN_FLIGHT_RISK_AMOUNT, _IN_FLIGHT_TRADES
    if isinstance(symbol_or_amount, (int, float)):
        amount = float(symbol_or_amount)
        _IN_FLIGHT_RISK_AMOUNT = max(0.0, _IN_FLIGHT_RISK_AMOUNT - amount)
        if _IN_FLIGHT_TRADES:
            _IN_FLIGHT_TRADES.pop(0)
    else:
        sym = str(symbol_or_amount)
        sig = signal_type
        amount = float(risk_amount) if risk_amount is not None else 0.0
        _IN_FLIGHT_RISK_AMOUNT = max(0.0, _IN_FLIGHT_RISK_AMOUNT - amount)
        for idx, item in enumerate(_IN_FLIGHT_TRADES):
            if item["symbol"] == sym and (sig is None or item["signal_type"] == sig):
                _IN_FLIGHT_TRADES.pop(idx)
                break
    log.info(f"In-flight risk cleared. Remaining total in-flight: {_IN_FLIGHT_RISK_AMOUNT:.2f}")


def get_total_portfolio_risk(magic: int = None, account_equity: float = None) -> float:
    """
    Calculates total active and in-flight risk percentage against equity.
    Formula: (Active Position Risk + In-Flight Risk) / Account Equity * 100
    """
    account = mt5.account_info()
    if account is None or account.equity <= 0:
        return 0.0

    equity = account_equity if (account_equity and account_equity > 0) else account.equity

    positions = mt5.positions_get()
    active_risk_amount = 0.0
    if positions:
        our_positions = [p for p in positions if magic is None or p.magic == magic]
        for p in our_positions:
            sl_distance = abs(p.price_open - p.sl) if p.sl else 0.0
            if sl_distance <= 0:
                continue
            loss_est = _broker_estimated_loss(p.symbol, p.type, p.volume, p.price_open, p.sl)
            if loss_est is not None:
                active_risk_amount += abs(loss_est)

    total_risk_amount = active_risk_amount + _IN_FLIGHT_RISK_AMOUNT
    return (total_risk_amount / equity) * 100.0


def _usd_direction(symbol: str, order_type) -> int:
    """
    Returns +1 if the position is net LONG USD, -1 if net SHORT USD.
    order_type is mt5.ORDER_TYPE_BUY / SELL (or POSITION_TYPE_BUY / SELL).
    """
    is_buy = (order_type == mt5.ORDER_TYPE_BUY)
    if symbol in USD_BASE_SYMBOLS:
        return 1 if is_buy else -1
    return -1 if is_buy else 1


# ---------------------------------------------------------------------------
# DAILY STATE
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def compute_indicators(df, bb_period, atr_period, atr_baseline, bb_std_mult, atr_regime_mult):
    """Single indicator implementation used by the live bot and diagnostics."""
    import numpy as np
    out = df.copy()
    out["sma"] = out["close"].rolling(bb_period).mean()
    out["std"] = out["close"].rolling(bb_period).std(ddof=0)
    out["z_score"] = (out["close"] - out["sma"]) / out["std"]
    hl = out["high"] - out["low"]
    hc = (out["high"] - out["close"].shift()).abs()
    lc = (out["low"] - out["close"].shift()).abs()
    out["tr"] = np.maximum(hl, np.maximum(hc, lc))
    out["atr"] = out["tr"].rolling(atr_period).mean()
    out["atr_baseline"] = out["atr"].rolling(atr_baseline).mean()
    out["regime_ok"] = out["atr"] < (out["atr_baseline"] * atr_regime_mult)
    out["signal"] = 0
    out.loc[out["regime_ok"] & (out["z_score"] < -bb_std_mult), "signal"] = 1
    out.loc[out["regime_ok"] & (out["z_score"] > bb_std_mult), "signal"] = -1
    return out


def _default_state() -> dict:
    return {
        "date":                      _today_str(),
        "start_equity":              None,
        "trades_today":              0,
        "consecutive_losses":        0,
        "last_trade_result":         None,
        CIRCUIT_BREAKER_ACTIVE_KEY:  False,
        "last_evaluated":            {},   # symbol -> ISO candle timestamp
        "processed_deal_tickets":    [],   # dedupe guard for reconciliation
    }


def load_daily_state() -> dict:
    """Loads daily state from disk. Auto-resets at UTC midnight."""
    today = _today_str()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == today:
                defaults = _default_state()
                for k, v in defaults.items():
                    state.setdefault(k, v)
                return state
        except (json.JSONDecodeError, KeyError, OSError) as e:
            log.error(f"State file unreadable ({e}). Starting fresh state.")

    state = _default_state()
    _save_daily_state(state)
    return state


def _save_daily_state(state: dict) -> None:
    """Atomic write: temp file in the same directory, then os.replace()."""
    dir_name = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".daily_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def record_trade_opened() -> None:
    """Call immediately after a fill is confirmed. Increments trades_today only."""
    state = load_daily_state()
    state["trades_today"] += 1
    log.info(
        f"Trade opened. Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}. "
        f"Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}."
    )
    _save_daily_state(state)


def record_trade_loss() -> None:
    """Call after a trade closes at a REALIZED loss."""
    state = load_daily_state()
    state["consecutive_losses"] += 1
    state["last_trade_result"]   = "loss"

    log.info(f"Loss recorded. Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}.")

    if state["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
        state[CIRCUIT_BREAKER_ACTIVE_KEY] = True
        log.warning(
            f"CIRCUIT BREAKER ACTIVATED. {state['consecutive_losses']} consecutive "
            f"losses reached the limit of {CONSECUTIVE_LOSS_LIMIT}. No new trades until UTC midnight."
        )

    _save_daily_state(state)


def record_trade_win() -> None:
    """Call after a trade closes at a REALIZED profit. Resets the streak."""
    state = load_daily_state()
    prev_streak = state["consecutive_losses"]
    state["consecutive_losses"] = 0
    state["last_trade_result"]  = "win"

    log.info(
        f"Win recorded. Consecutive loss streak reset from {prev_streak} to 0. "
        f"Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}."
    )
    _save_daily_state(state)


def record_trade_closed(realized_profit: float) -> None:
    """Routes to win/loss recording based on realized profit figure."""
    if realized_profit > 0:
        record_trade_win()
    elif realized_profit < 0:
        record_trade_loss()
    else:
        state = load_daily_state()
        state["last_trade_result"] = "breakeven"
        _save_daily_state(state)


def get_streak_status() -> str:
    state = load_daily_state()
    cb     = state.get(CIRCUIT_BREAKER_ACTIVE_KEY, False)
    streak = state.get("consecutive_losses", 0)
    trades = state.get("trades_today", 0)
    if cb:
        return f"CIRCUIT BREAKER ACTIVE ({streak} consecutive losses)"
    return (
        f"Streak: {streak}/{CONSECUTIVE_LOSS_LIMIT} consecutive losses | "
        f"Trades: {trades}/{MAX_DAILY_TRADES}"
    )


# ---------------------------------------------------------------------------
# STATE RECONCILIATION FROM MT5 HISTORY
# ---------------------------------------------------------------------------

def reconcile_state_from_history(magic: int) -> dict:
    """Rebuilds daily state from MT5 closed-deal history."""
    today_start = datetime.datetime.combine(
        datetime.datetime.utcnow().date(), datetime.time.min
    )
    now = datetime.datetime.utcnow()

    deals = mt5.history_deals_get(today_start, now)
    if deals is None:
        code, msg = mt5.last_error()
        log.error(
            f"reconcile_state_from_history: history_deals_get() returned None "
            f"(MT5 {code}: {msg}). State cannot be reconciled; caller must fail closed."
        )
        return None

    our_deals = [d for d in deals if d.magic == magic]
    our_deals.sort(key=lambda d: d.time)

    entries = [d for d in our_deals if d.entry == mt5.DEAL_ENTRY_IN]
    trades_today = len(entries)

    exits = [d for d in our_deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]

    consecutive_losses = 0
    last_trade_result  = None
    for d in exits:
        realized = d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        if realized > 0:
            consecutive_losses = 0
            last_trade_result  = "win"
        else:
            consecutive_losses += 1
            last_trade_result  = "loss"

    circuit_breaker_active = consecutive_losses >= CONSECUTIVE_LOSS_LIMIT

    state = load_daily_state()
    prior = (state["trades_today"], state["consecutive_losses"],
             state["last_trade_result"], state[CIRCUIT_BREAKER_ACTIVE_KEY])

    state["trades_today"]             = trades_today
    state["consecutive_losses"]       = consecutive_losses
    state["last_trade_result"]        = last_trade_result
    state[CIRCUIT_BREAKER_ACTIVE_KEY] = circuit_breaker_active
    state["processed_deal_tickets"]   = [d.ticket for d in exits]

    new = (trades_today, consecutive_losses, last_trade_result, circuit_breaker_active)
    if new != prior:
        log.warning(f"State reconciled from MT5 history. Cached={prior} -> Authoritative={new}.")
    else:
        log.info(f"State reconciled from MT5 history. No drift detected ({new}).")

    if CONSECUTIVE_LOSS_LIMIT >= MAX_DAILY_TRADES:
        log.info(
            f"Note: consecutive-loss limit ({CONSECUTIVE_LOSS_LIMIT}) >= daily trade "
            f"cap ({MAX_DAILY_TRADES})."
        )

    _save_daily_state(state)
    return state


def is_position_open(symbol: str, magic: int):
    """Fail-closed open-position check."""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        code, msg = mt5.last_error()
        log.error(f"[{symbol}] positions_get() returned None (MT5 {code}: {msg}). Failing closed.")
        return None
    return any(p.magic == magic for p in positions)


def realized_pnl_for_position(position_id: int, magic: int):
    """Return complete realized P&L for one position, or None on MT5 failure."""
    deals = mt5.history_deals_get(position=position_id)
    if deals is None:
        code, msg = mt5.last_error()
        log.error(f"Position {position_id}: history_deals_get failed ({code}: {msg}).")
        return None
    relevant = [d for d in deals if d.magic == magic]
    if not relevant:
        return None
    return sum(
        float(getattr(d, "profit", 0.0)) + float(getattr(d, "commission", 0.0))
        + float(getattr(d, "swap", 0.0)) + float(getattr(d, "fee", 0.0))
        for d in relevant
    )


# ---------------------------------------------------------------------------
# PORTFOLIO / CORRELATED EXPOSURE
# ---------------------------------------------------------------------------

def check_portfolio_exposure(symbol: str, signal_type: str, magic: int, candidate_risk_pct: float = 1.0) -> dict:
    """
    Aggregate exposure guard, incorporating active positions, in-flight orders,
    currency matrix limits, and portfolio-wide risk caps.
    """
    positions = mt5.positions_get()
    if positions is None:
        code, msg = mt5.last_error()
        return {"ok": False, "reason": f"positions_get() failed (MT5 {code}: {msg}); failing closed."}

    our_positions = [p for p in positions if p.magic == magic]
    total_concurrent = len(our_positions) + len(_IN_FLIGHT_TRADES)

    if total_concurrent >= MAX_CONCURRENT_POSITIONS:
        return {
            "ok": False,
            "reason": f"Max concurrent positions reached ({total_concurrent}/{MAX_CONCURRENT_POSITIONS} active + in-flight).",
        }

    candidate_type = mt5.ORDER_TYPE_BUY if signal_type == "BUY" else mt5.ORDER_TYPE_SELL
    candidate_dir  = _usd_direction(symbol, candidate_type)

    same_dir_count = 1  # counting the candidate itself
    for p in our_positions:
        if _usd_direction(p.symbol, p.type) == candidate_dir:
            same_dir_count += 1

    for inflight in _IN_FLIGHT_TRADES:
        inflight_type = mt5.ORDER_TYPE_BUY if inflight["signal_type"] == "BUY" else mt5.ORDER_TYPE_SELL
        if _usd_direction(inflight["symbol"], inflight_type) == candidate_dir:
            same_dir_count += 1

    if same_dir_count > MAX_SAME_DIRECTION_USD_TRADES:
        return {
            "ok": False,
            "reason": (
                f"Adding this trade would create {same_dir_count} concurrent positions "
                f"net-{'long' if candidate_dir > 0 else 'short'}-USD, exceeding limit "
                f"{MAX_SAME_DIRECTION_USD_TRADES}. Correlated exposure too high."
            ),
        }

    account = mt5.account_info()
    if account is None:
        return {"ok": False, "reason": "account_info() returned None; failing closed."}

    current_risk_pct = get_total_portfolio_risk(magic=magic, account_equity=account.equity)
    projected_risk_pct = current_risk_pct + candidate_risk_pct

    if projected_risk_pct > MAX_TOTAL_OPEN_RISK_PCT:
        return {
            "ok": False,
            "reason": (
                f"Projected open risk {projected_risk_pct:.2f}% exceeds "
                f"limit {MAX_TOTAL_OPEN_RISK_PCT:.2f}%. Skipping new trade."
            ),
        }

    return {"ok": True}


# ---------------------------------------------------------------------------
# BROKER-AWARE LOSS / PIP VALUE CALCULATION
# ---------------------------------------------------------------------------

def _broker_estimated_loss(symbol: str, order_type, volume: float, entry: float, sl: float):
    """Estimates loss at SL using MT5 order_calc_profit()."""
    try:
        result = mt5.order_calc_profit(order_type, symbol, volume, entry, sl)
        if result is not None:
            return result
    except Exception as e:
        log.debug(f"[{symbol}] order_calc_profit() unavailable: {e}")
    return None


def _get_pip_value_per_lot(symbol: str, account_currency: str) -> float:
    """Monetary value of 1 pip per 1.0 standard lot in account currency."""
    sym = mt5.symbol_info(symbol)
    if sym is None:
        log.warning(f"Cannot read symbol info for {symbol}.")
        return None

    tick_value = sym.trade_tick_value
    tick_size  = sym.trade_tick_size

    if tick_size == 0:
        log.warning(f"tick_size is 0 for {symbol}.")
        return None

    pip_value = tick_value * ((sym.point * 10) / tick_size)
    log.info(f"Pip value | {symbol}: {pip_value:.4f} {account_currency}/lot.")
    return pip_value


# ---------------------------------------------------------------------------
# PRE-TRADE BROKER VALIDATION
# ---------------------------------------------------------------------------

def validate_broker_constraints(symbol: str, order_type, volume: float, price: float) -> dict:
    """Runs margin check prior to order execution."""
    account = mt5.account_info()
    if account is None:
        return {"ok": False, "reason": "account_info() returned None."}

    margin = mt5.order_calc_margin(order_type, symbol, volume, price)
    if margin is None:
        code, msg = mt5.last_error()
        return {"ok": False, "reason": f"order_calc_margin() failed (MT5 {code}: {msg})."}

    if margin > account.margin_free:
        return {
            "ok": False,
            "reason": f"Insufficient free margin: need {margin:.2f}, have {account.margin_free:.2f}.",
        }

    return {"ok": True, "margin": margin}


# ---------------------------------------------------------------------------
# DYNAMIC TARGET & ABSOLUTE PROFIT LOCK HELPERS
# ---------------------------------------------------------------------------

def calculate_dynamic_rr(
    current_price: float,
    signal_type: str,
    sl_distance: float,
    swing_high: float = None,
    swing_low: float = None
) -> float:
    """Calculates dynamic R:R based on recent chart structure (Swing High/Low) or ATR regime."""
    if signal_type == "BUY" and swing_high and swing_high > current_price:
        structure_dist = swing_high - current_price
        calculated_rr = structure_dist / sl_distance
    elif signal_type == "SELL" and swing_low and swing_low < current_price:
        structure_dist = current_price - swing_low
        calculated_rr = structure_dist / sl_distance
    else:
        calculated_rr = 2.0  # Default structure-free volatility target

    # Clamp R:R between 1.2R (minimum viable) and 3.0R (realistic ceiling)
    return max(1.2, min(3.0, calculated_rr))


def evaluate_profit_lock_exit(net_pnl: float, peak_pnl: float) -> dict:
    """
    Priority #0 Exit Guard:
    1. Instantly cashes out if floating PnL >= €70.
    2. If peak profit previously crossed €70 and retraces back down to €70, closes immediately.
    """
    if net_pnl >= HARD_PROFIT_LOCK_EUR:
        return {
            "exit": True,
            "reason": f"PROFIT LOCK HIT: Net PnL €{net_pnl:.2f} >= €{HARD_PROFIT_LOCK_EUR:.2f}. Cashing out."
        }

    if peak_pnl >= HARD_PROFIT_LOCK_EUR and net_pnl < HARD_PROFIT_LOCK_EUR:
        return {
            "exit": True,
            "reason": f"PROFIT FLOOR GUARD: Peak reached €{peak_pnl:.2f}. Retraced to €{net_pnl:.2f}. Locking in €70 floor."
        }

    return {"exit": False}


def price_for_profit(symbol: str, pos_type: int, volume: float, current_price: float, profit_amount: float) -> float | None:
    """Returns the SL price that would result in exactly profit_amount (positive) for given position."""
    account = mt5.account_info()
    if account is None:
        return None
    pip_val = _get_pip_value_per_lot(symbol, account.currency)
    if pip_val is None or pip_val <= 0:
        return None
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None or sym_info.point <= 0:
        return None

    pips = profit_amount / (volume * pip_val)
    price_diff = pips * (sym_info.point * 10.0)

    if pos_type == 0:  # BUY
        return current_price - price_diff
    else:              # SELL
        return current_price + price_diff


# ---------------------------------------------------------------------------
# RATCHET SL CALCULATION & EXIT RULES
# ---------------------------------------------------------------------------

def calculate_ratchet_sl(
    pos_type: int,
    open_price: float,
    current_sl: float,
    current_price: float,
    sl_distance: float,
    net_pnl: float = 0.0,
    point_value: float = 0.0001
) -> float | None:
    """
    Evaluates unrealized profit and returns target ratchet SL price.
    Triggers Breakeven (+ fee buffer) if trade hits +0.5R OR floating profit >= €35.
    """
    if sl_distance <= 0:
        return None

    # Calculate unrealized profit distance
    profit_dist = (current_price - open_price) if pos_type == 0 else (open_price - current_price)
    r_multiple = profit_dist / sl_distance

    # Calculate fee-adjusted breakeven offset
    fee_offset = BREAKEVEN_FEE_BUFFER_PIPS * (point_value * 10.0)

    # Determine locked R or Breakeven trigger
    target_sl = None

    if r_multiple >= 3.0:
        target_r_dist = 2.0 * sl_distance
        target_sl = (open_price + target_r_dist) if pos_type == 0 else (open_price - target_r_dist)
    elif r_multiple >= 2.0:
        target_r_dist = 1.0 * sl_distance
        target_sl = (open_price + target_r_dist) if pos_type == 0 else (open_price - target_r_dist)
    elif r_multiple >= 1.0:
        target_r_dist = 0.5 * sl_distance
        target_sl = (open_price + target_r_dist) if pos_type == 0 else (open_price - target_r_dist)
    elif r_multiple >= 0.5 or net_pnl >= 35.0:  # Breakeven condition
        target_sl = (open_price + fee_offset) if pos_type == 0 else (open_price - fee_offset)

    if target_sl is None:
        return None

    # Only apply if target SL is better than current SL
    if pos_type == 0:  # BUY
        return target_sl if target_sl > current_sl else None
    else:              # SELL
        return target_sl if (current_sl == 0.0 or target_sl < current_sl) else None


def calculate_time_decay_tp(open_price: float, pos_type: int, sl_distance: float, 
                            elapsed_seconds: float, digits: int) -> float:
    """Calculates updated TP price based on elapsed trade duration."""
    target_r = 0.05
    for max_elapsed, r_val in TIME_DECAY_TP_R:
        if elapsed_seconds <= max_elapsed:
            target_r = r_val
            break

    # MT5 Buy = 0, Sell = 1
    if pos_type == 0:
        raw_tp = open_price + (target_r * sl_distance)
    else:
        raw_tp = open_price - (target_r * sl_distance)

    return round(raw_tp, digits)


def evaluate_hard_giveback_cap(peak_pnl: float, current_pnl: float) -> dict:
    """Circuit Breaker: Forces exit if trade loses >= 50% of peak AND drops into negative territory."""
    if peak_pnl >= GIVEBACK_ACTIVATION_EUR and current_pnl < 0:
        retrace_pct = (peak_pnl - current_pnl) / peak_pnl
        if retrace_pct >= HARD_GIVEBACK_CAP_PCT:
            return {
                "exit": True, 
                "reason": f"HARD CAP: Retraced {retrace_pct * 100:.1f}% off peak €{peak_pnl:.2f} (Current: €{current_pnl:.2f})"
            }
    return {"exit": False}


def evaluate_giveback_exit(peak_pnl: float, current_pnl: float) -> dict:
    """Tiered high-water mark lock with non-negative (€0.00) floor guard."""
    if peak_pnl < GIVEBACK_ACTIVATION_EUR:
        return {"exit": False}

    allowed_pct = 0.40
    for ceiling, pct in GIVEBACK_TIERS:
        if peak_pnl <= ceiling:
            allowed_pct = pct
            break

    target_exit_pnl = max(0.0, peak_pnl * (1.0 - allowed_pct))
    
    if current_pnl <= target_exit_pnl:
        return {
            "exit": True,
            "reason": f"Tiered Exit: Target €{target_exit_pnl:.2f} hit off peak €{peak_pnl:.2f} ({allowed_pct * 100:.0f}% allowed give-back)"
        }
    return {"exit": False}


def evaluate_decline_to_zero(pnl_history: list) -> dict:
    """Triggers exit if PnL shows a stable downward trend and crosses into <= €0."""
    if len(pnl_history) < 4:
        return {"exit": False}

    recent = [p[1] for p in pnl_history[-4:]]
    is_declining = all(
        recent[i + 1] <= (recent[i] + DECLINE_NOISE_TOLERANCE)
        for i in range(len(recent) - 1)
    )
    crossed_zero = recent[-2] > 0 and recent[-1] <= 0

    if is_declining and crossed_zero:
        return {"exit": True, "reason": "30s Trend Decay: Steady decline crossed zero threshold."}
    return {"exit": False}


# ---------------------------------------------------------------------------
# MAIN RISK EVALUATION
# ---------------------------------------------------------------------------

def evaluate_risk(
    signal_type:       str,
    current_price:     float,
    atr_val:           float,
    risk_pct:          float = 1.0,
    rr_ratio:          float = 1.5,
    symbol:            str   = "EURUSD",
    swing_high:        float = None,
    swing_low:         float = None,
    tp_price_override: float = None,
) -> dict:
    """Evaluates whether a trade should be taken and computes exact parameters."""

    def reject(reason: str) -> dict:
        log.info(f"Risk REJECT [{symbol}]: {reason}")
        return {"is_approved": False, "reject_reason": reason}

    if signal_type not in ["BUY", "SELL"]:
        return reject(f"Invalid signal type: {signal_type}")
    if atr_val <= 0 or atr_val != atr_val:
        return reject(f"Invalid ATR value: {atr_val}")

    state = load_daily_state()
    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY):
        streak = state.get("consecutive_losses", 0)
        return reject(f"Circuit breaker active: {streak} consecutive losses. Resets at UTC midnight.")

    trades_today = state.get("trades_today", 0)
    if trades_today >= MAX_DAILY_TRADES:
        return reject(f"Daily trade limit: {trades_today}/{MAX_DAILY_TRADES} reached.")

    account = mt5.account_info()
    sym     = mt5.symbol_info(symbol)
    if account is None:
        return reject("mt5.account_info() returned None.")
    if sym is None:
        return reject(f"mt5.symbol_info({symbol}) returned None.")

    equity           = account.equity
    account_currency = account.currency
    digits           = sym.digits

    if state["start_equity"] is None:
        state["start_equity"] = equity
        _save_daily_state(state)
        log.info(f"Start-of-day equity: {equity:.2f} {account_currency}.")

    sl_distance = atr_val * 1.5
    max_sl      = MAX_SL_DISTANCE.get(symbol, DEFAULT_MAX_SL_DISTANCE)
    if sl_distance > max_sl:
        return reject(f"SL distance {sl_distance:.5f} > max {max_sl:.5f}. Market too volatile.")

    sl_pips = sl_distance / (sym.point * 10)
    if sl_pips < 1.0:
        return reject(f"SL {sl_pips:.2f} pips too small — spread risk.")

    # Calculate initial SL price
    if signal_type == "BUY":
        sl_price = round(current_price - sl_distance, digits)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl_price = round(current_price + sl_distance, digits)
        order_type = mt5.ORDER_TYPE_SELL

    # Determine dynamic or overridden TP placement
    if tp_price_override is not None:
        tp_price = round(tp_price_override, digits)
        effective_rr = abs(tp_price - current_price) / sl_distance if sl_distance > 0 else rr_ratio
    else:
        effective_rr = calculate_dynamic_rr(current_price, signal_type, sl_distance, swing_high, swing_low)
        if signal_type == "BUY":
            tp_price = round(current_price + (sl_distance * effective_rr), digits)
        else:
            tp_price = round(current_price - (sl_distance * effective_rr), digits)

    risk_amount = equity * (risk_pct / 100.0)
    loss_per_lot = _broker_estimated_loss(symbol, order_type, 1.0, current_price, sl_price)
    if loss_per_lot is None or loss_per_lot >= 0:
        return reject("Broker cannot calculate a valid account-currency loss at SL.")

    step     = sym.volume_step
    raw_lot  = risk_amount / abs(loss_per_lot)
    lot_size = (raw_lot // step) * step
    lot_size = round(lot_size, 2)

    if lot_size < sym.volume_min:
        return reject(
            f"Risk-correct lot {lot_size} < broker minimum {sym.volume_min}. "
            f"Broker minimum would exceed configured {risk_pct}% risk."
        )
    if lot_size > sym.volume_max:
        lot_size = sym.volume_max

    actual_loss = _broker_estimated_loss(symbol, order_type, lot_size, current_price, sl_price)
    if actual_loss is None or actual_loss >= 0 or abs(actual_loss) > risk_amount * 1.001:
        return reject("Broker-calculated loss at SL exceeds or cannot verify the configured risk.")

    log.info(
        f"Risk APPROVED [{symbol}] | {signal_type} {lot_size} lots | "
        f"Entry: {current_price:.{digits}f} | SL: {sl_price:.{digits}f} | TP: {tp_price:.{digits}f} (R:R {effective_rr:.2f}) | "
        f"Risk: {risk_amount:.2f} {account_currency} ({risk_pct}%) | SL pips: {sl_pips:.1f} | "
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
        "risk_amount":   round(abs(actual_loss), 2),
        "sl_pips":       round(sl_pips, 1),
        "sl_distance":   sl_distance,
        "rr_ratio":      round(effective_rr, 2),
        "reject_reason": None,
    }