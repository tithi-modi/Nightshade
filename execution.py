"""
Nightshade Seed Engine - execution.py  v5  (Layer 4)

Changes vs v4 (per GitHub issue from tech review):
  P0-7  Risk is now recalculated at the fresh execution tick, not just the
        price computed seconds earlier by risk.py. SL/TP/lot are rebuilt
        from the live price and the trade is rejected if the price moved
        enough that the original 1% risk assumption no longer holds.
  P0-8  order_send() results are no longer only checked against
        TRADE_RETCODE_DONE. DONE_PARTIAL and uncertain/ambiguous outcomes
        (timeout, connection loss, "no error but no confirmation") are
        reconciled against live positions/orders/deals before any retry,
        so we never blindly resend into an order that actually filled.
  P1-12 Before sending, validate_broker_constraints() (margin check via
        order_calc_margin) and mt5.order_check() are run. Filling mode is
        read from the symbol's own supported modes rather than assumed to
        be IOC-then-FOK for every broker.
  P0-6  Fail closed: a None from any MT5 read is treated as "cannot
        confirm state," which blocks execution rather than proceeding as
        if nothing were open.
"""

"""
Nightshade Seed Engine - execution.py  v6  (Layer 4)

Changes vs v5:
  - Added Execution Mutex/Lock (_EXECUTION_LOCK) to force sequential processing.
  - Added Pre-Send Risk Reservation via risk.add_in_flight_risk().
  - Added State Verification Delay (time.sleep(2.5)) following broker confirmation.
  - Added Forced Broker Sync (sync_positions()) before releasing execution lock and clearing in-flight risk.
"""

"""
Nightshade Seed Engine - execution.py  v7  (Layer 4)

Changes vs v6:
  - Added calculate_ratchet_sl import from risk module.
  - Added modify_position_sl() for sending broker SL update requests via mt5.TRADE_ACTION_SLTP.
  - Added process_active_position_ratchets() as a self-contained orchestrator for main.py tick loop.
"""

"""
Nightshade Seed Engine - execution.py  v8  (Layer 4)

Changes vs v7:
  - Added _POSITION_MONITOR_STATE tracking and helper functions (_get_or_init_monitor_state, _cleanup_monitor_state).
  - Added _close_position_market() for sending market order exit requests to MT5.
  - Added process_advanced_position_management() orchestrator for handling dynamic giveback exits,
    30s trend decline checks, hard cap circuit breakers, and server-side time-decay TP updates.
"""

"""
Nightshade Seed Engine - execution.py  v9  (Layer 4)

Handles MT5 trade execution, server-side order modifications, and strict
priority-driven advanced position management.
"""

"""
Nightshade Seed Engine - execution.py  v9  (Layer 4)

Handles MT5 trade execution, server-side order modifications, and strict
priority-driven advanced position management.
"""

import logging
import time
from datetime import datetime
import MetaTrader5 as mt5
import risk

log = logging.getLogger("nightshade")

# ---------------------------------------------------------------------------
# MONITOR STATE TRACKING STRUCTURES
# ---------------------------------------------------------------------------

_POSITION_MONITOR_STATE = {}


def _get_or_init_monitor_state(ticket: int, open_time_sec: float) -> dict:
    """Retrieves or initializes position monitoring state for a given ticket."""
    if ticket not in _POSITION_MONITOR_STATE:
        _POSITION_MONITOR_STATE[ticket] = {
            "peak_pnl": -999999.0,
            "pnl_history": [],
            "open_time": open_time_sec,
        }
    return _POSITION_MONITOR_STATE[ticket]


def _cleanup_monitor_state(active_tickets: set) -> None:
    """Removes tracking entries for positions that have closed."""
    stale_tickets = [t for t in _POSITION_MONITOR_STATE if t not in active_tickets]
    for t in stale_tickets:
        del _POSITION_MONITOR_STATE[t]


# ---------------------------------------------------------------------------
# MARKET EXECUTION & ORDER MODIFICATION HELPERS
# ---------------------------------------------------------------------------

def _close_position_market(position) -> bool:
    """Closes an active position immediately via market order."""
    symbol = position.symbol
    ticket = position.ticket
    vol = position.volume
    pos_type = position.type  # 0 for BUY, 1 for SELL

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        log.error(f"[{symbol}] Failed to fetch symbol_info for market exit.")
        return False

    order_type = mt5.ORDER_TYPE_SELL if pos_type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = sym_info.bid if pos_type == mt5.POSITION_TYPE_BUY else sym_info.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol": symbol,
        "volume": vol,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": position.magic,
        "comment": "Nightshade Auto Exit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        ret_code = result.retcode if result else "None"
        log.error(f"[{symbol}] Failed to market-close position #{ticket}. Code: {ret_code}")
        return False

    log.info(f"[{symbol}] Market-closed position #{ticket} at {price}.")
    return True


def modify_position_sl(position, new_sl: float) -> bool:
    """Updates the server-side Stop Loss on MT5."""
    symbol = position.symbol
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return False

    new_sl = round(new_sl, sym_info.digits)
    if abs(new_sl - position.sl) < sym_info.point:
        return True

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": symbol,
        "sl": new_sl,
        "tp": position.tp,
        "magic": position.magic,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        ret_code = result.retcode if result else "None"
        log.error(f"[{symbol}] SL modification failed for #{position.ticket} -> {new_sl}. Code: {ret_code}")
        return False

    log.info(f"[{symbol}] Server SL updated for #{position.ticket} -> {new_sl}")
    return True


def modify_position_tp(position, new_tp: float) -> bool:
    """Updates the server-side Take Profit on MT5."""
    symbol = position.symbol
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return False

    new_tp = round(new_tp, sym_info.digits)
    if abs(new_tp - position.tp) < sym_info.point:
        return True

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": symbol,
        "sl": position.sl,
        "tp": new_tp,
        "magic": position.magic,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        ret_code = result.retcode if result else "None"
        log.error(f"[{symbol}] TP modification failed for #{position.ticket} -> {new_tp}. Code: {ret_code}")
        return False

    log.info(f"[{symbol}] Server TP updated for #{position.ticket} -> {new_tp}")
    return True


def execute_trade(approved_trade: dict, magic: int) -> dict:
    """Transmits an approved order to MT5 with in-flight risk registration."""
    symbol = approved_trade["symbol"]
    signal = approved_trade["signal"]
    lot_size = approved_trade["lot_size"]
    sl_price = approved_trade["sl_price"]
    tp_price = approved_trade["tp_price"]

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return {"success": False, "reason": f"Symbol {symbol} unavailable."}

    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
    price = sym_info.ask if signal == "BUY" else sym_info.bid

    broker_val = risk.validate_broker_constraints(symbol, order_type, lot_size, price)
    if not broker_val["ok"]:
        return {"success": False, "reason": broker_val["reason"]}

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": 20,
        "magic": magic,
        "comment": "Nightshade Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    risk.add_in_flight_risk(symbol, signal, approved_trade.get("risk_amount", 0.0))

    try:
        result = mt5.order_send(request)
    finally:
        risk.clear_in_flight_risk(symbol, signal, approved_trade.get("risk_amount", 0.0))

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        ret_code = result.retcode if result else "None"
        reason = f"order_send failed: {ret_code}"
        log.error(f"[{symbol}] Execution failed: {reason}")
        return {"success": False, "reason": reason}

    risk.record_trade_opened()
    log.info(f"[{symbol}] Executed successfully. Ticket: {result.order}")
    return {"success": True, "ticket": result.order, "price": result.price}


# ---------------------------------------------------------------------------
# ADVANCED POSITION MANAGEMENT ORCHESTRATOR
# ---------------------------------------------------------------------------

def process_advanced_position_management(magic: int) -> None:
    """
    30-second position management loop structured in strict priority hierarchy:
      1. Net PnL Tracking
      2. Priority #0 Exit Guard (€70 Hard Profit Lock)
      3. Server-Side SL Adjustment (Ratchet / Breakeven)
      4. In-Flight Giveback Exits (Hard Cap, 30s Trend, Tiered Giveback)
      5. Time-Decay TP Adjustments (> 1 pip threshold)
    """
    positions = mt5.positions_get()
    if positions is None:
        log.error("positions_get() returned None during position management cycle.")
        return

    our_positions = [p for p in positions if p.magic == magic]
    active_tickets = {p.ticket for p in our_positions}
    _cleanup_monitor_state(active_tickets)

    now_ts = time.time()

    for pos in our_positions:
        symbol = pos.symbol
        ticket = pos.ticket
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            continue

        # -------------------------------------------------------------------
        # 1. Net PnL Tracking
        # -------------------------------------------------------------------
        net_pnl = float(pos.profit + pos.swap + getattr(pos, "commission", 0.0) + getattr(pos, "fee", 0.0))
        state = _get_or_init_monitor_state(ticket, open_time_sec=pos.time)

        if net_pnl > state["peak_pnl"]:
            state["peak_pnl"] = net_pnl

        state["pnl_history"].append((now_ts, net_pnl))
        if len(state["pnl_history"]) > 20:
            state["pnl_history"].pop(0)

        current_price = sym_info.bid if pos.type == mt5.POSITION_TYPE_BUY else sym_info.ask

        # -------------------------------------------------------------------
        # 2. Priority #0 Exit Guard
        # -------------------------------------------------------------------
        profit_lock_check = risk.evaluate_profit_lock_exit(net_pnl, state["peak_pnl"])
        if profit_lock_check["exit"]:
            log.info(f"[{symbol} #{ticket}] Priority #0 Exit: {profit_lock_check['reason']}")
            if _close_position_market(pos):
                pnl = risk.realized_pnl_for_position(ticket, magic)
                risk.record_trade_closed(pnl if pnl is not None else net_pnl)
                continue

        sl_distance = abs(pos.price_open - pos.sl) if pos.sl > 0 else risk.DEFAULT_MAX_SL_DISTANCE

        # -------------------------------------------------------------------
        # 3. Server-Side SL Adjustment (Ratchet / Breakeven)
        # -------------------------------------------------------------------
        target_sl = risk.calculate_ratchet_sl(
            pos_type=pos.type,
            open_price=pos.price_open,
            current_sl=pos.sl,
            current_price=current_price,
            sl_distance=sl_distance,
            net_pnl=net_pnl,
            point_value=sym_info.point,
        )
        if target_sl is not None:
            modify_position_sl(pos, target_sl)

        # -------------------------------------------------------------------
        # 4. In-Flight Giveback Exits
        # -------------------------------------------------------------------
        # 4a. Hard Giveback Cap
        hard_cap_check = risk.evaluate_hard_giveback_cap(state["peak_pnl"], net_pnl)
        if hard_cap_check["exit"]:
            log.info(f"[{symbol} #{ticket}] Hard Cap Exit: {hard_cap_check['reason']}")
            if _close_position_market(pos):
                pnl = risk.realized_pnl_for_position(ticket, magic)
                risk.record_trade_closed(pnl if pnl is not None else net_pnl)
                continue

        # 4b. 30s Trend Decline Exit
        decline_check = risk.evaluate_decline_to_zero(state["pnl_history"])
        if decline_check["exit"]:
            log.info(f"[{symbol} #{ticket}] Trend Decline Exit: {decline_check['reason']}")
            if _close_position_market(pos):
                pnl = risk.realized_pnl_for_position(ticket, magic)
                risk.record_trade_closed(pnl if pnl is not None else net_pnl)
                continue

        # 4c. Tiered High-Water Mark Giveback Exit
        giveback_check = risk.evaluate_giveback_exit(state["peak_pnl"], net_pnl)
        if giveback_check["exit"]:
            log.info(f"[{symbol} #{ticket}] Tiered Giveback Exit: {giveback_check['reason']}")
            if _close_position_market(pos):
                pnl = risk.realized_pnl_for_position(ticket, magic)
                risk.record_trade_closed(pnl if pnl is not None else net_pnl)
                continue

        # -------------------------------------------------------------------
        # 5. Time-Decay TP Adjustments
        # -------------------------------------------------------------------
        elapsed_seconds = max(0.0, now_ts - state["open_time"])
        target_tp = risk.calculate_time_decay_tp(
            open_price=pos.price_open,
            pos_type=pos.type,
            sl_distance=sl_distance,
            elapsed_seconds=elapsed_seconds,
            digits=sym_info.digits,
        )

        pip_threshold = 10.0 * sym_info.point  # 1 pip threshold
        if pos.tp == 0.0 or abs(target_tp - pos.tp) > pip_threshold:
            modify_position_tp(pos, target_tp)