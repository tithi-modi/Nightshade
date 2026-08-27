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

import MetaTrader5 as mt5
import time
import logging
import threading

import risk
from risk import (
    record_trade_opened,
    evaluate_risk,
    validate_broker_constraints,
    add_in_flight_risk,
    clear_in_flight_risk,
    calculate_ratchet_sl,
)

MAGIC_NUMBER        = 20260818
MAX_DEVIATION       = 20
RETRY_WAIT_S        = 5
MAX_PRICE_DRIFT_PCT = 15.0   # if execution-time SL distance implies risk more
                              # than this % worse than originally approved,
                              # reject rather than send at stale sizing

RETRYABLE_RETCODES = {
    mt5.TRADE_RETCODE_REQUOTE,
    mt5.TRADE_RETCODE_CONNECTION,
    mt5.TRADE_RETCODE_PRICE_CHANGED,
    mt5.TRADE_RETCODE_TIMEOUT,
    mt5.TRADE_RETCODE_PRICE_OFF,
    mt5.TRADE_RETCODE_REJECT,
    mt5.TRADE_RETCODE_ERROR,
}

# Outcomes that mean "the broker accepted something, don't assume it's
# safe to resend" -- must be reconciled against live state first.
AMBIGUOUS_RETCODES = {
    mt5.TRADE_RETCODE_DONE_PARTIAL,
    mt5.TRADE_RETCODE_PLACED,
}

# Bitmask constants for sym.filling_mode (1 = FOK, 2 = IOC)
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

_EXECUTION_LOCK = threading.Lock()
_POSITION_MONITOR_STATE = {}


def sync_positions(symbol: str = None) -> None:
    """Forces MT5 client cache update for positions and account info."""
    if symbol:
        mt5.positions_get(symbol=symbol)
    mt5.positions_get()
    mt5.account_info()


def _supported_filling_mode(sym) -> int:
    """Determine a filling mode actually supported by this symbol/broker."""
    mode = sym.filling_mode
    if mode & SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    if mode & SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _fallback_filling_mode(sym, primary: int):
    """Return a different broker-supported mode, or None."""
    mode = sym.filling_mode
    if primary != mt5.ORDER_FILLING_IOC and (mode & SYMBOL_FILLING_IOC):
        return mt5.ORDER_FILLING_IOC
    if primary != mt5.ORDER_FILLING_FOK and (mode & SYMBOL_FILLING_FOK):
        return mt5.ORDER_FILLING_FOK
    return None


def _build_request(trade_proposal: dict, filling_mode: int, log: logging.Logger):
    """Build the request at the same fresh price used for risk sizing."""
    symbol     = trade_proposal["symbol"]
    signal     = trade_proposal["signal"]
    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL

    price = trade_proposal["entry_price"]

    return {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       trade_proposal["lot_size"],
        "type":         order_type,
        "price":        price,
        "sl":           trade_proposal["sl_price"],
        "tp":           trade_proposal["tp_price"],
        "deviation":    MAX_DEVIATION,
        "magic":        MAGIC_NUMBER,
        "comment":      "NSD_SEED_v6",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }, price


def _revalidate_at_execution_price(trade_proposal: dict, log: logging.Logger) -> dict:
    """
    P0-7: recompute SL/TP/lot against the live tick before sending.
    Returns an updated, re-approved proposal, or a rejection dict.
    """
    symbol = trade_proposal["symbol"]
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"is_approved": False, "reject_reason": f"[{symbol}] cannot read fresh tick for revalidation."}

    fresh_price = tick.ask if trade_proposal["signal"] == "BUY" else tick.bid

    sl_distance = trade_proposal.get("sl_distance")
    if not sl_distance:
        return {"is_approved": False, "reject_reason": f"[{symbol}] original proposal missing sl_distance; cannot revalidate."}

    revalidated = evaluate_risk(
        signal_type=trade_proposal["signal"],
        current_price=fresh_price,
        atr_val=sl_distance / 1.5,
        risk_pct=(trade_proposal["risk_amount"] / trade_proposal["equity"] * 100.0)
                  if trade_proposal.get("equity") else 1.0,
        rr_ratio=trade_proposal.get("rr_ratio", 1.5),
        symbol=symbol,
    )

    if not revalidated.get("is_approved"):
        return revalidated

    drift_pct = abs(revalidated["risk_amount"] - trade_proposal["risk_amount"]) / max(trade_proposal["risk_amount"], 1e-9) * 100.0
    if drift_pct > MAX_PRICE_DRIFT_PCT:
        return {
            "is_approved": False,
            "reject_reason": (
                f"[{symbol}] Price moved enough that risk drifted {drift_pct:.1f}% "
                f"(> {MAX_PRICE_DRIFT_PCT}% tolerance) between signal and execution. Rejecting stale trade."
            ),
        }

    log.info(
        f"[{symbol}] Revalidated at execution price {fresh_price:.5f} "
        f"(risk drift {drift_pct:.2f}%). Lot: {revalidated['lot_size']}."
    )
    return revalidated


def _reconcile_ambiguous_result(symbol: str, magic: int, log: logging.Logger) -> bool:
    """
    P0-8: after an ambiguous result (timeout/connection/partial/placed),
    check positions, active orders, and recent deals before deciding
    whether it's safe to retry.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        log.error(f"[{symbol}] Reconciliation: positions_get() failed. Failing closed (no retry).")
        return True
    if any(p.magic == magic for p in positions):
        log.warning(f"[{symbol}] Reconciliation: an open position already exists. Skipping retry.")
        return True

    orders = mt5.orders_get(symbol=symbol)
    if orders is None:
        log.error(f"[{symbol}] Reconciliation: orders_get() failed. Failing closed (no retry).")
        return True
    if any(o.magic == magic for o in orders):
        log.warning(f"[{symbol}] Reconciliation: a pending/active order already exists. Skipping retry.")
        return True

    since = time.time() - 300
    from_dt = __import__("datetime").datetime.utcfromtimestamp(since)
    now_dt  = __import__("datetime").datetime.utcnow()
    deals = mt5.history_deals_get(from_dt, now_dt, group=f"*{symbol}*")
    if deals is None:
        log.error(f"[{symbol}] Reconciliation: history_deals_get() failed. Failing closed (no retry).")
        return True
    if any(d.magic == magic for d in deals):
        log.warning(f"[{symbol}] Reconciliation: a recent deal for this bot already exists. Skipping retry.")
        return True

    log.info(f"[{symbol}] Reconciliation: no position/order/deal found. Safe to retry.")
    return False


def execute_order(
    trade_proposal: dict,
    log: logging.Logger = None,
) -> bool:
    """
    Sends a validated trade proposal to MT5 as a market order using an
    execution mutex lock to force single-file execution processing.
    """
    if log is None:
        log = logging.getLogger("nightshade")

    with _EXECUTION_LOCK:
        return _execute_order_locked(trade_proposal, log)


def _execute_order_locked(
    trade_proposal: dict,
    log: logging.Logger,
) -> bool:
    if not trade_proposal or not trade_proposal.get("is_approved", False):
        log.error("Execution called with unapproved proposal.")
        return False

    symbol = trade_proposal["symbol"]
    sym    = mt5.symbol_info(symbol)

    if sym is None:
        log.error(f"[{symbol}] symbol_info() returned None. Aborting.")
        return False
    if not sym.visible:
        log.error(f"[{symbol}] Not visible in Market Watch. Aborting.")
        return False

    # --- P0-6: fail-closed duplicate-position check ---
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        log.error(f"[{symbol}] positions_get() returned None pre-trade. Failing closed. Aborting.")
        return False
    if any(p.magic == MAGIC_NUMBER for p in positions):
        log.warning(f"[{symbol}] Position already open for this bot. Aborting to avoid duplicate.")
        return False

    # --- P0-7: revalidate risk at (near-)execution price ---
    trade_proposal = _revalidate_at_execution_price(trade_proposal, log)
    if not trade_proposal.get("is_approved"):
        log.warning(f"[{symbol}] Execution-time revalidation rejected trade: {trade_proposal.get('reject_reason')}")
        return False

    # --- P1-12: pre-trade broker validation (margin) ---
    order_type = mt5.ORDER_TYPE_BUY if trade_proposal["signal"] == "BUY" else mt5.ORDER_TYPE_SELL
    check_price = trade_proposal["entry_price"]

    margin_check = validate_broker_constraints(symbol, order_type, trade_proposal["lot_size"], check_price)
    if not margin_check.get("ok"):
        log.error(f"[{symbol}] Pre-trade validation failed: {margin_check.get('reason')}. Aborting.")
        return False

    # --- Pre-Send Risk Reservation ---
    add_in_flight_risk(symbol, trade_proposal["signal"], trade_proposal["risk_amount"])

    try:
        # --- Determine broker-supported filling mode (P1-12) ---
        primary_filling = _supported_filling_mode(sym)

        built = _build_request(trade_proposal, primary_filling, log)
        if built is None:
            log.error(f"[{symbol}] Cannot build order request. Aborting.")
            return False
        request, exec_price = built

        # --- order_check() before order_send() ---
        check_result = mt5.order_check(request)
        if check_result is None:
            code, msg = mt5.last_error()
            log.error(f"[{symbol}] order_check() returned None. MT5 {code}: {msg}. Aborting.")
            return False
        if check_result.retcode != mt5.TRADE_RETCODE_DONE and check_result.retcode != 0:
            log.error(
                f"[{symbol}] order_check() rejected request. Retcode: {check_result.retcode}. "
                f"{check_result.comment}. Aborting."
            )
            return False

        log.info(
            f"[{symbol}] Sending: {trade_proposal['signal']} {trade_proposal['lot_size']} lots @ "
            f"{exec_price:.5f} | SL: {request['sl']:.5f} | TP: {request['tp']:.5f} | "
            f"Risk: {trade_proposal['risk_amount']:.2f} | Filling: {primary_filling} | "
            f"Margin: {margin_check.get('margin', 0):.2f}"
        )

        result = mt5.order_send(request)

        if result is None:
            code, msg = mt5.last_error()
            log.error(f"[{symbol}] order_send() None. MT5 {code}: {msg}.")
            return False

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                f"[{symbol}] ORDER FILLED | Ticket: #{result.order} | "
                f"{trade_proposal['signal']} {trade_proposal['lot_size']} lots | Fill: {result.price:.5f}"
            )
            record_trade_opened()
            time.sleep(2.5)  # State verification delay
            return True

        # --- P0-8: ambiguous outcomes must be reconciled, never blindly retried ---
        if result.retcode in AMBIGUOUS_RETCODES:
            log.warning(f"[{symbol}] Ambiguous retcode {result.retcode} ({result.comment}). Reconciling before any action.")
            already_exists = _reconcile_ambiguous_result(symbol, MAGIC_NUMBER, log)
            if already_exists:
                if result.retcode == mt5.TRADE_RETCODE_DONE_PARTIAL:
                    record_trade_opened()
                    time.sleep(2.5)  # State verification delay
                log.error(f"[{symbol}] Ambiguous fill state after reconciliation. NOT retrying. Manual review recommended.")
                return False
            result_retcode_for_retry_check = result.retcode
        else:
            result_retcode_for_retry_check = result.retcode

        # --- Retryable ---
        if result_retcode_for_retry_check in RETRYABLE_RETCODES or result_retcode_for_retry_check in AMBIGUOUS_RETCODES:
            log.warning(f"[{symbol}] Retcode {result.retcode} ({result.comment}). Retrying in {RETRY_WAIT_S}s...")
            time.sleep(RETRY_WAIT_S)

            if _reconcile_ambiguous_result(symbol, MAGIC_NUMBER, log):
                log.error(f"[{symbol}] Position/order appeared during retry wait. Aborting retry.")
                return False

            retry_filling = _fallback_filling_mode(sym, primary_filling)
            if retry_filling is None:
                log.error(f"[{symbol}] No alternative broker-supported filling mode; not retrying.")
                return False
            built_retry = _build_request(trade_proposal, retry_filling, log)
            if built_retry is None:
                log.error(f"[{symbol}] Retry: cannot build request.")
                return False
            retry_req, retry_price = built_retry

            retry_check = mt5.order_check(retry_req)
            if retry_check is None or (retry_check.retcode != mt5.TRADE_RETCODE_DONE and retry_check.retcode != 0):
                log.error(f"[{symbol}] Retry order_check() rejected request. Aborting retry.")
                return False

            retry_result = mt5.order_send(retry_req)
            if retry_result is None:
                code, msg = mt5.last_error()
                log.error(f"[{symbol}] Retry None. MT5 {code}: {msg}.")
                return False

            if retry_result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    f"[{symbol}] ORDER FILLED (retry) | #{retry_result.order} | "
                    f"{trade_proposal['signal']} {trade_proposal['lot_size']} lots | Fill: {retry_result.price:.5f}"
                )
                record_trade_opened()
                time.sleep(2.5)  # State verification delay
                return True

            if retry_result.retcode in AMBIGUOUS_RETCODES:
                log.error(f"[{symbol}] Retry produced ambiguous result again ({retry_result.retcode}). NOT retrying further. Manual review recommended.")
                return False

            log.error(f"[{symbol}] Retry failed. Retcode: {retry_result.retcode}. {retry_result.comment}.")
            return False

        log.error(f"[{symbol}] FAILED (non-retryable). Retcode: {result.retcode}. {result.comment}.")
        return False

    finally:
        sync_positions(symbol)
        clear_in_flight_risk(symbol, trade_proposal["signal"], trade_proposal["risk_amount"])


# ---------------------------------------------------------------------------
# POSITION MODIFICATION & ADVANCED POSITION MANAGEMENT
# ---------------------------------------------------------------------------

def modify_position_sl(ticket: int, symbol: str, new_sl: float, tp: float = 0.0) -> bool:
    """Sends TRADE_ACTION_SLTP request to MT5 to update position Stop Loss."""
    sym_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not sym_info or not tick:
        return False

    # Check broker minimum stop distance limits
    stop_level = sym_info.trade_stops_level * sym_info.point
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False

    pos_type = positions[0].type
    if pos_type == mt5.POSITION_TYPE_BUY and (tick.bid - new_sl) < stop_level:
        return False
    elif pos_type == mt5.POSITION_TYPE_SELL and (new_sl - tick.ask) < stop_level:
        return False

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol,
        "sl": round(new_sl, sym_info.digits),
        "tp": round(tp, sym_info.digits),
    }

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        logging.getLogger("nightshade").info(f"[{symbol}] Ratchet SL set to {new_sl:.5f} (#Ticket {ticket})")
        return True
    return False


def process_active_position_ratchets(trade_sl_distances: dict) -> None:
    """
    Self-contained orchestrator function called by main.py.
    Queries open trades, evaluates risk steps, and sends updates to MT5.
    """
    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        sl_dist = trade_sl_distances.get(pos.ticket) or trade_sl_distances.get(pos.symbol)
        if not sl_dist:
            continue

        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            continue

        current_price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
        target_sl = calculate_ratchet_sl(
            pos_type=pos.type,
            open_price=pos.price_open,
            current_sl=pos.sl,
            current_price=current_price,
            sl_distance=sl_dist
        )

        if target_sl:
            modify_position_sl(pos.ticket, pos.symbol, target_sl, pos.tp)


def _get_or_init_monitor_state(ticket: int) -> dict:
    """Fetch or initialize active trade state tracker."""
    if ticket not in _POSITION_MONITOR_STATE:
        _POSITION_MONITOR_STATE[ticket] = {
            "peak_pnl": 0.0,
            "pnl_history": []  # Stores (timestamp, net_pnl) tuples
        }
    return _POSITION_MONITOR_STATE[ticket]


def _cleanup_monitor_state(active_tickets: set) -> None:
    """Prunes closed trades from memory."""
    for ticket in list(_POSITION_MONITOR_STATE.keys()):
        if ticket not in active_tickets:
            _POSITION_MONITOR_STATE.pop(ticket, None)


def _close_position_market(pos, reason: str, log: logging.Logger) -> bool:
    """Executes market order to close an open position by ticket."""
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return False

    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": order_type,
        "price": price,
        "deviation": 10,
        "magic": pos.magic,
        "comment": f"Close: {reason[:20]}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"[{pos.symbol}] Closed ticket #{pos.ticket}. Reason: {reason}")
        return True
    
    log.error(f"[{pos.symbol}] Failed to close ticket #{pos.ticket}. Result: {result.comment if result else 'None'}")
    return False


def process_advanced_position_management(trade_sl_distances: dict, log: logging.Logger = None) -> None:
    """Orchestrator called on every 30-second polling cycle."""
    if log is None:
        log = logging.getLogger("nightshade")

    positions = mt5.positions_get()
    if not positions:
        _cleanup_monitor_state(set())
        return

    # Filter positions for system magic number if applicable
    our_positions = [p for p in positions if getattr(p, 'magic', 0) == getattr(risk, 'MAGIC_NUMBER', getattr(p, 'magic', MAGIC_NUMBER))]
    _cleanup_monitor_state({p.ticket for p in our_positions})

    now = time.time()

    for pos in our_positions:
        sym_info = mt5.symbol_info(pos.symbol)
        if not sym_info:
            continue

        # 1. Calculate true NET floating PnL (profit + swap + commission)
        net_pnl = pos.profit + pos.swap + pos.commission
        state = _get_or_init_monitor_state(pos.ticket)

        # 2. Update High-Water Mark continuously
        state["peak_pnl"] = max(state["peak_pnl"], net_pnl)

        # 3. Update rolling 30-second history buffer
        state["pnl_history"].append((now, net_pnl))
        state["pnl_history"] = [entry for entry in state["pnl_history"] if (now - entry[0]) <= 30]

        # 4. Evaluate dynamic exit conditions in strict priority order
        hard_cap = risk.evaluate_hard_giveback_cap(state["peak_pnl"], net_pnl)
        decline = risk.evaluate_decline_to_zero(state["pnl_history"])
        giveback = risk.evaluate_giveback_exit(state["peak_pnl"], net_pnl)

        if hard_cap["exit"] or decline["exit"] or giveback["exit"]:
            exit_reason = hard_cap.get("reason") or decline.get("reason") or giveback.get("reason")
            _close_position_market(pos, exit_reason, log)
            continue

        # 5. Evaluate and apply Server-Side Time-Decay TP adjustments
        elapsed_seconds = now - pos.time
        sl_distance = trade_sl_distances.get(pos.ticket, trade_sl_distances.get(pos.symbol))

        if sl_distance:
            new_tp = risk.calculate_time_decay_tp(
                open_price=pos.price_open,
                pos_type=pos.type,
                sl_distance=sl_distance,
                elapsed_seconds=elapsed_seconds,
                digits=sym_info.digits
            )
            
            # Update server TP if changed by more than 1 pip/point threshold
            if abs(pos.tp - new_tp) >= (sym_info.point * 10):
                modify_request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "sl": pos.sl,
                    "tp": new_tp,
                }
                mt5.order_send(modify_request)
                log.info(f"[{pos.symbol}] Updated Time-Decay TP to {new_tp} (Elapsed: {elapsed_seconds / 60:.1f}m)")