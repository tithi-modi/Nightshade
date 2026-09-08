# NightshadE
### Currently this repository has only one bot, but will hold multiple bots that run on different strategies in the future.

# Bot 1
**Mean-Reversion Trading Bot for MetaTrader 5**

Nightshade is a fully automated trading bot designed for the MetaTrader 5 platform. It implements a **mean-reversion strategy** using Bollinger Bands® and ATR regime filters, enhanced with **dynamic position management** (ratchet stop‑loss, time‑decay take‑profit, high‑water mark giveback protection) and **portfolio‑aware risk allocation**. Built for robustness, it survives restarts, partial fills, and broker disconnects.

---

## Key Features

- **Entry Signal** – M5/M15 candle Z‑score (Bollinger Bands®) + ATR regime filter.
- **Dynamic Risk per Trade** – each trade uses up to 1% risk; with concurrent positions, risk is split to keep total portfolio risk ≤ 2%.
- **Up to 3 Concurrent Positions** – subject to correlation and USD‑direction limits.
- **Continuous High‑Water Mark Tracking** – peak P&L and peak R are persisted across restarts.
- **Tiered Giveback Protection** – the allowed retracement tightens as peak profit increases:
  - 0.5R – 1.0R peak → 40% giveback allowed
  - 1.0R – 2.0R peak → 25% giveback
  - ≥ 2.0R peak → 10% giveback
- **Hard Giveback Safety Net** – if P&L retraces ≥ 50% from peak and turns negative, the trade is closed immediately.
- **Decline‑to‑Zero Detection** – monitors 30‑second P&L trend; exits if a steady decline crosses breakeven.
- **Time‑Decay Take‑Profit** – the target shrinks as the trade ages (1.5R → 1.0R → 0.5R → 0.05R).
- **Ratchet Stop‑Loss** – moves SL to breakeven at +0.5R, then to +0.5R at +1.0R, +1.0R at +2.0R, and +2.0R at +3.0R.
- **SMA Mean‑Reversion Exit** – closes when price crosses the 20‑period SMA.
- **Broker‑Side Order Hardening** – supports partial fills, retries, ambiguous order states, and safe filling‑mode fallback.
- **Persistent State** – daily trade count, loss streak, and open‑position peak data survive bot restarts.
- **Heartbeat & Candle‑Scan Logging** – transparent monitoring of every 30‑second cycle and every new M15 candle.

---

## Requirements

- **Python** 3.8 or higher
- **MetaTrader 5** (IC Markets or any broker providing MT5)
- Python packages:
  - `MetaTrader5`
  - `pandas`
  - `numpy`

Install dependencies with:

```bash
pip install MetaTrader5 pandas numpy
```

---

## Live Trading Protection

By default, the bot runs on **demo accounts**. To enable live trading, you must set the following environment variables **before running `main.py`**:

| Variable | Description |
|----------|-------------|
| `LIVE_TRADING_ENABLED` | Must be exactly `"true"` (case‑sensitive). |
| `LIVE_ACCOUNT_ALLOWLIST` | Comma‑separated list of MT5 login numbers (e.g., `"12345678"`). |
| `LIVE_BROKER_ALLOWLIST` | Comma‑separated list of broker names (e.g., `"IC Markets"`). |
| `LIVE_SERVER_ALLOWLIST` | Comma‑separated list of server names (e.g., `"ICMarketsSC-Live"`). |

**Example (Windows CMD):**

```cmd
set LIVE_TRADING_ENABLED=true
set LIVE_ACCOUNT_ALLOWLIST=12345678
set LIVE_BROKER_ALLOWLIST=IC Markets
set LIVE_SERVER_ALLOWLIST=ICMarketsSC-Live
python main.py
```

**Example (Linux/macOS):**

```bash
export LIVE_TRADING_ENABLED=true
export LIVE_ACCOUNT_ALLOWLIST=12345678
export LIVE_BROKER_ALLOWLIST="IC Markets"
export LIVE_SERVER_ALLOWLIST="ICMarketsSC-Live"
python main.py
```

> ⚠️ **Warning:** Live trading is risky. Test thoroughly on demo before enabling live. The bot uses real money once these variables are set and validated.

---

## File Structure

```
Nightshade/
├── Bot1/
│   ├── main.py            # Main loop & entry scan
│   ├── execution.py       # MT5 orders, modifications, position management
│   ├── risk.py            # All calculations, state, configuration
│   ├── pull_mt5.py        # Diagnostic script
│   ├── statistics.py      # Market snapshot
│   └── logs/              # (created at runtime) – log files
├── daily_state.json       # (ignored) – current daily trade count & streak
├── position_state.json    # (ignored) – per‑position peak P&L and history
├── nightshade.lock        # (ignored) – prevents multiple instances
└── README.md              # This file
```

---

## Configuration (in `risk.py`)

You can adjust the following constants to fine‑tune the strategy:

| Constant | Description | Default |
|----------|-------------|---------|
| `BB_PERIOD` | Bollinger Bands® period | 20 |
| `BB_STD_MULT` | Number of standard deviations for bands | 2.0 |
| `ATR_PERIOD` | ATR calculation period | 14 |
| `ATR_BASELINE` | Period for ATR baseline (regime filter) | 100 |
| `ATR_REGIME_MULT` | Multiplier to allow trading when ATR < baseline × mult | 1.5 |
| `SL_ATR_MULT` | Initial stop‑loss = ATR × mult | 1.5 |
| `INITIAL_RR_MIN` | Minimum R:R for entry target | 1.2 |
| `INITIAL_RR_MAX` | Maximum R:R for entry target | 3.0 |
| `RISK_PCT` | Base risk per trade (%) | 1.0 |
| `MAX_DAILY_TRADES` | Daily trade cap | 5 |
| `CONSECUTIVE_LOSS_LIMIT` | Consecutive losses to trigger circuit breaker | 3 |
| `MAX_CONCURRENT_POSITIONS` | Max positions at once | 3 |
| `MAX_TOTAL_OPEN_RISK_PCT` | Total portfolio risk limit | 2.0% |

---

## Strategy Overview

1. **Entry**: Every M15 candle close, compute Z‑score and ATR. If Z exceeds ±2.0 and ATR regime is “quiet” (ATR < 1.5× ATR baseline), a candidate is generated.
2. **Target**: The initial take‑profit is set dynamically based on the nearest swing high/low (clamped between 1.2R and 3.0R) – or falls back to 2.0R if no structure is found.
3. **Position Management** (every 30s):
   - Update peak P&L and peak R.
   - Check hard giveback, decline‑to‑zero, tiered giveback, SMA cross.
   - Apply time‑decay TP (reduce target as trade ages).
   - Ratchet SL to lock in profit.
4. **Risk Allocation**: When multiple trades are open, the bot splits the available risk budget (≤2%) equally among them, allowing up to 3 positions simultaneously.

---

## Why This Bot Is Different

- **No static 1.5R TP** – initial target adapts to market structure.
- **No fixed €70 profit lock** – all protections are expressed in R‑multiples, automatically scaling with account size and volatility.
- **True breakeven after fees** – SL moves to open + 1 pip buffer, not just open price.
- **Persistent state** – peak tracking survives crashes and restarts.
- **Hardened broker interaction** – handles partial fills, PLACED orders, and MT5 errors without corrupting state.

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change. Make sure to update tests (if any) and the README accordingly.

---

## License

This project is open‑source and available under the **MIT License**.

---

## Disclaimer

**This software is for educational and research purposes only. Trading financial markets involves substantial risk. Use at your own risk. The authors are not responsible for any financial losses incurred by using this bot.**

---

## Contact

For questions, suggestions, or bug reports, please open an issue on the [GitHub repository](https://github.com/tithi-modi/Nightshade).
