# spike_detector_window.py
import time
import ccxt
import ccxt.pro as ccxtpro
import asyncio
from collections import deque
from datetime import datetime, timedelta

from PySide6.QtWidgets import (QMainWindow, QLabel, QWidget, QVBoxLayout,
                             QGroupBox, QFormLayout, QComboBox, QSpinBox,
                             QDoubleSpinBox, QPushButton, QHBoxLayout, QTextEdit,
                             QListWidget, QListWidgetItem, QGridLayout, QMessageBox,
                             QApplication)
from PySide6.QtCore import Qt, QThread, Signal

class FetchSpikeDetectorMarketsThread(QThread):
    markets_fetched_signal = Signal(list)
    error_signal = Signal(str)
    finished_signal = Signal()

    def __init__(self, exchange_id_ccxt, market_type_filter, parent=None):
        super().__init__(parent)
        self.exchange_id_ccxt = exchange_id_ccxt
        self.market_type_filter = market_type_filter

    def run(self):
        try:
            exchange = getattr(ccxt, self.exchange_id_ccxt)({'enableRateLimit': True})
            markets = exchange.load_markets()
            available_pairs = [symbol for symbol, market_data in markets.items() if market_data.get('active', False) and market_data.get('quote', '').upper() == 'USDT' and self.type_matches(market_data)]
            self.markets_fetched_signal.emit(sorted(list(set(available_pairs))))
        except Exception as e:
            self.error_signal.emit(f"Błąd pobierania par dla {self.exchange_id_ccxt}: {type(e).__name__} - {str(e)}")
        finally:
            self.finished_signal.emit()

    def type_matches(self, market):
        market_type = market.get('type')
        if self.market_type_filter == market_type: return True
        if self.exchange_id_ccxt == 'binance' and self.market_type_filter == 'future' and market.get('linear') and market_type in ['future', 'swap']: return True
        return False

class SpikeDetectorThread(QThread):
    spike_detected = Signal(str)
    log_message = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, exchange_id, pairs, params, parent=None):
        super().__init__(parent)
        self.exchange_id = exchange_id
        self.pairs = pairs
        self.params = params
        self.is_running = False
        self.exchange = None

        # Kolejka przechowuje dane z okresu bazowego + okna piku
        # np. 10 min + 60s = 660 sekund. Zakładamy ok. 5 transakcji na sekundę.
        maxlen = (self.params['baseline_minutes'] * 60 + self.params['time_window']) * 5
        self.trade_data = {pair: deque(maxlen=maxlen) for pair in self.pairs}
        self.alerted_pairs = {}

    def run(self):
        self.is_running = True
        try:
            asyncio.run(self.main_loop())
        except Exception as e:
            self.error_occurred.emit(f"Błąd krytyczny pętli asyncio: {e}")

    async def main_loop(self):
        self.log_message.emit(f"Inicjalizacja giełdy {self.exchange_id} dla WebSocket...")
        exchange_class = getattr(ccxtpro, self.exchange_id)
        self.exchange = exchange_class()

        tasks = [self.watch_pair(pair) for pair in self.pairs]
        self.log_message.emit(f"Rozpoczynanie nasłuchu dla {len(self.pairs)} par...")
        await asyncio.gather(*tasks)

        await self.exchange.close()
        self.log_message.emit("Połączenie WebSocket zamknięte.")

    async def watch_pair(self, pair):
        while self.is_running:
            try:
                trades = await self.exchange.watch_trades(pair)
                if self.is_running:
                    self.process_trades(pair, trades)
            except Exception as e:
                self.error_occurred.emit(f"Błąd dla {pair}: {str(e)}")
                await asyncio.sleep(5)

    def process_trades(self, pair, trades):
        if not trades: return

        current_timestamp_ms = time.time() * 1000

        for trade in trades:
            self.trade_data[pair].append(trade)

        spike_window_sec = self.params['time_window']
        baseline_window_sec = self.params['baseline_minutes'] * 60

        spike_window_start_ts = current_timestamp_ms - (spike_window_sec * 1000)
        baseline_window_start_ts = current_timestamp_ms - (baseline_window_sec * 1000)

        spike_window_trades = [t for t in self.trade_data[pair] if t['timestamp'] >= spike_window_start_ts]
        baseline_trades = [t for t in self.trade_data[pair] if t['timestamp'] >= baseline_window_start_ts and t['timestamp'] < spike_window_start_ts]

        if len(spike_window_trades) < 2 or len(baseline_trades) < 2: return

        prices = [t['price'] for t in spike_window_trades]
        min_price, max_price = min(prices), max(prices)
        if min_price == 0: return
        price_change_percent = ((max_price - min_price) / min_price) * 100

        spike_volume = sum(t['amount'] for t in spike_window_trades)
        baseline_volume = sum(t['amount'] for t in baseline_trades)

        if baseline_volume == 0: # Jeśli wcześniej nie było ruchu, każdy ruch jest pikiem
            volume_factor = float('inf')
        else:
            # Normalizujemy wolumen do wolumenu na sekundę
            avg_volume_per_second_baseline = baseline_volume / (baseline_window_sec - spike_window_sec)
            avg_volume_per_second_spike = spike_volume / spike_window_sec
            if avg_volume_per_second_baseline == 0:
                 volume_factor = float('inf')
            else:
                volume_factor = avg_volume_per_second_spike / avg_volume_per_second_baseline

        price_ok = price_change_percent >= self.params['price_change']
        volume_ok = volume_factor >= self.params['volume_factor']

        if pair in self.alerted_pairs and (time.time() - self.alerted_pairs[pair]) < 60: return

        if price_ok and volume_ok:
            self.spike_detected.emit(
                f"<b>{pair}</b> | "
                f"Zmiana ceny: <b>{price_change_percent:.2f}%</b>, "
                f"Mnożnik wol.: <b>x{volume_factor:.1f}</b> "
                f"w ciągu {self.params['time_window']}s"
            )
            self.alerted_pairs[pair] = time.time()

    def stop(self):
        self.log_message.emit("Wysyłanie sygnału zatrzymania do wątku...")
        self.is_running = False

class SpikeDetectorWindow(QMainWindow):
    def __init__(self, exchange_options, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options
        self.setWindowTitle("Detektor Pików Cenowych")
        self.setGeometry(200, 200, 800, 600)

        self.detector_thread = None
        self.fetch_markets_thread = None

        main_widget = QWidget(); self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)
        controls_widget = QWidget(); controls_layout = QVBoxLayout(controls_widget)
        controls_widget.setFixedWidth(350)
        exchange_group = QGroupBox("Giełda")
        exchange_layout = QFormLayout(exchange_group); self.exchange_combo = QComboBox()
        self.supported_exchanges = ['binance', 'bybit']
        self.exchange_combo.addItems([name for name, data in self.exchange_options.items() if data.get('id_ccxt') in self.supported_exchanges])
        self.exchange_combo.currentTextChanged.connect(self.trigger_fetch_markets)
        exchange_layout.addRow("Wybierz:", self.exchange_combo); controls_layout.addWidget(exchange_group)

        self.pairs_group = QGroupBox("Zarządzanie Listą Par"); pairs_layout = QVBoxLayout(self.pairs_group)
        available_pairs_group = QGroupBox("Dostępne Pary:"); available_layout = QVBoxLayout(available_pairs_group)
        self.available_pairs_list_widget = QListWidget(); self.available_pairs_list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        available_layout.addWidget(self.available_pairs_list_widget)
        buttons_layout = QGridLayout()
        self.add_pair_button = QPushButton(">"); self.add_pair_button.setToolTip("Dodaj"); buttons_layout.addWidget(self.add_pair_button, 0, 0)
        self.remove_pair_button = QPushButton("<"); self.remove_pair_button.setToolTip("Usuń"); buttons_layout.addWidget(self.remove_pair_button, 1, 0)
        self.add_all_button = QPushButton(">>"); self.add_all_button.setToolTip("Dodaj wszystkie"); buttons_layout.addWidget(self.add_all_button, 0, 1)
        self.remove_all_button = QPushButton("<<"); self.remove_all_button.setToolTip("Usuń wszystkie"); buttons_layout.addWidget(self.remove_all_button, 1, 1)
        watchlist_group = QGroupBox("Wybrane do monitorowania:"); watchlist_layout = QVBoxLayout(watchlist_group)
        self.monitor_pairs_list_widget = QListWidget(); self.monitor_pairs_list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        watchlist_layout.addWidget(self.monitor_pairs_list_widget)
        pairs_layout.addWidget(available_pairs_group); pairs_layout.addLayout(buttons_layout); pairs_layout.addWidget(watchlist_group)
        self.refresh_pairs_button = QPushButton("Odśwież Dostępne Pary"); self.refresh_pairs_button.clicked.connect(self.trigger_fetch_markets)
        pairs_layout.addWidget(self.refresh_pairs_button); controls_layout.addWidget(self.pairs_group)
        self.add_pair_button.clicked.connect(self.add_to_monitor); self.remove_pair_button.clicked.connect(self.remove_from_monitor)
        self.add_all_button.clicked.connect(self.add_all_to_monitor); self.remove_all_button.clicked.connect(self.remove_all_from_monitor)

        params_group = QGroupBox("Parametry Wykrywania Piku")
        params_layout = QFormLayout(params_group)
        self.time_window_spin = QSpinBox(); self.time_window_spin.setRange(1, 300); self.time_window_spin.setValue(60); self.time_window_spin.setSuffix(" s"); params_layout.addRow("Okno piku:", self.time_window_spin)
        self.price_change_spin = QDoubleSpinBox(); self.price_change_spin.setRange(0.1, 100.0); self.price_change_spin.setValue(3.0); self.price_change_spin.setSuffix(" %"); params_layout.addRow("Zmiana ceny o (min.):", self.price_change_spin)
        self.baseline_minutes_spin = QSpinBox(); self.baseline_minutes_spin.setRange(1, 60); self.baseline_minutes_spin.setValue(10); self.baseline_minutes_spin.setSuffix(" min"); params_layout.addRow("Minuty do uśrednienia:", self.baseline_minutes_spin)
        self.volume_factor_spin = QDoubleSpinBox(); self.volume_factor_spin.setRange(1.1, 1000.0); self.volume_factor_spin.setValue(10.0); self.volume_factor_spin.setSuffix(" x"); params_layout.addRow("Mnożnik wolumenu (min.):", self.volume_factor_spin)
        controls_layout.addWidget(params_group)

        self.start_button = QPushButton("Start Detekcji"); self.stop_button = QPushButton("Stop Detekcji"); self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_detection); self.stop_button.clicked.connect(self.stop_detection)
        controls_layout.addWidget(self.start_button); controls_layout.addWidget(self.stop_button)
        controls_layout.addStretch()

        log_widget = QWidget(); log_layout = QVBoxLayout(log_widget)
        self.log_output = QTextEdit(); self.log_output.setReadOnly(True)
        log_layout.addWidget(QLabel("<b>Log Detektora:</b>")); log_layout.addWidget(self.log_output)
        main_layout.addWidget(controls_widget); main_layout.addWidget(log_widget, 1)

        self.trigger_fetch_markets()

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return
        selected_exchange_gui = self.exchange_combo.currentText()
        if not selected_exchange_gui: return
        selected_config = self.exchange_options.get(selected_exchange_gui)
        ccxt_id, market_type = selected_config["id_ccxt"], selected_config["type"]
        self.refresh_pairs_button.setEnabled(False); self.refresh_pairs_button.setText("Pobieranie...")
        self.fetch_markets_thread = FetchSpikeDetectorMarketsThread(ccxt_id, market_type, self)
        self.fetch_markets_thread.markets_fetched_signal.connect(self.populate_available_pairs)
        self.fetch_markets_thread.error_signal.connect(self.log_error)
        self.fetch_markets_thread.finished_signal.connect(self.on_fetch_markets_finished)
        self.fetch_markets_thread.start()

    def populate_available_pairs(self, pairs_list):
        self.available_pairs_list_widget.clear(); self.available_pairs_list_widget.addItems(pairs_list)

    def on_fetch_markets_finished(self):
        self.refresh_pairs_button.setEnabled(True); self.refresh_pairs_button.setText("Odśwież Dostępne Pary")

    def add_to_monitor(self):
        for item in self.available_pairs_list_widget.selectedItems():
            self.monitor_pairs_list_widget.addItem(self.available_pairs_list_widget.takeItem(self.available_pairs_list_widget.row(item)))
        self.monitor_pairs_list_widget.sortItems()

    def remove_from_monitor(self):
        for item in self.monitor_pairs_list_widget.selectedItems():
            self.available_pairs_list_widget.addItem(self.monitor_pairs_list_widget.takeItem(self.monitor_pairs_list_widget.row(item)))
        self.available_pairs_list_widget.sortItems()

    def add_all_to_monitor(self):
        while self.available_pairs_list_widget.count() > 0:
            self.monitor_pairs_list_widget.addItem(self.available_pairs_list_widget.takeItem(0))
        self.monitor_pairs_list_widget.sortItems()

    def remove_all_from_monitor(self):
        while self.monitor_pairs_list_widget.count() > 0:
            self.available_pairs_list_widget.addItem(self.monitor_pairs_list_widget.takeItem(0))
        self.available_pairs_list_widget.sortItems()

    def start_detection(self):
        if self.detector_thread and self.detector_thread.isRunning(): return

        pairs_to_monitor = [self.monitor_pairs_list_widget.item(i).text() for i in range(self.monitor_pairs_list_widget.count())]
        if not pairs_to_monitor:
            QMessageBox.warning(self, "Brak par", "Wybierz przynajmniej jedną parę do monitorowania.")
            return

        selected_exchange_name = self.exchange_combo.currentText()
        exchange_id = self.exchange_options[selected_exchange_name]['id_ccxt']

        params = {
            "time_window": self.time_window_spin.value(),
            "price_change": self.price_change_spin.value(),
            "volume_factor": self.volume_factor_spin.value(),
            "baseline_minutes": self.baseline_minutes_spin.value()
        }

        self.detector_thread = SpikeDetectorThread(exchange_id, pairs_to_monitor, params, self)
        self.detector_thread.spike_detected.connect(self.log_spike)
        self.detector_thread.log_message.connect(self.log_message)
        self.detector_thread.error_occurred.connect(self.log_error)
        self.detector_thread.finished.connect(self.on_detection_finished)

        self.detector_thread.start()
        self.start_button.setEnabled(False); self.stop_button.setEnabled(True)

    def stop_detection(self):
        if self.detector_thread and self.detector_thread.isRunning():
            self.detector_thread.stop()

    def on_detection_finished(self):
        self.start_button.setEnabled(True); self.stop_button.setEnabled(False)
        self.log_message("Detektor zatrzymany.")

    def log_message(self, message): self.log_output.append(message)
    def log_spike(self, message): self.log_output.append(f"<font color='green'>{message}</font>")
    def log_error(self, message): self.log_output.append(f"<font color='red'>Błąd: {message}</font>")

    def closeEvent(self, event):
        self.stop_detection()
        super().closeEvent(event)
