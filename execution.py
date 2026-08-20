"""
Nightshade Seed Engine - execution.py  v3  (Layer 4)
Sends validated trade proposals to MT5 as live market orders.

Changes vs v2:
  - Calls record_trade_opened() from risk.py after every confirmed fill
    so the daily trade counter stays accurate across all four pairs
  - Symbol-aware logging (already present, preserved)
  - IOC then FOK retry logic preserved
  - Fresh tick price at send time preserved
  - No mt5.initialize() / mt5.shutdown()
"""

import MetaTrader5 as mt5
import time
import logging

from risk import record_trade_opened

MAGIC_NUMBER      = 20260818
MAX_DEVIATION     = 20       # max slippage in points
RETRY_WAIT_S      = 5        # seconds before retry on transient failure

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
    """
    Builds the MT5 order request with a freshly fetched execution price.
    Returns None if the tick cannot be read.
    """
    symbol     = trade_proposal["symbol"]
    signal     = trade_proposal["signal"]
    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None

    execution_price = tick.ask if signal == "BUY" else tick.bid

    return {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       trade_proposal["lot_size"],
        "type":         order_type,
        "price":        execution_price,
        "sl":           trade_proposal["sl_price"],
        "tp":           trade_proposal["tp_price"],
        "deviation":    MAX_DEVIATION,
        "magic":        MAGIC_NUMBER,
        "comment":      "NSD_SEED_v3",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }


def execute_order(
    trade_proposal: dict,
    log: logging.Logger = None,
) -> bool:
    """
    Sends a validated trade proposal to MT5 as a market order.
    On success, increments the daily trade counter in daily_state.json.

    Attempts IOC filling first, FOK on retry.
    Returns True if order filled, False otherwise.
    Does NOT call mt5.initialize() / mt5.shutdown().
    """
    if log is None:
        log = logging.getLogger("nightshade")

    if not trade_proposal or not trade_proposal.get("is_approved", False):
        log.error("Execution called with unapproved or missing trade proposal.")
        return False

    symbol = trade_proposal["symbol"]

    sym = mt5.symbol_info(symbol)
    if sym is None:
        log.error(f"[{symbol}] symbol_info() returned None. Aborting.")
        return False
    if not sym.visible:
        log.error(f"[{symbol}] Not visible in Market Watch. Aborting.")
        return False

    # --- First attempt: IOC ---
    request = _build_request(trade_proposal, mt5.ORDER_FILLING_IOC)
    if request is None:
        log.error(f"[{symbol}] Cannot read tick for execution. Aborting.")
        return False

    log.info(
        f"[{symbol}] Sending order: {trade_proposal['signal']} "
        f"{trade_proposal['lot_size']} lots @ {request['price']:.5f} | "
        f"SL: {request['sl']:.5f} | TP: {request['tp']:.5f} | "
        f"Risk: {trade_proposal['risk_amount']:.2f}"
    )

    result = mt5.order_send(request)

    if result is None:
        code, msg = mt5.last_error()
        log.error(
            f"[{symbol}] order_send() returned None. "
            f"MT5 error {code}: {msg}. Aborting."
        )
        return False

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(
            f"[{symbol}] ORDER FILLED | Ticket: #{result.order} | "
            f"{trade_proposal['signal']} {trade_proposal['lot_size']} lots | "
            f"Fill: {result.price:.5f}"
        )
        record_trade_opened()   # increment daily trade counter
        return True

    # --- Retryable error handling ---
    if result.retcode in RETRYABLE_RETCODES:
        log.warning(
            f"[{symbol}] Retcode {result.retcode} ({result.comment}) is retryable. "
            f"Retrying with FOK in {RETRY_WAIT_S}s..."
        )
        time.sleep(RETRY_WAIT_S)

        retry_req = _build_request(trade_proposal, mt5.ORDER_FILLING_FOK)
        if retry_req is None:
            log.error(f"[{symbol}] Retry: Cannot read tick. Aborting.")
            return False

        retry_result = mt5.order_send(retry_req)

        if retry_result is None:
            code, msg = mt5.last_error()
            log.error(
                f"[{symbol}] Retry order_send() returned None. "
                f"MT5 error {code}: {msg}."
            )
            return False

        if retry_result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                f"[{symbol}] ORDER FILLED (retry) | Ticket: #{retry_result.order} | "
                f"{trade_proposal['signal']} {trade_proposal['lot_size']} lots | "
                f"Fill: {retry_result.price:.5f}"
            )
            record_trade_opened()   # increment daily trade counter
            return True

        log.error(
            f"[{symbol}] Retry also failed. "
            f"Retcode: {retry_result.retcode}. Comment: {retry_result.comment}. "
            f"Signal skipped."
        )
        return False

    # --- Non-retryable failure ---
    log.error(
        f"[{symbol}] Order FAILED (non-retryable). "
        f"Retcode: {result.retcode}. Comment: {result.comment}."
    )
    return False
