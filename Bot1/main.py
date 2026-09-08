# main.py
# Nightshade Seed Engine - main.py v12
# Entry scanning and main loop with candidate ranking, M15 entry cadence,
# heartbeat logging, and M15 scan logging.

import time
import logging
import datetime
import pandas as pd
import MetaTrader5 as mt5
from risk import *   # all constants and functions
import risk
import execution

# Ensure log directory exists
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("nightshade")

# ----------------------------------------------------------------------
# MT5 startup / shutdown
# ----------------------------------------------------------------------
def startup_mt5():
    if not mt5.initialize():
        log.critical("MT5 init failed")
        return False
    account = mt5.account_info()
    if account is None:
        log.critical("No account")
        mt5.shutdown()
        return False
    log.info(f"Connected to {account.server}, login {account.login}")
    for sym in SYMBOLS:
        if not mt5.symbol_select(sym, True):
            log.critical(f"Symbol {sym} not available")
            mt5.shutdown()
            return False
    return True

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def get_swing_target(symbol, num_bars=20):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 1, num_bars)
    if rates is None or len(rates) == 0:
        return None, None
    df = pd.DataFrame(rates)
    return float(df["high"].max()), float(df["low"].min())

def fetch_indicators(symbol):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 150)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df = risk.compute_indicators(df, BB_PERIOD, ATR_PERIOD, ATR_BASELINE, BB_STD_MULT, ATR_REGIME_MULT)
    return df

# M15 candle tracker
_last_m15_boundary = None

def is_new_m15_candle():
    global _last_m15_boundary
    now = datetime.datetime.utcnow()
    minutes = now.minute
    boundary_min = (minutes // 15) * 15
    boundary = now.replace(minute=boundary_min, second=0, microsecond=0)
    if _last_m15_boundary != boundary:
        _last_m15_boundary = boundary
        return True
    return False

# ----------------------------------------------------------------------
# Entry scan – collects candidates, ranks by |Z|, executes with dynamic risk
# ----------------------------------------------------------------------
def evaluate_and_execute_signals():
    log.info("--- M15 Candle Scan ---")
    candidates = []
    for symbol in SYMBOLS:
        if risk.is_position_open(symbol, MAGIC_NUMBER):
            continue
        df = fetch_indicators(symbol)
        if df is None or len(df) < MIN_HISTORY_CANDLES:
            log.debug(f"[{symbol}] Insufficient data")
            continue
        latest = df.iloc[-1]
        signal = latest["signal"]
        if signal == 0:
            continue
        signal_str = "BUY" if signal == 1 else "SELL"
        price = latest["close"]
        atr = latest["atr"]
        z_score = abs(latest["z_score"])

        high, low = get_swing_target(symbol)

        # Check exposure; this also returns dynamic risk percentage
        exp = risk.check_portfolio_exposure(symbol, signal_str, MAGIC_NUMBER)
        if not exp["ok"]:
            log.info(f"[{symbol}] Exposure blocked: {exp['reason']}")
            continue

        # Store candidate with its dynamic risk
        candidates.append({
            "symbol": symbol,
            "signal": signal_str,
            "price": price,
            "atr": atr,
            "z_score": z_score,
            "high": high,
            "low": low,
            "dynamic_risk": exp.get("dynamic_risk_pct", RISK_PCT)
        })

    if not candidates:
        log.info("No candidates found this M15 cycle.")
        return

    # Rank by |Z| (strongest first)
    candidates.sort(key=lambda x: x["z_score"], reverse=True)
    log.info(f"Found {len(candidates)} candidates. Top: {candidates[0]['symbol']} |Z|={candidates[0]['z_score']:.2f}")

    for cand in candidates:
        # Re-check exposure before each execution (portfolio may have changed)
        exp = risk.check_portfolio_exposure(cand["symbol"], cand["signal"], MAGIC_NUMBER)
        if not exp["ok"]:
            log.info(f"[{cand['symbol']}] Exposure changed: {exp['reason']}")
            continue
        risk_pct = exp.get("dynamic_risk_pct", RISK_PCT)
        proposal = risk.evaluate_risk(
            signal_type=cand["signal"],
            current_price=cand["price"],
            atr_val=cand["atr"],
            risk_pct=risk_pct,
            symbol=cand["symbol"],
            swing_high=cand["high"],
            swing_low=cand["low"],
            tp_price_override=None
        )
        if proposal["is_approved"]:
            log.info(f"[{cand['symbol']}] Trade approved with {risk_pct:.2f}% risk. Executing...")
            res = execution.execute_trade(proposal, MAGIC_NUMBER)
            if not res["success"]:
                log.error(f"[{cand['symbol']}] Execution failed: {res['reason']}")
            # After executing, next candidate will re-check exposure

# ----------------------------------------------------------------------
# Main loop – heartbeat every 30s, entry scan on new M15
# ----------------------------------------------------------------------
def main():
    log.info("Nightshade starting...")
    if not startup_mt5():
        return
    risk.reconcile_state_from_history(MAGIC_NUMBER)

    # Initialize M15 boundary tracker
    global _last_m15_boundary
    _last_m15_boundary = None

    # Counter for heartbeat
    cycle_count = 0

    try:
        while True:
            cycle_start = time.time()
            cycle_count += 1

            # 1. Reconcile state (preserves trades_today)
            risk.reconcile_state_from_history(MAGIC_NUMBER)

            # 2. Position management (exits, ratchet, time-decay)
            execution.process_advanced_position_management(MAGIC_NUMBER)

            # 3. Entry scan only on new M15 candle
            if is_new_m15_candle():
                evaluate_and_execute_signals()

            # 4. Heartbeat log every 10 cycles (≈5 minutes) to avoid log spam,
            #    but we can log every cycle if desired. Let's log every cycle.
            #    We'll log status and position count.
            state = risk.load_daily_state()
            positions = mt5.positions_get()
            open_count = len([p for p in positions if p.magic == MAGIC_NUMBER]) if positions else 0
            log.info(f"Heartbeat #{cycle_count} | Trades: {state.get('trades_today',0)}/{MAX_DAILY_TRADES} | "
                     f"Streak: {state.get('consecutive_losses',0)}/{CONSECUTIVE_LOSS_LIMIT} | "
                     f"Open positions: {open_count} | Next M15 scan at: {_last_m15_boundary}")

            elapsed = time.time() - cycle_start
            sleep_time = max(1.0, POLL_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        mt5.shutdown()
        log.info("MT5 closed")

if __name__ == "__main__":
    main()