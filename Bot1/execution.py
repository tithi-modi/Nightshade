# execution.py
# Nightshade Seed Engine - execution.py v11
# Broker operations and position management orchestrator.

import MetaTrader5 as mt5
import time
import logging
import math
import pandas as pd
from risk import *   # all config and risk functions
import risk

log = logging.getLogger("nightshade")

# ----------------------------------------------------------------------
# Filling mode helpers (reject invalid RETURN for market execution)
# ----------------------------------------------------------------------
def _supported_filling_mode(sym):
    mode = sym.filling_mode
    exemode = sym.trade_exemode  # 0=execution, 1=market, 2=exchange
    # For market execution (exemode 1 or 2), we need IOC or FOK
    if exemode in (1, 2):
        if mode & 2:   # IOC
            return mt5.ORDER_FILLING_IOC
        elif mode & 1: # FOK
            return mt5.ORDER_FILLING_FOK
        else:
            # RETURN is not valid for market execution; reject
            return None
    else:
        return mt5.ORDER_FILLING_RETURN

def _validate_and_send(request):
    check = mt5.order_check(request)
    if check is None or (check.retcode != mt5.TRADE_RETCODE_DONE and check.retcode != 0):
        return None, "order_check failed"
    result = mt5.order_send(request)
    if result is None:
        # reconcile with positions, orders, deals
        positions = mt5.positions_get(symbol=request.get("symbol"))
        if positions:
            for p in positions:
                if p.magic == request.get("magic"):
                    return {"order": p.ticket, "price": p.price_open, "retcode": mt5.TRADE_RETCODE_DONE}, None
        orders = mt5.orders_get(symbol=request.get("symbol"))
        if orders:
            for o in orders:
                if o.magic == request.get("magic"):
                    return {"order": o.ticket, "price": o.price_open, "retcode": mt5.TRADE_RETCODE_PLACED}, None
        return None, "order_send returned None and no position/order found"
    return result, None

# ----------------------------------------------------------------------
# Trade Execution – keep in‑flight risk on PLACED
# ----------------------------------------------------------------------
def execute_trade(approved_trade, magic):
    symbol = approved_trade["symbol"]
    signal = approved_trade["signal"]
    lot = approved_trade["lot_size"]
    sl = approved_trade["sl_price"]
    tp = approved_trade["tp_price"]
    risk_amount = approved_trade["risk_amount"]

    sym = mt5.symbol_info(symbol)
    if sym is None:
        return {"success": False, "reason": "Symbol not found"}
    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
    price = sym.ask if signal == "BUY" else sym.bid

    val = risk.validate_broker_constraints(symbol, order_type, lot, price)
    if not val["ok"]:
        return {"success": False, "reason": val["reason"]}

    filling = _supported_filling_mode(sym)
    if filling is None:
        return {"success": False, "reason": "No valid filling mode for market execution"}

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": magic,
        "comment": "NS Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    risk.add_in_flight_risk(symbol, signal, risk_amount)

    result, err = _validate_and_send(request)
    if result is None:
        risk.clear_in_flight_risk(symbol, signal, risk_amount)
        return {"success": False, "reason": err}

    if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            our_pos = [p for p in positions if p.magic == magic and p.ticket == result.order]
            if our_pos:
                risk.record_trade_opened()
                actual_loss = risk._broker_estimated_loss(symbol, order_type, lot, result.price, sl)
                if actual_loss is not None:
                    log.info(f"Actual risk at fill: {abs(actual_loss):.2f}")
                risk.clear_in_flight_risk(symbol, signal, risk_amount)
                return {"success": True, "ticket": result.order, "price": result.price}

    # PLACED: do not clear in-flight risk; it remains until filled or cancelled
    if result.retcode == mt5.TRADE_RETCODE_PLACED:
        log.warning(f"Order PLACED but not filled: ticket {result.order}. In‑flight risk retained.")
        return {"success": False, "reason": "Order placed but not filled", "placed": True}

    # Any other retcode: clear risk and fail
    risk.clear_in_flight_risk(symbol, signal, risk_amount)
    return {"success": False, "reason": f"Retcode {result.retcode}"}

# ----------------------------------------------------------------------
# Market Close (uses same robust flow)
# ----------------------------------------------------------------------
def close_position_market(position, magic):
    symbol = position.symbol
    sym = mt5.symbol_info(symbol)
    if sym is None:
        return False
    order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = sym.bid if position.type == mt5.POSITION_TYPE_BUY else sym.ask
    filling = _supported_filling_mode(sym)
    if filling is None:
        log.error(f"Invalid filling mode for close on {symbol}")
        return False
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": symbol,
        "volume": position.volume,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": magic,
        "comment": "NS Exit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result, err = _validate_and_send(request)
    if result is None:
        log.error(f"Close failed: {err}")
        return False
    if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        log.info(f"Closed position #{position.ticket} at {result.price}")
        pnl = risk.realized_pnl_for_position(position.ticket, magic)
        if pnl is not None:
            risk.record_trade_closed(pnl)
        else:
            risk.record_trade_closed(position.profit + position.swap)
        return True
    log.error(f"Close failed retcode {result.retcode}")
    return False

# ----------------------------------------------------------------------
# Modify SL / TP – with stop/freeze level checks
# ----------------------------------------------------------------------
def modify_sl(position, new_sl):
    sym = mt5.symbol_info(position.symbol)
    if sym is None:
        return False
    new_sl = round(new_sl, sym.digits)
    if abs(new_sl - position.sl) < sym.point:
        return True
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    # Check stop level
    if position.type == mt5.POSITION_TYPE_BUY and (tick.bid - new_sl) < sym.trade_stops_level * sym.point:
        return False
    if position.type == mt5.POSITION_TYPE_SELL and (new_sl - tick.ask) < sym.trade_stops_level * sym.point:
        return False
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": new_sl,
        "tp": position.tp,
        "magic": position.magic,
    }
    result, err = _validate_and_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"SL updated to {new_sl}")
        return True
    return False

def modify_tp(position, new_tp):
    sym = mt5.symbol_info(position.symbol)
    if sym is None:
        return False
    new_tp = round(new_tp, sym.digits)
    if abs(new_tp - position.tp) < sym.point:
        return True
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    # Check if new TP is behind market (would be immediately triggered)
    if position.type == mt5.POSITION_TYPE_BUY and new_tp <= tick.bid:
        log.info("TP target already exceeded, closing market")
        return close_position_market(position, position.magic)
    if position.type == mt5.POSITION_TYPE_SELL and new_tp >= tick.ask:
        log.info("TP target already exceeded, closing market")
        return close_position_market(position, position.magic)
    # Check stop/freeze level (some brokers require TP away from market)
    if position.type == mt5.POSITION_TYPE_BUY and (new_tp - tick.bid) < sym.trade_stops_level * sym.point:
        return False
    if position.type == mt5.POSITION_TYPE_SELL and (tick.ask - new_tp) < sym.trade_stops_level * sym.point:
        return False
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": position.sl,
        "tp": new_tp,
        "magic": position.magic,
    }
    result, err = _validate_and_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"TP updated to {new_tp}")
        return True
    return False

# ----------------------------------------------------------------------
# Advanced Position Management Orchestrator (with SMA exit)
# ----------------------------------------------------------------------
def get_sma(symbol, period=20):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, period+1)
    if rates is None or len(rates) < period:
        return None
    # Access the 'close' field using dictionary-style indexing
    closes = [r['close'] for r in rates[-period:]]
    return sum(closes) / period
def process_advanced_position_management(magic):
    positions = mt5.positions_get()
    if positions is None:
        log.error("positions_get failed in management")
        return
    our_positions = [p for p in positions if p.magic == magic]
    active_tickets = {p.ticket for p in our_positions}
    risk.cleanup_position_state(active_tickets)

    now = time.time()

    for pos in our_positions:
        symbol = pos.symbol
        sym = mt5.symbol_info(symbol)
        if sym is None:
            continue

        net_pnl = pos.profit + pos.swap
        sl_dist = abs(pos.price_open - pos.sl) if pos.sl else 0.0
        if sl_dist == 0:
            continue

        if pos.type == mt5.POSITION_TYPE_BUY:
            profit_dist = sym.bid - pos.price_open
            current_price = sym.bid
        else:
            profit_dist = pos.price_open - sym.ask
            current_price = sym.ask
        current_r = profit_dist / sl_dist if sl_dist else 0.0

        mon = risk.get_position_monitor_state(pos.ticket)
        if mon["peak_r"] is None or current_r > mon["peak_r"]:
            mon["peak_r"] = current_r
        mon["pnl_history_r"].append((now, current_r))
        if len(mon["pnl_history_r"]) > 20:
            mon["pnl_history_r"].pop(0)

        # ------------------------------------------------------------------
        # Exit Priority Order (as per spec)
        # ------------------------------------------------------------------

        # 1. Hard Giveback
        hard = risk.evaluate_hard_giveback(mon["peak_r"], current_r)
        if hard["exit"]:
            log.info(f"Hard giveback: {hard['reason']}")
            if close_position_market(pos, magic):
                continue

        # 2. Decline to Zero
        decline = risk.evaluate_decline_to_zero(mon["pnl_history_r"])
        if decline["exit"]:
            log.info(f"Decline to zero: {decline['reason']}")
            if close_position_market(pos, magic):
                continue

        # 3. Tiered Giveback
        giveback = risk.evaluate_giveback_exit(mon["peak_r"], current_r)
        if giveback["exit"]:
            log.info(f"Giveback: {giveback['reason']}")
            if close_position_market(pos, magic):
                continue

        # 4. SMA Mean‑Reversion Exit
        sma = get_sma(symbol)
        if sma is not None:
            if pos.type == mt5.POSITION_TYPE_BUY and sym.bid <= sma:
                log.info(f"SMA exit: BUY price {sym.bid} crossed below SMA {sma:.5f}")
                if close_position_market(pos, magic):
                    continue
            elif pos.type == mt5.POSITION_TYPE_SELL and sym.ask >= sma:
                log.info(f"SMA exit: SELL price {sym.ask} crossed above SMA {sma:.5f}")
                if close_position_market(pos, magic):
                    continue

        # 5. Time-Decay TP
        elapsed = now - pos.time
        target_r = risk.get_time_decay_target(elapsed)
        if current_r >= target_r:
            log.info(f"Time-decay target reached (+{target_r:.2f}R), closing")
            if close_position_market(pos, magic):
                continue
        else:
            new_tp = pos.price_open + (target_r * sl_dist) if pos.type == mt5.POSITION_TYPE_BUY else pos.price_open - (target_r * sl_dist)
            new_tp = round(new_tp, sym.digits)
            if abs(new_tp - pos.tp) > sym.point * 10:
                modify_tp(pos, new_tp)

        # 6. Ratchet SL
        new_sl = risk.calculate_ratchet_sl(pos.type, pos.price_open, pos.sl,
                                           current_price, sl_dist, mon["peak_r"])
        if new_sl is not None:
            modify_sl(pos, new_sl)

        mon["last_update"] = now
        risk.update_position_monitor_state(pos.ticket, mon)# execution.py
# Nightshade Seed Engine - execution.py v11
# Broker operations and position management orchestrator.

import MetaTrader5 as mt5
import time
import logging
import math
import pandas as pd
from risk import *   # all config and risk functions
import risk

log = logging.getLogger("nightshade")

# ----------------------------------------------------------------------
# Filling mode helpers (reject invalid RETURN for market execution)
# ----------------------------------------------------------------------
def _supported_filling_mode(sym):
    mode = sym.filling_mode
    exemode = sym.trade_exemode  # 0=execution, 1=market, 2=exchange
    # For market execution (exemode 1 or 2), we need IOC or FOK
    if exemode in (1, 2):
        if mode & 2:   # IOC
            return mt5.ORDER_FILLING_IOC
        elif mode & 1: # FOK
            return mt5.ORDER_FILLING_FOK
        else:
            # RETURN is not valid for market execution; reject
            return None
    else:
        return mt5.ORDER_FILLING_RETURN

def _validate_and_send(request):
    check = mt5.order_check(request)
    if check is None or (check.retcode != mt5.TRADE_RETCODE_DONE and check.retcode != 0):
        return None, "order_check failed"
    result = mt5.order_send(request)
    if result is None:
        # reconcile with positions, orders, deals
        positions = mt5.positions_get(symbol=request.get("symbol"))
        if positions:
            for p in positions:
                if p.magic == request.get("magic"):
                    return {"order": p.ticket, "price": p.price_open, "retcode": mt5.TRADE_RETCODE_DONE}, None
        orders = mt5.orders_get(symbol=request.get("symbol"))
        if orders:
            for o in orders:
                if o.magic == request.get("magic"):
                    return {"order": o.ticket, "price": o.price_open, "retcode": mt5.TRADE_RETCODE_PLACED}, None
        return None, "order_send returned None and no position/order found"
    return result, None

# ----------------------------------------------------------------------
# Trade Execution – keep in‑flight risk on PLACED
# ----------------------------------------------------------------------
def execute_trade(approved_trade, magic):
    symbol = approved_trade["symbol"]
    signal = approved_trade["signal"]
    lot = approved_trade["lot_size"]
    sl = approved_trade["sl_price"]
    tp = approved_trade["tp_price"]
    risk_amount = approved_trade["risk_amount"]

    sym = mt5.symbol_info(symbol)
    if sym is None:
        return {"success": False, "reason": "Symbol not found"}
    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
    price = sym.ask if signal == "BUY" else sym.bid

    val = risk.validate_broker_constraints(symbol, order_type, lot, price)
    if not val["ok"]:
        return {"success": False, "reason": val["reason"]}

    filling = _supported_filling_mode(sym)
    if filling is None:
        return {"success": False, "reason": "No valid filling mode for market execution"}

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": magic,
        "comment": "NS Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    risk.add_in_flight_risk(symbol, signal, risk_amount)

    result, err = _validate_and_send(request)
    if result is None:
        risk.clear_in_flight_risk(symbol, signal, risk_amount)
        return {"success": False, "reason": err}

    if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            our_pos = [p for p in positions if p.magic == magic and p.ticket == result.order]
            if our_pos:
                risk.record_trade_opened()
                actual_loss = risk._broker_estimated_loss(symbol, order_type, lot, result.price, sl)
                if actual_loss is not None:
                    log.info(f"Actual risk at fill: {abs(actual_loss):.2f}")
                risk.clear_in_flight_risk(symbol, signal, risk_amount)
                return {"success": True, "ticket": result.order, "price": result.price}

    # PLACED: do not clear in-flight risk; it remains until filled or cancelled
    if result.retcode == mt5.TRADE_RETCODE_PLACED:
        log.warning(f"Order PLACED but not filled: ticket {result.order}. In‑flight risk retained.")
        return {"success": False, "reason": "Order placed but not filled", "placed": True}

    # Any other retcode: clear risk and fail
    risk.clear_in_flight_risk(symbol, signal, risk_amount)
    return {"success": False, "reason": f"Retcode {result.retcode}"}

# ----------------------------------------------------------------------
# Market Close (uses same robust flow)
# ----------------------------------------------------------------------
def close_position_market(position, magic):
    symbol = position.symbol
    sym = mt5.symbol_info(symbol)
    if sym is None:
        return False
    order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = sym.bid if position.type == mt5.POSITION_TYPE_BUY else sym.ask
    filling = _supported_filling_mode(sym)
    if filling is None:
        log.error(f"Invalid filling mode for close on {symbol}")
        return False
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": symbol,
        "volume": position.volume,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": magic,
        "comment": "NS Exit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result, err = _validate_and_send(request)
    if result is None:
        log.error(f"Close failed: {err}")
        return False
    if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        log.info(f"Closed position #{position.ticket} at {result.price}")
        pnl = risk.realized_pnl_for_position(position.ticket, magic)
        if pnl is not None:
            risk.record_trade_closed(pnl)
        else:
            risk.record_trade_closed(position.profit + position.swap)
        return True
    log.error(f"Close failed retcode {result.retcode}")
    return False

# ----------------------------------------------------------------------
# Modify SL / TP – with stop/freeze level checks
# ----------------------------------------------------------------------
def modify_sl(position, new_sl):
    sym = mt5.symbol_info(position.symbol)
    if sym is None:
        return False
    new_sl = round(new_sl, sym.digits)
    if abs(new_sl - position.sl) < sym.point:
        return True
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    # Check stop level
    if position.type == mt5.POSITION_TYPE_BUY and (tick.bid - new_sl) < sym.trade_stops_level * sym.point:
        return False
    if position.type == mt5.POSITION_TYPE_SELL and (new_sl - tick.ask) < sym.trade_stops_level * sym.point:
        return False
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": new_sl,
        "tp": position.tp,
        "magic": position.magic,
    }
    result, err = _validate_and_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"SL updated to {new_sl}")
        return True
    return False

def modify_tp(position, new_tp):
    sym = mt5.symbol_info(position.symbol)
    if sym is None:
        return False
    new_tp = round(new_tp, sym.digits)
    if abs(new_tp - position.tp) < sym.point:
        return True
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    # Check if new TP is behind market (would be immediately triggered)
    if position.type == mt5.POSITION_TYPE_BUY and new_tp <= tick.bid:
        log.info("TP target already exceeded, closing market")
        return close_position_market(position, position.magic)
    if position.type == mt5.POSITION_TYPE_SELL and new_tp >= tick.ask:
        log.info("TP target already exceeded, closing market")
        return close_position_market(position, position.magic)
    # Check stop/freeze level (some brokers require TP away from market)
    if position.type == mt5.POSITION_TYPE_BUY and (new_tp - tick.bid) < sym.trade_stops_level * sym.point:
        return False
    if position.type == mt5.POSITION_TYPE_SELL and (tick.ask - new_tp) < sym.trade_stops_level * sym.point:
        return False
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": position.sl,
        "tp": new_tp,
        "magic": position.magic,
    }
    result, err = _validate_and_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"TP updated to {new_tp}")
        return True
    return False

# ----------------------------------------------------------------------
# Advanced Position Management Orchestrator (with SMA exit)
# ----------------------------------------------------------------------
def get_sma(symbol, period=20):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, period+1)
    if rates is None or len(rates) < period:
        return None
    # Access the 'close' field using dictionary-style indexing
    closes = [r['close'] for r in rates[-period:]]
    return sum(closes) / period
def process_advanced_position_management(magic):
    positions = mt5.positions_get()
    if positions is None:
        log.error("positions_get failed in management")
        return
    our_positions = [p for p in positions if p.magic == magic]
    active_tickets = {p.ticket for p in our_positions}
    risk.cleanup_position_state(active_tickets)

    now = time.time()

    for pos in our_positions:
        symbol = pos.symbol
        sym = mt5.symbol_info(symbol)
        if sym is None:
            continue

        net_pnl = pos.profit + pos.swap
        sl_dist = abs(pos.price_open - pos.sl) if pos.sl else 0.0
        if sl_dist == 0:
            continue

        if pos.type == mt5.POSITION_TYPE_BUY:
            profit_dist = sym.bid - pos.price_open
            current_price = sym.bid
        else:
            profit_dist = pos.price_open - sym.ask
            current_price = sym.ask
        current_r = profit_dist / sl_dist if sl_dist else 0.0

        mon = risk.get_position_monitor_state(pos.ticket)
        if mon["peak_r"] is None or current_r > mon["peak_r"]:
            mon["peak_r"] = current_r
        mon["pnl_history_r"].append((now, current_r))
        if len(mon["pnl_history_r"]) > 20:
            mon["pnl_history_r"].pop(0)

        # ------------------------------------------------------------------
        # Exit Priority Order (as per spec)
        # ------------------------------------------------------------------

        # 1. Hard Giveback
        hard = risk.evaluate_hard_giveback(mon["peak_r"], current_r)
        if hard["exit"]:
            log.info(f"Hard giveback: {hard['reason']}")
            if close_position_market(pos, magic):
                continue

        # 2. Decline to Zero
        decline = risk.evaluate_decline_to_zero(mon["pnl_history_r"])
        if decline["exit"]:
            log.info(f"Decline to zero: {decline['reason']}")
            if close_position_market(pos, magic):
                continue

        # 3. Tiered Giveback
        giveback = risk.evaluate_giveback_exit(mon["peak_r"], current_r)
        if giveback["exit"]:
            log.info(f"Giveback: {giveback['reason']}")
            if close_position_market(pos, magic):
                continue

        # 4. SMA Mean‑Reversion Exit
        sma = get_sma(symbol)
        if sma is not None:
            if pos.type == mt5.POSITION_TYPE_BUY and sym.bid <= sma:
                log.info(f"SMA exit: BUY price {sym.bid} crossed below SMA {sma:.5f}")
                if close_position_market(pos, magic):
                    continue
            elif pos.type == mt5.POSITION_TYPE_SELL and sym.ask >= sma:
                log.info(f"SMA exit: SELL price {sym.ask} crossed above SMA {sma:.5f}")
                if close_position_market(pos, magic):
                    continue

        # 5. Time-Decay TP
        elapsed = now - pos.time
        target_r = risk.get_time_decay_target(elapsed)
        if current_r >= target_r:
            log.info(f"Time-decay target reached (+{target_r:.2f}R), closing")
            if close_position_market(pos, magic):
                continue
        else:
            new_tp = pos.price_open + (target_r * sl_dist) if pos.type == mt5.POSITION_TYPE_BUY else pos.price_open - (target_r * sl_dist)
            new_tp = round(new_tp, sym.digits)
            if abs(new_tp - pos.tp) > sym.point * 10:
                modify_tp(pos, new_tp)

        # 6. Ratchet SL
        new_sl = risk.calculate_ratchet_sl(pos.type, pos.price_open, pos.sl,
                                           current_price, sl_dist, mon["peak_r"])
        if new_sl is not None:
            modify_sl(pos, new_sl)

        mon["last_update"] = now
        risk.update_position_monitor_state(pos.ticket, mon)
