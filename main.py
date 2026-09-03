"""
Nightshade Seed Engine - main.py  v5  (Layer 5)

Changes vs v4:
  - Central polling loop and position management orchestrator integration.
"""

"""
Nightshade Seed Engine - main.py  v6  (Layer 5)

Changes vs v5:
  - Integrated in-flight risk handling and execution updates.
"""

"""
Nightshade Seed Engine - main.py  v7  (Layer 5)

Changes vs v6:
  - Added server-side ratchet Stop Loss orchestration.
"""

"""
Nightshade Seed Engine - main.py  v8  (Layer 5)

Changes vs v7:
  - Integrated time-decay TP updates and peak giveback evaluation loop.
"""

"""
Nightshade Seed Engine - main.py  v9  (Layer 5)

Changes vs v8:
  - Integrated hard profit lock and fee-buffered breakeven ratchet checks.
"""

"""
Nightshade Seed Engine - main.py  v10  (Layer 5)

Changes vs v9:
  - Orchestrator Execution: Continues running the 30-second polling cycle, invoking the updated process_advanced_position_management(MAGIC_NUMBER) routine.
  - State Reconciliation: Maintains startup state reconciliation from MT5 history via risk.reconcile_state_from_history() to ensure active positions resume under the new exit rules after restart.
"""

import MetaTrader5 as mt5
import time
import datetime
import logging
import pandas as pd

import risk
import execution

# --- GLOBAL CONFIGURATION ---
MAGIC_NUMBER = 20260818
WATCHLIST = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
POLL_INTERVAL_SEC = 30
TIMEFRAME = mt5.TIMEFRAME_M15

# Technical Indicator Parameters
BB_PERIOD = 20
BB_STD_MULT = 2.5
ATR_PERIOD = 14
ATR_BASELINE_PERIOD = 50
ATR_REGIME_MULT = 1.2

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(filename)s:%(lineno)d) - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("nightshade_engine.log")
    ]
)
log = logging.getLogger("nightshade")


def initialize_terminal() -> bool:
    """Initializes MetaTrader 5 terminal connection."""
    if not mt5.initialize():
        code, msg = mt5.last_error()
        log.critical(f"MT5 Initialization failed (Code {code}: {msg})")
        return False

    terminal_info = mt5.terminal_info()
    if terminal_info is None:
        log.critical("Could not retrieve MT5 terminal info.")
        return False

    if not terminal_info.trade_allowed:
        log.warning("Automated trading is DISABLED in MT5 terminal settings!")

    account_info = mt5.account_info()
    if account_info is not None:
        log.info(f"Connected to MT5 Account: {account_info.login} | Server: {account_info.server} | Equity: {account_info.equity:.2f} {account_info.currency}")

    return True


def run_engine_loop():
    """Main execution loop running every 30 seconds."""
    log.info("Starting Nightshade Seed Engine continuous polling loop...")

    # Startup State Reconciliation from authoritative MT5 history
    reconciled_state = risk.reconcile_state_from_history(MAGIC_NUMBER)
    if reconciled_state is None:
        log.critical("Startup state reconciliation failed. Failing closed to prevent over-trading.")
        return

    log.info(f"Startup state verified: {risk.get_streak_status()}")

    while True:
        try:
            loop_start = time.time()

            # 1. Active Position Management Orchestration (Three-Tier Exit Architecture)
            execution.process_advanced_position_management(MAGIC_NUMBER)

            # 2. Daily Limits and Circuit Breaker Validation
            daily_state = risk.load_daily_state()
            if daily_state.get(risk.CIRCUIT_BREAKER_ACTIVE_KEY):
                log.info(f"Circuit Breaker ACTIVE. Skipping new signal scans. ({daily_state['consecutive_losses']} consecutive losses)")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            if daily_state.get("trades_today", 0) >= risk.MAX_DAILY_TRADES:
                log.info(f"Daily trade limit reached ({daily_state['trades_today']}/{risk.MAX_DAILY_TRADES}). Skipping signal scans.")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # 3. Watchlist Signal Evaluation
            for symbol in WATCHLIST:
                # Check if position already open for symbol
                is_open = risk.is_position_open(symbol, MAGIC_NUMBER)
                if is_open is None:
                    log.error(f"[{symbol}] Failed to verify position status. Skipping.")
                    continue
                if is_open:
                    continue  # Symbol already has an active trade

                # Fetch candle rates
                rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 100)
                if rates is None or len(rates) < ATR_BASELINE_PERIOD + 1:
                    log.warning(f"[{symbol}] Insufficient rate data fetched from MT5.")
                    continue

                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s")

                # Compute strategy indicators
                df = risk.compute_indicators(
                    df,
                    bb_period=BB_PERIOD,
                    atr_period=ATR_PERIOD,
                    atr_baseline=ATR_BASELINE_PERIOD,
                    bb_std_mult=BB_STD_MULT,
                    atr_regime_mult=ATR_REGIME_MULT
                )

                # Evaluate last COMPLETED candle (iloc[-2]) to prevent repaint issues
                completed_candle = df.iloc[-2]
                candle_time_str = completed_candle["time"].isoformat()

                # Deduplicate: Skip if candle already evaluated
                last_eval = daily_state.get("last_evaluated", {}).get(symbol)
                if last_eval == candle_time_str:
                    continue

                signal_val = completed_candle["signal"]
                if signal_val == 0:
                    # Update evaluated timestamp
                    daily_state.setdefault("last_evaluated", {})[symbol] = candle_time_str
                    risk._save_daily_state(daily_state)
                    continue

                signal_type = "BUY" if signal_val == 1 else "SELL"
                current_price = completed_candle["close"]
                atr_val = completed_candle["atr"]

                # Portfolio and Correlated Exposure Check
                exposure_check = risk.check_portfolio_exposure(symbol, signal_type, MAGIC_NUMBER)
                if not exposure_check["ok"]:
                    log.info(f"[{symbol}] Signal {signal_type} rejected by exposure guard: {exposure_check['reason']}")
                    daily_state.setdefault("last_evaluated", {})[symbol] = candle_time_str
                    risk._save_daily_state(daily_state)
                    continue

                # Calculate swing high/low for dynamic R:R positioning
                swing_high = df["high"].iloc[-12:-2].max()
                swing_low = df["low"].iloc[-12:-2].min()

                # Evaluate Risk Parameters
                risk_eval = risk.evaluate_risk(
                    signal_type=signal_type,
                    current_price=current_price,
                    atr_val=atr_val,
                    risk_pct=1.0,
                    rr_ratio=1.5,
                    symbol=symbol,
                    swing_high=swing_high,
                    swing_low=swing_low
                )

                if risk_eval["is_approved"]:
                    log.info(f"[{symbol}] Entry Signal Approved! Transmitting order to execution...")
                    exec_res = execution.execute_trade(risk_eval, MAGIC_NUMBER)
                    if exec_res["success"]:
                        log.info(f"[{symbol}] Order successfully executed! Ticket: {exec_res['ticket']}")
                    else:
                        log.error(f"[{symbol}] Order execution failed: {exec_res['reason']}")

                # Mark candle evaluated
                daily_state.setdefault("last_evaluated", {})[symbol] = candle_time_str
                risk._save_daily_state(daily_state)

            # Maintain constant polling interval cadence
            elapsed = time.sleep(max(0.0, POLL_INTERVAL_SEC - (time.time() - loop_start)))

        except KeyboardInterrupt:
            log.info("Engine shutdown requested by user.")
            break
        except Exception as e:
            log.error(f"Unexpected exception in main loop: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        if initialize_terminal():
            run_engine_loop()
    finally:
        mt5.shutdown()
        log.info("MetaTrader 5 connection closed. Engine shutdown complete.")
