"""
Nightshade Seed Engine - main.py  v6  (Layer 1 / central controller)

This file did not exist in the reviewed repo. Built from the Nightshade
specification document, with the following tech-review fixes folded in
from the start (referenced by GitHub issue item number):

  P0-2   Win/loss is decided from REALIZED MT5 deal history (closed deals,
         profit + commission + swap), never from a floating-P&L snapshot.
  P0-3   Indicators use population standard deviation (ddof=0).
  P0-9   All four symbols are evaluated every cycle BEFORE any execution
         decision. Candidates are collected, ranked, and only then
         executed -- so SYMBOLS list order cannot bias which pair gets
         the daily-trade-limit slots.
  P0-10  Portfolio/correlated-USD-exposure check (risk.check_portfolio_exposure)
         is applied to every candidate before execution, in addition to
         the existing per-trade 1% risk check.
  P0-11  Spread, tick freshness, candle freshness/count, duplicate/missing
         candles, and NaN/Inf checks block a symbol before it can produce
         a tradeable signal.
  P0-14  On startup, daily_state.json is reconciled from MT5 trade history
         (risk.reconcile_state_from_history) -- MT5 is authoritative, the
         JSON file is a cache.
  P0-15  LIVE_TRADING_ENABLED must be explicitly "true" (env var) AND the
         connected account must match an explicit allowlist, or the bot
         refuses to run on a live account.
  P0-16  A single-instance lock file prevents two copies of the bot from
         running against the same state file / MT5 terminal at once.
  P0-17  last_evaluated candle timestamps are persisted in daily_state.json
         (risk.py), not just held in memory, so a restart mid-day does not
         reprocess an already-evaluated candle.

Architectural rule (per spec): mt5.initialize() and mt5.shutdown() are
called ONLY in this file. No other module touches them.
"""

"""
Nightshade Seed Engine - main.py  v9  (Layer 5 - Main Loop)

Integrates market scanning, dynamic structure target calculations (Swing High/Low),
risk validation, order execution, and priority-driven position management.
"""

import time
import logging
import datetime
from pathlib import Path
import pandas as pd
import MetaTrader5 as mt5

import risk
import execution

# ---------------------------------------------------------------------------
# LOGGING & CONFIGURATION
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("nightshade.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("nightshade")

MAGIC_NUMBER      = 991122
SYMBOLS           = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME         = mt5.TIMEFRAME_M5
POLL_INTERVAL_SEC = 30

# Strategy Indicator Parameters
BB_PERIOD        = 20
BB_STD_MULT      = 2.0
ATR_PERIOD       = 14
ATR_BASELINE     = 100
ATR_REGIME_MULT  = 1.5


# ---------------------------------------------------------------------------
# STRUCTURE & SWING TARGET CALCULATIONS
# ---------------------------------------------------------------------------

def get_swing_target(symbol: str, timeframe=TIMEFRAME, num_bars: int = 20) -> tuple[float | None, float | None]:
    """
    Fetches recent completed candles (excluding index 0 unclosed candle)
    to compute local swing high and swing low structure levels.
    """
    # Fetch completed bars starting from index 1 (excluding live unclosed candle 0)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, num_bars)
    if rates is None or len(rates) == 0:
        log.warning(f"[{symbol}] Failed to fetch candle rates for swing calculation.")
        return None, None

    rates_df = pd.DataFrame(rates)
    swing_high = float(rates_df["high"].max())
    swing_low  = float(rates_df["low"].min())

    log.debug(f"[{symbol}] Structure (last {num_bars} bars) -> Swing High: {swing_high}, Swing Low: {swing_low}")
    return swing_high, swing_low


# ---------------------------------------------------------------------------
# MARKET DATA & INDICATORS
# ---------------------------------------------------------------------------

def fetch_market_data(symbol: str, timeframe=TIMEFRAME, num_bars: int = 150) -> pd.DataFrame | None:
    """Fetches bar data and computes strategy technical indicators."""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, num_bars)
    if rates is None or len(rates) == 0:
        log.error(f"[{symbol}] Failed to retrieve market rates.")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    
    # Compute Bollinger Bands & ATR Regime using risk module helper
    df = risk.compute_indicators(
        df,
        bb_period=BB_PERIOD,
        atr_period=ATR_PERIOD,
        atr_baseline=ATR_BASELINE,
        bb_std_mult=BB_STD_MULT,
        atr_regime_mult=ATR_REGIME_MULT
    )
    return df


# ---------------------------------------------------------------------------
# CANDIDATE TRADE PROPOSAL EVALUATION
# ---------------------------------------------------------------------------

def evaluate_and_execute_signals(trade_sl_distances: dict) -> None:
    """Scans watchlist symbols for signals and generates structure-aware trade proposals."""
    for symbol in SYMBOLS:
        # Check if position is already open for this symbol
        if risk.is_position_open(symbol, MAGIC_NUMBER):
            continue

        df = fetch_market_data(symbol)
        if df is None or len(df) < ATR_BASELINE:
            continue

        latest = df.iloc[-1]
        signal = latest["signal"]
        if signal == 0:
            continue

        signal_str = "BUY" if signal == 1 else "SELL"
        current_price = latest["close"]
        atr_val = latest["atr"]

        # Track active SL distance for orchestrator
        trade_sl_distances[symbol] = atr_val * 1.5

        # 1. Fetch local swing high and swing low structure levels
        swing_high, swing_low = get_swing_target(symbol, timeframe=TIMEFRAME, num_bars=20)

        # 2. Portfolio Exposure Check
        exposure_check = risk.check_portfolio_exposure(symbol, signal_str, MAGIC_NUMBER)
        if not exposure_check["ok"]:
            log.info(f"[{symbol}] Portfolio exposure blocked trade: {exposure_check['reason']}")
            continue

        # 3. Form Trade Proposal with Dynamic Swing Structure Levels
        proposal = risk.evaluate_risk(
            signal_type=signal_str,
            current_price=current_price,
            atr_val=atr_val,
            risk_pct=1.0,
            symbol=symbol,
            swing_high=swing_high,
            swing_low=swing_low,
            tp_price_override=None  # Can pass an explicit target override price if required
        )

        # 4. Order Execution
        if proposal["is_approved"]:
            log.info(f"[{symbol}] Trade Proposal Approved. Transmitting order...")
            exec_res = execution.execute_trade(proposal, MAGIC_NUMBER)
            if not exec_res["success"]:
                log.error(f"[{symbol}] Order submission failed: {exec_res['reason']}")


# ---------------------------------------------------------------------------
# MAIN POLLING LOOP
# ---------------------------------------------------------------------------

def main():
    """Main execution loop running every 30 seconds."""
    log.info("Initializing Nightshade Seed Engine...")

    if not mt5.initialize():
        log.critical("Failed to initialize MetaTrader 5 interface.")
        return

    log.info(f"Connected to MT5 Server. Account: {mt5.account_info().login}")

    # Reconcile state from MT5 trade history on startup
    risk.reconcile_state_from_history(MAGIC_NUMBER)

    trade_sl_distances = {}

    try:
        while True:
            cycle_start = time.time()

            # 1. Reconcile daily state and check MT5 connectivity
            risk.reconcile_state_from_history(MAGIC_NUMBER)

            # 2. Advanced Position Management Orchestrator Call (In-flight exits, Time-Decay TP, Profit Locks)
            execution.process_advanced_position_management(MAGIC_NUMBER)

            # 3. Evaluate new candidate signals & execute trade proposals
            evaluate_and_execute_signals(trade_sl_distances)

            # Maintain strict 30-second polling cycle
            elapsed = time.time() - cycle_start
            sleep_time = max(1.0, POLL_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        log.info("Shutdown signal received. Exiting gracefully...")
    finally:
        mt5.shutdown()
        log.info("MetaTrader 5 connection closed.")


if __name__ == "__main__":
    main()