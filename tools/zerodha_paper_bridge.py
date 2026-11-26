"""
Zerodha Kite Connect -> signal-only NSE paper trader.

This program is deliberately incapable of placing/cancelling/modifying orders:
it contains no order execution calls. It subscribes to live FULL ticks, reads
five-level depth, and writes simulated BUY/SELL/WAIT events to CSV.

Environment variables required:
  KITE_API_KEY
  KITE_ACCESS_TOKEN
  KITE_INSTRUMENT_TOKEN   (NSE instrument token for selected symbol)
  KITE_TRADINGSYMBOL      (e.g. RELIANCE)
Optional:
  PAPER_STARTING_CASH=100000
  PAPER_MAX_ALLOCATION=0.20
  PAPER_STOP_PCT=0.02
  PAPER_TRAILING_PCT=0.02
  PAPER_TARGET_PCT=0.04
  PAPER_CSV=paper_signals.csv
"""

import csv
import logging
import math
import os
import signal
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect, KiteTicker

IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger("kite-paper")


def env_float(name, default):
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else float(default)


def ema(values, period):
    if len(values) < period:
        raise ValueError(f"Need at least {period} observations for EMA")
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def rsi_wilder(closes, period=14):
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes for RSI")
    gains, losses = [], []
    for old, new in zip(closes[:-1], closes[1:]):
        change = float(new) - float(old)
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class PaperEngine:
    def __init__(self, symbol, instrument_token, kite):
        self.symbol = symbol
        self.token = int(instrument_token)
        self.kite = kite
        self.cash = env_float("PAPER_STARTING_CASH", 100000)
        self.max_allocation = env_float("PAPER_MAX_ALLOCATION", 0.20)
        self.stop_pct = env_float("PAPER_STOP_PCT", 0.02)
        self.trailing_pct = env_float("PAPER_TRAILING_PCT", 0.02)
        self.target_pct = env_float("PAPER_TARGET_PCT", 0.04)
        self.min_imbalance = 0.20
        self.wall_multiple = 2.5
        self.depth_history = deque(maxlen=20)
        self.last_depth_time = None
        self.last_signal_day = None
        self.last_indicator_refresh_day = None
        self.indicators = None
        self.qty = 0
        self.entry = None
        self.highest = None
        self.csv_path = Path(os.getenv("PAPER_CSV", "paper_signals.csv"))
        self.lock = threading.RLock()
        self._ensure_csv()
        self.refresh_daily_indicators(force=True)

    def _ensure_csv(self):
        if not self.csv_path.exists():
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "timestamp_ist", "symbol", "signal", "ltp", "quantity",
                    "virtual_cash", "virtual_position_qty", "entry_price",
                    "paper_pnl", "reason", "depth_imbalance", "bid_wall",
                    "ask_wall"
                ])

    def log_event(self, signal_name, ltp, qty, reason, imbalance="", bid_wall="", ask_wall=""):
        pnl = ""
        if self.qty and self.entry is not None and ltp:
            pnl = round((float(ltp) - self.entry) * self.qty, 2)
        row = [
            datetime.now(IST).isoformat(timespec="seconds"), self.symbol,
            signal_name, round(float(ltp), 2) if ltp else "",
            qty, round(self.cash, 2), self.qty,
            round(self.entry, 2) if self.entry is not None else "",
            pnl, reason, imbalance, bid_wall, ask_wall
        ]
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
        LOG.info("%s %s LTP=%s qty=%s | %s", signal_name, self.symbol, ltp, qty, reason)

    def refresh_daily_indicators(self, force=False):
        now = datetime.now(IST)
        if not force and self.last_indicator_refresh_day == now.date():
            return
        end = now.date()
        start = end - timedelta(days=180)
        candles = self.kite.historical_data(
            self.token,
            datetime.combine(start, datetime.min.time()),
            datetime.combine(end, datetime.max.time()),
            "day"
        )
        closes = [float(c["close"]) for c in candles if c.get("close") is not None]
        if len(closes) < 60:
            raise RuntimeError(
                f"Only {len(closes)} daily candles returned for {self.symbol}; "
                "need at least 60. Check token, permissions and historical data."
            )
        self.indicators = {
            "ema20": ema(closes[-100:], 20),
            "ema50": ema(closes[-150:], 50),
            "rsi14": rsi_wilder(closes[-100:], 14),
            "asof": candles[-1]["date"]
        }
        self.last_indicator_refresh_day = now.date()
        LOG.info(
            "Daily indicators %s: EMA20=%.2f EMA50=%.2f RSI14=%.2f candle=%s",
            self.symbol, self.indicators["ema20"], self.indicators["ema50"],
            self.indicators["rsi14"], self.indicators["asof"]
        )

    def _paper_exit(self, ltp, reason, imbalance="", bid_wall="", ask_wall=""):
        qty = self.qty
        pnl = (float(ltp) - self.entry) * qty
        self.cash += float(ltp) * qty
        self.qty = 0
        self.entry = None
        self.highest = None
        self.log_event("SELL (PAPER)", ltp, qty, f"{reason}; realized_pnl={pnl:.2f}",
                       imbalance, bid_wall, ask_wall)

    def on_tick(self, tick):
        with self.lock:
            now = datetime.now(IST)
            if now.date() != self.last_indicator_refresh_day and now.hour >= 15 and now.minute >= 35:
                try:
                    self.refresh_daily_indicators()
                except Exception:
                    LOG.exception("Daily indicator refresh failed; entries disabled until refreshed")

            ltp = tick.get("last_price")
            if not ltp or float(ltp) <= 0:
                self.log_event("WAIT", ltp, 0, "Missing/invalid last traded price")
                return
            ltp = float(ltp)

            # Risk exits are evaluated from LTP even if the order book is absent.
            if self.qty > 0:
                self.highest = max(self.highest or ltp, ltp)
                hard_stop = self.entry * (1.0 - self.stop_pct)
                trailing_stop = self.highest * (1.0 - self.trailing_pct)
                target = self.entry * (1.0 + self.target_pct)
                if ltp <= max(hard_stop, trailing_stop):
                    self._paper_exit(ltp, "2% hard/trailing stop")
                    return
                if ltp >= target:
                    self._paper_exit(ltp, "4% take-profit")
                    return

            depth = tick.get("深度") or tick.get("depth")
            if not isinstance(depth, dict):
                self.log_event("WAIT", ltp, 0, "No depth in tick; fail-closed")
                return
            bids = depth.get("buy") or []
            asks = depth.get("sell") or []
            if len(bids) < 5 or len(asks) < 5:
                self.log_event("WAIT", ltp, 0, "Incomplete five-level depth; fail-closed")
                return

            try:
                bid_qty = [float(x["quantity"]) for x in bids[:5]]
                ask_qty = [float(x["quantity"]) for x in asks[:5]]
                bid_prices = [float(x["price"]) for x in bids[:5]]
                ask_prices = [float(x["price"]) for x in asks[:5]]
                if any(q < 0 for q in bid_qty + ask_qty) or any(p <= 0 for p in bid_prices + ask_prices):
                    raise ValueError("negative quantity or non-positive price")
                if bid_prices[0] >= ask_prices[0]:
                    raise ValueError("crossed/locked best bid-ask; invalid snapshot")
            except (KeyError, TypeError, ValueError):
                self.log_event("WAIT", ltp, 0, "Invalid/crossed order-book snapshot")
                return

            self.last_depth_time = now
            bid_total, ask_total = sum(bid_qty), sum(ask_qty)
            total = bid_total + ask_total
            if total <= 0:
                self.log_event("WAIT", ltp, 0, "Empty order-book quantities")
                return
            imbalance = (bid_total - ask_total) / total
            per_level_mean = total / 10.0
            self.depth_history.append(per_level_mean)
            baseline = sum(self.depth_history) / len(self.depth_history)
            bid_wall = max(bid_qty) >= self.wall_multiple * baseline if baseline > 0 else False
            ask_wall = max(ask_qty) >= self.wall_multiple * baseline if baseline > 0 else False

            if not self.indicators:
                self.log_event("WAIT", ltp, 0, "Daily indicators unavailable")
                return

            # One entry/one reversal decision per trading date; risk exits remain tick-driven.
            today = now.date()
            if self.qty == 0:
                bullish = (
                    self.indicators["ema20"] > self.indicators["ema50"]
                    and 50.0 < self.indicators["rsi14"] < 70.0
                    and imbalance >= self.min_imbalance
                    and bid_wall
                )
                if bullish and self.last_signal_day != today:
                    budget = self.cash * self.max_allocation
                    qty = int(budget // ltp)
                    if qty > 0:
                        self.cash -= qty * ltp
                        self.qty, self.entry, self.highest = qty, ltp, ltp
                        self.last_signal_day = today
                        self.log_event("BUY (PAPER)", ltp, qty,
                                       "EMA20>EMA50 + RSI 50-70 + bid imbalance >=20% + bid liquidity wall",
                                       round(imbalance, 4), bid_wall, ask_wall)
                        return
                self.log_event("WAIT", ltp, 0, "Entry conditions not all satisfied",
                               round(imbalance, 4), bid_wall, ask_wall)
                return

            bearish = (
                self.indicators["ema20"] < self.indicators["ema50"]
                and self.indicators["rsi14"] < 45.0
                and imbalance <= -self.min_imbalance
                and ask_wall
            )
            if bearish and self.last_signal_day != today:
                self.last_signal_day = today
                self._paper_exit(ltp, "Bearish trend + RSI + ask pressure/wall",
                                 round(imbalance, 4), bid_wall, ask_wall)
                return
            self.log_event("WAIT", ltp, 0, "Virtual long held; no exit trigger",
                           round(imbalance, 4), bid_wall, ask_wall)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    token = os.getenv("KITE_INSTRUMENT_TOKEN")
    symbol = os.getenv("KITE_TRADINGSYMBOL", "RELIANCE").upper()
    if not all([api_key, access_token, token]):
        raise SystemExit(
            "Set KITE_API_KEY, KITE_ACCESS_TOKEN and KITE_INSTRUMENT_TOKEN "
            "as environment variables. Never commit them to GitHub."
        )

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    engine = PaperEngine(symbol, token, kite)
    ticker = KiteTicker(api_key, access_token)

    def on_connect(ws, response):
        LOG.info("Connected. Subscribing to %s token=%s in FULL mode.", symbol, token)
        ws.subscribe([int(token)])
        ws.set_mode(ws.MODE_FULL, [int(token)])

    def on_ticks(ws, ticks):
        for tick in ticks:
            if int(tick.get("instrument_token", -1)) == int(token):
                try:
                    engine.on_tick(tick)
                except Exception:
                    LOG.exception("Tick processing failed; no order will be sent")

    def on_error(ws, code, reason):
        LOG.error("WebSocket error %s: %s", code, reason)

    def on_close(ws, code, reason):
        LOG.warning("WebSocket closed %s: %s", code, reason)

    ticker.on_connect = on_connect
    ticker.on_ticks = on_ticks
    ticker.on_error = on_error
    ticker.on_close = on_close

    LOG.warning("PAPER-ONLY MODE. This script has no broker order-placement calls.")
    ticker.connect()


if __name__ == "__main__":
    main()
