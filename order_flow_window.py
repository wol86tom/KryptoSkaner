# ZREFAKTORYZOWANY I OSTATECZNIE POPRAWIONY PLIK: order_flow_window.py

import sys
import asyncio
import ccxt.pro as ccxtpro
import pyqtgraph as pg
import numpy as np
import datetime
import time
import configparser
from queue import Queue
from threading import Thread
from collections import deque

from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QLabel, QHBoxLayout,
                             QApplication, QGroupBox, QFormLayout, QComboBox,
                             QLineEdit, QPushButton, QMessageBox, QCheckBox, QListWidget, QListWidgetItem,
                             QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QSpinBox)
from PyQt6.QtCore import Qt, QTimer, QEvent, QPointF, QRectF
from PyQt6.QtGui import QFont, QPainter, QPen, QColor

import utils

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')


class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data=[]):
        pg.GraphicsObject.__init__(self)
        self.data = data
        self.generatePicture()

    def setData(self, data):
        self.data = data
        self.generatePicture()
        self.informViewBoundsChanged()
        self.update()

    def generatePicture(self):
        self.picture = pg.QtGui.QPicture()
        p = QPainter(self.picture)
        if not self.data:
            p.end()
            return

        interval_seconds = self.data[0].get('interval_seconds', 1)
        w = interval_seconds * 0.4

        for d in self.data:
            t, o, h, l, c = d['x'], d['open'], d['high'], d['low'], d['close']

            p.setPen(pg.mkPen('k', width=1))
            if h > max(o, c):
                p.drawLine(QPointF(t, h), QPointF(t, max(o, c)))
            if l < min(o, c):
                p.drawLine(QPointF(t, l), QPointF(t, min(o, c)))

            if o == c:
                p.drawLine(QPointF(t - w, o), QPointF(t + w, c))
            else:
                p.setBrush(pg.mkBrush('g' if c > o else 'r'))
                p.drawRect(QRectF(t - w, o, w * 2, c - o))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        if not self.data: return QRectF()
        interval_seconds = self.data[0].get('interval_seconds', 1)
        w = interval_seconds * 0.4
        x_min = min(d['x'] for d in self.data); x_max = max(d['x'] for d in self.data)
        y_min = np.min([d['low'] for d in self.data]); y_max = np.max([d['high'] for d in self.data])
        return QRectF(x_min - w, y_min, (x_max - x_min) + (2 * w), y_max - y_min).normalized()


class AsyncioWorker(Thread):
    def __init__(self, exchange_id, pair_symbol, market_type, data_queue):
        super().__init__()
        self.daemon = True
        self.exchange_id = exchange_id
        self.pair_symbol = pair_symbol
        self.market_type = market_type
        self.data_queue = data_queue
        self._is_running = False
        self.loop = None
        self.exchange = None
        self.tasks = []

    def run(self):
        self._is_running = True
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.main_task_wrapper())
        except Exception as e:
            if self._is_running:
                self.data_queue.put({'error': f"Błąd pętli głównej wątku: {e}"})

    async def main_task_wrapper(self):
        exchange_config = {'options': {'defaultType': self.market_type}}
        self.exchange = getattr(ccxtpro, self.exchange_id)(exchange_config)
        try:
            self.tasks = [
                self.watch_trades_loop(self.exchange, self.pair_symbol),
                self.watch_orderbook_loop(self.exchange, self.pair_symbol)
            ]
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            pass # Oczekiwane przy zamykaniu
        finally:
            if self.exchange.clients:
                await self.exchange.close()

    async def watch_trades_loop(self, exchange, symbol):
        while self._is_running:
            try:
                trades = await exchange.watch_trades(symbol)
                if self._is_running: self.data_queue.put({'exchange_id': self.exchange_id, 'trades': trades})
            except Exception as e:
                if self._is_running: self.data_queue.put({'error': f"Błąd (trades) z {self.exchange_id}: {e}"}); await asyncio.sleep(5)

    async def watch_orderbook_loop(self, exchange, symbol):
        while self._is_running:
            try:
                orderbook = await exchange.watch_order_book(symbol)
                if self._is_running: self.data_queue.put({'exchange_id': self.exchange_id, 'orderbook': orderbook})
            except Exception as e:
                if self._is_running: self.data_queue.put({'error': f"Błąd (orderbook) z {self.exchange_id}: {e}"}); await asyncio.sleep(5)

    def stop(self):
        self._is_running = False
        if self.tasks:
            for task in self.tasks:
                task.cancel()
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


class OrderFlowWindow(QMainWindow):
    def __init__(self, exchange_options, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options
        self.setWindowTitle("Analiza Order Flow"); self.setGeometry(200, 200, 1600, 900)

        self.config_path = utils.get_config_path()
        self.config = configparser.ConfigParser()

        self.active_workers = {}; self.fetch_markets_thread = None
        self.data_queue = Queue(); self.current_order_books = {}
        self.table_font = QFont(); self.table_font.setPointSize(9)
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)

        self.RESAMPLE_MAP = {"1s":1, "5s":5, "15s":15, "1Min":60, "5Min":300, "15Min":900, "1H":3600}

        self.pending_watchlist_symbols = set()
        self.pending_selected_pair = None

        self.setup_ui()
        self.load_settings()

        self.update_timer = QTimer(self); self.update_timer.timeout.connect(self.process_queue)
        self.reset_data_structures()

        self.last_plot_update_time = 0
        self.plot_update_interval_ms = 500

    def get_price_precision(self):
        agg_str = self.aggregation_combo.currentText()
        if agg_str == "Brak": return 8
        try: return int(max(0, -np.log10(float(agg_str))))
        except (ValueError, TypeError): return 8

    def reset_data_structures(self):
        self.cumulative_delta = 0; self.current_candle = {}
        self.cvd_data = deque(maxlen=20000); self.candle_data = deque(maxlen=5000)
        self.current_order_books = {}; self.active_workers = {}

    def setup_ui(self):
        self.setup_controls_panel()
        self.setup_order_book_panel()
        self.setup_plots()

    def setup_controls_panel(self):
        controls_widget = QWidget(); controls_widget.setFixedWidth(350); controls_layout = QVBoxLayout(controls_widget)
        exchange_group = QGroupBox("Sterowanie"); form_layout = QFormLayout(exchange_group)
        self.exchange_combo = QComboBox()
        supported = ['binance', 'binanceusdm', 'bybit']; self.exchange_combo.addItems([name for name, data in self.exchange_options.items() if data.get('id_ccxt') in supported])
        self.exchange_combo.currentTextChanged.connect(self.on_exchange_changed); form_layout.addRow("Giełda:", self.exchange_combo)

        self.resample_combo = QComboBox(); self.resample_combo.addItems(self.RESAMPLE_MAP.keys())
        self.resample_combo.currentTextChanged.connect(self.on_settings_changed)
        form_layout.addRow("Interwał świecy:", self.resample_combo)

        self.num_candles_spinbox = QSpinBox()
        self.num_candles_spinbox.setRange(20, 500); self.num_candles_spinbox.setValue(100)
        self.num_candles_spinbox.setSingleStep(10)
        self.num_candles_spinbox.valueChanged.connect(self.on_settings_changed)
        form_layout.addRow("Ilość świec na wykresie:", self.num_candles_spinbox)

        self.delta_mode_combo = QComboBox(); self.delta_mode_combo.addItems(["CVD (Skumulowana Delta)", "Delta na Świecę"])
        self.delta_mode_combo.currentTextChanged.connect(self.save_settings); self.delta_mode_combo.currentTextChanged.connect(self.redraw_plots)
        form_layout.addRow("Tryb Delty:", self.delta_mode_combo)

        self.aggregation_combo = QComboBox(); self.aggregation_combo.addItems(["Brak", "0.01", "0.05", "0.1", "0.5", "1.0", "5.0", "10.0"])
        self.aggregation_combo.currentTextChanged.connect(self.save_settings); self.aggregation_combo.currentTextChanged.connect(self.aggregate_and_update_display)
        form_layout.addRow("Agregacja OB:", self.aggregation_combo)

        self.ob_source_combo = QComboBox(); self.ob_source_combo.addItems(["Wybrana giełda", "Wszystkie aktywne giełdy"])
        self.ob_source_combo.currentTextChanged.connect(self.save_settings)
        form_layout.addRow("Źródło OB:", self.ob_source_combo)
        controls_layout.addWidget(exchange_group)

        pairs_group = QGroupBox("Zarządzanie Parami"); pairs_layout = QVBoxLayout(pairs_group)
        pairs_layout.addWidget(QLabel("Dostępne Pary:")); self.available_pairs_list = QListWidget(); self.available_pairs_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); pairs_layout.addWidget(self.available_pairs_list)
        buttons_layout = QHBoxLayout(); add_button = QPushButton(">"); add_button.clicked.connect(self.add_to_watchlist); remove_button = QPushButton("<"); remove_button.clicked.connect(self.remove_from_watchlist); buttons_layout.addStretch(); buttons_layout.addWidget(add_button); buttons_layout.addWidget(remove_button); buttons_layout.addStretch(); pairs_layout.addLayout(buttons_layout)
        pairs_layout.addWidget(QLabel("Obserwowane Pary:")); self.watchlist = QListWidget(); pairs_layout.addWidget(self.watchlist)
        controls_layout.addWidget(pairs_group)

        self.start_button = QPushButton("Start Stream"); self.stop_button = QPushButton("Stop Stream"); self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_stream); self.stop_button.clicked.connect(self.stop_stream)
        controls_layout.addWidget(self.start_button); controls_layout.addWidget(self.stop_button); controls_layout.addStretch()
        self.main_layout.addWidget(controls_widget)

    def setup_order_book_panel(self):
        orderbook_widget = QWidget(); orderbook_widget.setFixedWidth(800); ob_layout = QVBoxLayout(orderbook_widget)
        tables_layout = QHBoxLayout()
        bids_container = QWidget(); bids_layout = QVBoxLayout(bids_container); self.bids_table = QTableWidget(); bids_layout.addWidget(QLabel("Kupno (Bids)")); bids_layout.addWidget(self.bids_table)
        asks_container = QWidget(); asks_layout = QVBoxLayout(asks_container); self.asks_table = QTableWidget(); asks_layout.addWidget(QLabel("Sprzedaż (Asks)")); asks_layout.addWidget(self.asks_table)
        self.setup_orderbook_table(self.bids_table, QColor(230, 255, 230)); self.setup_orderbook_table(self.asks_table, QColor(255, 230, 230))
        tables_layout.addWidget(bids_container); tables_layout.addWidget(asks_container); ob_layout.addLayout(tables_layout)
        self.main_layout.addWidget(orderbook_widget)

    def setup_orderbook_table(self, table, color):
        table.setColumnCount(4); table.setHorizontalHeaderLabels(["Ilość", "Cena", "Wartość (USDT)", "Giełda"])
        table.verticalHeader().setVisible(False); table.setRowCount(50)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = table.horizontalHeader(); header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def setup_plots(self):
        plots_widget = QWidget(); plots_layout = QVBoxLayout(plots_widget)
        self.price_plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()}); self.price_plot_widget.getPlotItem().setLabel('left', "Cena"); self.price_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.candlestick_item = CandlestickItem(); self.price_plot_widget.addItem(self.candlestick_item)
        self.cvd_plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()}); self.cvd_plot_widget.getPlotItem().setLabel('left', "Delta"); self.cvd_plot_widget.showGrid(x=True, y=True, alpha=0.3); self.cvd_plot_widget.setXLink(self.price_plot_widget)
        self.cvd_plot_line = self.cvd_plot_widget.plot(pen=pg.mkPen('g', width=2)); self.delta_bar_item = pg.BarGraphItem(x=[], height=[], width=0.8, brushes=[]); self.cvd_plot_widget.addItem(self.delta_bar_item)
        plots_layout.addWidget(self.price_plot_widget, stretch=3); plots_layout.addWidget(self.cvd_plot_widget, stretch=1)
        self.main_layout.addWidget(plots_widget, 1)

    def on_settings_changed(self):
        self.candle_data.clear(); self.current_candle = {}
        self.redraw_plots(); self.save_settings()

    def on_exchange_changed(self):
        self.save_settings(); self.watchlist.clear(); self.available_pairs_list.clear(); self.trigger_fetch_markets()

    def set_controls_enabled(self, enabled):
        for w in [self.exchange_combo, self.resample_combo, self.num_candles_spinbox, self.delta_mode_combo, self.aggregation_combo, self.ob_source_combo, self.available_pairs_list, self.watchlist]:
            w.setEnabled(enabled)
        self.start_button.setEnabled(enabled); self.stop_button.setEnabled(not enabled)

    def start_stream(self):
        if self.active_workers: QMessageBox.warning(self, "Stream aktywny", "Stream już działa."); return
        current_item = self.watchlist.currentItem()
        if not current_item: QMessageBox.warning(self, "Brak pary", "Wybierz parę z listy obserwowanych."); return
        self.save_settings(); market_data = current_item.data(Qt.ItemDataRole.UserRole); pair_symbol = market_data['symbol']
        self.reset_data_structures(); self.redraw_plots()
        selected_exchange_name = self.exchange_combo.currentText()
        if self.ob_source_combo.currentText() == "Wybrana giełda":
            exchange_id = self.exchange_options[selected_exchange_name]['id_ccxt']; market_type = self.exchange_options[selected_exchange_name]['type']
            worker = AsyncioWorker(exchange_id, pair_symbol, market_type, self.data_queue); self.active_workers[exchange_id] = worker; worker.start()
        else:
            for ex_name, ex_data in self.exchange_options.items():
                if ex_data['id_ccxt'] in ['binance', 'binanceusdm', 'bybit']:
                    worker = AsyncioWorker(ex_data['id_ccxt'], pair_symbol, ex_data['type'], self.data_queue); self.active_workers[ex_data['id_ccxt']] = worker; worker.start()
        if not self.active_workers: QMessageBox.warning(self, "Brak streamów", "Brak wspieranych giełd do streamowania."); return
        self.update_timer.start(200); self.set_controls_enabled(False)

    def stop_stream(self):
        for worker in self.active_workers.values():
            if worker and worker.is_alive(): worker.stop()
        self.active_workers.clear(); self.update_timer.stop(); self.set_controls_enabled(True)

    def process_queue(self):
        processed_count = 0; has_trades = False
        while not self.data_queue.empty() and processed_count < 200:
            try: data = self.data_queue.get_nowait()
            except Exception: break
            processed_count += 1
            if 'trades' in data: self.process_trades(data['trades']); has_trades = True
            if 'orderbook' in data: self.current_order_books[data['exchange_id']] = data['orderbook']; self.aggregate_and_update_display()
            if 'error' in data: QMessageBox.critical(self, "Błąd Streamu", data['error']); self.stop_stream()
        if has_trades and time.time() - self.last_plot_update_time >= self.plot_update_interval_ms / 1000.0:
            self.redraw_plots(); self.last_plot_update_time = time.time()

    def process_trades(self, trades):
        interval = self.RESAMPLE_MAP.get(self.resample_combo.currentText(), 1)
        for trade in trades:
            ts = trade['timestamp'] / 1000.0; price = trade['price']; amount = trade['amount']
            self.cumulative_delta += amount if trade['side'] == 'buy' else -amount
            self.cvd_data.append({'x': ts, 'y': self.cumulative_delta})
            candle_ts = int(ts / interval) * interval
            if not self.current_candle or candle_ts != self.current_candle.get('x'):
                if self.current_candle: self.candle_data.append(self.current_candle)
                self.current_candle = {'x': candle_ts, 'open': price, 'high': price, 'low': price, 'close': price, 'delta': (amount if trade['side'] == 'buy' else -amount), 'interval_seconds': interval}
            else:
                self.current_candle.update({'high': max(self.current_candle['high'], price), 'low': min(self.current_candle['low'], price), 'close': price, 'delta': self.current_candle['delta'] + (amount if trade['side'] == 'buy' else -amount)})

    def aggregate_and_update_display(self):
        agg_level_str = self.aggregation_combo.currentText(); agg_level = 0.0 if agg_level_str == "Brak" else float(agg_level_str); bids_map, asks_map = {}, {}
        for ex_id, book in self.current_order_books.items():
            for price, amount in book.get('bids', []):
                agg_price = round(np.floor(price / agg_level) * agg_level, self.get_price_precision()) if agg_level > 0 else price
                if agg_price not in bids_map: bids_map[agg_price] = {'amount': 0, 'exchanges': {}}; bids_map[agg_price]['amount'] += amount; bids_map[agg_price]['exchanges'][ex_id] = bids_map[agg_price]['exchanges'].get(ex_id, 0) + amount
            for price, amount in book.get('asks', []):
                agg_price = round(np.ceil(price / agg_level) * agg_level, self.get_price_precision()) if agg_level > 0 else price
                if agg_price not in asks_map: asks_map[agg_price] = {'amount': 0, 'exchanges': {}}; asks_map[agg_price]['amount'] += amount; asks_map[agg_price]['exchanges'][ex_id] = asks_map[agg_price]['exchanges'].get(ex_id, 0) + amount
        bids_data = sorted([(v['amount'], k, k * v['amount'], max(v['exchanges'], key=v['exchanges'].get)) for k, v in bids_map.items()], key=lambda x: x[1], reverse=True)
        asks_data = sorted([(v['amount'], k, k * v['amount'], max(v['exchanges'], key=v['exchanges'].get)) for k, v in asks_map.items()], key=lambda x: x[1])
        self.update_book_table(self.bids_table, bids_data); self.update_book_table(self.asks_table, asks_data)

    def update_book_table(self, table, data):
        table.setSortingEnabled(False); max_rows = table.rowCount(); visible_data = data[:max_rows]; max_val = max(row[2] for row in visible_data) if visible_data else 1.0; prec = self.get_price_precision()
        for i in range(max_rows):
            if i < len(visible_data):
                amount, price, value, ex_id = visible_data[i]
                items = [QTableWidgetItem(f"{amount:.4f}"), QTableWidgetItem(f"{price:.{prec}f}"), QTableWidgetItem(f"{value:,.2f}"), QTableWidgetItem(ex_id)]
                base_color = QColor(250,250,250); strong_color = QColor(255,160,160) if table is self.asks_table else QColor(160,255,160); strength = min((value / max_val), 1.0)
                r = int(base_color.red()*(1-strength)+strong_color.red()*strength); g = int(base_color.green()*(1-strength)+strong_color.green()*strength); b = int(base_color.blue()*(1-strength)+strong_color.blue()*strength)
                bg_color = QColor(r,g,b)
                for j, item in enumerate(items):
                    item.setFont(self.table_font); item.setBackground(bg_color); table.setItem(i, j, item)
            else:
                for j in range(table.columnCount()): table.setItem(i, j, QTableWidgetItem(""))
        table.setSortingEnabled(True)

    def redraw_plots(self):
        full_candle_data = list(self.candle_data) + ([self.current_candle] if self.current_candle else [])
        if not full_candle_data:
            self.candlestick_item.setData([]); self.delta_bar_item.setOpts(x=[], height=[]); self.cvd_plot_line.setData([], [])
            return

        self.candlestick_item.setData(full_candle_data)
        x_data = np.array([d['x'] for d in full_candle_data])
        num_candles_to_show = self.num_candles_spinbox.value()
        interval_seconds = self.RESAMPLE_MAP.get(self.resample_combo.currentText(), 1)
        visible_range_seconds = num_candles_to_show * interval_seconds
        x_max_current = x_data[-1]
        x_min_current = x_max_current - visible_range_seconds

        self.price_plot_widget.setXRange(x_min_current, x_max_current, padding=0)
        self.cvd_plot_widget.setXRange(x_min_current, x_max_current, padding=0)

        if self.delta_mode_combo.currentText() == "CVD (Skumulowana Delta)":
            self.delta_bar_item.hide(); self.cvd_plot_line.show()
            if self.cvd_data: self.cvd_plot_line.setData([d['x'] for d in self.cvd_data], [d['y'] for d in self.cvd_data])
        else:
            self.delta_bar_item.show(); self.cvd_plot_line.hide()
            width = interval_seconds * 0.8
            brushes = ['g' if d.get('delta',0)>0 else 'r' for d in full_candle_data]
            self.delta_bar_item.setOpts(x=[d['x'] for d in full_candle_data], height=[d.get('delta',0) for d in full_candle_data], width=width, brushes=brushes)

    def closeEvent(self, event):
        self.save_settings(); self.stop_stream(); super().closeEvent(event)

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return
        self.exchange_combo.setEnabled(False)
        config = self.exchange_options[self.exchange_combo.currentText()]
        self.fetch_markets_thread = utils.FetchMarketsThread(config['id_ccxt'], config['type'], self)
        self.fetch_markets_thread.markets_fetched.connect(self.populate_available_pairs)
        self.fetch_markets_thread.error_occurred.connect(lambda e: QMessageBox.critical(self, "Błąd", e))
        self.fetch_markets_thread.finished.connect(self.on_fetch_markets_finished)
        self.fetch_markets_thread.start()

    def on_fetch_markets_finished(self):
        self.exchange_combo.setEnabled(True)

    def populate_available_pairs(self, markets):
        self.available_pairs_list.clear(); [self.available_pairs_list.addItem(self.create_list_item(m)) for m in markets]
        self._apply_pending_watchlist()

    def add_to_watchlist(self):
        for item in self.available_pairs_list.selectedItems():
            if not self.watchlist.findItems(item.text(), Qt.MatchFlag.MatchExactly):
                self.watchlist.addItem(self.create_list_item(item.data(Qt.ItemDataRole.UserRole)))
        self.save_settings()

    def remove_from_watchlist(self):
        for item in self.watchlist.selectedItems():
            self.watchlist.takeItem(self.watchlist.row(item))
        self.save_settings()

    def create_list_item(self, market_data):
        item = QListWidgetItem(market_data['symbol']); item.setData(Qt.ItemDataRole.UserRole, market_data); return item

    def load_settings(self):
        try:
            self.config.read(self.config_path)
            if not self.config.has_section("order_flow_settings"):
                self.trigger_fetch_markets(); return

            s = self.config["order_flow_settings"]

            widgets_to_block = [self.exchange_combo, self.resample_combo, self.num_candles_spinbox, self.delta_mode_combo, self.aggregation_combo, self.ob_source_combo]
            for w in widgets_to_block: w.blockSignals(True)

            self.exchange_combo.setCurrentText(s.get("exchange", self.exchange_combo.itemText(0)))
            self.resample_combo.setCurrentText(s.get("resample_interval", "1s"))
            self.num_candles_spinbox.setValue(s.getint("num_candles", 100))
            self.delta_mode_combo.setCurrentText(s.get("delta_mode", "CVD (Skumulowana Delta)"))
            self.aggregation_combo.setCurrentText(s.get("aggregation", "Brak"))
            self.ob_source_combo.setCurrentText(s.get("ob_source", "Wybrana giełda"))

            for w in widgets_to_block: w.blockSignals(False)

            self.pending_watchlist_symbols = {p.strip() for p in s.get("watchlist", "").split(',') if p.strip()}
            self.pending_selected_pair = s.get("last_selected_pair", None)

            self.trigger_fetch_markets()
            self.watchlist.model().rowsInserted.connect(self.save_settings)
            self.watchlist.model().rowsRemoved.connect(self.save_settings)

        except Exception as e:
            print(f"Błąd wczytywania ustawień: {e}")
            self.trigger_fetch_markets()

    def _apply_pending_watchlist(self):
        if not self.pending_watchlist_symbols: return
        available_map = {self.available_pairs_list.item(i).text(): self.available_pairs_list.item(i) for i in range(self.available_pairs_list.count())}
        for symbol in self.pending_watchlist_symbols:
            if symbol in available_map:
                if not self.watchlist.findItems(symbol, Qt.MatchFlag.MatchExactly):
                    self.watchlist.addItem(self.create_list_item(available_map[symbol].data(Qt.ItemDataRole.UserRole)))

        if self.pending_selected_pair:
            items = self.watchlist.findItems(self.pending_selected_pair, Qt.MatchFlag.MatchExactly)
            if items: self.watchlist.setCurrentItem(items[0])
        self.pending_watchlist_symbols.clear(); self.pending_selected_pair = None

    def save_settings(self):
        if not self.config.has_section("order_flow_settings"):
            self.config.add_section("order_flow_settings")
        s = self.config["order_flow_settings"]
        s["exchange"] = self.exchange_combo.currentText()
        s["resample_interval"] = self.resample_combo.currentText()
        s["num_candles"] = str(self.num_candles_spinbox.value())
        s["delta_mode"] = self.delta_mode_combo.currentText()
        s["aggregation"] = self.aggregation_combo.currentText()
        s["ob_source"] = self.ob_source_combo.currentText()
        s["watchlist"] = ",".join([self.watchlist.item(i).text() for i in range(self.watchlist.count())])
        current_item = self.watchlist.currentItem()
        s["last_selected_pair"] = current_item.text() if current_item else ""

        try:
            with open(self.config_path, 'w') as configfile:
                self.config.write(configfile)
        except Exception as e:
            print(f"Błąd zapisu ustawień: {e}")
