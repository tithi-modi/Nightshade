"""
Nightshade Seed Engine - main.py  v3
Multi-pair controller: EURUSD, GBPUSD, USDJPY, AUDUSD

Changes vs v2:
  - SYMBOLS list replaces single SYMBOL — all four pairs monitored each cycle
  - RISK_PCT lowered to 1.0% per trade
  - MAX_DAILY_TRADES = 3 enforced in Layer 3 (risk.py); main.py checks the
    gate before even calling evaluate_risk so no wasted computation
  - Per-symbol last_evaluated_candle_time dict replaces single scalar
  - Position monitor loops over all four symbols
  - ATR regime filter preserved exactly — only regime_ok candles trade
  - Single MT5 init/shutdown lifecycle preserved
  - All logging to rotating file preserved
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import logging
import datetime
import os
from logging.handlers import RotatingFileHandler

from risk import (
    evaluate_risk,
    load_daily_state,
    CIRCUIT_BREAKER_ACTIVE_KEY,
    MAX_DAILY_TRADES,
)
from execution import execute_order

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

SYMBOLS          = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME        = mt5.TIMEFRAME_M15
CANDLE_SECONDS   = 900          # 15 minutes
WAKEUP_BUFFER    = 2            # seconds after candle close before reading
POSITION_CHECK_S = 30           # position monitor interval between candles

RISK_PCT         = 1.0          # % of equity risked per trade (1% per pair)
RR_RATIO         = 1.5          # reward-to-risk ratio for static TP
BB_PERIOD        = 20           # Bollinger Band / Z-score lookback periods
BB_STD_MULT      = 2.5          # Z-score entry threshold (±2.5 std dev)
ATR_PERIOD       = 14           # ATR lookback for SL sizing
ATR_BASELINE     = 50           # ATR baseline lookback for regime filter
ATR_REGIME_MULT  = 1.2          # ATR must be < baseline × this to allow trade
CANDLES_TO_FETCH = 120          # must be > ATR_BASELINE + ATR_PERIOD

LOG_DIR          = "logs"
LOG_FILE         = os.path.join(LOG_DIR, "nightshade.log")
MAGIC_NUMBER     = 20260818     # identifies this bot's orders in MT5 history

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------

os.makedirs(LOG_DIR, exist_ok=True)
logging.Formatter.converter = time.gmtime  # all timestamps in UTC

formatter = logging.Formatter(
    fmt="%(asctime)s UTC | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=30, encoding="utf-8"
)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

log = logging.getLogger("nightshade")
log.setLevel(logging.INFO)
log.addHandler(file_handler)
log.addHandler(console_handler)

# ---------------------------------------------------------------------------
# STATE
# Per-symbol dict so each pair tracks its own last-evaluated candle time.
# Prevents the same candle being evaluated twice for any individual symbol.
# ---------------------------------------------------------------------------

last_evaluated: dict[str, object] = {sym: None for sym in SYMBOLS}

# ---------------------------------------------------------------------------
# MT5 CONNECTION MANAGEMENT
# ---------------------------------------------------------------------------

def startup_mt5() -> bool:
    """
    Initializes MT5 exactly once at program startup.
    Verifies all four symbols are available before returning True.
    """
    if not mt5.initialize():
        code, msg = mt5.last_error()
        log.critical(
            f"MT5 initialization failed. Code: {code}. Message: {msg}. "
            f"Is MT5 terminal open and logged in?"
        )
        return False

    terminal = mt5.terminal_info()
    account  = mt5.account_info()

    if terminal is None or account is None:
        log.critical("MT5 connected but terminal/account info unavailable.")
        mt5.shutdown()
        return False

    if not terminal.trade_allowed:
        log.critical(
            "MT5 terminal has trading disabled. "
            "Enable Expert Advisors: MT5 toolbar AutoTrading button + "
            "Tools > Options > Expert Advisors > Allow algorithmic trading."
        )
        mt5.shutdown()
        return False

    log.info(
        f"MT5 connected. Build: {terminal.build}. "
        f"Account: {account.login} | {account.company} | "
        f"Balance: {account.balance:.2f} {account.currency} | "
        f"Equity: {account.equity:.2f} | Leverage: 1:{account.leverage}"
    )

    # Verify every symbol is available
    for sym in SYMBOLS:
        info = mt5.symbol_info(sym)
        if info is None:
            log.critical(
                f"{sym} not found on this broker. "
                f"Check the exact symbol name — some brokers use suffixes."
            )
            mt5.shutdown()
            return False
        if not info.visible:
            if not mt5.symbol_select(sym, True):
                log.critical(f"Cannot add {sym} to Market Watch.")
                mt5.shutdown()
                return False
            log.info(f"{sym} added to Market Watch.")
        log.info(
            f"{sym} ready. Spread: {info.spread} pts. "
            f"Min lot: {info.volume_min}. Step: {info.volume_step}."
        )

    return True


def check_connection() -> bool:
    """
    Lightweight connection check before every cycle.
    Attempts one reconnect if terminal reports disconnected.
    """
    info = mt5.terminal_info()
    if info is None:
        log.warning("MT5 terminal_info() returned None. Attempting reconnect...")
        mt5.shutdown()
        time.sleep(5)
        return startup_mt5()
    if not info.connected:
        log.warning("MT5 not connected to broker. Waiting...")
        return False
    if not info.trade_allowed:
        log.warning("Trading disabled in MT5 terminal.")
        return False
    return True

# ---------------------------------------------------------------------------
# CANDLE SLEEP TIMER
# ---------------------------------------------------------------------------

def seconds_until_next_candle() -> float:
    """
    Returns seconds until the next 15-minute candle closes + WAKEUP_BUFFER.
    The engine evaluates all four symbols on the same candle schedule because
    all four pairs use the same 15-minute timeframe.
    """
    now = datetime.datetime.utcnow()
    secs_past_hour = now.minute * 60 + now.second
    for boundary in [0, 900, 1800, 2700, 3600]:
        target = boundary + WAKEUP_BUFFER
        if target > secs_past_hour:
            return float(target - secs_past_hour)
    return float(3600 - secs_past_hour + WAKEUP_BUFFER)

# ---------------------------------------------------------------------------
# POSITION MONITOR — dynamic take-profit for all four symbols
# ---------------------------------------------------------------------------

def monitor_all_positions(sma_values: dict[str, float]) -> None:
    """
    Checks every open position across all four symbols.
    Closes a position if price has returned to the 20-period SMA
    (the mean-reversion target).

    sma_values: dict mapping symbol -> current SMA from latest candle data.
    """
    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        sym = pos.symbol
        if sym not in SYMBOLS:
            continue
        if pos.magic != MAGIC_NUMBER:
            continue

        sma = sma_values.get(sym, 0.0)
        if sma <= 0:
            continue

        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            continue

        close_condition = False
        reason = ""

        if pos.type == mt5.ORDER_TYPE_BUY and tick.bid >= sma:
            close_condition = True
            reason = f"BUY returned to SMA {sma:.5f}. Bid: {tick.bid:.5f}"

        elif pos.type == mt5.ORDER_TYPE_SELL and tick.ask <= sma:
            close_condition = True
            reason = f"SELL returned to SMA {sma:.5f}. Ask: {tick.ask:.5f}"

        if close_condition:
            close_req = {
                "action":   mt5.TRADE_ACTION_DEAL,
                "symbol":   sym,
                "volume":   pos.volume,
                "type":     mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY
                            else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price":    tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask,
                "deviation": 20,
                "magic":    MAGIC_NUMBER,
                "comment":  "NSD_DYN_TP",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(close_req)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    f"DYNAMIC TP | {sym} Ticket #{pos.ticket} closed. "
                    f"{reason}. P&L: {pos.profit:.2f}."
                )
            else:
                rc = result.retcode if result else "None"
                log.error(
                    f"Dynamic TP FAILED | {sym} Ticket #{pos.ticket}. "
                    f"Retcode: {rc}."
                )

# ---------------------------------------------------------------------------
# OPEN POSITION GATE — per symbol
# ---------------------------------------------------------------------------

def has_open_position(symbol: str) -> bool:
    """Returns True if this bot has an open position for the given symbol."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return False
    return any(p.magic == MAGIC_NUMBER for p in positions)

# ---------------------------------------------------------------------------
# STRATEGY CALCULATIONS
# ---------------------------------------------------------------------------

def compute_indicators(rates, symbol: str) -> pd.DataFrame:
    """
    Computes all indicators on the full candle dataset.
    Preserves ATR regime filter exactly — only regime_ok candles generate signals.

    ATR regime: current ATR(14) must be below ATR_BASELINE(50) * ATR_REGIME_MULT(1.2).
    This blocks trading during news spikes and abnormal volatility periods.
    """
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # Z-Score / Bollinger Band
    df["sma"]     = df["close"].rolling(BB_PERIOD).mean()
    df["std"]     = df["close"].rolling(BB_PERIOD).std()
    df["z_score"] = (df["close"] - df["sma"]) / df["std"]

    # ATR
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    df["tr"]           = np.maximum(hl, np.maximum(hc, lc))
    df["atr"]          = df["tr"].rolling(ATR_PERIOD).mean()
    df["atr_baseline"] = df["atr"].rolling(ATR_BASELINE).mean()

    # Regime filter — PRESERVED: no trade unless ATR is within normal range
    df["regime_ok"] = df["atr"] < (df["atr_baseline"] * ATR_REGIME_MULT)

    # Signals — only fire when regime is OK and Z-score crosses threshold
    df["signal"] = 0
    df.loc[df["regime_ok"] & (df["z_score"] < -BB_STD_MULT), "signal"] =  1  # BUY
    df.loc[df["regime_ok"] & (df["z_score"] >  BB_STD_MULT), "signal"] = -1  # SELL

    return df

# ---------------------------------------------------------------------------
# SINGLE SYMBOL CYCLE
# ---------------------------------------------------------------------------

def evaluate_symbol(symbol: str) -> float:
    """
    Runs one full evaluation cycle for a single symbol.
    Returns the current SMA value for use by the position monitor.
    Returns 0.0 on any data failure.
    """
    global last_evaluated

    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, CANDLES_TO_FETCH)
    sym_info = mt5.symbol_info(symbol)

    if rates is None or len(rates) == 0 or sym_info is None:
        log.error(f"{symbol}: Failed to fetch candle data or symbol info.")
        return 0.0

    df        = compute_indicators(rates, symbol)
    completed = df.iloc[-2]   # iloc[-1] is the still-forming candle
    current_sma = float(completed["sma"]) if not pd.isna(completed["sma"]) else 0.0

    # --- Duplicate candle guard (per symbol) ---
    candle_time = completed["time"]
    if last_evaluated[symbol] == candle_time:
        return current_sma  # already processed this candle for this symbol

    last_evaluated[symbol] = candle_time

    # --- Read signal ---
    sig        = completed["signal"]
    signal_str = "BUY" if sig == 1 else ("SELL" if sig == -1 else "HOLD")
    z          = completed["z_score"]
    atr        = completed["atr"]
    regime     = completed["regime_ok"]

    log.info(
        f"{symbol} | Candle {candle_time} | "
        f"Close: {completed['close']:.5f} | Z: {z:.2f} | "
        f"ATR: {atr:.5f} | Regime OK: {regime} | Signal: {signal_str}"
    )

    if signal_str == "HOLD":
        return current_sma

    # --- Open position gate ---
    if has_open_position(symbol):
        log.info(f"{symbol}: Signal {signal_str} skipped — position already open.")
        return current_sma

    # --- Daily trade count gate (checked here before calling risk engine) ---
    state = load_daily_state()
    trades_today = state.get("trades_today", 0)
    if trades_today >= MAX_DAILY_TRADES:
        log.warning(
            f"{symbol}: Signal {signal_str} blocked — daily trade limit reached "
            f"({trades_today}/{MAX_DAILY_TRADES}). No more trades today."
        )
        return current_sma

    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY):
        log.warning(
            f"{symbol}: Signal {signal_str} blocked — circuit breaker active."
        )
        return current_sma

    # --- Fresh tick for entry price ---
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"{symbol}: Cannot read current tick. Skipping.")
        return current_sma
    current_price = tick.ask if signal_str == "BUY" else tick.bid

    # --- Risk gate ---
    log.info(f"{symbol}: Passing to Risk Engine...")
    trade_proposal = evaluate_risk(
        signal_type   = signal_str,
        current_price = current_price,
        atr_val       = float(atr),
        risk_pct      = RISK_PCT,
        rr_ratio      = RR_RATIO,
        symbol        = symbol,
    )

    if not trade_proposal.get("is_approved"):
        reason = trade_proposal.get("reject_reason", "Unknown")
        log.info(f"{symbol}: Trade REJECTED. Reason: {reason}")
        return current_sma

    # --- Execution ---
    log.info(f"{symbol}: Trade APPROVED. Sending to Execution Engine...")
    execute_order(trade_proposal, log)

    return current_sma

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def run_all_symbols() -> dict[str, float]:
    """
    Runs one evaluation cycle across all four symbols.
    Returns a dict of symbol -> current SMA for the position monitor.
    """
    sma_values: dict[str, float] = {}

    if not check_connection():
        return sma_values

    for symbol in SYMBOLS:
        try:
            sma_values[symbol] = evaluate_symbol(symbol)
        except Exception as e:
            log.error(f"{symbol}: Unhandled exception in evaluate_symbol: {e}")
            sma_values[symbol] = 0.0

    return sma_values


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("NIGHTSHADE SEED ENGINE STARTING — MULTI-PAIR v3")
    log.info(f"Pairs:  {', '.join(SYMBOLS)}")
    log.info(f"Risk:   {RISK_PCT}% per trade | RR: {RR_RATIO} | "
             f"Max trades/day: {MAX_DAILY_TRADES}")
    log.info("=" * 60)

    if not startup_mt5():
        log.critical("Cannot start. Fix MT5 connection and restart.")
        raise SystemExit(1)

    sma_values: dict[str, float] = {sym: 0.0 for sym in SYMBOLS}

    try:
        while True:
            # Evaluate all four symbols on this candle
            sma_values = run_all_symbols()

            # Sleep toward next candle, running position monitor every 30s
            sleep_total = seconds_until_next_candle()
            log.info(f"Next candle read in {sleep_total:.1f}s.")

            elapsed = 0.0
            while elapsed < sleep_total:
                chunk = min(POSITION_CHECK_S, sleep_total - elapsed)
                time.sleep(chunk)
                elapsed += chunk

                if check_connection():
                    monitor_all_positions(sma_values)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt. Shutting down cleanly.")
    finally:
        mt5.shutdown()
        log.info("MT5 connection closed. Engine stopped.")
