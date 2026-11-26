# Zerodha live-depth paper bridge

This companion process reads Zerodha Kite Connect live FULL ticks and five-level
market depth, computes the configured swing filters, and maintains a virtual
paper portfolio. It does not place, modify, or cancel orders.

## 1. Requirements

- Python 3.10+
- Active Zerodha Kite Connect app/subscription and a valid access token
- NSE instrument token for the selected cash-equity symbol
- Internet connection during market hours

Install dependencies:

```bash
python -m pip install -r tools/requirements-kite-paper.txt
```

## 2. Configure secrets locally

Set environment variables in your terminal. Do not add credentials to source,
GitHub, screenshots, chat, or shared files.

PowerShell:

```powershell
$env:KITE_API_KEY="YOUR_API_KEY"
$env:KITE_ACCESS_TOKEN="YOUR_DAILY_ACCESS_TOKEN"
$env:KITE_INSTRUMENT_TOKEN="YOUR_NSE_INSTRUMENT_TOKEN"
$env:KITE_TRADINGSYMBOL="RELIANCE"
python tools/zerodha_paper_bridge.py
```

macOS/Linux:

```bash
export KITE_API_KEY="YOUR_API_KEY"
export KITE_ACCESS_TOKEN="YOUR_DAILY_ACCESS_TOKEN"
export KITE_INSTRUMENT_TOKEN="YOUR_NSE_INSTRUMENT_TOKEN"
export KITE_TRADINGSYMBOL="RELIANCE"
python tools/zerodha_paper_bridge.py
```

The Kite access token must be obtained using the official Kite Connect login
flow for your own app. Tokens generally expire and must be refreshed according
to Zerodha's current authentication process. This script intentionally does
not automate login or store credentials.

## 3. Choose the correct NSE instrument token

Use the instrument master supplied by Kite Connect and verify that the
instrument is the NSE cash-equity listing for the exact trading symbol.
Do not guess a token. The token is not the stock's ticker symbol.

## 4. Paper-trading rules

- Daily EMA(20) > EMA(50), RSI(14) between 50 and 70.
- Five-level bid quantity imbalance >= +20%.
- Largest displayed bid quantity >= 2.5x the rolling mean quantity per level.
- Virtual allocation is capped at 20% of virtual cash.
- Virtual exits: 2% hard/trailing stop, 4% take-profit, or bearish reversal.
- Missing/invalid depth or unavailable indicators produce WAIT.
- Every decision is appended to `paper_signals.csv`.

Environment overrides:

| Variable | Default |
|---|---:|
| PAPER_STARTING_CASH | 100000 |
| PAPER_MAX_ALLOCATION | 0.20 |
| PAPER_STOP_PCT | 0.02 |
| PAPER_TRAILING_PCT | 0.02 |
| PAPER_TARGET_PCT | 0.04 |
| PAPER_CSV | paper_signals.csv |

## 5. Important limitations

1. This is a live-data paper simulator, not an exchange simulator. It does not
   model brokerage, STT, GST, stamp duty, slippage, queue priority, partial
   fills, circuit limits or market impact. Net P&L will differ from real trades.
2. It uses live five-level depth snapshots. Historical daily candles do not
   contain historical five-level order-book data, so candle backtests cannot
   validate liquidity-wall signals.
3. The script currently handles one NSE cash-equity instrument per process.
4. The EMA/RSI values are refreshed from the latest available daily candle;
   live intraday depth does not turn the daily trend filter into an intraday
   strategy.
5. It logs repeated WAIT observations on incoming ticks. Keep the process and
   CSV on a trusted local machine.
6. Test first during market hours with small virtual capital and review several
   weeks of records. This is experimental software, not a recommendation to buy
   or sell any security.

## 6. LEAN relationship

The existing `Algorithm.Python/HeatmapLiquidityStrategy.py` is a separate
LEAN-side reference implementation. This standalone Kite bridge is not wired
into a running LEAN process because LEAN's algorithm runtime and this local
KiteTicker process do not share a data channel by default. Use this bridge as
the first live-depth paper signal runner; a later explicit authenticated local
data adapter can be built if you want one unified LEAN dashboard.
