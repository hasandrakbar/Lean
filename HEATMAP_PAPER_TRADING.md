# Heatmap Liquidity Strategy — Paper Only

Branch: `feat/heatmap-strategy`

## What is included

- `Algorithm.Python/HeatmapLiquidityStrategy.py`: QuantConnect LEAN Python algorithm.
- Daily EMA(20)/EMA(50) trend filter and RSI(14) momentum confirmation.
- Five-level bid/ask depth snapshot contract.
- Liquidity wall detection at 2.5x the rolling mean displayed quantity per level.
- Bid/ask quantity imbalance confirmation.
- Virtual paper ledger only: BUY (PAPER), SELL (PAPER), WAIT.
- 2% hard stop, 2% trailing stop, 4% take-profit.
- Missing, invalid or stale (>5 seconds) depth data fails closed to WAIT.

## Strict safety boundary

**This code does not submit orders.** It does not call `MarketOrder`, `SetHoldings`, Zerodha order endpoints, or any broker execution API. The paper position is an internal simulation only. No API key, API secret, access token or password belongs in this repository.

## Important integration boundary

The LEAN algorithm consumes depth through the method:

```python
ingest_depth_snapshot(timestamp, bids, asks, last_price)
```

The arguments are:
- `timestamp`: timestamp in the algorithm/exchange timezone.
- `bids`: up to five `(price, quantity)` pairs, best bid first.
- `asks`: up to five `(price, quantity)` pairs, best ask first.
- `last_price`: latest traded price.

This method is a deliberate adapter boundary, not an already-connected Kite WebSocket client. A separate KiteTicker process still needs to authenticate using the official Zerodha Kite Connect SDK and securely forward validated snapshots to the running strategy. The adapter must handle token expiry, reconnects, timestamp validation, duplicate/out-of-order messages, and instrument-token-to-symbol mapping. Until that is implemented and tested, the strategy correctly returns WAIT because it has no fresh depth.

The standard historical OHLC candle dataset does not reconstruct historical five-level order books. Consequently, a candle-only backtest tests only the trend/indicator plumbing and risk ledger; it does **not** validate the liquidity-wall or OFI component. A valid L2 backtest requires timestamped historical depth snapshots.

## Suggested local LEAN workflow

Use the LEAN CLI in a separate project directory (not by running this engine source tree as if it were a CLI project):

```bash
python -m pip install --upgrade lean
lean init
lean create-project "HeatmapLiquidityPaper"
```

Copy `Algorithm.Python/HeatmapLiquidityStrategy.py` from this repository into the generated project's algorithm file (normally `main.py`) and set that file as the project entry point if required by the generated `config.json`.

Then run:

```bash
lean backtest "HeatmapLiquidityPaper"
```

For a local backtest, configure an India-equity data source that actually supports the selected NSE ticker and daily resolution. If no India data provider is configured, the backtest will not be meaningful. Do not substitute US-market data or a different instrument without changing the strategy and documenting the change.

## First validation checklist

1. Confirm the selected symbol is available from the chosen LEAN data provider.
2. Run the daily-candle test with no depth adapter; expected behaviour is WAIT/no entries.
3. Unit-test the adapter with synthetic five-level snapshots, including empty books, zero quantities, crossed/invalid prices, stale timestamps and delayed/reordered messages.
4. Verify a 2.5x wall against the rolling per-level baseline with deterministic fixtures.
5. Replay historical depth snapshots before making any claim about L2 performance.
6. Keep this version signal-only and paper-only. Do not add live execution to this file.

## Default parameters

| Parameter | Value |
|---|---:|
| EMA fast / slow | 20 / 50 |
| RSI | 14 |
| Rolling depth window | 20 snapshots |
| Liquidity wall threshold | 2.5x mean quantity per displayed level |
| Minimum bid/ask imbalance | 20% |
| Hard stop | 2% |
| Trailing stop | 2% |
| Take profit | 4% |
| Maximum virtual allocation | 20% |
| Stale depth timeout | 5 seconds |
