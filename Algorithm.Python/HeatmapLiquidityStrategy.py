# HeatmapLiquidityStrategy.py
# QuantConnect LEAN-compatible, signal-only paper-trading algorithm.
#
# SAFETY: This strategy NEVER submits broker orders. It maintains an internal
# virtual position ledger and logs BUY/SELL/WAIT decisions only.
#
# Level-2 data is deliberately fail-closed: until a fresh Zerodha depth snapshot
# is supplied through ingest_depth_snapshot(), the strategy emits WAIT.
#
# Daily swing filters: EMA(20)/EMA(50), RSI(14), and a 2.5x rolling depth-wall
# threshold. Risk controls: 2% hard stop, 4% target, 2% trailing stop.
#
# Do not treat a candle-only backtest as validation of Level-2 signals.

from AlgorithmImports import *
from collections import deque
from datetime import timedelta


class HeatmapLiquidityStrategy(QCAlgorithm):

    def initialize(self):
        self.set_start_date(2021, 1, 1)
        self.set_cash(100000)
        self.set_time_zone(TimeZones.IST)

        # Change/add NSE symbols only after confirming LEAN data availability.
        self.symbol = self.add_equity("RELIANCE", Resolution.DAILY, Market.INDIA).symbol

        self.ema_fast = self.ema(self.symbol, 20, Resolution.DAILY)
        self.ema_slow = self.ema(self.symbol, 50, Resolution.DAILY)
        self.rsi = self.rsi(self.symbol, 14, MovingAverageType.WILDERS, Resolution.DAILY)
        self.set_warm_up(60, Resolution.DAILY)

        self.depth_window = 20
        self.wall_multiplier = 2.5
        self.depth_history = deque(maxlen=self.depth_window)
        self.depth_snapshot = None
        self.depth_max_age = timedelta(seconds=5)

        self.stop_pct = 0.02
        self.target_pct = 0.04
        self.trailing_pct = 0.02
        self.paper_cash = 100000.0
        self.paper_qty = 0
        self.entry_price = None
        self.highest_price = None
        self.last_signal = "WAIT"

        # Conservative exposure cap for virtual paper positions.
        self.max_allocation = 0.20
        self.minimum_imbalance = 0.20

        self.debug("HeatmapLiquidityStrategy initialized in SIGNAL-ONLY PAPER mode. No orders will be submitted.")

    def ingest_depth_snapshot(self, timestamp, bids, asks, last_price):
        """
        Feed adapter contract.
        timestamp: datetime in exchange-local time
        bids/asks: iterable of (price, quantity), best level first, up to 5 levels
        last_price: latest traded price

        Call this method from a separately validated Zerodha KiteTicker bridge.
        This method is intentionally not wired to broker credentials or orders.
        """
        try:
            bid_levels = [(float(p), float(q)) for p, q in bids if float(p) > 0 and float(q) >= 0][:5]
            ask_levels = [(float(p), float(q)) for p, q in asks if float(p) > 0 and float(q) >= 0][:5]
            if not bid_levels or not ask_levels or float(last_price) <= 0:
                self.depth_snapshot = None
                return False

            bid_total = sum(q for _, q in bid_levels)
            ask_total = sum(q for _, q in ask_levels)
            total = bid_total + ask_total
            imbalance = (bid_total - ask_total) / total if total > 0 else 0.0

            bid_wall = max((q for _, q in bid_levels), default=0.0)
            ask_wall = max((q for _, q in ask_levels), default=0.0)
            current_depth = (bid_total + ask_total) / 2.0
            self.depth_history.append(current_depth)
            baseline = (sum(self.depth_history) / len(self.depth_history)) if self.depth_history else 0.0

            self.depth_snapshot = {
                "timestamp": timestamp,
                "imbalance": imbalance,
                "bid_wall": bid_wall,
                "ask_wall": ask_wall,
                "baseline": baseline,
                "last_price": float(last_price),
                "bid_wall_detected": baseline > 0 and bid_wall >= self.wall_multiplier * baseline,
                "ask_wall_detected": baseline > 0 and ask_wall >= self.wall_multiplier * baseline
            }
            return True
        except Exception as exc:
            self.depth_snapshot = None
            self.debug(f"Invalid depth snapshot; fail-closed to WAIT: {exc}")
            return False

    def _depth_is_fresh(self):
        if self.depth_snapshot is None:
            return False
        age = self.time - self.depth_snapshot["timestamp"]
        return timedelta(seconds=0) <= age <= self.depth_max_age

    def _emit(self, signal, reason, price):
        self.last_signal = signal
        self.debug(f"{self.time} | {signal} | {self.symbol} | price={price:.2f} | {reason}")

    def on_data(self, data):
        if self.is_warming_up or not data.contains_key(self.symbol):
            return

        bar = data[self.symbol]
        price = float(bar.close)
        if price <= 0 or not (self.ema_fast.is_ready and self.ema_slow.is_ready and self.rsi.is_ready):
            self._emit("WAIT", "Indicators not ready or invalid price", price)
            return

        # Update and enforce virtual position risk controls on every daily bar.
        if self.paper_qty > 0 and self.entry_price is not None:
            self.highest_price = max(self.highest_price or price, price)
            hard_stop = self.entry_price * (1.0 - self.stop_pct)
            trailing_stop = self.highest_price * (1.0 - self.trailing_pct)
            target = self.entry_price * (1.0 + self.target_pct)
            stop_level = max(hard_stop, trailing_stop)

            if price <= stop_level:
                self._paper_exit(price, "STOP/TRAILING STOP triggered")
                return
            if price >= target:
                self._paper_exit(price, "TAKE-PROFIT target triggered")
                return

        # No fresh Level-2 snapshot means no entry/exit signal based on liquidity.
        if not self._depth_is_fresh():
            self._emit("WAIT", "Missing/stale Zerodha Level-2 snapshot; fail-closed", price)
            return

        depth = self.depth_snapshot
        trend_up = self.ema_fast.current.value > self.ema_slow.current.value
        momentum_up = 50.0 < self.rsi.current.value < 70.0
        buy_pressure = depth["imbalance"] >= self.minimum_imbalance
        sell_pressure = depth["imbalance"] <= -self.minimum_imbalance
        bid_wall = depth["bid_wall_detected"]
        ask_wall = depth["ask_wall_detected"]

        if self.paper_qty == 0:
            if trend_up and momentum_up and buy_pressure and bid_wall:
                allocation = self.paper_cash * self.max_allocation
                qty = int(allocation // price)
                if qty > 0:
                    self._paper_entry(price, qty, "EMA20>EMA50, RSI confirmation, positive depth imbalance and bid wall")
                    return
            self._emit("WAIT", "Entry conditions not all satisfied", price)
            return

        # A confirmed reversal closes the virtual long; no short selling.
        trend_down = self.ema_fast.current.value < self.ema_slow.current.value
        momentum_down = self.rsi.current.value < 45.0
        if trend_down and momentum_down and sell_pressure and ask_wall:
            self._paper_exit(price, "Confirmed bearish reversal: trend, RSI and order-book pressure")
            return

        self._emit("WAIT", "Virtual long held; no exit condition", price)

    def _paper_entry(self, price, qty, reason):
        cost = price * qty
        if cost > self.paper_cash:
            self._emit("WAIT", "Insufficient virtual cash", price)
            return
        self.paper_cash -= cost
        self.paper_qty = qty
        self.entry_price = price
        self.highest_price = price
        self._emit("BUY (PAPER)", f"qty={qty}; {reason}", price)

    def _paper_exit(self, price, reason):
        if self.paper_qty <= 0:
            return
        proceeds = price * self.paper_qty
        pnl = (price - self.entry_price) * self.paper_qty
        qty = self.paper_qty
        self.paper_cash += proceeds
        self.paper_qty = 0
        self.entry_price = None
        self.highest_price = None
        self._emit("SELL (PAPER)", f"qty={qty}; realized_pnl={pnl:.2f}; {reason}", price)

    def on_end_of_algorithm(self):
        self.debug(
            f"PAPER SUMMARY | cash={self.paper_cash:.2f} | "
            f"open_qty={self.paper_qty} | last_signal={self.last_signal}"
        )
