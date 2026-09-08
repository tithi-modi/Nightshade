# risk.py
# Nightshade Seed Engine - risk.py v11
# All configuration, risk, and position state logic.

import MetaTrader5 as mt5
import json
import os
import tempfile
import datetime
import logging
import math
import time
from pathlib import Path

log = logging.getLogger("nightshade")

# =========================================================================
# CONFIGURATION (central source of truth)
# =========================================================================

BASE_DIR = Path(__file__).resolve().parent

# --- Account & Broker ---
MAGIC_NUMBER = 991122
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME = mt5.TIMEFRAME_M5
POLL_INTERVAL_SEC = 30

# --- Strategy Indicators ---
BB_PERIOD = 20
BB_STD_MULT = 2.0
ATR_PERIOD = 14
ATR_BASELINE = 100
ATR_REGIME_MULT = 1.5
SL_ATR_MULT = 1.5
INITIAL_RR_MIN = 1.2
INITIAL_RR_MAX = 3.0
DEFAULT_RR = 2.0

# --- Risk & Trade Limits ---
RISK_PCT = 1.0
MAX_DAILY_TRADES = 5
CONSECUTIVE_LOSS_LIMIT = 3
MAX_CONCURRENT_POSITIONS = 3
MAX_SAME_DIRECTION_USD_TRADES = 2
MAX_TOTAL_OPEN_RISK_PCT = 2.0
USD_BASE_SYMBOLS = {"USDJPY"}

# --- Spread & Data Quality ---
MAX_SPREAD_PIPS = 5.0
MAX_CANDLE_AGE_S = 90
MIN_HISTORY_CANDLES = 64

# --- Maximum SL distance (price terms) ---
MAX_SL_DISTANCE = {
    "EURUSD": 0.010,
    "GBPUSD": 0.012,
    "USDJPY": 1.00,
    "AUDUSD": 0.010,
}
DEFAULT_MAX_SL_DISTANCE = 0.012

# --- Giveback (in R multiples) ---
ACTIVATION_R = 0.5
GIVEBACK_TIERS_R = [
    (0.5, 1.0, 0.40),   # 0.5R - 1.0R peak: 40% giveback
    (1.0, 2.0, 0.25),   # 1.0R - 2.0R peak: 25% giveback
    (2.0, float("inf"), 0.10),  # >= 2.0R: 10% giveback
]
HARD_GIVEBACK_RETRACE_PCT = 0.50  # 50% retracement from peak
DECLINE_NOISE_TOLERANCE = 0.05   # in R units

# --- Time-Decay TP (in R multiples) ---
TIME_DECAY_R = [
    (15 * 60, 1.5),
    (30 * 60, 1.0),
    (45 * 60, 0.5),
    (float("inf"), 0.05),
]

# --- Ratchet SL thresholds (R multiples) ---
RATCHET_LEVELS = [
    (0.5, 0.0),    # hit 0.5R -> SL to entry (breakeven)
    (1.0, 0.5),    # hit 1.0R -> SL to +0.5R
    (2.0, 1.0),    # hit 2.0R -> SL to +1.0R
    (3.0, 2.0),    # hit 3.0R -> SL to +2.0R
]

# --- Files ---
STATE_FILE = BASE_DIR / "daily_state.json"
POSITION_STATE_FILE = BASE_DIR / "position_state.json"
LOG_FILE = BASE_DIR / "logs" / "nightshade.log"

# =========================================================================
# Daily State (trades, losses, breaker)
# =========================================================================
CIRCUIT_BREAKER_ACTIVE_KEY = "circuit_breaker_active"
_IN_FLIGHT_RISK_AMOUNT = 0.0
_IN_FLIGHT_TRADES = []

def _today_str():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def _default_state():
    return {
        "date": _today_str(),
        "start_equity": None,
        "trades_today": 0,
        "consecutive_losses": 0,
        "last_trade_result": None,
        CIRCUIT_BREAKER_ACTIVE_KEY: False,
        "last_evaluated": {},
        "processed_deal_tickets": [],
        "account_login": None,
        "server": None,
    }

def load_daily_state():
    today = _today_str()
    acc = mt5.account_info()
    login = acc.login if acc else None
    server = acc.server if acc else None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == today and state.get("account_login") == login and state.get("server") == server:
                defaults = _default_state()
                for k, v in defaults.items():
                    state.setdefault(k, v)
                return state
        except Exception as e:
            log.error(f"State file error: {e}")
    state = _default_state()
    state["account_login"] = login
    state["server"] = server
    _save_daily_state(state)
    return state

def _save_daily_state(state):
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

def record_trade_opened():
    state = load_daily_state()
    state["trades_today"] += 1
    log.info(f"Trade opened. Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}. Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}.")
    _save_daily_state(state)

def record_trade_closed(realized_profit):
    state = load_daily_state()
    if realized_profit > 0:
        state["consecutive_losses"] = 0
        state["last_trade_result"] = "win"
        log.info(f"Win recorded. Streak reset.")
    elif realized_profit < 0:
        state["consecutive_losses"] += 1
        state["last_trade_result"] = "loss"
        log.info(f"Loss recorded. Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}.")
        if state["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
            state[CIRCUIT_BREAKER_ACTIVE_KEY] = True
            log.warning(f"CIRCUIT BREAKER ACTIVATED.")
    else:
        state["last_trade_result"] = "breakeven"
    _save_daily_state(state)

def get_streak_status():
    state = load_daily_state()
    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY, False):
        return f"CIRCUIT BREAKER ACTIVE ({state['consecutive_losses']} losses)"
    return f"Streak: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT} | Trades: {state['trades_today']}/{MAX_DAILY_TRADES}"

# =========================================================================
# Reconcile from MT5 history (group by position) - preserves trades_today
# =========================================================================
def reconcile_state_from_history(magic):
    today_start = datetime.datetime.combine(datetime.datetime.utcnow().date(), datetime.time.min)
    now = datetime.datetime.utcnow()
    deals = mt5.history_deals_get(today_start, now)
    if deals is None:
        log.error("history_deals_get failed.")
        return None
    our_deals = [d for d in deals if d.magic == magic]
    # Group by position_id
    pos_map = {}
    for d in our_deals:
        pid = d.position_id
        if pid not in pos_map:
            pos_map[pid] = []
        pos_map[pid].append(d)
    # For each position, calculate total realized P&L
    trades_today_from_entries = len([d for d in our_deals if d.entry == mt5.DEAL_ENTRY_IN])
    consecutive_losses = 0
    last_result = None
    for pid, deals_list in pos_map.items():
        # Check if position closed (has exit deals)
        exits = [d for d in deals_list if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
        if not exits:
            continue
        total_pnl = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals_list)
        if total_pnl > 0:
            consecutive_losses = 0
            last_result = "win"
        elif total_pnl < 0:
            consecutive_losses += 1
            last_result = "loss"
        else:
            last_result = "breakeven"
    circuit_breaker_active = consecutive_losses >= CONSECUTIVE_LOSS_LIMIT
    state = load_daily_state()
    # Preserve trades_today (it should never decrease) – use max with entries count
    state["trades_today"] = max(state.get("trades_today", 0), trades_today_from_entries)
    state["consecutive_losses"] = consecutive_losses
    state["last_trade_result"] = last_result
    state[CIRCUIT_BREAKER_ACTIVE_KEY] = circuit_breaker_active
    state["processed_deal_tickets"] = [d.ticket for d in our_deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
    _save_daily_state(state)
    return state

def realized_pnl_for_position(position_id, magic):
    deals = mt5.history_deals_get(position=position_id)
    if deals is None:
        return None
    relevant = [d for d in deals if d.magic == magic]
    if not relevant:
        return None
    return sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in relevant)

def is_position_open(symbol, magic):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return None
    return any(p.magic == magic for p in positions)

# =========================================================================
# Portfolio Exposure & In-Flight Risk (now includes broker orders)
# =========================================================================
def add_in_flight_risk(symbol, signal_type, risk_amount):
    global _IN_FLIGHT_RISK_AMOUNT, _IN_FLIGHT_TRADES
    _IN_FLIGHT_RISK_AMOUNT += risk_amount
    _IN_FLIGHT_TRADES.append({"symbol": symbol, "signal_type": signal_type, "risk_amount": risk_amount})
    log.info(f"In-flight risk added: {risk_amount:.2f} [{symbol} {signal_type}]. Total: {_IN_FLIGHT_RISK_AMOUNT:.2f}")

def clear_in_flight_risk(symbol, signal_type, risk_amount):
    global _IN_FLIGHT_RISK_AMOUNT, _IN_FLIGHT_TRADES
    _IN_FLIGHT_RISK_AMOUNT = max(0.0, _IN_FLIGHT_RISK_AMOUNT - risk_amount)
    for i, item in enumerate(_IN_FLIGHT_TRADES):
        if item["symbol"] == symbol and item["signal_type"] == signal_type:
            _IN_FLIGHT_TRADES.pop(i)
            break
    log.info(f"In-flight risk cleared. Remaining: {_IN_FLIGHT_RISK_AMOUNT:.2f}")

def get_total_portfolio_risk(magic):
    account = mt5.account_info()
    if account is None or account.equity <= 0:
        return None
    positions = mt5.positions_get()
    if positions is None:
        return None
    our_positions = [p for p in positions if p.magic == magic]
    active_risk = 0.0
    for p in our_positions:
        if p.sl == 0.0:
            return None  # unbounded
        loss_est = _broker_estimated_loss(p.symbol, p.type, p.volume, p.price_open, p.sl)
        if loss_est is None:
            return None
        if loss_est < 0:
            active_risk += abs(loss_est)
    # Include broker orders (pending orders)
    orders = mt5.orders_get()
    if orders is not None:
        for o in orders:
            if o.magic == magic and o.sl != 0.0:
                # Estimate risk if order were filled at its price
                loss_est = _broker_estimated_loss(o.symbol, o.type, o.volume, o.price_open, o.sl)
                if loss_est is not None and loss_est < 0:
                    active_risk += abs(loss_est)
    total_risk = active_risk + _IN_FLIGHT_RISK_AMOUNT
    return (total_risk / account.equity) * 100.0

def _usd_direction(symbol, order_type):
    is_buy = (order_type == mt5.ORDER_TYPE_BUY)
    if symbol in USD_BASE_SYMBOLS:
        return 1 if is_buy else -1
    return -1 if is_buy else 1

def check_portfolio_exposure(symbol, signal_type, magic):
    positions = mt5.positions_get()
    if positions is None:
        return {"ok": False, "reason": "positions_get failed"}
    our_positions = [p for p in positions if p.magic == magic]
    orders = mt5.orders_get()
    if orders is None:
        return {"ok": False, "reason": "orders_get failed"}
    broker_orders = [o for o in orders if o.magic == magic]
    total_concurrent = len(our_positions) + len(_IN_FLIGHT_TRADES) + len(broker_orders)
    if total_concurrent >= MAX_CONCURRENT_POSITIONS:
        return {"ok": False, "reason": f"Max concurrent positions ({MAX_CONCURRENT_POSITIONS}) reached"}
    candidate_type = mt5.ORDER_TYPE_BUY if signal_type == "BUY" else mt5.ORDER_TYPE_SELL
    candidate_dir = _usd_direction(symbol, candidate_type)
    same_dir = 1
    for p in our_positions:
        if _usd_direction(p.symbol, p.type) == candidate_dir:
            same_dir += 1
    for item in _IN_FLIGHT_TRADES:
        if _usd_direction(item["symbol"], mt5.ORDER_TYPE_BUY if item["signal_type"]=="BUY" else mt5.ORDER_TYPE_SELL) == candidate_dir:
            same_dir += 1
    for o in broker_orders:
        if _usd_direction(o.symbol, o.type) == candidate_dir:
            same_dir += 1
    if same_dir > MAX_SAME_DIRECTION_USD_TRADES:
        return {"ok": False, "reason": f"Too many same-direction USD trades ({same_dir} > {MAX_SAME_DIRECTION_USD_TRADES})"}
    
    # Dynamic risk allocation: available risk spread across concurrent positions
    current_risk = get_total_portfolio_risk(magic)
    if current_risk is None:
        return {"ok": False, "reason": "Cannot determine portfolio risk (MT5 error)"}
    # Calculate dynamic risk for this trade: at most RISK_PCT, but also ensure total <= MAX_TOTAL_OPEN_RISK_PCT
    # We want to allocate risk evenly if we have multiple positions
    num_positions = len(our_positions) + len(_IN_FLIGHT_TRADES) + len(broker_orders) + 1  # including this candidate
    risk_per_trade = min(RISK_PCT, MAX_TOTAL_OPEN_RISK_PCT / num_positions)
    # But we can also allow using full RISK_PCT if there is room
    available = MAX_TOTAL_OPEN_RISK_PCT - current_risk
    dynamic_risk_pct = min(risk_per_trade, available)
    if dynamic_risk_pct <= 0:
        return {"ok": False, "reason": f"No risk budget left (current {current_risk:.2f}%, max {MAX_TOTAL_OPEN_RISK_PCT}%)"}
    # Store dynamic risk for this candidate
    return {"ok": True, "dynamic_risk_pct": dynamic_risk_pct}

# =========================================================================
# Broker Helpers
# =========================================================================
def _broker_estimated_loss(symbol, order_type, volume, entry, sl):
    try:
        return mt5.order_calc_profit(order_type, symbol, volume, entry, sl)
    except:
        return None

def _get_pip_value(symbol):
    sym = mt5.symbol_info(symbol)
    if sym is None:
        return None
    tick_val = sym.trade_tick_value
    tick_size = sym.trade_tick_size
    if tick_size == 0:
        return None
    pip_size = sym.point * (10 ** (sym.digits - 1))
    return tick_val * (pip_size / tick_size)

def validate_broker_constraints(symbol, order_type, volume, price):
    account = mt5.account_info()
    if account is None:
        return {"ok": False, "reason": "No account info"}
    margin = mt5.order_calc_margin(order_type, symbol, volume, price)
    if margin is None:
        return {"ok": False, "reason": "order_calc_margin failed"}
    if margin > account.margin_free:
        return {"ok": False, "reason": f"Need {margin:.2f}, free {account.margin_free:.2f}"}
    return {"ok": True, "margin": margin}

# =========================================================================
# Dynamic RR, Giveback, Time Decay, Ratchet (all R-based) – no Profit Lock
# =========================================================================
def calculate_dynamic_rr(current_price, signal_type, sl_distance, swing_high=None, swing_low=None):
    if signal_type == "BUY" and swing_high and swing_high > current_price:
        dist = swing_high - current_price
        rr = dist / sl_distance if sl_distance > 0 else DEFAULT_RR
    elif signal_type == "SELL" and swing_low and swing_low < current_price:
        dist = current_price - swing_low
        rr = dist / sl_distance if sl_distance > 0 else DEFAULT_RR
    else:
        rr = DEFAULT_RR
    return max(INITIAL_RR_MIN, min(INITIAL_RR_MAX, rr))

def get_giveback_tier(peak_r):
    for low, high, pct in GIVEBACK_TIERS_R:
        if low <= peak_r < high:
            return pct
    return 0.10

def evaluate_giveback_exit(peak_r, current_r):
    if peak_r < ACTIVATION_R:
        return {"exit": False}
    allowed = get_giveback_tier(peak_r)
    floor_r = peak_r * (1 - allowed)
    if current_r <= floor_r:
        return {"exit": True, "reason": f"Giveback floor hit: peak {peak_r:.2f}R, floor {floor_r:.2f}R"}
    return {"exit": False}

def evaluate_hard_giveback(peak_r, current_r):
    if peak_r >= ACTIVATION_R and current_r < 0:
        retrace = (peak_r - current_r) / peak_r
        if retrace >= HARD_GIVEBACK_RETRACE_PCT:
            return {"exit": True, "reason": f"Hard cap: retraced {retrace*100:.1f}% from peak {peak_r:.2f}R to {current_r:.2f}R"}
    return {"exit": False}

def evaluate_decline_to_zero(pnl_history_r):
    if len(pnl_history_r) < 4:
        return {"exit": False}
    recent = [x[1] for x in pnl_history_r[-4:]]
    declining = all(recent[i+1] <= recent[i] + DECLINE_NOISE_TOLERANCE for i in range(len(recent)-1))
    crossed = recent[-2] > 0 and recent[-1] <= 0
    if declining and crossed:
        return {"exit": True, "reason": "Steady decline crossed zero"}
    return {"exit": False}

def get_time_decay_target(elapsed_seconds):
    for max_time, r in TIME_DECAY_R:
        if elapsed_seconds <= max_time:
            return r
    return 0.05

def calculate_ratchet_sl(pos_type, open_price, current_sl, current_price, sl_distance, peak_r):
    if sl_distance <= 0:
        return None
    profit_dist = (current_price - open_price) if pos_type == 0 else (open_price - current_price)
    current_r = profit_dist / sl_distance
    best_target = None
    for threshold, lock_r in RATCHET_LEVELS:
        if current_r >= threshold:
            if lock_r == 0:
                target_sl = open_price  # breakeven
            else:
                target_sl = (open_price + lock_r * sl_distance) if pos_type == 0 else (open_price - lock_r * sl_distance)
            if pos_type == 0:
                if target_sl > current_sl:
                    best_target = target_sl
            else:
                if target_sl < current_sl:
                    best_target = target_sl
    return best_target

# =========================================================================
# Main Risk Evaluation (entry) – now uses dynamic_risk_pct from exposure check
# =========================================================================
def evaluate_risk(signal_type, current_price, atr_val, risk_pct, symbol,
                  swing_high=None, swing_low=None, tp_price_override=None):
    def reject(reason):
        log.info(f"Risk REJECT [{symbol}]: {reason}")
        return {"is_approved": False, "reject_reason": reason}

    state = load_daily_state()
    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY):
        return reject("Circuit breaker active")
    if state.get("trades_today", 0) >= MAX_DAILY_TRADES:
        return reject("Daily limit reached")
    account = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    if account is None or sym is None:
        return reject("MT5 info unavailable")

    equity = account.equity
    digits = sym.digits
    sl_distance = atr_val * SL_ATR_MULT
    max_sl = MAX_SL_DISTANCE.get(symbol, DEFAULT_MAX_SL_DISTANCE)
    if sl_distance > max_sl:
        return reject(f"SL distance {sl_distance:.5f} > max {max_sl:.5f}")
    sl_pips = sl_distance / (sym.point * (10 ** (digits - 1)))
    if sl_pips < 1.0:
        return reject(f"SL {sl_pips:.2f} pips too small")

    if signal_type == "BUY":
        sl_price = round(current_price - sl_distance, digits)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl_price = round(current_price + sl_distance, digits)
        order_type = mt5.ORDER_TYPE_SELL

    if tp_price_override is not None:
        tp_price = round(tp_price_override, digits)
        rr = abs(tp_price - current_price) / sl_distance if sl_distance > 0 else DEFAULT_RR
    else:
        rr = calculate_dynamic_rr(current_price, signal_type, sl_distance, swing_high, swing_low)
        tp_price = round(current_price + (sl_distance * rr) if signal_type == "BUY" else current_price - (sl_distance * rr), digits)

    risk_amount = equity * (risk_pct / 100.0)
    loss_per_lot = _broker_estimated_loss(symbol, order_type, 1.0, current_price, sl_price)
    if loss_per_lot is None or loss_per_lot >= 0:
        return reject("Cannot compute loss per lot")
    step = sym.volume_step
    raw_lot = risk_amount / abs(loss_per_lot)
    lot_size = math.floor(raw_lot / step) * step
    dec = 0
    if step < 1:
        dec = int(round(-math.log10(step)))
    lot_size = round(lot_size, dec)
    if lot_size < sym.volume_min:
        return reject(f"Lot {lot_size} < min {sym.volume_min}")
    if lot_size > sym.volume_max:
        lot_size = sym.volume_max

    actual_loss = _broker_estimated_loss(symbol, order_type, lot_size, current_price, sl_price)
    if actual_loss is None or actual_loss >= 0 or abs(actual_loss) > risk_amount * 1.001:
        return reject("Actual loss mismatch")

    log.info(f"Risk APPROVED [{symbol}] {signal_type} {lot_size} lots | Entry: {current_price:.{digits}f} SL: {sl_price:.{digits}f} TP: {tp_price:.{digits}f} (R:R {rr:.2f}) | Risk %: {risk_pct:.2f}%")
    return {
        "is_approved": True,
        "symbol": symbol,
        "signal": signal_type,
        "lot_size": lot_size,
        "entry_price": current_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "risk_amount": abs(actual_loss),
        "sl_distance": sl_distance,
        "rr_ratio": rr,
        "reject_reason": None,
    }

# =========================================================================
# Position State (persistent) – scoped to account/server
# =========================================================================
def _get_account_scope():
    acc = mt5.account_info()
    if acc:
        return f"{acc.login}_{acc.server}"
    return "unknown"

def load_position_state():
    scope = _get_account_scope()
    if os.path.exists(POSITION_STATE_FILE):
        try:
            with open(POSITION_STATE_FILE, "r") as f:
                data = json.load(f)
            if data.get("scope") == scope:
                return data.get("states", {})
        except:
            pass
    return {}

def save_position_state(states):
    scope = _get_account_scope()
    data = {"scope": scope, "states": states}
    fd, tmp = tempfile.mkstemp(dir=BASE_DIR, prefix=".pos_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, POSITION_STATE_FILE)
    except:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

def get_position_monitor_state(ticket):
    states = load_position_state()
    if str(ticket) not in states:
        states[str(ticket)] = {
            "peak_pnl": None,
            "peak_r": 0.0,
            "giveback_armed": False,
            "pnl_history_r": [],
            "last_update": time.time(),
            "ratchet_level": 0,
        }
    return states[str(ticket)]

def update_position_monitor_state(ticket, data):
    states = load_position_state()
    states[str(ticket)] = data
    save_position_state(states)

def cleanup_position_state(active_tickets):
    states = load_position_state()
    for tid in list(states.keys()):
        if int(tid) not in active_tickets:
            del states[tid]
    save_position_state(states)

# =========================================================================
# Indicator computation (shared)
# =========================================================================
def compute_indicators(df, bb_period, atr_period, atr_baseline, bb_std_mult, atr_regime_mult):
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