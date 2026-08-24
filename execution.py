"""
Nightshade Seed Engine - execution.py  v4  (Layer 4)

Changes vs v3:
  - Imports record_trade_opened from risk.py (unchanged behaviour,
    just keeping import consistent with v4 risk module)
  - No other changes — IOC/FOK retry, fresh tick price, symbol-aware
    logging all preserved
"""

import MetaTrader5 as mt5
import time
import logging

from risk import record_trade_opened

MAGIC_NUMBER   = 20260818
MAX_DEVIATION  = 20
RETRY_WAIT_S   = 5

RETRYABLE_RETCODES = {
    mt5.TRADE_RETCODE_REQUOTE,
    mt5.TRADE_RETCODE_CONNECTION,
    mt5.TRADE_RETCODE_PRICE_CHANGED,
    mt5.TRADE_RETCODE_TIMEOUT,
    mt5.TRADE_RETCODE_PRICE_OFF,
    mt5.TRADE_RETCODE_REJECT,
    mt5.TRADE_RETCODE_ERROR,
}


def _build_request(trade_proposal: dict, filling_mode: int) -> dict | None:
    """Builds MT5 order request with a freshly fetched execution price."""
    symbol     = trade_proposal["symbol"]
    signal     = trade_proposal["signal"]
    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None

    return {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       trade_proposal["lot_size"],
        "type":         order_type,
        "price":        tick.ask if signal == "BUY" else tick.bid,
        "sl":           trade_proposal["sl_price"],
        "tp":           trade_proposal["tp_price"],
        "deviation":    MAX_DEVIATION,
        "magic":        MAGIC_NUMBER,
        "comment":      "NSD_SEED_v4",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }


def execute_order(
    trade_proposal: dict,
    log: logging.Logger = None,
) -> bool:
    """
    Sends a validated trade proposal to MT5 as a market order.
    On confirmed fill, calls record_trade_opened() to increment
    the daily trade counter in daily_state.json.

    Win/loss recording happens in main.py's position monitor,
    not here — we only know the trade opened, not how it closed.

    Returns True if filled, False otherwise.
    Does NOT call mt5.initialize() or mt5.shutdown().
    """
    if log is None:
        log = logging.getLogger("nightshade")

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

    # --- First attempt: IOC ---
    request = _build_request(trade_proposal, mt5.ORDER_FILLING_IOC)
    if request is None:
        log.error(f"[{symbol}] Cannot read tick. Aborting.")
        return False

    log.info(
        f"[{symbol}] Sending: {trade_proposal['signal']} "
        f"{trade_proposal['lot_size']} lots @ {request['price']:.5f} | "
        f"SL: {request['sl']:.5f} | TP: {request['tp']:.5f} | "
        f"Risk: {trade_proposal['risk_amount']:.2f}"
    )

    result = mt5.order_send(request)

    if result is None:
        code, msg = mt5.last_error()
        log.error(f"[{symbol}] order_send() None. MT5 {code}: {msg}.")
        return False

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(
            f"[{symbol}] ORDER FILLED | Ticket: #{result.order} | "
            f"{trade_proposal['signal']} {trade_proposal['lot_size']} lots | "
            f"Fill: {result.price:.5f}"
        )
        record_trade_opened()
        return True

    # --- Retryable ---
    if result.retcode in RETRYABLE_RETCODES:
        log.warning(
            f"[{symbol}] Retcode {result.retcode} ({result.comment}). "
            f"Retrying with FOK in {RETRY_WAIT_S}s..."
        )
        time.sleep(RETRY_WAIT_S)

        retry_req = _build_request(trade_proposal, mt5.ORDER_FILLING_FOK)
        if retry_req is None:
            log.error(f"[{symbol}] Retry: cannot read tick.")
            return False

        retry_result = mt5.order_send(retry_req)
        if retry_result is None:
            code, msg = mt5.last_error()
            log.error(f"[{symbol}] Retry None. MT5 {code}: {msg}.")
            return False

        if retry_result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                f"[{symbol}] ORDER FILLED (retry) | #{retry_result.order} | "
                f"{trade_proposal['signal']} {trade_proposal['lot_size']} lots | "
                f"Fill: {retry_result.price:.5f}"
            )
            record_trade_opened()
            return True

        log.error(
            f"[{symbol}] Retry failed. "
            f"Retcode: {retry_result.retcode}. {retry_result.comment}."
        )
        return False

    log.error(
        f"[{symbol}] FAILED (non-retryable). "
        f"Retcode: {result.retcode}. {result.comment}."
    )
    return False