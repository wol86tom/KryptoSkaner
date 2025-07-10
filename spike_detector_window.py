# ZREFAKTORYZOWANY PLIK: spike_detector_window.py

import sys
import os
import configparser
import datetime
import time
import ccxt.pro as ccxtpro
import asyncio
from collections import deque

from PyQt6.QtWidgets import (QMainWindow, QLabel, QWidget, QVBoxLayout, QGroupBox, QFormLayout,
                             QComboBox, QSpinBox, QDoubleSpinBox, QPushButton, QHBoxLayout,
                             QTextEdit, QListWidget, QMessageBox, QListWidgetItem)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal

# Import naszego nowego modułu pomocniczego
import utils

# --- Wątek detektora (unikalny dla tego modułu) ---

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
        self.tasks = []
        self.loop = None

        maxlen = (self.params['baseline_minutes'] * 60 + self.params['time_window']) * 5
        self.trade_data = {pair: deque(maxlen=maxlen) for pair in self.pairs}
        self.alerted_pairs = {} # {pair: last_alert_timestamp}

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.is_running = True

        try:
            self.log_message.emit(f"Inicjalizacja giełdy {self.exchange_id} dla WebSocket...")
            exchange_class = getattr(ccxtpro, self.exchange_id)
            self.exchange = exchange_class()
            self.tasks = [self.loop.create_task(self.watch_pair(pair)) for pair in self.pairs]
            self.log_message.emit(f"Rozpoczynanie nasłuchu dla {len(self.pairs)} par...")
            self.loop.run_forever()
        except Exception as e:
            self.error_occurred.emit(f"Błąd krytyczny pętli asyncio: {e}")
        finally:
            self.log_message.emit("Kończenie wątku detektora...")
            if self.exchange and self.exchange.connected:
                self.loop.run_until_complete(self.exchange.close())
            for task in self.tasks:
                task.cancel()

            try:
                self.loop.run_until_complete(asyncio.gather(*self.tasks, return_exceptions=True))
            except asyncio.CancelledError:
                pass

            if self.loop.is_running():
                self.loop.stop()
            self.loop.close()

    async def watch_pair(self, pair):
        while self.is_running:
            try:
                trades = await self.exchange.watch_trades(pair)
                if self.is_running and trades:
                    self.process_trades(pair, trades)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.error_occurred.emit(f"Błąd dla {pair}: {e}")
                await asyncio.sleep(5)

    def process_trades(self, pair, trades):
        current_time_ms = time.time() * 1000

        for trade in trades:
            if trade.get('price') is not None and trade.get('amount') is not None:
                self.trade_data[pair].append({
                    'timestamp': trade.get('timestamp', int(current_time_ms)),
                    'price': trade['price'], 'amount': trade['amount']
                })

        # Cooldown check
        last_alert_time = self.alerted_pairs.get(pair, 0)
        if (current_time_ms - last_alert_time) / (60 * 1000) < self.params['alert_cooldown_minutes']:
            return

        # Data preparation
        min_ts_baseline = current_time_ms - (self.params['baseline_minutes'] * 60 * 1000)
        baseline_trades = [t for t in self.trade_data[pair] if t['timestamp'] >= min_ts_baseline]
        if not baseline_trades: return

        baseline_prices = [t['price'] for t in baseline_trades]
        baseline_volumes = [t['amount'] for t in baseline_trades]
        avg_baseline_price = sum(baseline_prices) / len(baseline_prices) if baseline_prices else 0
        avg_baseline_volume = sum(baseline_volumes) / len(baseline_volumes) if baseline_volumes else 0

        min_ts_window = current_time_ms - (self.params['time_window'] * 1000)
        spike_window_trades = [t for t in baseline_trades if t['timestamp'] >= min_ts_window]
        if not spike_window_trades: return

        # Spike detection
        spike_volume = sum(t['amount'] for t in spike_window_trades)
        prices_in_window = [t['price'] for t in spike_window_trades]
        price_change_percent = ((max(prices_in_window) - min(prices_in_window)) / avg_baseline_price) * 100 if avg_baseline_price else 0

        is_volume_spike = spike_volume > (avg_baseline_volume * self.params['volume_threshold_multiplier'])
        is_price_spike = price_change_percent > self.params['price_threshold_percent']

        if is_volume_spike or is_price_spike:
            spike_type = []
            if is_volume_spike: spike_type.append("Wolumen")
            if is_price_spike: spike_type.append("Cena")

            message = (f"PIK! {pair} ({', '.join(spike_type)}) | "
                       f"Wolumen w {self.params['time_window']}s: {spike_volume:.2f} "
                       f"(próg: {(avg_baseline_volume * self.params['volume_threshold_multiplier']):.2f}) | "
                       f"Zmiana ceny: {price_change_percent:.2f}% (próg: {self.params['price_threshold_percent']:.2f}%)")
            self.spike_detected.emit(message)
            self.alerted_pairs[pair] = current_time_ms

    def stop(self):
        self.log_message.emit("Wysyłanie sygnału zatrzymania do wątku...")
        self.is_running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

# --- Główne okno detektora pików ---

class SpikeDetectorWindow(QMainWindow):
    def __init__(self, exchange_options: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detektor Pików Cenowych i Wolumenowych")
        self.setGeometry(200, 200, 900, 700)

        self.exchange_options = exchange_options
        self.active_detector_thread = None
        self.fetch_markets_thread = None

        self.config_path = utils.get_config_path()
        self.config = configparser.ConfigParser()

        self.init_ui()
        self.load_settings()
        self.apply_settings_to_ui()
        self.on_exchange_selection_changed()

    def init_ui(self):
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        # Panel wyboru giełdy i par
        exchange_pair_group = QGroupBox("Wybór Giełdy i Pary")
        exchange_pair_layout = QHBoxLayout(exchange_pair_group)
        self.exchange_combo = QComboBox()
        self.exchange_combo.addItems(self.exchange_options.keys())
        self.exchange_combo.currentTextChanged.connect(self.on_exchange_selection_changed)
        exchange_pair_layout.addWidget(QLabel("Giełda:"))
        exchange_pair_layout.addWidget(self.exchange_combo)
        self.available_pairs_list = QListWidget()
        self.available_pairs_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.available_pairs_list.itemDoubleClicked.connect(self.add_to_watchlist_from_available)
        exchange_pair_layout.addWidget(QLabel("Dostępne Pary:"))
        exchange_pair_layout.addWidget(self.available_pairs_list)
        self.main_layout.addWidget(exchange_pair_group)

        # Watchlista
        watchlist_group = QGroupBox("Watchlista (pary do monitorowania)")
        watchlist_layout = QHBoxLayout(watchlist_group)
        self.watchlist = QListWidget()
        self.watchlist.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        watchlist_layout.addWidget(self.watchlist)
        watchlist_buttons_layout = QVBoxLayout()
        self.add_selected_button = QPushButton("Dodaj >")
        self.add_selected_button.clicked.connect(self.add_to_watchlist_from_available)
        watchlist_buttons_layout.addWidget(self.add_selected_button)
        self.remove_selected_button = QPushButton("< Usuń")
        self.remove_selected_button.clicked.connect(self.remove_from_watchlist)
        watchlist_buttons_layout.addWidget(self.remove_selected_button)
        watchlist_layout.addLayout(watchlist_buttons_layout)
        self.main_layout.addWidget(watchlist_group)

        # Parametry detekcji
        params_group = QGroupBox("Parametry Detekcji Pików")
        params_layout = QFormLayout(params_group)
        self.baseline_minutes_spin = QSpinBox(); self.baseline_minutes_spin.setRange(1, 60)
        params_layout.addRow("Okres bazowy (minuty):", self.baseline_minutes_spin)
        self.time_window_spin = QSpinBox(); self.time_window_spin.setRange(1, 60)
        params_layout.addRow("Okno piku (sekundy):", self.time_window_spin)
        self.volume_threshold_multiplier_spin = QDoubleSpinBox(); self.volume_threshold_multiplier_spin.setRange(1.1, 50.0)
        params_layout.addRow("Mnożnik progu wolumenu:", self.volume_threshold_multiplier_spin)
        self.price_threshold_percent_spin = QDoubleSpinBox(); self.price_threshold_percent_spin.setRange(0.01, 10.0)
        params_layout.addRow("Próg zmiany ceny (%):", self.price_threshold_percent_spin)
        self.alert_cooldown_minutes_spin = QSpinBox(); self.alert_cooldown_minutes_spin.setRange(1, 60)
        params_layout.addRow("Cooldown alertu (minuty):", self.alert_cooldown_minutes_spin)
        self.main_layout.addWidget(params_group)

        # Przyciski kontrolne i Logi
        bottom_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Detektora")
        self.start_button.clicked.connect(self.start_detector)
        bottom_layout.addWidget(self.start_button)
        self.stop_button = QPushButton("Zatrzymaj Detektor")
        self.stop_button.clicked.connect(self.stop_detector)
        self.stop_button.setEnabled(False)
        bottom_layout.addWidget(self.stop_button)
        self.main_layout.addLayout(bottom_layout)
        self.alerts_display = QTextEdit(); self.alerts_display.setReadOnly(True)
        self.main_layout.addWidget(QLabel("Wykryte Piki:"))
        self.main_layout.addWidget(self.alerts_display)
        self.log_display = QTextEdit(); self.log_display.setReadOnly(True); self.log_display.setFixedHeight(80)
        self.main_layout.addWidget(QLabel("Logi:"))
        self.main_layout.addWidget(self.log_display)

        # Połącz sygnały zmian parametrów ze slotem save_settings
        for widget in [self.baseline_minutes_spin, self.time_window_spin,
                       self.volume_threshold_multiplier_spin, self.price_threshold_percent_spin,
                       self.alert_cooldown_minutes_spin, self.exchange_combo]:
            if hasattr(widget, 'valueChanged'):
                widget.valueChanged.connect(self.save_settings)
            elif hasattr(widget, 'currentTextChanged'):
                widget.currentTextChanged.connect(self.save_settings)

    def load_settings(self):
        self.config.read(self.config_path)

    def apply_settings_to_ui(self):
        current_exchange = self.exchange_combo.currentText()
        config_section = self.exchange_options.get(current_exchange, {}).get("config_section")

        if config_section and self.config.has_section(config_section):
            sd_settings = self.config[config_section]
            self.baseline_minutes_spin.setValue(utils.safe_int_cast(sd_settings.get('sd_baseline_minutes', '15')))
            self.time_window_spin.setValue(utils.safe_int_cast(sd_settings.get('sd_time_window', '5')))
            self.volume_threshold_multiplier_spin.setValue(utils.safe_float_cast(sd_settings.get('sd_volume_threshold_multiplier', '2.0')))
            self.price_threshold_percent_spin.setValue(utils.safe_float_cast(sd_settings.get('sd_price_threshold_percent', '0.5')))
            self.alert_cooldown_minutes_spin.setValue(utils.safe_int_cast(sd_settings.get('sd_alert_cooldown_minutes', '5')))

            watchlist_str = sd_settings.get('sd_watchlist_pairs', "")
            self.watchlist.clear()
            if watchlist_str:
                self.watchlist.addItems([p.strip() for p in watchlist_str.split(',') if p.strip()])

    def save_settings(self):
        current_exchange = self.exchange_combo.currentText()
        config_section = self.exchange_options.get(current_exchange, {}).get("config_section")
        if not config_section: return

        if not self.config.has_section(config_section):
            self.config.add_section(config_section)

        sd_settings = self.config[config_section]
        sd_settings['sd_baseline_minutes'] = str(self.baseline_minutes_spin.value())
        sd_settings['sd_time_window'] = str(self.time_window_spin.value())
        sd_settings['sd_volume_threshold_multiplier'] = str(self.volume_threshold_multiplier_spin.value())
        sd_settings['sd_price_threshold_percent'] = str(self.price_threshold_percent_spin.value())
        sd_settings['sd_alert_cooldown_minutes'] = str(self.alert_cooldown_minutes_spin.value())

        watchlist_items = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]
        sd_settings['sd_watchlist_pairs'] = ",".join(watchlist_items)

        try:
            with open(self.config_path, 'w') as configfile:
                self.config.write(configfile)
        except Exception as e:
            self.error_message(f"Błąd zapisu ustawień: {e}")

    def on_exchange_selection_changed(self):
        self.stop_detector()
        self.load_settings()
        self.apply_settings_to_ui()
        self.trigger_fetch_markets()

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return

        exchange_name = self.exchange_combo.currentText()
        details = self.exchange_options.get(exchange_name)
        if details:
            self.fetch_markets_thread = utils.FetchMarketsThread(details["id_ccxt"], details["type"])
            self.fetch_markets_thread.markets_fetched.connect(self.populate_available_pairs)
            self.fetch_markets_thread.error_occurred.connect(self.error_message)
            self.fetch_markets_thread.start()

    def populate_available_pairs(self, markets):
        self.available_pairs_list.clear()
        current_watchlist = {self.watchlist.item(i).text() for i in range(self.watchlist.count())}
        for market in markets:
            if market['symbol'] not in current_watchlist:
                self.available_pairs_list.addItem(QListWidgetItem(market['symbol']))

    def add_to_watchlist_from_available(self):
        for item in self.available_pairs_list.selectedItems():
            self.watchlist.addItem(item.text())
            self.available_pairs_list.takeItem(self.available_pairs_list.row(item))
        self.save_settings()

    def remove_from_watchlist(self):
        for item in self.watchlist.selectedItems():
            self.available_pairs_list.addItem(item.text())
            self.watchlist.takeItem(self.watchlist.row(item))
        self.available_pairs_list.sortItems()
        self.save_settings()

    def start_detector(self):
        if self.active_detector_thread and self.active_detector_thread.isRunning():
            self.error_message("Detektor już działa."); return

        pairs_to_monitor = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]
        if not pairs_to_monitor:
            self.error_message("Watchlista jest pusta."); return

        exchange_id = self.exchange_options[self.exchange_combo.currentText()]['id_ccxt']
        params = {
            'baseline_minutes': self.baseline_minutes_spin.value(),
            'time_window': self.time_window_spin.value(),
            'volume_threshold_multiplier': self.volume_threshold_multiplier_spin.value(),
            'price_threshold_percent': self.price_threshold_percent_spin.value(),
            'alert_cooldown_minutes': self.alert_cooldown_minutes_spin.value()
        }

        self.active_detector_thread = SpikeDetectorThread(exchange_id, pairs_to_monitor, params)
        self.active_detector_thread.spike_detected.connect(self.display_alert)
        self.active_detector_thread.log_message.connect(self.log_message)
        self.active_detector_thread.error_occurred.connect(self.error_message)
        self.active_detector_thread.start()

        self.start_button.setEnabled(False); self.stop_button.setEnabled(True)
        self.log_message("Detektor pików uruchomiony.")

    def stop_detector(self):
        if self.active_detector_thread and self.active_detector_thread.isRunning():
            self.active_detector_thread.stop()
            self.active_detector_thread.wait(3000)
            self.active_detector_thread = None
            self.log_message("Detektor pików zatrzymany.")
        self.start_button.setEnabled(True); self.stop_button.setEnabled(False)

    def display_alert(self, message):
        self.alerts_display.append(f"<font color='red'><b>{datetime.datetime.now().strftime('%H:%M:%S')}</b> - {message}</font>")

    def log_message(self, message):
        self.log_display.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}")

    def error_message(self, message):
        self.log_display.append(f"<font color='orange'>[{datetime.datetime.now().strftime('%H:%M:%S')}] BŁĄD: {message}</font>")
        QMessageBox.warning(self, "Błąd Detektora", message)

    def closeEvent(self, event):
        self.stop_detector()
        self.save_settings()
        event.accept()
