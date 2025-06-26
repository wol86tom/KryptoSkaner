import sys
import asyncio
import ccxt
import ccxt.pro as ccxtpro
import pyqtgraph as pg
import pandas as pd
import numpy as np
import datetime
import time
from queue import Queue
from threading import Thread
from collections import deque
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QLabel, QHBoxLayout,
                             QApplication, QGroupBox, QFormLayout, QComboBox,
                             QLineEdit, QPushButton, QMessageBox, QCheckBox, QListWidget, QListWidgetItem,
                             QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QEvent, QPointF, QRectF
from PyQt6.QtGui import QFont, QPainter, QPen, QColor

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
        if not self.data: p.end(); return

        resample_map = {"1s":1, "5s":5, "15s":15, "1Min":60, "5Min":300, "15Min":900, "1H":3600}
        interval_str = self.data[0].get('interval', '1s') if self.data else '1s'
        interval_seconds = resample_map.get(interval_str, 1)
        w = interval_seconds * 0.4

        for d in self.data:
            t, o, h, l, c = d['x'], d['open'], d['high'], d['low'], d['close']
            p.setPen(pg.mkPen('k'))
            if o == c:
                p.drawLine(QPointF(t - w, o), QPointF(t + w, c))
            else:
                p.drawLine(QPointF(t, l), QPointF(t, h))
                p.setBrush(pg.mkBrush('g' if o < c else 'r'))
                p.drawRect(QRectF(t - w, o, w * 2, c - o))
        p.end()
    def paint(self, p, *args): p.drawPicture(0, 0, self.picture)
    def boundingRect(self):
        if not self.data: return QRectF()
        x_values = [d['x'] for d in self.data]
        y_lows = [d['low'] for d in self.data]
        y_highs = [d['high'] for d in self.data]

        x_min = min(x_values)
        x_max = max(x_values)
        y_min = np.min(y_lows)
        y_max = np.max(y_highs)

        interval_str = self.data[0].get('interval', '1s') if self.data else '1s'
        resample_map = {"1s":1, "5s":5, "15s":15, "1Min":60, "5Min":300, "15Min":900, "1H":3600}
        interval_seconds = resample_map.get(interval_str, 1)
        w = interval_seconds * 0.4

        return QRectF(x_min - w, y_min, (x_max - x_min) + 2 * w, y_max - y_min)


class AsyncioWorker(Thread):
    def __init__(self, exchange_id, pair_symbol, pair_id, pair_type, data_queue):
        super().__init__()
        self.daemon = True
        self.exchange_id = exchange_id
        self.pair_symbol = pair_symbol
        self.pair_id = pair_id
        self.pair_type = pair_type
        self.data_queue = data_queue
        self._is_running = False

    def run(self):
        self._is_running = True
        try:
            asyncio.run(self.main_loop())
        except Exception as e:
            self.data_queue.put({'error': str(e)})

    async def main_loop(self):
        exchange = getattr(ccxtpro, self.exchange_id)()
        print(f"[ASYNCIO]: Uruchamianie pętli dla {self.exchange_id} ({self.pair_type}) - {self.pair_symbol} (ID: {self.pair_id})...")

        await asyncio.gather(
            self.watch_trades_loop(exchange, self.pair_symbol),
            self.watch_orderbook_loop(exchange, self.pair_symbol)
        )
        await exchange.close()
        print(f"[ASYNCIO]: Połączenie WebSocket dla {self.exchange_id} - {self.pair_symbol} zamknięte.")

    async def watch_trades_loop(self, exchange, symbol_to_watch):
        while self._is_running:
            try:
                trades = await exchange.watch_trades(symbol_to_watch)
                if self._is_running and trades:
                    self.data_queue.put({'exchange_id': self.exchange_id, 'trades': trades})
            except Exception as e:
                self.data_queue.put({'error': f"Błąd (trades) z {self.exchange_id} ({self.pair_symbol}): {e}"})
                await asyncio.sleep(5)

    async def watch_orderbook_loop(self, exchange, symbol_to_watch):
        while self._is_running:
            try:
                orderbook = await exchange.watch_order_book(symbol_to_watch)
                if self._is_running and orderbook:
                    self.data_queue.put({'exchange_id': self.exchange_id, 'orderbook': orderbook})
            except Exception as e:
                self.data_queue.put({'error': f"Błąd (orderbook) z {self.exchange_id} ({self.pair_symbol}): {e}"})
                await asyncio.sleep(5)

    def stop(self):
        self._is_running = False

class FetchMarketsThread(QThread):
    markets_fetched = Signal(list)
    error_occurred = Signal(str)

    def __init__(self, exchange_id, market_type, parent=None):
        super().__init__(parent)
        self.exchange_id = exchange_id
        self.market_type_filter = market_type

    def run(self):
        print(f"[DEBUG_MARKET_FETCH]: Rozpoczynam pobieranie rynków dla giełdy: {self.exchange_id}, typ: {self.market_type_filter}")
        try:
            exchange = getattr(ccxt, self.exchange_id)()
            markets = exchange.load_markets()
            print(f"[DEBUG_MARKET_FETCH]: Pobrane rynki z {self.exchange_id}. Liczba rynków: {len(markets)}")

            available_market_data = []
            for m in markets.values():
                if self.type_matches(m):
                    available_market_data.append({
                        'symbol': m['symbol'],
                        'id': m['id'],
                        'type': m['type']
                    })

            print(f"[DEBUG_MARKET_FETCH]: Znaleziono {len(available_market_data)} pasujących par dla {self.exchange_id}.")
            self.markets_fetched.emit(sorted(available_market_data, key=lambda x: x['symbol']))
        except Exception as e:
            print(f"[DEBUG_MARKET_FETCH]: WYSTĄPIŁ BŁĄD podczas pobierania rynków z {self.exchange_id}: {e}")
            self.error_occurred.emit(str(e))

    def type_matches(self, market):
        if not (market.get('active') and market.get('quote') == 'USDT'):
            return False

        market_type = market.get('type')

        if self.market_type_filter == market_type:
            return True

        if self.exchange_id == 'binanceusdm' and self.market_type_filter == 'future':
            if market.get('linear') and market_type in ['future', 'swap']:
                return True

        if self.exchange_id == 'bybit' and self.market_type_filter == 'swap' and market_type == 'swap':
            return True

        return False

class OrderFlowWindow(QMainWindow):
    def __init__(self, exchange_options, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options
        self.setWindowTitle("Analiza Order Flow")
        self.setGeometry(200, 200, 1600, 900)

        self.worker_thread = None
        self.active_workers = {}
        self.fetch_markets_thread = None
        self.data_queue = Queue()

        self.current_order_books = {}

        self.table_font = QFont()
        self.table_font.setPointSize(9)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)

        self.setup_ui()

        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.process_queue)

        self.reset_data_structures()
        self.trigger_fetch_markets()

        self.last_plot_update_time = time.time()
        self.plot_update_interval_ms = 1000

    def get_price_precision(self):
        """
        Zwraca liczbę miejsc po przecinku dla wyświetlania ceny w tabeli Order Booka,
        w zależności od wybranego poziomu agregacji.
        """
        aggregation_level_str = self.aggregation_combo.currentText()
        if aggregation_level_str == "Brak":
            return 8 # Domyślna precyzja dla 'Brak' (można dostosować)
        try:
            # Oblicz liczbę miejsc po przecinku na podstawie logarytmu
            # np. 0.01 -> 2, 0.001 -> 3, 0.0001 -> 4
            # Używamy max(0, ...) aby zapewnić nieujemną precyzję
            return int(max(0, -np.log10(float(aggregation_level_str))))
        except (ValueError, TypeError): # Obsłuż błędy konwersji, jeśli level nie jest liczbą
            return 8

    def reset_data_structures(self):
        self.cumulative_delta = 0
        self.current_candle = {}

        self.cvd_data = deque(maxlen=5000)
        self.candle_data = deque(maxlen=1000)

        self.current_order_books = {}
        self.active_workers = {}

    def setup_ui(self):
        self.setup_controls_panel()
        self.setup_order_book_panel()
        self.setup_plots()

    def setup_controls_panel(self):
        controls_widget = QWidget()
        controls_widget.setFixedWidth(350)
        controls_layout = QVBoxLayout(controls_widget)

        exchange_group = QGroupBox("Sterowanie")
        form_layout = QFormLayout(exchange_group)

        self.exchange_combo = QComboBox()
        if self.exchange_options:
            supported = ['binance', 'binanceusdm', 'bybit']
            self.exchange_combo.addItems([name for name, data in self.exchange_options.items() if data.get('id_ccxt') in supported])
        self.exchange_combo.currentTextChanged.connect(self.trigger_fetch_markets)
        form_layout.addRow("Giełda:", self.exchange_combo)

        self.resample_combo = QComboBox()
        self.resample_combo.addItems(["1s", "5s", "15s", "1Min", "5Min", "15Min"])
        self.resample_combo.setCurrentText("1s")
        form_layout.addRow("Interwał świecy:", self.resample_combo)

        self.delta_mode_combo = QComboBox()
        self.delta_mode_combo.addItems(["CVD (Skumulowana Delta)", "Delta na Świecę"])
        self.delta_mode_combo.currentTextChanged.connect(self.redraw_plots)
        form_layout.addRow("Tryb Delty:", self.delta_mode_combo)

        self.aggregation_combo = QComboBox()
        self.aggregation_combo.addItems([
            "Brak",
            "0.0000001",
            "0.000001",
            "0.00001",
            "0.0001",
            "0.001",
            "0.01",
            "0.05",
            "0.1",
            "0.5",
            "1.0",
            "5.0",
            "10.0",
            "50.0",
            "100.0"
        ])
        self.aggregation_combo.setCurrentText("Brak")
        self.aggregation_combo.currentTextChanged.connect(self.on_aggregation_changed)
        form_layout.addRow("Agregacja OB:", self.aggregation_combo)

        self.ob_source_combo = QComboBox()
        self.ob_source_combo.addItems(["Wybrana giełda", "Wszystkie aktywne giełdy"])
        self.ob_source_combo.setCurrentText("Wybrana giełda")
        self.ob_source_combo.currentTextChanged.connect(self.on_ob_source_changed)
        form_layout.addRow("Źródło OB:", self.ob_source_combo)


        controls_layout.addWidget(exchange_group)

        pairs_group = QGroupBox("Zarządzanie Parami")
        pairs_layout = QVBoxLayout(pairs_group)

        pairs_layout.addWidget(QLabel("Dostępne Pary:"))
        self.available_pairs_list = QListWidget()
        self.available_pairs_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        pairs_layout.addWidget(self.available_pairs_list)

        buttons_layout = QHBoxLayout()
        add_button = QPushButton(">")
        add_button.clicked.connect(self.add_to_watchlist)
        remove_button = QPushButton("<")
        remove_button.clicked.connect(self.remove_from_watchlist_ui_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(add_button)
        buttons_layout.addWidget(remove_button)
        buttons_layout.addStretch()
        pairs_layout.addLayout(buttons_layout)

        pairs_layout.addWidget(QLabel("Obserwowane Pary:"))
        self.watchlist = QListWidget()
        pairs_layout.addWidget(self.watchlist)

        self.refresh_markets_button = QPushButton("Odśwież Listę Par")
        self.refresh_markets_button.clicked.connect(self.trigger_fetch_markets)
        pairs_layout.addWidget(self.refresh_markets_button)

        controls_layout.addWidget(pairs_group)

        self.start_button = QPushButton("Start Stream")
        self.stop_button = QPushButton("Stop Stream")
        self.stop_button.setEnabled(False)

        self.start_button.clicked.connect(self.start_stream)
        self.stop_button.clicked.connect(self.stop_stream)

        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addStretch()

        self.main_layout.addWidget(controls_widget)

    def setup_order_book_panel(self):
        orderbook_widget = QWidget()
        # Zwiększamy szerokość panelu order booka
        orderbook_widget.setFixedWidth(800) # Zmieniono z 650 na 800
        ob_layout = QVBoxLayout(orderbook_widget)
        ob_layout.setContentsMargins(0, 0, 0, 0)
        ob_layout.setSpacing(1)

        tables_layout = QHBoxLayout()
        tables_layout.setSpacing(1)

        bids_container = QWidget()
        bids_layout = QVBoxLayout(bids_container)
        bids_layout.setContentsMargins(0,0,0,0)
        self.bids_table = QTableWidget()
        bids_layout.addWidget(QLabel("Kupno (Bids)"))
        bids_layout.addWidget(self.bids_table)

        asks_container = QWidget()
        asks_layout = QVBoxLayout(asks_container)
        asks_layout.setContentsMargins(0,0,0,0)
        self.asks_table = QTableWidget()
        asks_layout.addWidget(QLabel("Sprzedaż (Asks)"))
        asks_layout.addWidget(self.asks_table)

        # Ustawienie tabel order booka - teraz 4 kolumny!
        self.setup_orderbook_table(self.bids_table, QColor(230, 255, 230))
        self.setup_orderbook_table(self.asks_table, QColor(255, 230, 230))

        tables_layout.addWidget(bids_container)
        tables_layout.addWidget(asks_container)
        ob_layout.addLayout(tables_layout)

        self.main_layout.addWidget(orderbook_widget)

    def setup_orderbook_table(self, table, color):
        table.setColumnCount(4) # Zmieniono na 4 kolumny
        table.setHorizontalHeaderLabels(["Ilość", "Cena", "Wartość (USDT)", "Giełda"]) # Nowy nagłówek
        table.verticalHeader().setVisible(False)
        table.setRowCount(50)
        table.setStyleSheet(f"QTableWidget {{ background-color: {color.name()}; gridline-color: #d0d0d0; }}"
                            f"QHeaderView::section {{ background-color: #f0f0f0; }}")

        header = table.horizontalHeader()
        # Pierwsze 3 kolumny dopasowują się do zawartości
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        # Czwarta kolumna (Giełda) rozciąga się, aby wypełnić pozostałą przestrzeń
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def setup_plots(self):
        plots_widget = QWidget()
        plots_layout = QVBoxLayout(plots_widget)

        self.price_plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()})
        self.price_plot_widget.getPlotItem().setLabel('left', "Cena")
        self.price_plot_widget.showGrid(x=True, y=True, alpha=0.3)

        self.candlestick_item = CandlestickItem()
        self.price_plot_widget.addItem(self.candlestick_item)

        self.cvd_plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()})
        self.cvd_plot_widget.getPlotItem().setLabel('left', "Delta")
        self.cvd_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.cvd_plot_widget.setXLink(self.price_plot_widget)

        self.cvd_plot_line = self.cvd_plot_widget.plot(pen=pg.mkPen('g', width=2))
        self.delta_bar_item = pg.BarGraphItem(x=[], height=[], width=0.8, brushes=[])
        self.cvd_plot_widget.addItem(self.delta_bar_item)

        plots_layout.addWidget(self.price_plot_widget, stretch=3)
        plots_layout.addWidget(self.cvd_plot_widget, stretch=1)
        self.main_layout.addWidget(plots_widget, 1)

    def start_stream(self):
        if self.active_workers:
            QMessageBox.warning(self, "Stream aktywny", "Stream już działa.")
            return

        current_item = self.watchlist.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Brak pary", "Najpierw dodaj parę do listy obserwowanych i ją zaznacz."); return

        market_data = current_item.data(Qt.ItemDataRole.UserRole)
        pair_symbol = market_data['symbol']
        pair_id = market_data['id']
        pair_type = market_data['type']

        self.reset_data_structures()

        self.price_plot_widget.clear()
        self.cvd_plot_widget.clear()
        self.asks_table.clearContents()
        self.bids_table.clearContents()

        self.candlestick_item = CandlestickItem()
        self.price_plot_widget.addItem(self.candlestick_item)

        self.cvd_plot_line = self.cvd_plot_widget.plot(pen=pg.mkPen('g', width=2))
        self.delta_bar_item = pg.BarGraphItem(x=[], height=[], width=0.8, brushes=[])
        self.cvd_plot_widget.addItem(self.delta_bar_item)


        selected_ob_source = self.ob_source_combo.currentText()

        if selected_ob_source == "Wybrana giełda":
            selected_exchange_id = self.exchange_options[self.exchange_combo.currentText()]['id_ccxt']
            worker = AsyncioWorker(selected_exchange_id, pair_symbol, pair_id, pair_type, self.data_queue)
            self.active_workers[selected_exchange_id] = worker
            worker.start()
        elif selected_ob_source == "Wszystkie aktywne giełdy":
            self.current_order_books = {}
            for ex_name, ex_data in self.exchange_options.items():
                exchange_id = ex_data['id_ccxt']
                if ex_data.get('id_ccxt') in ['binance', 'binanceusdm', 'bybit']:
                     worker = AsyncioWorker(exchange_id, pair_symbol, pair_id, pair_type, self.data_queue)
                     self.active_workers[exchange_id] = worker
                     worker.start()
                else:
                    print(f"Giełda {ex_name} ({exchange_id}) nie jest aktualnie wspierana dla streamowania.")

            if not self.active_workers:
                QMessageBox.warning(self, "Brak streamów", "Brak aktywnych giełd do streamowania dla tej pary. Sprawdź konfigurację giełd i listę obserwowanych par.")
                return


        self.update_timer.start(500)

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.exchange_combo.setEnabled(False)
        self.resample_combo.setEnabled(False)
        self.delta_mode_combo.setEnabled(False)
        self.aggregation_combo.setEnabled(False)
        self.ob_source_combo.setEnabled(False)
        self.available_pairs_list.setEnabled(False)
        self.watchlist.setEnabled(False)
        self.refresh_markets_button.setEnabled(False)


    def stop_stream(self):
        for exchange_id, worker in self.active_workers.items():
            if worker and worker.is_alive():
                worker.stop()
        self.active_workers = {}

        self.update_timer.stop()

        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.exchange_combo.setEnabled(True)
        self.resample_combo.setEnabled(True)
        self.delta_mode_combo.setEnabled(True)
        self.aggregation_combo.setEnabled(True)
        self.ob_source_combo.setEnabled(True)
        self.available_pairs_list.setEnabled(True)
        self.watchlist.setEnabled(True)
        self.refresh_markets_button.setEnabled(True)

    def process_queue(self):
        start_time = time.time()
        has_updates = False
        processed_count = 0
        max_process_per_call = 50

        while not self.data_queue.empty() and processed_count < max_process_per_call:
            if (time.time() - start_time) > 0.1:
                break
            try:
                data = self.data_queue.get_nowait()
            except Exception:
                break

            has_updates = True
            processed_count += 1

            exchange_id = data.get('exchange_id')
            if exchange_id:
                if 'trades' in data:
                    self.process_trades(data['trades'])
                if 'orderbook' in data:
                    self.current_order_books[exchange_id] = data['orderbook']
                    self.aggregate_and_update_display()
            elif 'error' in data:
                QMessageBox.critical(self, "Błąd Streamu", data['error'])
                self.stop_stream()

        current_time = time.time()
        if (current_time - self.last_plot_update_time) * 1000 >= self.plot_update_interval_ms:
            self.redraw_plots()
            self.last_plot_update_time = current_time


    def process_trades(self, trades):
        resample_map = {"1s":1, "5s":5, "15s":15, "1Min":60, "5Min":300, "15Min":900, "1H":3600}
        interval = resample_map.get(self.resample_combo.currentText(), 1)

        for trade in trades:
            timestamp = trade['timestamp'] / 1000.0
            price = trade['price']
            amount = trade['amount']

            delta = amount if trade['side'] == 'buy' else -amount

            self.cumulative_delta += delta
            self.cvd_data.append({'x': timestamp, 'y': self.cumulative_delta})

            candle_start_ts = int(timestamp / interval) * interval

            if not self.current_candle or candle_start_ts != self.current_candle.get('x'):
                if self.current_candle:
                    self.candle_data.append(self.current_candle)
                self.current_candle = {
                    'x': candle_start_ts,
                    'open': price,
                    'high': price,
                    'low': price,
                    'close': price,
                    'delta': delta,
                    'interval': self.resample_combo.currentText()
                }
            else:
                self.current_candle['high'] = max(self.current_candle['high'], price)
                self.current_candle['low'] = min(self.current_candle['low'], price)
                self.current_candle['close'] = price
                self.current_candle['delta'] += delta

    def aggregate_and_update_display(self):
        selected_ob_source = self.ob_source_combo.currentText()
        aggregation_level_str = self.aggregation_combo.currentText()
        aggregation_level = 0.0
        if aggregation_level_str != "Brak":
            try:
                aggregation_level = float(aggregation_level_str)
            except ValueError:
                aggregation_level = 0.0

        # Finalne listy do wyświetlenia w tabelach
        final_bids_display = []
        final_asks_display = []

        if selected_ob_source == "Wybrana giełda":
            selected_exchange_id = self.exchange_options[self.exchange_combo.currentText()]['id_ccxt']
            book = self.current_order_books.get(selected_exchange_id)
            if book:
                # Agregacja bidów dla wybranej giełdy
                aggregated_bids_map = {}
                for price_str, amount_str in book.get('bids', []):
                    try:
                        price = float(price_str)
                        amount = float(amount_str)
                        agg_price = self.get_aggregated_price(price, aggregation_level, is_bid=True)
                        aggregated_bids_map[agg_price] = aggregated_bids_map.get(agg_price, 0.0) + amount
                    except ValueError: pass

                # Konwersja do formatu wyświetlania
                for agg_price, total_amount in aggregated_bids_map.items():
                    final_bids_display.append((total_amount, agg_price, agg_price * total_amount, selected_exchange_id))

                # Agregacja asków dla wybranej giełdy
                aggregated_asks_map = {}
                for price_str, amount_str in book.get('asks', []):
                    try:
                        price = float(price_str)
                        amount = float(amount_str)
                        agg_price = self.get_aggregated_price(price, aggregation_level, is_bid=False)
                        aggregated_asks_map[agg_price] = aggregated_asks_map.get(agg_price, 0.0) + amount
                    except ValueError: pass

                # Konwersja do formatu wyświetlania
                for agg_price, total_amount in aggregated_asks_map.items():
                    final_asks_display.append((total_amount, agg_price, agg_price * total_amount, selected_exchange_id))


        elif selected_ob_source == "Wszystkie aktywne giełdy":
            # Mapy do agregowania danych ze wszystkich giełd dla każdego poziomu cenowego
            # Format: {agg_price: total_amount}
            overall_aggregated_bids = {}
            overall_aggregated_asks = {}

            # Jeśli agregacja_level to "Brak", będziemy zbierać indywidualne zlecenia
            if aggregation_level == 0.0:
                # Format: [(amount, price, value, exchange_id), ...]
                all_individual_bids = []
                all_individual_asks = []

                for ex_id, book in self.current_order_books.items():
                    if book:
                        for price_str, amount_str in book.get('bids', []):
                            try:
                                price = float(price_str)
                                amount = float(amount_str)
                                all_individual_bids.append((amount, price, price * amount, ex_id))
                            except ValueError: pass
                        for price_str, amount_str in book.get('asks', []):
                            try:
                                price = float(price_str)
                                amount = float(amount_str)
                                all_individual_asks.append((amount, price, price * amount, ex_id))
                            except ValueError: pass

                # Sortuj indywidualne zlecenia (najpierw po cenie, potem po giełdzie dla stabilności)
                final_bids_display = sorted(all_individual_bids, key=lambda x: (x[1], x[3]), reverse=True)
                final_asks_display = sorted(all_individual_asks, key=lambda x: (x[1], x[3]))

            else: # Agregacja na poziomach (sumowanie z wielu giełd)
                for ex_id, book in self.current_order_books.items():
                    if book:
                        # Agreguj bidy dla aktualnej giełdy w pętli
                        for price_str, amount_str in book.get('bids', []):
                            try:
                                price = float(price_str)
                                amount = float(amount_str)
                                agg_price = self.get_aggregated_price(price, aggregation_level, is_bid=True)
                                overall_aggregated_bids[agg_price] = overall_aggregated_bids.get(agg_price, 0.0) + amount
                            except ValueError: pass
                        # Agreguj aski dla aktualnej giełdy w pętli
                        for price_str, amount_str in book.get('asks', []):
                            try:
                                price = float(price_str)
                                amount = float(amount_str)
                                agg_price = self.get_aggregated_price(price, aggregation_level, is_bid=False)
                                overall_aggregated_asks[agg_price] = overall_aggregated_asks.get(agg_price, 0.0) + amount
                            except ValueError: pass

                # Konwersja zagregowanych danych do formatu wyświetlania
                for agg_price, total_amount in overall_aggregated_bids.items():
                    final_bids_display.append((total_amount, agg_price, agg_price * total_amount, "Agregacja"))

                for agg_price, total_amount in overall_aggregated_asks.items():
                    final_asks_display.append((total_amount, agg_price, agg_price * total_amount, "Agregacja"))

                final_bids_display = sorted(final_bids_display, key=lambda x: x[1], reverse=True)
                final_asks_display = sorted(final_asks_display, key=lambda x: x[1])


        self.update_book_table(self.bids_table, final_bids_display)
        self.update_book_table(self.asks_table, final_asks_display)


    def get_aggregated_price(self, price, level, is_bid):
        if level == 0.0: return price
        if is_bid:
            agg_price = np.floor(price / level) * level
        else:
            agg_price = np.ceil(price / level) * level
        return round(agg_price, self.get_price_precision())


    def get_aggregation_precision_for_rounding(self, level):
        if level == 0: return 8
        try:
            return int(max(0, -np.log10(level)))
        except (ValueError, TypeError):
            return 8


    def on_aggregation_changed(self):
        self.asks_table.clearContents()
        self.bids_table.clearContents()
        self.aggregate_and_update_display()


    def on_ob_source_changed(self):
        if self.active_workers:
            QMessageBox.information(self, "Zmień źródło Order Booka",
                                    "Aby zmienić źródło Order Booka, proszę zatrzymać i ponownie uruchomić stream.")

        self.asks_table.clearContents()
        self.bids_table.clearContents()
        self.aggregate_and_update_display()


    def update_book_table(self, table, data):
        max_display_rows = table.rowCount()
        display_data = data[:max_display_rows]

        table.setSortingEnabled(False)
        table.setRowCount(max_display_rows) # Zapewnia stałą liczbę wierszy

        try:
            # Używamy price * amount, które jest już w display_data
            values = [row[0] * row[1] for row in display_data] # (amount * price)
            max_value = max(values) if values else 1.0
        except Exception:
            max_value = 1.0

        current_precision = self.get_price_precision()

        for row_idx, row_data in enumerate(display_data):
            amount = row_data[0]
            price = row_data[1]
            value = row_data[2]
            source_exchange = row_data[3] # Nowa kolumna

            item_amount = QTableWidgetItem(f"{amount:.4f}")
            item_price = QTableWidgetItem(f"{price:.{current_precision}f}")
            item_value = QTableWidgetItem(f"{value:,.2f}")
            item_source = QTableWidgetItem(source_exchange)

            item_price.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            item_value.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            item_source.setTextAlignment(Qt.AlignmentFlag.AlignCenter) # Wyrównaj giełdę do środka

            for item in [item_amount, item_price, item_value, item_source]:
                item.setFont(self.table_font)

            if max_value > 0:
                strength = min((value / max_value) * 1.5, 1.0)
                base_color = QColor(250,250,250)
                strong_color = QColor(255, 160, 160) if table is self.asks_table else QColor(160, 255, 160)

                r = int(base_color.red() * (1 - strength) + strong_color.red() * strength)
                g = int(base_color.green() * (1 - strength) + strong_color.green() * strength)
                b = int(base_color.blue() * (1 - strength) + strong_color.blue() * strength)

                bg_color = QColor(r, g, b)
                for item in [item_amount, item_price, item_value, item_source]:
                    item.setBackground(bg_color)

            table.setItem(row_idx, 0, item_amount)
            table.setItem(row_idx, 1, item_price)
            table.setItem(row_idx, 2, item_value)
            table.setItem(row_idx, 3, item_source)

        for row_idx in range(len(display_data), max_display_rows):
            for col_idx in range(table.columnCount()):
                table.setItem(row_idx, col_idx, QTableWidgetItem(""))

        table.setSortingEnabled(True)

    def redraw_plots(self):
        if not hasattr(self, 'price_plot_widget') or not self.price_plot_widget: return
        if not hasattr(self, 'cvd_plot_widget') or not self.cvd_plot_widget: return

        if not hasattr(self, 'candlestick_item') or self.candlestick_item.scene() is None:
            return
        if not hasattr(self, 'cvd_plot_line') or self.cvd_plot_line.scene() is None:
            return
        if not hasattr(self, 'delta_bar_item') or self.delta_bar_item.scene() is None:
            return

        full_candle_data = list(self.candle_data)
        if self.current_candle: full_candle_data.append(self.current_candle)

        if full_candle_data:
            self.candlestick_item.setData(full_candle_data)

            x_data = np.array([d['x'] for d in full_candle_data])
            if len(x_data) > 1:
                resample_map = {"1s":1, "5s":5, "15s":15, "1Min":60, "5Min":300, "15Min":900, "1H":3600}
                interval_str = self.resample_combo.currentText()
                interval_seconds = resample_map.get(interval_str, 1)

                visible_range_seconds = interval_seconds * 60

                x_max_current = x_data[-1]
                x_min_current = x_max_current - visible_range_seconds

                self.price_plot_widget.setXRange(x_min_current, x_max_current, padding=0.05)
                self.cvd_plot_widget.setXRange(x_min_current, x_max_current, padding=0.05)
            elif len(x_data) == 1:
                 self.price_plot_widget.setXRange(x_data[0] - 10, x_data[0] + 10)
                 self.cvd_plot_widget.setXRange(x_data[0] - 10, x_data[0] + 10)


            y_lows = np.array([d['low'] for d in full_candle_data])
            y_highs = np.array([d['high'] for d in full_candle_data])
            if len(y_lows) > 0 and len(y_highs) > 0:
                y_min_display = np.min(y_lows) * 0.99
                y_max_display = np.max(y_highs) * 1.01
                self.price_plot_widget.setYRange(y_min_display, y_max_display)


        if self.delta_mode_combo.currentText() == "CVD (Skumulowana Delta)":
            self.delta_bar_item.hide()
            self.cvd_plot_line.show()
            if self.cvd_data:
                cvd_list = list(self.cvd_data)
                x = [d['x'] for d in cvd_list]
                y = [d['y'] for d in cvd_list]
                self.cvd_plot_line.setData(x, y)
                if len(y) > 0:
                    self.cvd_plot_widget.setYRange(min(y) * 0.9, max(y) * 1.1)

        else:
            self.delta_bar_item.show()
            self.cvd_plot_line.hide()
            resample_map = {"1s":1, "5s":5, "15s":15, "1Min":60, "5Min":300, "15Min":900}; interval = resample_map.get(self.resample_combo.currentText(), 60)
            bar_width = interval * 0.8; brushes = [pg.mkBrush('g') if d.get('delta', 0) > 0 else pg.mkBrush('r') for d in full_candle_data]
            self.delta_bar_item.setOpts(x=[d['x'] for d in full_candle_data], height=[d.get('delta', 0) for d in full_candle_data], width=bar_width, brushes=brushes)
            if full_candle_data:
                deltas = [d.get('delta', 0) for d in full_candle_data]
                if len(deltas) > 0:
                    y_min_delta = min(deltas) * 1.2
                    y_max_delta = max(deltas) * 1.2
                    self.cvd_plot_widget.setYRange(y_min_delta, y_max_delta)

    def closeEvent(self, event):
        self.stop_stream(); super().closeEvent(event)

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return
        selected_config = self.exchange_options[self.exchange_combo.currentText()]; exchange_id = selected_config['id_ccxt']; market_type = selected_config['type']
        self.refresh_markets_button.setEnabled(False); self.refresh_markets_button.setText("Pobieranie...")
        self.fetch_markets_thread = FetchMarketsThread(exchange_id, market_type, self); self.fetch_markets_thread.markets_fetched.connect(self.populate_available_pairs); self.fetch_markets_thread.error_occurred.connect(lambda e: QMessageBox.critical(self, "Błąd pobierania par", e)); self.fetch_markets_thread.finished.connect(lambda: (self.refresh_markets_button.setEnabled(True), self.refresh_markets_button.setText("Odśwież Listę Par"))); self.fetch_markets_thread.start()
    def populate_available_pairs(self, markets_data):
        print(f"[DEBUG_POPULATE_PAIRS]: Otrzymano {len(markets_data)} rynków do populacji.")
        for i, market in enumerate(markets_data[:5]):
            print(f"  [DEBUG_POPULATE_PAIRS]: Rynek {i}: {market}")

        current_watchlist_items = {self.watchlist.item(i).data(Qt.ItemDataRole.UserRole)['symbol'] for i in range(self.watchlist.count()) if self.watchlist.item(i).data(Qt.ItemDataRole.UserRole)}
        self.available_pairs_list.clear()

        added_count = 0
        for market in markets_data:
            if market['symbol'] not in current_watchlist_items:
                item = QListWidgetItem(market['symbol'])
                item.setData(Qt.ItemDataRole.UserRole, market)
                self.available_pairs_list.addItem(item)
                added_count += 1
        print(f"[DEBUG_POPULATE_PAIRS]: Dodano {added_count} par do listy dostępnych.")

    def add_to_watchlist(self):
        selected_item = self.available_pairs_list.currentItem()
        if selected_item:
            market_data = selected_item.data(Qt.ItemDataRole.UserRole)
            if market_data:
                new_item = QListWidgetItem(market_data['symbol'])
                new_item.setData(Qt.ItemDataRole.UserRole, market_data)
                self.watchlist.addItem(new_item)
                self.available_pairs_list.takeItem(self.available_pairs_list.row(selected_item))
                print(f"[DEBUG_WATCHLIST]: Dodano do watchlist: {market_data['symbol']}")

    def remove_from_watchlist_ui_button(self):
        selected_item = self.watchlist.currentItem()
        if selected_item:
            row = self.watchlist.row(selected_item)
            item_to_remove = self.watchlist.takeItem(row)

            market_data = item_to_remove.data(Qt.ItemDataRole.UserRole)

            if market_data:
                is_already_available = False
                for i in range(self.available_pairs_list.count()):
                    item_data = self.available_pairs_list.item(i).data(Qt.ItemDataRole.UserRole)
                    if item_data and item_data.get('symbol') == market_data['symbol']:
                        is_already_available = True
                        break

                if not is_already_available:
                    new_item_available = QListWidgetItem(market_data['symbol'])
                    new_item_available.setData(Qt.ItemDataRole.UserRole, market_data)
                    self.available_pairs_list.addItem(new_item_available)
                print(f"[DEBUG_WATCHLIST]: Usunięto z watchlist: {market_data['symbol']}")


# Punkt wejścia aplikacji
if __name__ == '__main__':
    app_instance = QApplication(sys.argv)

    exchange_options_data = {
        'Binance Spot': {'id_ccxt': 'binance', 'type': 'spot'},
        'Binance Futures': {'id_ccxt': 'binanceusdm', 'type': 'future'},
        'Bybit Swap': {'id_ccxt': 'bybit', 'type': 'swap'},
    }

    main_window = OrderFlowWindow(exchange_options_data)
    main_window.show()
    sys.exit(app_instance.exec())
