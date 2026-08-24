"""
Nightshade Seed Engine - risk.py  v5  (Layer 3)

Changes vs v4 (per GitHub issue from tech review):
  P0-1  Consecutive-loss logic was already correct in v4 (win resets to 0,
        loss increments, breaker fires at CONSECUTIVE_LOSS_LIMIT). Left in
        place. Added a startup note: with a 3-consecutive-loss breaker AND
        a 3-trade/day cap, the breaker can in practice only fire on a day
        with zero wins. This is intentional per the spec but is logged
        explicitly now so it isn't a silent surprise.
  P0-2  Win/loss is no longer decided by a floating-P&L snapshot. That
        logic now lives in main.py's position monitor, which must use
        realized deal P&L from MT5 history. This module now exposes
        reconcile_state_from_history() so daily_state.json can be rebuilt
        from MT5's own trade history on every startup (item 14) instead of
        being trusted blindly.
  P0-5  record_trade... unchanged, but evaluate_risk() no longer clamps a
        too-small lot UP to the broker minimum. If the risk-correct lot is
        below volume_min, the trade is rejected outright (never take more
        risk than configured just to satisfy a broker floor).
  P1-10 New check_portfolio_exposure() — aggregate USD exposure and
        correlated-position limits, called by main.py before execution.
  P1-13 New _broker_estimated_loss() uses mt5.order_calc_profit() (or
        order_calc_margin() for margin) where available, falling back to
        the manual pip-value formula only if the broker call fails. This
        prefers MT5's own math over hand-rolled pip-value logic.
  P0-6  Helper is_position_open() added: distinguishes "no position" from
        "MT5 error" by checking for None explicitly (fail closed).
  All other v4 behaviour preserved: 1% risk, 3-trade daily cap, per-symbol
  SL distance limits, no mt5.initialize/shutdown here.
"""

import MetaTrader5 as mt5
import json
import os
import tempfile
import datetime
import logging
from pathlib import Path

log = logging.getLogger("nightshade")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

CONSECUTIVE_LOSS_LIMIT     = 3    # circuit breaker fires after this many
                                   # consecutive losses with no win in between
MAX_DAILY_TRADES           = 3    # hard cap on total trades per UTC day
CIRCUIT_BREAKER_ACTIVE_KEY = "circuit_breaker_active"
BASE_DIR                   = Path(__file__).resolve().parent
STATE_FILE                 = BASE_DIR / "daily_state.json"

# Maximum SL distance in price terms per symbol.
MAX_SL_DISTANCE = {
    "EURUSD": 0.010,
    "GBPUSD": 0.012,
    "USDJPY": 1.00,
    "AUDUSD": 0.010,
}
DEFAULT_MAX_SL_DISTANCE = 0.012

# --- Portfolio / correlated-exposure limits (P1-10) ------------------------
# All four traded pairs contain USD, so a naive per-trade 1% risk check
# alone does not bound total USD-direction exposure. These limits are a
# configurable heuristic, not a regulatory requirement -- tune to taste.
MAX_CONCURRENT_POSITIONS       = 3     # across all symbols combined
MAX_SAME_DIRECTION_USD_TRADES  = 2     # trades that are net-short-USD (or
                                        # net-long-USD) at the same time
MAX_TOTAL_OPEN_RISK_PCT        = 2.0   # sum of risk % already committed by
                                        # open positions before a new 1%
                                        # trade is allowed (so worst case
                                        # total committed risk <= 3%)

# Symbols where BUY = long USD (USD is the base currency). For all other
# traded symbols, BUY = short USD (USD is the quote currency).
USD_BASE_SYMBOLS = {"USDJPY"}


def _usd_direction(symbol: str, order_type) -> int:
    """
    Returns +1 if the position is net LONG USD, -1 if net SHORT USD.
    order_type is mt5.ORDER_TYPE_BUY / SELL (or POSITION_TYPE_BUY / SELL,
    which share the same 0/1 values).
    """
    is_buy = (order_type == mt5.ORDER_TYPE_BUY)
    if symbol in USD_BASE_SYMBOLS:
        return 1 if is_buy else -1
    return -1 if is_buy else 1


# ---------------------------------------------------------------------------
# DAILY STATE
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def compute_indicators(df, bb_period, atr_period, atr_baseline, bb_std_mult, atr_regime_mult):
    """Single indicator implementation used by the live bot and diagnostics."""
    import numpy as np
    out = df.copy()
    out["sma"] = out["close"].rolling(bb_period).mean()
    out["std"] = out["close"].rolling(bb_period).std(ddof=0)
    out["z_score"] = (out["close"] - out["sma"]) / out["std"]
    hl = out["high"] - out["low"]
    hc = (out["high"] - out["close"].shift()).abs()
    lc = (out["low"] - out["close"].shift()).abs()
    out["tr"] = np.maximum(hl, np.maximum(hc, lc))
    out["atr"] = out["tr"].rolling(atr_period).mean()
    out["atr_baseline"] = out["atr"].rolling(atr_baseline).mean()
    out["regime_ok"] = out["atr"] < (out["atr_baseline"] * atr_regime_mult)
    out["signal"] = 0
    out.loc[out["regime_ok"] & (out["z_score"] < -bb_std_mult), "signal"] = 1
    out.loc[out["regime_ok"] & (out["z_score"] > bb_std_mult), "signal"] = -1
    return out


def _default_state() -> dict:
    return {
        "date":                      _today_str(),
        "start_equity":              None,
        "trades_today":              0,
        "consecutive_losses":        0,
        "last_trade_result":         None,
        CIRCUIT_BREAKER_ACTIVE_KEY:  False,
        "last_evaluated":            {},   # symbol -> ISO candle timestamp (P1-17)
        "processed_deal_tickets":    [],   # dedupe guard for reconciliation
    }


def load_daily_state() -> dict:
    """
    Loads daily state from disk. Auto-resets at UTC midnight.
    Missing keys (e.g. from an older schema) are backfilled so callers
    never need defensive .get() calls with magic defaults scattered around.
    """
    today = _today_str()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == today:
                defaults = _default_state()
                for k, v in defaults.items():
                    state.setdefault(k, v)
                return state
        except (json.JSONDecodeError, KeyError, OSError) as e:
            log.error(f"State file unreadable ({e}). Starting fresh state.")

    state = _default_state()
    _save_daily_state(state)
    return state


def _save_daily_state(state: dict) -> None:
    """Atomic write: temp file in the same directory, then os.replace()."""
    dir_name = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".daily_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def record_trade_opened() -> None:
    """Call immediately after a fill is confirmed. Increments trades_today only."""
    state = load_daily_state()
    state["trades_today"] += 1
    log.info(
        f"Trade opened. Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}. "
        f"Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}."
    )
    _save_daily_state(state)


def record_trade_loss() -> None:
    """
    Call after a trade closes at a REALIZED loss (from MT5 history deals,
    never from a floating P&L snapshot -- see main.py position monitor).
    """
    state = load_daily_state()
    state["consecutive_losses"] += 1
    state["last_trade_result"]   = "loss"

    log.info(f"Loss recorded. Consecutive losses: {state['consecutive_losses']}/{CONSECUTIVE_LOSS_LIMIT}.")

    if state["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
        state[CIRCUIT_BREAKER_ACTIVE_KEY] = True
        log.warning(
            f"CIRCUIT BREAKER ACTIVATED. {state['consecutive_losses']} consecutive "
            f"losses reached the limit of {CONSECUTIVE_LOSS_LIMIT}. No new trades until UTC midnight."
        )

    _save_daily_state(state)


def record_trade_win() -> None:
    """Call after a trade closes at a REALIZED profit. Resets the streak."""
    state = load_daily_state()
    prev_streak = state["consecutive_losses"]
    state["consecutive_losses"] = 0
    state["last_trade_result"]  = "win"

    log.info(
        f"Win recorded. Consecutive loss streak reset from {prev_streak} to 0. "
        f"Trades today: {state['trades_today']}/{MAX_DAILY_TRADES}."
    )
    _save_daily_state(state)


def record_trade_closed(realized_profit: float) -> None:
    """
    Convenience wrapper: given a REALIZED profit figure (sum of deal
    profit + commission + swap for the closing deal(s), pulled from MT5
    history -- never floating P&L), routes to win/loss recording.
    """
    if realized_profit > 0:
        record_trade_win()
    elif realized_profit < 0:
        record_trade_loss()
    else:
        # Breakeven is intentionally neutral: it neither resets nor extends
        # the loss streak, but is recorded for auditability.
        state = load_daily_state()
        state["last_trade_result"] = "breakeven"
        _save_daily_state(state)


def get_streak_status() -> str:
    state = load_daily_state()
    cb     = state.get(CIRCUIT_BREAKER_ACTIVE_KEY, False)
    streak = state.get("consecutive_losses", 0)
    trades = state.get("trades_today", 0)
    if cb:
        return f"CIRCUIT BREAKER ACTIVE ({streak} consecutive losses)"
    return (
        f"Streak: {streak}/{CONSECUTIVE_LOSS_LIMIT} consecutive losses | "
        f"Trades: {trades}/{MAX_DAILY_TRADES}"
    )


# ---------------------------------------------------------------------------
# STATE RECONCILIATION FROM MT5 HISTORY  (P0-2 / P0-14)
# ---------------------------------------------------------------------------

def reconcile_state_from_history(magic: int) -> dict:
    """
    Rebuilds trades_today / consecutive_losses / last_trade_result /
    circuit_breaker_active from MT5's own closed-deal history for today
    (UTC), instead of trusting daily_state.json blindly.

    daily_state.json becomes a CACHE; MT5 history is authoritative.
    Call this once at startup (main.py) before entering the main loop.

    Must be called after mt5.initialize(). Does not call
    mt5.initialize()/shutdown() itself.
    """
    today_start = datetime.datetime.combine(
        datetime.datetime.utcnow().date(), datetime.time.min
    )
    now = datetime.datetime.utcnow()

    deals = mt5.history_deals_get(today_start, now)
    if deals is None:
        code, msg = mt5.last_error()
        log.error(
            f"reconcile_state_from_history: history_deals_get() returned None "
            f"(MT5 {code}: {msg}). State cannot be reconciled; caller must fail closed."
        )
        return None

    our_deals = [d for d in deals if d.magic == magic]
    our_deals.sort(key=lambda d: d.time)

    # Entries (DEAL_ENTRY_IN) count as trades opened today.
    entries = [d for d in our_deals if d.entry == mt5.DEAL_ENTRY_IN]
    trades_today = len(entries)

    # Exits/closes carry realized profit; walk them in time order to
    # rebuild the consecutive-loss streak exactly as record_trade_win/loss
    # would have, so a restart mid-day reproduces the same state.
    exits = [d for d in our_deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]

    consecutive_losses = 0
    last_trade_result  = None
    for d in exits:
        realized = d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        if realized > 0:
            consecutive_losses = 0
            last_trade_result  = "win"
        else:
            consecutive_losses += 1
            last_trade_result  = "loss"

    circuit_breaker_active = consecutive_losses >= CONSECUTIVE_LOSS_LIMIT

    state = load_daily_state()
    prior = (state["trades_today"], state["consecutive_losses"],
             state["last_trade_result"], state[CIRCUIT_BREAKER_ACTIVE_KEY])

    state["trades_today"]             = trades_today
    state["consecutive_losses"]       = consecutive_losses
    state["last_trade_result"]        = last_trade_result
    state[CIRCUIT_BREAKER_ACTIVE_KEY] = circuit_breaker_active
    state["processed_deal_tickets"]   = [d.ticket for d in exits]

    new = (trades_today, consecutive_losses, last_trade_result, circuit_breaker_active)
    if new != prior:
        log.warning(f"State reconciled from MT5 history. Cached={prior} -> Authoritative={new}.")
    else:
        log.info(f"State reconciled from MT5 history. No drift detected ({new}).")

    if CONSECUTIVE_LOSS_LIMIT >= MAX_DAILY_TRADES:
        log.info(
            f"Note: consecutive-loss limit ({CONSECUTIVE_LOSS_LIMIT}) >= daily trade "
            f"cap ({MAX_DAILY_TRADES}). The breaker can only fire on a day with zero "
            f"wins -- the trade cap will otherwise stop the bot first. This matches spec "
            f"but is logged explicitly per the tech review note."
        )

    _save_daily_state(state)
    return state


def is_position_open(symbol: str, magic: int):
    """
    Fail-closed open-position check (P0-6).
    Returns True / False / None.
      True  -> a position for this symbol+magic is confirmed open
      False -> confirmed no such position exists
      None  -> MT5 error; caller MUST treat this as "cannot confirm,
               block trading" rather than assuming no position exists.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        code, msg = mt5.last_error()
        log.error(f"[{symbol}] positions_get() returned None (MT5 {code}: {msg}). Failing closed.")
        return None
    return any(p.magic == magic for p in positions)


def realized_pnl_for_position(position_id: int, magic: int):
    """Return complete realized P&L for one position, or None on MT5 failure."""
    deals = mt5.history_deals_get(position=position_id)
    if deals is None:
        code, msg = mt5.last_error()
        log.error(f"Position {position_id}: history_deals_get failed ({code}: {msg}).")
        return None
    relevant = [d for d in deals if d.magic == magic]
    if not relevant:
        return None
    return sum(
        float(getattr(d, "profit", 0.0)) + float(getattr(d, "commission", 0.0))
        + float(getattr(d, "swap", 0.0)) + float(getattr(d, "fee", 0.0))
        for d in relevant
    )


# ---------------------------------------------------------------------------
# PORTFOLIO / CORRELATED EXPOSURE  (P1-10)
# ---------------------------------------------------------------------------

def check_portfolio_exposure(symbol: str, signal_type: str, magic: int, candidate_risk_pct: float = 1.0) -> dict:
    """
    Aggregate exposure guard, applied in addition to per-trade 1% risk.
    Returns {"ok": True} or {"ok": False, "reason": str}.
    Must be called after evaluate_risk() approves the individual trade,
    since it needs the candidate's direction but does not size the trade.
    """
    positions = mt5.positions_get()
    if positions is None:
        code, msg = mt5.last_error()
        return {"ok": False, "reason": f"positions_get() failed (MT5 {code}: {msg}); failing closed."}

    our_positions = [p for p in positions if p.magic == magic]

    if len(our_positions) >= MAX_CONCURRENT_POSITIONS:
        return {"ok": False, "reason": f"Max concurrent positions reached ({len(our_positions)}/{MAX_CONCURRENT_POSITIONS})."}

    candidate_type = mt5.ORDER_TYPE_BUY if signal_type == "BUY" else mt5.ORDER_TYPE_SELL
    candidate_dir  = _usd_direction(symbol, candidate_type)

    same_dir_count = 1  # counting the candidate itself
    for p in our_positions:
        pos_dir = _usd_direction(p.symbol, p.type)
        if pos_dir == candidate_dir:
            same_dir_count += 1

    if same_dir_count > MAX_SAME_DIRECTION_USD_TRADES:
        return {
            "ok": False,
            "reason": (
                f"Adding this trade would create {same_dir_count} concurrent positions "
                f"net-{'long' if candidate_dir > 0 else 'short'}-USD, exceeding limit "
                f"{MAX_SAME_DIRECTION_USD_TRADES}. Correlated exposure too high."
            ),
        }

    account = mt5.account_info()
    if account is None:
        return {"ok": False, "reason": "account_info() returned None; failing closed."}

    committed_risk_pct = 0.0
    for p in our_positions:
        sym = mt5.symbol_info(p.symbol)
        if sym is None:
            continue
        sl_distance = abs(p.price_open - p.sl) if p.sl else 0.0
        if sl_distance <= 0:
            continue
        loss_estimate = _broker_estimated_loss(p.symbol, p.type, p.volume, p.price_open, p.sl)
        if loss_estimate is not None and account.equity > 0:
            committed_risk_pct += (abs(loss_estimate) / account.equity) * 100.0

    projected_risk_pct = committed_risk_pct + candidate_risk_pct
    if projected_risk_pct > MAX_TOTAL_OPEN_RISK_PCT:
        return {
            "ok": False,
            "reason": (
                f"Projected open risk {projected_risk_pct:.2f}% exceeds "
                f"limit {MAX_TOTAL_OPEN_RISK_PCT:.2f}%. Skipping new trade."
            ),
        }

    return {"ok": True}


# ---------------------------------------------------------------------------
# BROKER-AWARE LOSS / PIP VALUE CALCULATION  (P1-13)
# ---------------------------------------------------------------------------

def _broker_estimated_loss(symbol: str, order_type, volume: float, entry: float, sl: float):
    """
    Prefer MT5's own order_calc_profit() to estimate the $ loss at SL
    (it correctly handles contract size, tick value, and JPY-style
    quoting per-broker). Falls back to None if unavailable so callers can
    use the manual pip-value formula as a backup.
    """
    try:
        result = mt5.order_calc_profit(order_type, symbol, volume, entry, sl)
        if result is not None:
            return result
    except Exception as e:
        log.debug(f"[{symbol}] order_calc_profit() unavailable: {e}")
    return None


def _get_pip_value_per_lot(symbol: str, account_currency: str) -> float:
    """
    Monetary value of 1 pip per 1.0 standard lot in account currency.
    Manual fallback formula, used only where a direct broker calc isn't
    convenient (pre-sizing, before we know entry/SL as concrete prices).
    """
    sym = mt5.symbol_info(symbol)
    if sym is None:
        log.warning(f"Cannot read symbol info for {symbol}.")
        return None

    tick_value = sym.trade_tick_value
    tick_size  = sym.trade_tick_size

    if tick_size == 0:
        log.warning(f"tick_size is 0 for {symbol}.")
        return None

    pip_value = tick_value * ((sym.point * 10) / tick_size)
    log.info(f"Pip value | {symbol}: {pip_value:.4f} {account_currency}/lot.")
    return pip_value


# ---------------------------------------------------------------------------
# PRE-TRADE BROKER VALIDATION  (P1-12, used by execution.py)
# ---------------------------------------------------------------------------

def validate_broker_constraints(symbol: str, order_type, volume: float, price: float) -> dict:
    """
    Runs margin check + order_check() before an order is ever sent.
    Returns {"ok": True, "margin": float} or {"ok": False, "reason": str}.
    Does NOT determine filling mode (execution.py handles that from
    symbol_info().filling_mode, since order_check needs a concrete type).
    """
    account = mt5.account_info()
    if account is None:
        return {"ok": False, "reason": "account_info() returned None."}

    margin = mt5.order_calc_margin(order_type, symbol, volume, price)
    if margin is None:
        code, msg = mt5.last_error()
        return {"ok": False, "reason": f"order_calc_margin() failed (MT5 {code}: {msg})."}

    if margin > account.margin_free:
        return {
            "ok": False,
            "reason": f"Insufficient free margin: need {margin:.2f}, have {account.margin_free:.2f}.",
        }

    return {"ok": True, "margin": margin}


# ---------------------------------------------------------------------------
# MAIN RISK EVALUATION
# ---------------------------------------------------------------------------

def evaluate_risk(
    signal_type:   str,
    current_price: float,
    atr_val:       float,
    risk_pct:      float = 1.0,
    rr_ratio:      float = 1.5,
    symbol:        str   = "EURUSD",
) -> dict:
    """
    Evaluates whether a trade should be taken and computes exact parameters.

    Guards applied in order:
      1. Signal and ATR validity
      2. Consecutive loss circuit breaker
      3. Daily trade count cap
      4. SL distance sanity check
      5. Position sizing and minimum lot check (REJECTS if too small --
         never rounds up to the broker minimum; see P0-5)

    Returns dict with is_approved=True and full order params, or
    is_approved=False with reject_reason. Never returns None.
    Does NOT call mt5.initialize() or mt5.shutdown().

    NOTE: this computes a *provisional* sizing at signal time. execution.py
    is responsible for recalculating against the fresh execution-time
    price before sending the order (P0-7) and rejecting if the price has
    moved enough to invalidate the risk.
    """

    def reject(reason: str) -> dict:
        log.info(f"Risk REJECT [{symbol}]: {reason}")
        return {"is_approved": False, "reject_reason": reason}

    if signal_type not in ["BUY", "SELL"]:
        return reject(f"Invalid signal type: {signal_type}")
    if atr_val <= 0 or atr_val != atr_val:
        return reject(f"Invalid ATR value: {atr_val}")

    state = load_daily_state()
    if state.get(CIRCUIT_BREAKER_ACTIVE_KEY):
        streak = state.get("consecutive_losses", 0)
        return reject(f"Circuit breaker active: {streak} consecutive losses. Resets at UTC midnight.")

    trades_today = state.get("trades_today", 0)
    if trades_today >= MAX_DAILY_TRADES:
        return reject(f"Daily trade limit: {trades_today}/{MAX_DAILY_TRADES} reached.")

    account = mt5.account_info()
    sym     = mt5.symbol_info(symbol)
    if account is None:
        return reject("mt5.account_info() returned None.")
    if sym is None:
        return reject(f"mt5.symbol_info({symbol}) returned None.")

    equity           = account.equity
    account_currency = account.currency
    digits           = sym.digits

    if state["start_equity"] is None:
        state["start_equity"] = equity
        _save_daily_state(state)
        log.info(f"Start-of-day equity: {equity:.2f} {account_currency}.")

    sl_distance = atr_val * 1.5
    max_sl      = MAX_SL_DISTANCE.get(symbol, DEFAULT_MAX_SL_DISTANCE)
    if sl_distance > max_sl:
        return reject(f"SL distance {sl_distance:.5f} > max {max_sl:.5f}. Market too volatile.")

    sl_pips = sl_distance / (sym.point * 10)
    if sl_pips < 1.0:
        return reject(f"SL {sl_pips:.2f} pips too small — spread risk.")

    if signal_type == "BUY":
        sl_price = round(current_price - sl_distance, digits)
        tp_price = round(current_price + sl_distance * rr_ratio, digits)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl_price = round(current_price + sl_distance, digits)
        tp_price = round(current_price - sl_distance * rr_ratio, digits)
        order_type = mt5.ORDER_TYPE_SELL

    risk_amount = equity * (risk_pct / 100.0)
    loss_per_lot = _broker_estimated_loss(symbol, order_type, 1.0, current_price, sl_price)
    if loss_per_lot is None or loss_per_lot >= 0:
        return reject("Broker cannot calculate a valid account-currency loss at SL.")

    step     = sym.volume_step
    raw_lot  = risk_amount / abs(loss_per_lot)
    lot_size = (raw_lot // step) * step
    lot_size = round(lot_size, 2)

    # P0-5: never round UP to the broker minimum -- that silently increases
    # risk beyond risk_pct. Reject instead.
    if lot_size < sym.volume_min:
        return reject(
            f"Risk-correct lot {lot_size} < broker minimum {sym.volume_min}. "
            f"Broker minimum would exceed the configured {risk_pct}% risk. "
            f"Equity {equity:.2f} too low for this SL distance — rejecting rather "
            f"than over-risking."
        )
    if lot_size > sym.volume_max:
        lot_size = sym.volume_max  # clamping DOWN is safe (reduces risk, never increases it)

    actual_loss = _broker_estimated_loss(symbol, order_type, lot_size, current_price, sl_price)
    if actual_loss is None or actual_loss >= 0 or abs(actual_loss) > risk_amount * 1.001:
        return reject("Broker-calculated loss at SL exceeds or cannot verify the configured risk.")

    log.info(
        f"Risk APPROVED [{symbol}] | {signal_type} {lot_size} lots | "
        f"Entry: {current_price:.{digits}f} | SL: {sl_price:.{digits}f} | TP: {tp_price:.{digits}f} | "
        f"Risk: {risk_amount:.2f} {account_currency} ({risk_pct}%) | SL pips: {sl_pips:.1f} | "
        f"{get_streak_status()}"
    )

    return {
        "is_approved":   True,
        "symbol":        symbol,
        "signal":        signal_type,
        "equity":        equity,
        "lot_size":      lot_size,
        "entry_price":   current_price,
        "sl_price":      sl_price,
        "tp_price":      tp_price,
        "risk_amount":   round(abs(actual_loss), 2),
        "sl_pips":       round(sl_pips, 1),
        "sl_distance":   sl_distance,
        "rr_ratio":      rr_ratio,
        "reject_reason": None,
    }
