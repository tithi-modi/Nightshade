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

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import datetime
import time
import os
import sys
import atexit
import logging
import logging.handlers
from pathlib import Path

import risk
import execution

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

SYMBOLS       = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAME     = mt5.TIMEFRAME_M15
FETCH_COUNT   = 120
MAGIC_NUMBER  = 20260818

BB_PERIOD         = 20
BB_STD_MULT       = 2.5
ATR_PERIOD        = 14
ATR_BASELINE      = 50
ATR_REGIME_MULT   = 1.2
RISK_PCT          = 1.0
RR_RATIO          = 1.5

CANDLE_BUFFER_S      = 2      # wake 2s after the candle boundary
POSITION_POLL_S      = 30     # position monitor cadence during sleep
MAX_SPREAD_PIPS      = 5.0
MAX_CANDLE_AGE_S      = 90    # completed candle must be this fresh (P1-11)
MIN_HISTORY_CANDLES   = 64    # 50 (ATR baseline) + 14 (ATR) minimum

BASE_DIR       = Path(__file__).resolve().parent
LOG_DIR        = BASE_DIR / "logs"
LOCK_FILE      = LOG_DIR / "nightshade.lock"

# --- P0-15: live trading is opt-in, not opt-out ---
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
# Comma-separated list of MT5 account logins allowed to run LIVE, e.g. "12345678".
LIVE_ACCOUNT_ALLOWLIST = {
    a.strip() for a in os.environ.get("LIVE_ACCOUNT_ALLOWLIST", "").split(",") if a.strip()
}
LIVE_BROKER_ALLOWLIST = {a.strip() for a in os.environ.get("LIVE_BROKER_ALLOWLIST", "").split(",") if a.strip()}
LIVE_SERVER_ALLOWLIST = {a.strip() for a in os.environ.get("LIVE_SERVER_ALLOWLIST", "").split(",") if a.strip()}


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nightshade")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)sZ [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt.converter = time.gmtime  # UTC timestamps

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "nightshade.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=30,
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# P0-16: SINGLE-INSTANCE LOCK
# ---------------------------------------------------------------------------

def _pid_is_running(pid: int) -> bool:
    if sys.platform.startswith("win"):
        try:
            import subprocess
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.DEVNULL
            ).decode(errors="ignore")
            return str(pid) in out
        except Exception:
            return True  # can't confirm -> assume running (fail closed)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except Exception:
            return True


def acquire_single_instance_lock() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
        except (ValueError, OSError):
            old_pid = None

        if old_pid is not None and _pid_is_running(old_pid):
            log.critical(
                f"Another Nightshade instance appears to be running (PID {old_pid}). "
                f"Refusing to start a second instance against the same state file. Exiting."
            )
            raise SystemExit(1)
        else:
            log.warning(f"Stale lock file found (PID {old_pid} not running). Removing and continuing.")
            os.remove(LOCK_FILE)

    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        log.critical("Lock file was created by another process between check and acquire. Exiting.")
        raise SystemExit(1)

    atexit.register(_release_lock)


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r") as f:
                pid = f.read().strip()
            if pid == str(os.getpid()):
                os.remove(LOCK_FILE)
    except Exception as e:
        log.warning(f"Could not remove lock file cleanly: {e}")


# ---------------------------------------------------------------------------
# MT5 STARTUP / CONNECTION  (only file that calls initialize/shutdown)
# ---------------------------------------------------------------------------

def startup_mt5() -> bool:
    if not mt5.initialize():
        code, msg = mt5.last_error()
        log.critical(f"mt5.initialize() failed. MT5 {code}: {msg}. Exiting.")
        return False

    terminal = mt5.terminal_info()
    account  = mt5.account_info()

    if terminal is None or account is None:
        log.critical("Cannot read terminal_info()/account_info() after initialize(). Exiting.")
        return False
    if not terminal.connected:
        log.critical("Terminal reports not connected. Exiting.")
        return False
    if not terminal.trade_allowed:
        log.critical("AutoTrading not allowed in terminal. Enable it and restart. Exiting.")
        return False

    # --- P0-15: live trading protection ---
    is_live = account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO
    if is_live:
        if not LIVE_TRADING_ENABLED:
            log.critical(
                f"Connected account {account.login} ({account.server}) is LIVE, but "
                f"LIVE_TRADING_ENABLED is not 'true'. Refusing to run on live capital. Exiting."
            )
            return False
        if str(account.login) not in LIVE_ACCOUNT_ALLOWLIST:
            log.critical(
                f"Connected LIVE account {account.login} is not in LIVE_ACCOUNT_ALLOWLIST. "
                f"Refusing to run on an unexpected live account. Exiting."
            )
            return False
        if not LIVE_BROKER_ALLOWLIST or account.company not in LIVE_BROKER_ALLOWLIST:
            log.critical("Live broker is not explicitly allowlisted. Refusing to run.")
            return False
        if not LIVE_SERVER_ALLOWLIST or account.server not in LIVE_SERVER_ALLOWLIST:
            log.critical("Live server is not explicitly allowlisted. Refusing to run.")
            return False
        log.warning(
            f"LIVE TRADING ENABLED for allowlisted account {account.login} on {account.server}. "
            f"Real capital is at risk."
        )
    else:
        log.info(f"Connected to DEMO account {account.login} on {account.server}.")

    for sym_name in SYMBOLS:
        sym = mt5.symbol_info(sym_name)
        if sym is None:
            log.critical(f"Symbol {sym_name} not available on this broker. Exiting.")
            return False
        if not sym.visible and not mt5.symbol_select(sym_name, True):
            log.critical(f"Could not add {sym_name} to Market Watch. Exiting.")
            return False

    log.info("MT5 startup checks passed. Terminal connected, AutoTrading allowed, all symbols available.")
    return True


def check_connection() -> bool:
    terminal = mt5.terminal_info()
    if terminal is not None and terminal.connected and terminal.trade_allowed:
        return True

    log.warning("Connection check failed (not connected or AutoTrading disabled). Attempting reconnect...")
    mt5.shutdown()
    time.sleep(2)
    if startup_mt5():
        log.info("Reconnect successful.")
        return True

    log.error("Reconnect failed. Skipping this cycle.")
    return False


# ---------------------------------------------------------------------------
# CANDLE SLEEP TIMER
# ---------------------------------------------------------------------------

def seconds_until_next_candle() -> float:
    now = datetime.datetime.now(datetime.timezone.utc)
    minute_block = (now.minute // 15 + 1) * 15
    next_boundary = now.replace(second=0, microsecond=0)
    if minute_block == 60:
        next_boundary = (next_boundary + datetime.timedelta(hours=1)).replace(minute=0)
    else:
        next_boundary = next_boundary.replace(minute=minute_block)
    delta = (next_boundary - now).total_seconds() + CANDLE_BUFFER_S
    return max(delta, 0.0)


# ---------------------------------------------------------------------------
# DATA QUALITY  (P1-11)
# ---------------------------------------------------------------------------

def data_quality_check(sym_name: str, df: pd.DataFrame, sym) -> str | None:
    """Returns None if data quality is acceptable, else a reason string."""
    if df is None or len(df) < MIN_HISTORY_CANDLES:
        return f"Insufficient history: {0 if df is None else len(df)} < {MIN_HISTORY_CANDLES} candles."

    if len(df) < FETCH_COUNT:
        log.warning(f"[{sym_name}] Only {len(df)}/{FETCH_COUNT} candles returned (proceeding, above minimum).")

    # Sort chronologically to inspect candle deltas
    df_sorted = df.sort_values("time").reset_index(drop=True)
    diffs = df_sorted["time"].diff().dropna()
    expected_interval = pd.Timedelta(minutes=15)

    # 1. Duplicate timestamp check
    if (diffs == pd.Timedelta(0)).any():
        return "Duplicate candle timestamps detected."

    # 2. Gap analysis (> 1.5x expected interval)
    gap_indices = diffs[diffs > (expected_interval * 1.5)].index

    for idx in gap_indices:
        gap_start = df_sorted.loc[idx - 1, "time"]
        gap_end = df_sorted.loc[idx, "time"]
        gap_duration = gap_end - gap_start

        # Validate whether the gap represents a standard weekend closure:
        # - Starts on Friday (4) or Saturday (5)
        # - Ends on Sunday (6) or Monday (0)
        # - Total duration is between 40 and 72 hours
        is_weekend_start = gap_start.dayofweek in (4, 5)
        is_weekend_end = gap_end.dayofweek in (6, 0)
        is_weekend_duration = pd.Timedelta(hours=40) <= gap_duration <= pd.Timedelta(hours=72)

        is_valid_weekend_gap = is_weekend_start and is_weekend_end and is_weekend_duration

        if not is_valid_weekend_gap:
            return f"Mid-week gap detected ({gap_duration} between {gap_start} and {gap_end})."

    last_candle_time = df["time"].iloc[-2]  # the completed candle we'll evaluate
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    age_s = (now_utc - last_candle_time.to_pydatetime()).total_seconds()
    if age_s > (15 * 60 + MAX_CANDLE_AGE_S):
        return f"Completed candle is stale ({age_s:.0f}s old)."

    tick = mt5.symbol_info_tick(sym_name)
    if tick is None:
        return "Cannot read live tick."
    tick_age_s = time.time() - tick.time
    if tick_age_s > MAX_CANDLE_AGE_S:
        return f"Tick is stale ({tick_age_s:.0f}s old)."
    if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
        return f"Abnormal bid/ask: bid={tick.bid}, ask={tick.ask}."

    spread_pips = (tick.ask - tick.bid) / (sym.point * 10)
    if spread_pips > MAX_SPREAD_PIPS:
        return f"Spread too wide: {spread_pips:.1f} pips > {MAX_SPREAD_PIPS} limit."

    check_cols = ["close", "high", "low", "open"]
    if not np.isfinite(df[check_cols].to_numpy()).all():
        return "Non-finite (NaN/Inf) OHLC values in candle data."

    return None


# ---------------------------------------------------------------------------
# INDICATORS
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    return risk.compute_indicators(df, BB_PERIOD, ATR_PERIOD, ATR_BASELINE, BB_STD_MULT, ATR_REGIME_MULT)


# ---------------------------------------------------------------------------
# PER-SYMBOL EVALUATION (checks 1-4), COLLECTS CANDIDATES ONLY
# ---------------------------------------------------------------------------

def evaluate_symbol(sym_name: str, state: dict):
    """
    Runs indicator calc + checks 1-4 + risk engine + portfolio exposure for
    one symbol. Returns a candidate dict if a trade is approved, else None.
    Does NOT execute anything and does NOT mutate global state beyond
    returning the completed-candle timestamp for the caller to persist.
    P0-9: this function makes no execution decision -- it only reports
    whether this symbol WOULD trade, so main() can rank all symbols
    before choosing.
    """
    sym = mt5.symbol_info(sym_name)
    if sym is None:
        log.error(f"[{sym_name}] symbol_info() returned None. Skipping.")
        return None

    rates = mt5.copy_rates_from_pos(sym_name, TIMEFRAME, 0, FETCH_COUNT)
    if rates is None or len(rates) == 0:
        log.error(f"[{sym_name}] copy_rates_from_pos() returned no data. Skipping.")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    dq_reason = data_quality_check(sym_name, df, sym)
    if dq_reason:
        log.warning(f"[{sym_name}] Data quality check failed: {dq_reason}. Skipping.")
        return None

    df = compute_indicators(df)
    c = df.iloc[-2]
    indicator_cols = ["sma", "std", "z_score", "atr", "atr_baseline"]
    if not np.isfinite(c[indicator_cols].to_numpy(dtype=float)).all():
        log.warning(f"[{sym_name}] Completed candle has non-finite indicators. Skipping.")
        return None
    candle_time_str = c["time"].isoformat()

    # P0-17: duplicate-candle guard using PERSISTED last_evaluated, not memory.
    if state.get("last_evaluated", {}).get(sym_name) == candle_time_str:
        return None  # already evaluated this candle for this symbol

    sig_str = "BUY" if c["signal"] == 1 else ("SELL" if c["signal"] == -1 else "HOLD")
    log.info(
        f"[{sym_name}] Candle {c['time']} | Z: {c['z_score']:.2f} | ATR: {c['atr']:.5f} | "
        f"Regime: {c['regime_ok']} | Signal: {sig_str}"
    )

    # This candle is now considered evaluated regardless of outcome below.
    state.setdefault("last_evaluated", {})[sym_name] = candle_time_str

    # Check 1: signal
    if c["signal"] == 0:
        return None

    # Check 2: existing position (fail closed on MT5 error, P0-6)
    has_position = risk.is_position_open(sym_name, MAGIC_NUMBER)
    if has_position is None:
        log.error(f"[{sym_name}] Cannot confirm open-position state. Skipping (fail closed).")
        return None
    if has_position:
        log.info(f"[{sym_name}] Already has an open position. Skipping.")
        return None

    # Check 3: circuit breaker
    if state.get(risk.CIRCUIT_BREAKER_ACTIVE_KEY):
        log.info(f"[{sym_name}] Circuit breaker active. Skipping.")
        return None

    # Check 4: daily trade count
    if state.get("trades_today", 0) >= risk.MAX_DAILY_TRADES:
        log.info(f"[{sym_name}] Daily trade limit reached. Skipping.")
        return None

    signal_type = "BUY" if c["signal"] == 1 else "SELL"
    tick = mt5.symbol_info_tick(sym_name)
    if tick is None:
        log.error(f"[{sym_name}] Cannot read tick for risk evaluation. Skipping.")
        return None
    price = tick.ask if signal_type == "BUY" else tick.bid

    proposal = risk.evaluate_risk(
        signal_type=signal_type,
        current_price=price,
        atr_val=float(c["atr"]),
        risk_pct=RISK_PCT,
        rr_ratio=RR_RATIO,
        symbol=sym_name,
    )
    if not proposal.get("is_approved"):
        return None

    # P1-10: portfolio / correlated exposure check, in addition to per-trade risk.
    exposure = risk.check_portfolio_exposure(sym_name, signal_type, MAGIC_NUMBER, RISK_PCT)
    if not exposure.get("ok"):
        log.info(f"[{sym_name}] Portfolio exposure guard rejected trade: {exposure.get('reason')}")
        return None

    proposal["z_score_abs"] = abs(float(c["z_score"]))
    proposal["spread_pips"] = (tick.ask - tick.bid) / (sym.point * 10)
    proposal["sma"] = float(c["sma"])
    return proposal


# ---------------------------------------------------------------------------
# CANDLE CYCLE — evaluate all symbols, rank, execute (P0-9)
# ---------------------------------------------------------------------------

def run_candle_cycle(sma_cache: dict) -> None:
    state = risk.load_daily_state()
    candidates = []

    for sym_name in SYMBOLS:
        candidate = evaluate_symbol(sym_name, state)
        if candidate is not None:
            candidates.append(candidate)
        # Track latest SMA for dynamic TP regardless of whether a trade fired.
        sym = mt5.symbol_info(sym_name)
        if sym is not None:
            rates = mt5.copy_rates_from_pos(sym_name, TIMEFRAME, 0, FETCH_COUNT)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["close"] = df["close"]
                sma_val = pd.Series(df["close"]).rolling(BB_PERIOD).mean().iloc[-2]
                if pd.notna(sma_val):
                    sma_cache[sym_name] = float(sma_val)

    # Reload before persisting our candle markers so a state update made by
    # evaluate_risk() (for example start_equity) is never overwritten.
    newest_state = risk.load_daily_state()
    newest_state.setdefault("last_evaluated", {}).update(state.get("last_evaluated", {}))
    risk._save_daily_state(newest_state)

    if not candidates:
        return

    # Rank candidates by strength of mean-reversion signal (larger |Z| first).
    # List order of SYMBOLS never determines priority (P0-9).
    # Deterministic data-based tie breaker: lower current spread first.
    candidates.sort(key=lambda p: (-p["z_score_abs"], p.get("spread_pips", float("inf"))))

    for proposal in candidates:
        state = risk.load_daily_state()
        if state.get(risk.CIRCUIT_BREAKER_ACTIVE_KEY):
            log.info("Circuit breaker activated mid-cycle. Stopping further executions this cycle.")
            break
        if state.get("trades_today", 0) >= risk.MAX_DAILY_TRADES:
            log.info("Daily trade limit reached mid-cycle. Stopping further executions this cycle.")
            break

        # Re-read the portfolio immediately before every order; earlier fills
        # must change the decision for later candidates.
        exposure = risk.check_portfolio_exposure(proposal["symbol"], proposal["signal"], MAGIC_NUMBER, RISK_PCT)
        if not exposure.get("ok"):
            log.info(f"[{proposal['symbol']}] Portfolio exposure changed: {exposure.get('reason')}")
            continue
        log.info(
            f"[{proposal['symbol']}] Candidate ranked for execution (|Z|={proposal['z_score_abs']:.2f})."
        )
        execution.execute_order(proposal, log=log)


# ---------------------------------------------------------------------------
# POSITION MONITOR — dynamic TP + REALIZED close detection (P0-2)
# ---------------------------------------------------------------------------

def position_monitor(known_positions: dict, sma_cache: dict) -> None:
    positions = mt5.positions_get()
    if positions is None:
        log.error("position_monitor: positions_get() returned None. Cannot confirm state this pass.")
        return

    our_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
    current_tickets = {p.ticket for p in our_positions}

    # --- Dynamic take-profit ---
    for p in our_positions:
        sma_val = sma_cache.get(p.symbol)
        if sma_val is None:
            continue
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue

        should_close = False
        if p.type == mt5.ORDER_TYPE_BUY and tick.bid >= sma_val:
            should_close = True
        elif p.type == mt5.ORDER_TYPE_SELL and tick.ask <= sma_val:
            should_close = True

        if should_close:
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": close_type,
                "position": p.ticket,
                "price": close_price,
                "deviation": execution.MAX_DEVIATION,
                "magic": MAGIC_NUMBER,
                "comment": "NSD_DYNAMIC_TP",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": execution._supported_filling_mode(mt5.symbol_info(p.symbol)),
            }
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"[{p.symbol}] Dynamic TP closed ticket #{p.ticket} @ {result.price:.5f}.")
            else:
                code = result.retcode if result else None
                log.warning(f"[{p.symbol}] Dynamic TP close attempt failed for #{p.ticket} (retcode={code}).")

        known_positions[p.ticket] = p.profit  # last-seen floating P&L, informational only

    # --- P0-2: detect closes via REALIZED MT5 deal history, not floating P&L ---
    closed_tickets = [t for t in known_positions if t not in current_tickets]
    if closed_tickets:
        for ticket in closed_tickets:
                # Query MT5 by position id, not a short arbitrary time window:
                # this includes entry commissions, partial closes, swaps and fees.
                realized = risk.realized_pnl_for_position(ticket, MAGIC_NUMBER)
                if realized is None:
                    log.warning(f"Ticket #{ticket} has no complete MT5 deal history yet; will retry.")
                    continue
                risk.record_trade_closed(realized)
                log.info(f"Ticket #{ticket} closed. Realized P&L: {realized:.2f}. Recorded as "
                         f"{'WIN' if realized > 0 else 'LOSS' if realized < 0 else 'BREAKEVEN'}.")
                del known_positions[ticket]


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def main() -> None:
    acquire_single_instance_lock()

    log.info("=" * 60)
    log.info("NIGHTSHADE SEED ENGINE v6 — STARTING")
    log.info("=" * 60)

    if not startup_mt5():
        raise SystemExit(1)

    try:
        # P0-14: MT5 history is authoritative; reconcile cached JSON on startup.
        if risk.reconcile_state_from_history(MAGIC_NUMBER) is None:
            log.critical("Cannot reconcile MT5 state at startup; refusing to trade with uncertain limits.")
            raise SystemExit(1)

        known_positions: dict = {}
        sma_cache: dict = {}

        # Seed known_positions from whatever is already open at startup so a
        # restart doesn't lose track of positions opened by a prior run.
        existing = mt5.positions_get()
        if existing is not None:
            for p in existing:
                if p.magic == MAGIC_NUMBER:
                    known_positions[p.ticket] = p.profit

        while True:
            if not check_connection():
                time.sleep(POSITION_POLL_S)
                continue

            sleep_s = seconds_until_next_candle()
            log.info(f"Sleeping {sleep_s:.1f}s until next candle boundary. {risk.get_streak_status()}")

            slept = 0.0
            while slept < sleep_s:
                chunk = min(POSITION_POLL_S, sleep_s - slept)
                time.sleep(chunk)
                slept += chunk
                position_monitor(known_positions, sma_cache)

            if not check_connection():
                continue

            run_candle_cycle(sma_cache)

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received. Shutting down cleanly.")
    except Exception:
        log.exception("Unhandled exception in main loop. Shutting down.")
    finally:
        mt5.shutdown()
        log.info("MT5 shutdown complete. Nightshade stopped.")


if __name__ == "__main__":
    main()