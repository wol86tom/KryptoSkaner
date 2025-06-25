import sys
import asyncio
import ccxt
import ccxt.pro as ccxtpro
import pyqtgraph as pg
import pandas as pd
import numpy as np
import datetime
from queue import Queue
from threading import Thread
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QLabel, QHBoxLayout,
                             QApplication, QGroupBox, QFormLayout, QComboBox,
                             QLineEdit, QPushButton, QMessageBox, QCheckBox, QListWidget,
                             QTableWidget, QTableWidgetItem, QHeaderView)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QPointF, QRectF
from PySide6.QtGui import QFont, QPainter, QPen, QColor

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')

class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data=[]):
        pg.GraphicsObject.__init__(self); self.data = data; self.generatePicture()
    def setData(self, data):
        self.data = data; self.generatePicture(); self.informViewBoundsChanged(); self.update()
    def generatePicture(self):
        self.picture = pg.QtGui.QPicture(); p = QPainter(self.picture)
        if not self.data: p.end(); return

        if len(self.data) > 1: w = (self.data[1]['x'] - self.data[0]['x']) * 0.4
        else: w = 0.4 # Domyślna szerokość dla pojedynczej świecy na 1s

        for d in self.data:
            t, o, h, l, c = d['x'], d['open'], d['high'], d['low'], d['close']
            p.setPen(pg.mkPen('k'))
            if o == c: p.drawLine(QPointF(t - w, o), QPointF(t + w, c))
            else:
                p.drawLine(QPointF(t, l), QPointF(t, h))
                p.setBrush(pg.mkBrush('g' if o < c else 'r')); p.drawRect(QRectF(t - w, o, w * 2, c - o))
        p.end()
    def paint(self, p, *args): p.drawPicture(0, 0, self.picture)
    def boundingRect(self):
        if not self.data: return QRectF()
        x_min = self.data[0]['x']; x_max = self.data[-1]['x']; y_min = min(d['low'] for d in self.data); y_max = max(d['high'] for d in self.data)
        return QRectF(x_min, y_min, x_max - x_min, y_max - y_min)

class AsyncioWorker(Thread):
    def __init__(self, exchange_id, pair, data_queue):
        super().__init__(); self.exchange_id = exchange_id; self.pair = pair; self.data_queue = data_queue; self._is_running = False
    def run(self):
        self._is_running = True
        try: asyncio.run(self.main_loop())
        except Exception as e: self.data_queue.put({'error': str(e)})
    async def main_loop(self):
        exchange = getattr(ccxtpro, self.exchange_id)(); print(f"[ASYNCIO]: Nasłuchuję {self.pair} na {self.exchange_id}...")
        trade_task = asyncio.create_task(self.watch_trades_loop(exchange))
        book_task = asyncio.create_task(self.watch_orderbook_loop(exchange))
        await asyncio.gather(trade_task, book_task)
        await exchange.close(); print("[ASYNCIO]: Połączenie WebSocket zamknięte.")
    async def watch_trades_loop(self, exchange):
        while self._is_running:
            try:
                trades = await exchange.watch_trades(self.pair)
                if self._is_running and trades: self.data_queue.put({'trades': trades})
            except Exception as e: self.data_queue.put({'error': f"Błąd (trades): {e}"}); await asyncio.sleep(5)
    async def watch_orderbook_loop(self, exchange):
        while self._is_running:
            try:
                orderbook = await exchange.watch_order_book(self.pair)
                if self._is_running and orderbook: self.data_queue.put({'orderbook': orderbook})
            except Exception as e: self.data_queue.put({'error': f"Błąd (book): {e}"}); await asyncio.sleep(5)
    def stop(self): self._is_running = False

class FetchMarketsThread(QThread):
    markets_fetched = Signal(list); error_occurred = Signal(str)
    def __init__(self, exchange_id, market_type, parent=None):
        super().__init__(parent); self.exchange_id = exchange_id; self.market_type_filter = market_type
    def run(self):
        try:
            exchange = getattr(ccxt, self.exchange_id)(); markets = exchange.load_markets()
            available_pairs = [m['symbol'] for m in markets.values() if self.type_matches(m)]
            self.markets_fetched.emit(sorted(available_pairs))
        except Exception as e: self.error_occurred.emit(str(e))
    def type_matches(self, market):
        if not (market.get('active') and market.get('quote') == 'USDT'): return False
        market_type = market.get('type')
        if self.market_type_filter == market_type: return True
        if self.exchange_id == 'binance' and self.market_type_filter == 'future' and market.get('linear') and market_type in ['future', 'swap']: return True
        if self.exchange_id == 'bybit' and self.market_type_filter == 'swap' and market_type == 'swap': return True
        return False

class OrderFlowWindow(QMainWindow):
    def __init__(self, exchange_options, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options; self.setWindowTitle("Analiza Order Flow"); self.setGeometry(200, 200, 1600, 900) # Zwiększamy rozmiar okna
        self.worker_thread = None; self.fetch_markets_thread = None; self.data_queue = Queue(); self.all_trades = []; self.cumulative_delta = 0
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget); self.setup_ui()
        self.update_timer = QTimer(self); self.update_timer.timeout.connect(self.process_queue)
        self.trigger_fetch_markets()
    def setup_ui(self):
        self.setup_controls_panel(); self.setup_order_book_panel(); self.setup_plots() # Zmieniona kolejność
    def setup_controls_panel(self):
        controls_widget = QWidget(); controls_widget.setFixedWidth(300); controls_layout = QVBoxLayout(controls_widget)
        exchange_group = QGroupBox("Giełda"); form_layout = QFormLayout(exchange_group); self.exchange_combo = QComboBox()
        if self.exchange_options:
            supported = ['binance', 'binanceusdm', 'bybit']; self.exchange_combo.addItems([name for name, data in self.exchange_options.items() if data.get('id_ccxt') in supported])
        self.exchange_combo.currentTextChanged.connect(self.trigger_fetch_markets); form_layout.addRow("Wybierz:", self.exchange_combo)
        self.resample_combo = QComboBox(); self.resample_combo.addItems(["1Min", "5Min", "15Min", "1H", "1s", "5s", "15s"])
        self.resample_combo.setCurrentText("1Min"); form_layout.addRow("Interwał świecy:", self.resample_combo)
        self.delta_mode_combo = QComboBox(); self.delta_mode_combo.addItems(["CVD (Skumulowana Delta)", "Delta na Świecę"])
        form_layout.addRow("Tryb Delty:", self.delta_mode_combo)
        controls_layout.addWidget(exchange_group)
        pairs_group = QGroupBox("Zarządzanie Parami"); pairs_layout = QVBoxLayout(pairs_group)
        self.available_pairs_list = QListWidget(); self.watchlist = QListWidget()
        pairs_layout.addWidget(QLabel("Dostępne Pary:")); pairs_layout.addWidget(self.available_pairs_list)
        buttons_layout = QHBoxLayout(); add_button = QPushButton(">"); add_button.clicked.connect(self.add_to_watchlist); remove_button = QPushButton("<"); remove_button.clicked.connect(self.remove_from_watchlist)
        buttons_layout.addStretch(); buttons_layout.addWidget(add_button); buttons_layout.addWidget(remove_button); buttons_layout.addStretch()
        pairs_layout.addLayout(buttons_layout); pairs_layout.addWidget(QLabel("Obserwowane Pary:")); pairs_layout.addWidget(self.watchlist)
        self.refresh_markets_button = QPushButton("Odśwież Listę Par"); self.refresh_markets_button.clicked.connect(self.trigger_fetch_markets)
        pairs_layout.addWidget(self.refresh_markets_button); controls_layout.addWidget(pairs_group)
        self.start_button = QPushButton("Start Stream"); self.stop_button = QPushButton("Stop Stream"); self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_stream); self.stop_button.clicked.connect(self.stop_stream)
        controls_layout.addWidget(self.start_button); controls_layout.addWidget(self.stop_button); controls_layout.addStretch(); self.main_layout.addWidget(controls_widget)
    def setup_order_book_panel(self):
        orderbook_widget = QWidget(); orderbook_widget.setFixedWidth(350)
        ob_layout = QVBoxLayout(orderbook_widget)
        ob_layout.setContentsMargins(0,0,0,0); ob_layout.setSpacing(1)
        self.asks_table = QTableWidget(); self.bids_table = QTableWidget()
        self.setup_orderbook_table(self.asks_table, "Asks", QColor(255, 230, 230))
        self.setup_orderbook_table(self.bids_table, "Bids", QColor(230, 255, 230))
        ob_layout.addWidget(self.asks_table); ob_layout.addWidget(self.bids_table)
        self.main_layout.addWidget(orderbook_widget)
    def setup_orderbook_table(self, table, header_prefix, color):
        table.setColumnCount(3); table.setHorizontalHeaderLabels(["Ilość", "Cena", "Wartość (USDT)"]); table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); table.setRowCount(20)
        table.setStyleSheet(f"QTableWidget {{ background-color: {color.name()}; gridline-color: #d0d0d0; }} QHeaderView::section {{ background-color: #f0f0f0; }}")
    def setup_plots(self):
        plots_widget = QWidget(); plots_layout = QVBoxLayout(plots_widget)
        self.price_plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()}); self.price_plot_widget.getPlotItem().setLabel('left', "Cena"); self.price_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.cvd_plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()}); self.cvd_plot_widget.getPlotItem().setLabel('left', "Delta"); self.cvd_plot_widget.showGrid(x=True, y=True, alpha=0.3); self.cvd_plot_widget.setXLink(self.price_plot_widget)
        plots_layout.addWidget(self.price_plot_widget, stretch=3); plots_layout.addWidget(self.cvd_plot_widget, stretch=1); self.main_layout.addWidget(plots_widget, 1) # Wykresy zajmują resztę miejsca
    def start_stream(self):
        if self.worker_thread and self.worker_thread.is_alive(): return
        current_item = self.watchlist.currentItem()
        if not current_item: QMessageBox.warning(self, "Brak pary", "Najpierw dodaj parę do listy obserwowanych i ją zaznacz."); return
        pair = current_item.text()
        self.all_trades = []; self.cumulative_delta = 0; self.cvd_data_x = []; self.cvd_data_y = []
        self.price_plot_widget.clear(); self.cvd_plot_widget.clear()
        self.candlestick_item = CandlestickItem(); self.price_plot_widget.addItem(self.candlestick_item)
        selected_exchange = self.exchange_options[self.exchange_combo.currentText()]['id_ccxt']
        self.worker_thread = AsyncioWorker(selected_exchange, pair, self.data_queue); self.worker_thread.daemon = True; self.worker_thread.start(); self.update_timer.start(100)
        self.start_button.setEnabled(False); self.stop_button.setEnabled(True); self.exchange_combo.setEnabled(False)
    def stop_stream(self):
        if self.worker_thread and self.worker_thread.is_alive(): self.worker_thread.stop(); self.update_timer.stop()
        self.start_button.setEnabled(True); self.stop_button.setEnabled(False); self.exchange_combo.setEnabled(True)
    def process_queue(self):
        if self.data_queue.empty(): return
        while not self.data_queue.empty():
            data = self.data_queue.get()
            if 'error' in data: print(f"[BŁĄD WĄTKU]: {data['error']}"); continue
            if 'trades' in data: self.on_trades_received(data['trades'])
            if 'orderbook' in data: self.on_orderbook_received(data['orderbook'])
    def on_trades_received(self, trades): self.all_trades.extend(trades); self.update_charts()
    def on_orderbook_received(self, book):
        self.update_book_table(self.asks_table, book['asks'])
        self.update_book_table(self.bids_table, book['bids'])
    def update_book_table(self, table, data):
        table.setRowCount(len(data))
        for row_idx, (price, amount) in enumerate(data):
            value = price * amount; table.setItem(row_idx, 0, QTableWidgetItem(f"{amount:.4f}")); table.setItem(row_idx, 1, QTableWidgetItem(f"{price:.4f}")); table.setItem(row_idx, 2, QTableWidgetItem(f"{value:,.2f}"))
    def update_charts(self):
        if not self.all_trades: return
        df = pd.DataFrame(self.all_trades); df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms'); df = df.set_index('datetime')
        resample_map = {"1s":"1s", "5s":"5s", "15s":"15s", "1Min":"1min", "5Min":"5min", "15Min":"15min", "1H":"1H"}
        resample_period = resample_map.get(self.resample_combo.currentText(), "1min")
        candles_df = df['price'].resample(resample_period).ohlc(); candles_df.ffill(inplace=True); candles_df.dropna(inplace=True)
        if not candles_df.empty:
            candlestick_data = [{'x': d.timestamp(), 'open': r['open'], 'high': r['high'], 'low': r['low'], 'close': r['close']} for d, r in candles_df.iterrows()]
            self.candlestick_item.setData(candlestick_data)
        df['delta'] = np.where(df['side'] == 'buy', df['amount'], -df['amount']); self.cvd_plot_widget.clear()
        if self.delta_mode_combo.currentText() == "CVD (Skumulowana Delta)":
            df['cvd'] = df['delta'].cumsum()
            self.cvd_plot_widget.plot(df.index.astype(np.int64) // 10**9, df['cvd'], pen=pg.mkPen('g', width=2))
        else:
            delta_per_candle = df['delta'].resample(resample_period).sum()
            bar_width = pd.to_timedelta(resample_period).total_seconds() * 0.8
            brushes = [pg.mkBrush('g') if d > 0 else pg.mkBrush('r') for d in delta_per_candle.values]
            bar_item = pg.BarGraphItem(x=delta_per_candle.index.astype(np.int64) // 10**9, height=delta_per_candle.values, width=bar_width, brushes=brushes)
            self.cvd_plot_widget.addItem(bar_item)
        self.price_plot_widget.getPlotItem().autoRange(); self.cvd_plot_widget.getPlotItem().autoRange()
    def closeEvent(self, event): self.stop_stream(); super().closeEvent(event)
    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return
        selected_config = self.exchange_options[self.exchange_combo.currentText()]; exchange_id = selected_config['id_ccxt']; market_type = selected_config['type']
        self.refresh_markets_button.setEnabled(False); self.refresh_markets_button.setText("Pobieranie...")
        self.fetch_markets_thread = FetchMarketsThread(exchange_id, market_type, self); self.fetch_markets_thread.markets_fetched.connect(self.populate_available_pairs); self.fetch_markets_thread.error_occurred.connect(lambda e: QMessageBox.critical(self, "Błąd pobierania par", e)); self.fetch_markets_thread.finished.connect(lambda: (self.refresh_markets_button.setEnabled(True), self.refresh_markets_button.setText("Odśwież Listę Par"))); self.fetch_markets_thread.start()
    def populate_available_pairs(self, pairs):
        current_watchlist = {self.watchlist.item(i).text() for i in range(self.watchlist.count())}
        self.available_pairs_list.clear(); self.available_pairs_list.addItems([p for p in pairs if p not in current_watchlist])
    def add_to_watchlist(self):
        selected_item = self.available_pairs_list.currentItem()
        if selected_item: self.watchlist.addItem(self.available_pairs_list.takeItem(self.available_pairs_list.row(selected_item)))
    def remove_from_watchlist(self):
        selected_item = self.watchlist.currentItem()
        if selected_item: self.available_pairs_list.addItem(self.watchlist.takeItem(self.watchlist.row(selected_item)))
