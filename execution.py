"""
Nightshade Seed Engine - execution.py  v5  (Layer 4)

Changes vs v4:
  - Advanced position management orchestrator with state tracking.
  - Partial closures and trailing stop routines.
"""

"""
Nightshade Seed Engine - execution.py  v6  (Layer 4)

Changes vs v5:
  - Integrated in-flight risk registration with risk.add_in_flight_risk() and clear_in_flight_risk().
  - Enhanced error handling on order_send failures.
"""

"""
Nightshade Seed Engine - execution.py  v7  (Layer 4)

Changes vs v6:
  - Support for server-side ratchet Stop Loss updates based on R-multiple / monetary profit.
"""

"""
Nightshade Seed Engine - execution.py  v8  (Layer 4)

Changes vs v7:
  - Added time-decay TP updates and peak giveback exit triggers.
  - Added evaluate_decline_to_zero() calls in position management.
"""

"""
Nightshade Seed Engine - execution.py  v9  (Layer 4)

Changes vs v8:
  - Integrated HARD_PROFIT_LOCK_EUR (€70.0) priority exit guard.
  - Integrated dynamic R:R execution parameters and fee-buffered breakeven ratchet.
"""

"""
Nightshade Seed Engine - execution.py  v10  (Layer 4)

Changes vs v9:
  - Re-ordered 30-second position management loop to execute Three-Tier Exit Architecture.
  - Priority 0: Hard Profit Lock (€70.0 Cap / Floor Check).
  - Priority 1: Tier 1 Trailing Profit Lock (activates at >= €0.30).
  - Priority 2: Server-side SL Ratchet (Breakeven + 1 pip fee buffer at +0.5R or €35.00).
  - Priority 3: Tier 2 Hard Loss Cap (instant exit at <= -€15.00).
  - Priority 4: Tier 3 Stagnation Exit (exits flat trades after 30 minutes).
  - Priority 5: Time-Decay TP Target Adjustment.
  - Purged legacy 30-second trend decline check (evaluate_decline_to_zero).
"""

import MetaTrader5 as mt5
import datetime
import logging
import risk

log = logging.getLogger("nightshade")

# In-memory tracking for active position state (peak PnL, open timestamp, etc.)
_POSITION_MONITOR_STATE = {}


def execute_trade(risk_result: dict, magic: int) -> dict:
    """
    Transmits market order to MT5 with in-flight risk management.
    """
    if not risk_result.get("is_approved"):
        return {"success": False, "reason": "Trade not approved by risk engine."}

    symbol = risk_result["symbol"]
    signal = risk_result["signal"]
    lot_size = risk_result["lot_size"]
    sl_price = risk_result["sl_price"]
    tp_price = risk_result["tp_price"]
    risk_amount = risk_result["risk_amount"]

    # Register in-flight risk prior to order transmission
    risk.add_in_flight_risk(symbol, signal, risk_amount)

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        risk.clear_in_flight_risk(symbol, signal, risk_amount)
        return {"success": False, "reason": f"Could not get symbol_info for {symbol}"}

    if not sym_info.visible:
        if not mt5.symbol_select(symbol, True):
            risk.clear_in_flight_risk(symbol, signal, risk_amount)
            return {"success": False, "reason": f"Failed to select symbol {symbol}"}

    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        risk.clear_in_flight_risk(symbol, signal, risk_amount)
        return {"success": False, "reason": f"Could not get tick for {symbol}"}

    price = tick.ask if signal == "BUY" else tick.bid

    # Pre-trade broker margin validation
    margin_check = risk.validate_broker_constraints(symbol, order_type, lot_size, price)
    if not margin_check["ok"]:
        risk.clear_in_flight_risk(symbol, signal, risk_amount)
        return {"success": False, "reason": margin_check["reason"]}

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
        "comment": "Nightshade Seed Engine",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    # Fallback filling execution modes if IOC fails
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        if result is not None and result.retcode in (mt5.TRADE_RETCODE_INVALID_FILL, 10030):
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                request["type_filling"] = mt5.ORDER_FILLING_RETURN
                result = mt5.order_send(request)

    # Clear in-flight risk accumulator regardless of execution outcome
    risk.clear_in_flight_risk(symbol, signal, risk_amount)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error_code = result.retcode if result else "None"
        error_msg = result.comment if result else "Unknown error"
        log.error(f"[{symbol}] Order send failed (Code {error_code}: {error_msg})")
        return {"success": False, "reason": f"Order send failed ({error_code}: {error_msg})"}

    log.info(f"[{symbol}] Order executed successfully! Ticket: {result.order}, Fill Price: {result.price}")
    risk.record_trade_opened()
    return {"success": True, "ticket": result.order, "price": result.price}


def _close_position_market(position, reason: str) -> bool:
    """Closes an open position at market price."""
    symbol = position.symbol
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"[{symbol}] Cannot close position {position.ticket}: symbol_info_tick returned None")
        return False

    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": position.volume,
        "type": close_type,
        "position": position.ticket,
        "price": price,
        "deviation": 20,
        "magic": position.magic,
        "comment": f"Close: {reason[:25]}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        if result is not None and result.retcode in (mt5.TRADE_RETCODE_INVALID_FILL, 10030):
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else "None"
        log.error(f"[{symbol}] Close failed for ticket {position.ticket}: {err}")
        return False

    log.info(f"[{symbol}] Closed position {position.ticket} | Reason: {reason}")

    # Fetch complete realized deal PnL from MT5 history
    realized_pnl = risk.realized_pnl_for_position(position.ticket, position.magic)
    if realized_pnl is None:
        realized_pnl = position.profit + position.swap

    risk.record_trade_closed(realized_pnl)

    if position.ticket in _POSITION_MONITOR_STATE:
        del _POSITION_MONITOR_STATE[position.ticket]

    return True


def _modify_position_sl_tp(ticket: int, sl: float, tp: float) -> bool:
    """Modifies Stop Loss and Take Profit levels on the MT5 server."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.error(f"Cannot modify ticket {ticket}: Position not found.")
        return False

    pos = positions[0]
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": ticket,
        "sl": float(sl),
        "tp": float(tp),
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else "None"
        log.error(f"[{pos.symbol}] SL/TP modification failed for ticket {ticket}: {err}")
        return False

    log.info(f"[{pos.symbol}] Ticket {ticket} SL/TP updated -> SL: {sl:.5f}, TP: {tp:.5f}")
    return True


def process_advanced_position_management(magic: int) -> None:
    """
    30-Second Position Management Loop executing the Three-Tier Exit Architecture.
    Evaluates open positions in strict priority order.
    """
    positions = mt5.positions_get()
    if positions is None:
        log.error("process_advanced_position_management: positions_get() returned None.")
        return

    our_positions = [p for p in positions if p.magic == magic]
    active_tickets = {p.ticket for p in our_positions}

    # Clean stale tickets from state dictionary
    stale_tickets = [t for t in _POSITION_MONITOR_STATE if t not in active_tickets]
    for t in stale_tickets:
        del _POSITION_MONITOR_STATE[t]

    now_utc = datetime.datetime.utcnow().timestamp()

    for pos in our_positions:
        ticket = pos.ticket
        symbol = pos.symbol
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            continue

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue

        current_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        net_pnl = pos.profit + pos.swap + getattr(pos, "commission", 0.0)
        elapsed_seconds = max(0.0, now_utc - pos.time)

        # 1. Peak Tracking
        if ticket not in _POSITION_MONITOR_STATE:
            _POSITION_MONITOR_STATE[ticket] = {
                "peak_pnl": net_pnl,
                "open_time": pos.time
            }

        state_entry = _POSITION_MONITOR_STATE[ticket]
        if net_pnl > state_entry["peak_pnl"]:
            state_entry["peak_pnl"] = net_pnl

        peak_pnl = state_entry["peak_pnl"]

        # Priority 0: Hard Profit Lock (€70.0 Cap / Floor)
        profit_lock = risk.evaluate_profit_lock_exit(net_pnl, peak_pnl)
        if profit_lock["exit"]:
            _close_position_market(pos, profit_lock["reason"])
            continue

        # Priority 1: Tier 1 Trailing Profit Lock (Green Trades >= €0.30)
        tier1 = risk.evaluate_trailing_profit_lock(peak_pnl, net_pnl)
        if tier1["exit"]:
            _close_position_market(pos, tier1["reason"])
            continue

        # Priority 2: Server-Side SL Ratchet (Breakeven + 1 pip Fee Buffer at +0.5R or €35.00)
        sl_distance = abs(pos.price_open - pos.sl) if pos.sl > 0 else 0.0
        new_sl = risk.calculate_ratchet_sl(
            pos_type=pos.type,
            open_price=pos.price_open,
            current_sl=pos.sl,
            current_price=current_price,
            sl_distance=sl_distance,
            net_pnl=net_pnl,
            point_value=sym_info.point
        )
        if new_sl is not None and round(new_sl, sym_info.digits) != round(pos.sl, sym_info.digits):
            _modify_position_sl_tp(ticket, sl=new_sl, tp=pos.tp)

        # Priority 3: Tier 2 Hard Loss Cap (Red Trades <= -€15.00)
        tier2 = risk.evaluate_hard_loss_cap(net_pnl)
        if tier2["exit"]:
            _close_position_market(pos, tier2["reason"])
            continue

        # Priority 4: Tier 3 Time-Based Stagnation Exit (Flat Trades after 30 min)
        tier3 = risk.evaluate_stagnation_exit(net_pnl, elapsed_seconds)
        if tier3["exit"]:
            _close_position_market(pos, tier3["reason"])
            continue

        # Priority 5: Time-Decay TP Adjustment
        if sl_distance > 0:
            new_tp = risk.calculate_time_decay_tp(
                open_price=pos.price_open,
                pos_type=pos.type,
                sl_distance=sl_distance,
                elapsed_seconds=elapsed_seconds,
                digits=sym_info.digits
            )
            if pos.tp > 0 and round(new_tp, sym_info.digits) != round(pos.tp, sym_info.digits):
                _modify_position_sl_tp(ticket, sl=pos.sl, tp=new_tp)
