import sys, os, configparser, datetime, time, ccxt, ccxt.pro as ccxtpro, asyncio
from collections import deque
from PyQt6.QtWidgets import (QMainWindow, QLabel, QWidget, QVBoxLayout, QGroupBox, QFormLayout,
                             QComboBox, QSpinBox, QDoubleSpinBox, QPushButton, QHBoxLayout,
                             QTextEdit, QListWidget, QMessageBox, QApplication,
                             QListWidgetItem)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QStandardPaths

import qasync

# --- Konfiguracja ścieżek (dla samodzielnego uruchomienia) ---
CONFIG_DIR_NAME = "KryptoSkaner"
CONFIG_FILE_NAME = "app_settings.ini"

def get_config_path_standalone():
    config_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    app_config_path = os.path.join(config_dir, CONFIG_DIR_NAME)
    if not os.path.exists(app_config_path):
        os.makedirs(app_config_path, exist_ok=True)
    return os.path.join(app_config_path, CONFIG_FILE_NAME)

# --- Klasy pomocnicze ---

class FetchMarketsThread(QThread):
    markets_fetched = Signal(list)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self, exchange_id, market_type_filter):
        super().__init__()
        self.exchange_id = exchange_id
        self.market_type_filter = market_type_filter
        self.is_running = True

    def run(self):
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
            exchange = exchange_class({'enableRateLimit': True})
            markets = exchange.load_markets()
            filtered_markets = []
            for market_id, market_data in markets.items():
                if self.type_matches(market_data):
                    filtered_markets.append(market_data)
            self.markets_fetched.emit(filtered_markets)
        except Exception as e:
            self.error_occurred.emit(f"Błąd podczas pobierania rynków z {self.exchange_id}: {e}")
        finally:
            self.finished.emit()

    def type_matches(self, market: dict) -> bool:
        """
        Sprawdza, czy rynek pasuje do kryteriów (aktywny, USDT quote, typ rynku).
        """
        if not (market.get('active') and market.get('quote') == 'USDT'):
            return False

        market_type = market.get('type')

        if self.market_type_filter == market_type:
            return True

        # Specyficzne dla Binance Futures (USDT-M)
        if self.exchange_id == 'binanceusdm' and self.market_type_filter == 'future':
            if market.get('linear') and market_type in ['future', 'swap']:
                return True

        # Specyficzne dla Bybit Perpetual USDT
        if self.exchange_id == 'bybit' and self.market_type_filter == 'swap' and market_type == 'swap':
            return True

        return False

    def stop(self):
        self.is_running = False


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
        self.tasks = [] # Lista do przechowywania zadań asyncio
        self.loop = None # Pętla asyncio będzie tworzona i niszczona w run

        maxlen = (self.params['baseline_minutes'] * 60 + self.params['time_window']) * 5
        self.trade_data = {pair: deque(maxlen=maxlen) for pair in self.pairs}
        self.alerted_pairs = {} # {pair: last_alert_timestamp}

    def run(self):
        # Tworzenie nowej pętli zdarzeń dla tego wątku
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self.is_running = True
        try:
            self.log_message.emit(f"Inicjalizacja giełdy {self.exchange_id} dla WebSocket...")
            exchange_class = getattr(ccxtpro, self.exchange_id)
            self.exchange = exchange_class()

            # Dodaj zadania watch_pair do listy zadań
            self.tasks = [self.loop.create_task(self.watch_pair(pair)) for pair in self.pairs]
            self.log_message.emit(f"Rozpoczynanie nasłuchu dla {len(self.pairs)} par...")

            # Uruchom pętlę zdarzeń asyncio
            self.loop.run_forever()

        except Exception as e:
            self.error_occurred.emit(f"Błąd krytyczny pętli asyncio: {e}")
        finally:
            self.log_message.emit("Kończenie wątku detektora...")
            # Prawidłowe zamknięcie połączeń CCXT Pro
            if self.exchange and self.exchange.connected:
                self.loop.run_until_complete(self.exchange.close())
                self.log_message.emit("Połączenie WebSocket zamknięte.")

            # Anulowanie pozostałych zadań i czyszczenie pętli
            for task in self.tasks:
                task.cancel()

            # Czekamy na zakończenie zadań (z małym timeoutem)
            try:
                self.loop.run_until_complete(asyncio.gather(*self.tasks, return_exceptions=True))
            except asyncio.CancelledError:
                pass # Oczekiwane, jeśli zadania były anulowane

            # Sprawdź, czy pętla jest jeszcze uruchomiona, zanim ją zatrzymasz
            if self.loop.is_running():
                self.loop.stop()

            self.loop.close() # Zamknij pętlę po zakończeniu
            self.log_message.emit("Pętla asyncio detektora zamknięta.")


    async def watch_pair(self, pair):
        while self.is_running: # Kontynuuj, dopóki flaga is_running jest True
            try:
                trades = await self.exchange.watch_trades(pair)
                if self.is_running and trades:
                    self.process_trades(pair, trades)
            except asyncio.CancelledError:
                self.log_message.emit(f"Zadanie watch_trades dla {pair} anulowane.")
                break # Wychodzimy z pętli
            except Exception as e:
                self.error_occurred.emit(f"Błąd dla {pair}: {str(e)}")
                await asyncio.sleep(5)
        self.log_message.emit(f"Zakończono watch_pair dla {pair}.")


    def process_trades(self, pair, trades):
        current_time = datetime.datetime.now().timestamp() * 1000 # ms

        for trade in trades:
            trade_timestamp = trade.get('timestamp', int(current_time))
            trade_price = trade.get('price')
            trade_amount = trade.get('amount')
            trade_side = trade.get('side')

            if trade_price is None or trade_amount is None:
                continue

            self.trade_data[pair].append({
                'timestamp': trade_timestamp,
                'price': trade_price,
                'amount': trade_amount,
                'side': trade_side
            })

        min_timestamp_baseline = current_time - (self.params['baseline_minutes'] * 60 * 1000)
        min_timestamp_window = current_time - (self.params['time_window'] * 1000)

        while self.trade_data[pair] and self.trade_data[pair][0]['timestamp'] < min_timestamp_baseline:
            self.trade_data[pair].popleft()

        last_alert_time = self.alerted_pairs.get(pair, 0)
        if (current_time - last_alert_time) / (60 * 1000) < self.params['alert_cooldown_minutes']:
            return

        if len(self.trade_data[pair]) < 2:
            return

        baseline_trades = [t for t in self.trade_data[pair] if t['timestamp'] >= min_timestamp_baseline]
        if not baseline_trades:
            return

        baseline_prices = [t['price'] for t in baseline_trades]
        baseline_volumes = [t['amount'] for t in baseline_trades]

        avg_baseline_price = sum(baseline_prices) / len(baseline_prices) if baseline_prices else 0
        avg_baseline_volume = sum(baseline_volumes) / len(baseline_volumes) if baseline_volumes else 0

        spike_window_trades = [t for t in self.trade_data[pair] if t['timestamp'] >= min_timestamp_window]
        if not spike_window_trades:
            return

        spike_volume = sum(t['amount'] for t in spike_window_trades)
        prices_in_window = [t['price'] for t in spike_window_trades]

        if not prices_in_window:
            return

        min_price_window = min(prices_in_window)
        max_price_window = max(prices_in_window)
        price_change_percent = ((max_price_window - min_price_window) / avg_baseline_price) * 100 if avg_baseline_price else 0

        volume_spike_threshold = avg_baseline_volume * self.params['volume_threshold_multiplier']
        price_spike_threshold = self.params['price_threshold_percent']

        is_volume_spike = spike_volume > volume_spike_threshold
        is_price_spike = abs(price_change_percent) > price_spike_threshold

        if is_volume_spike or is_price_spike:
            spike_type = []
            if is_volume_spike:
                spike_type.append("Wolumen")
            if is_price_spike:
                spike_type.append("Cena")

            message = (f"[{datetime.datetime.fromtimestamp(current_time / 1000).strftime('%H:%M:%S')}] "
                       f"PIK DETEKCJA! {pair} na {self.exchange_id} - Typ: {', '.join(spike_type)}\n"
                       f"  Wolumen w oknie ({self.params['time_window']}s): {spike_volume:.2f} (Próg: {volume_spike_threshold:.2f})\n"
                       f"  Zmiana ceny w oknie ({self.params['time_window']}s): {price_change_percent:.2f}% (Próg: {price_spike_threshold:.2f}%)\n"
                       f"  Średnia cena bazowa ({self.params['baseline_minutes']}min): {avg_baseline_price:.4f}")
            self.spike_detected.emit(message)
            self.alerted_pairs[pair] = current_time

    def stop(self):
        self.log_message.emit("Wysyłanie sygnału zatrzymania do wątku...")
        self.is_running = False
        # To spowoduje, że pętle watch_trades przestaną czekać na nowe dane.
        # Następnie loop.run_forever() w metodzie run() zostanie przerwane,
        # co pozwoli na wykonanie kodu w bloku finally.


# --- Główne okno detektora pików ---

class SpikeDetectorWindow(QMainWindow):
    def __init__(self, exchange_options: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detektor Pików Cenowych i Wolumenowych")
        self.setGeometry(200, 200, 900, 700)

        self.exchange_options = exchange_options
        self.active_detector_threads = {}
        self.fetched_markets_data = {}
        self.exchange_fetch_threads = {}

        self.config_path = get_config_path_standalone()
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
        exchange_pair_layout = QHBoxLayout()
        exchange_pair_group.setLayout(exchange_pair_layout)

        self.exchange_combo = QComboBox()
        self.exchange_combo.addItems(self.exchange_options.keys())
        self.exchange_combo.currentTextChanged.connect(self.on_exchange_selection_changed)
        exchange_pair_layout.addWidget(QLabel("Giełda:"))
        exchange_pair_layout.addWidget(self.exchange_combo)

        self.available_pairs_list = QListWidget()
        self.available_pairs_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.available_pairs_list.itemDoubleClicked.connect(self.add_to_watchlist_from_available)
        exchange_pair_layout.addWidget(QLabel("Dostępne Pary:"))
        exchange_pair_layout.addWidget(self.available_pairs_list)

        self.main_layout.addWidget(exchange_pair_group)

        # Watchlista
        watchlist_group = QGroupBox("Watchlista")
        watchlist_layout = QVBoxLayout()
        watchlist_group.setLayout(watchlist_layout)

        self.watchlist = QListWidget()
        self.watchlist.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.watchlist.setMinimumHeight(100)
        watchlist_layout.addWidget(self.watchlist)

        watchlist_buttons_layout = QHBoxLayout()
        self.add_selected_button = QPushButton("Dodaj wybrane")
        self.add_selected_button.clicked.connect(self.add_to_watchlist_from_available)
        watchlist_buttons_layout.addWidget(self.add_selected_button)

        self.remove_selected_button = QPushButton("Usuń wybrane")
        self.remove_selected_button.clicked.connect(self.remove_from_watchlist)
        watchlist_buttons_layout.addWidget(self.remove_selected_button)
        watchlist_layout.addLayout(watchlist_buttons_layout)

        self.main_layout.addWidget(watchlist_group)

        # Parametry detekcji
        params_group = QGroupBox("Parametry Detekcji Pików")
        params_layout = QFormLayout()
        params_group.setLayout(params_layout)

        self.baseline_minutes_spin = QSpinBox()
        self.baseline_minutes_spin.setRange(1, 60)
        self.baseline_minutes_spin.setValue(15)
        params_layout.addRow("Okres bazowy (minuty):", self.baseline_minutes_spin)

        self.time_window_spin = QSpinBox()
        self.time_window_spin.setRange(1, 60)
        self.time_window_spin.setValue(5)
        params_layout.addRow("Okno piku (sekundy):", self.time_window_spin)

        self.volume_threshold_multiplier_spin = QDoubleSpinBox()
        self.volume_threshold_multiplier_spin.setRange(1.0, 10.0)
        self.volume_threshold_multiplier_spin.setSingleStep(0.1)
        self.volume_threshold_multiplier_spin.setValue(2.0)
        params_layout.addRow("Mnożnik progu wolumenu:", self.volume_threshold_multiplier_spin)

        self.price_threshold_percent_spin = QDoubleSpinBox()
        self.price_threshold_percent_spin.setRange(0.01, 5.0)
        self.price_threshold_percent_spin.setSingleStep(0.01)
        self.price_threshold_percent_spin.setValue(0.5)
        params_layout.addRow("Próg zmiany ceny (%):", self.price_threshold_percent_spin)

        self.alert_cooldown_minutes_spin = QSpinBox()
        self.alert_cooldown_minutes_spin.setRange(1, 60)
        self.alert_cooldown_minutes_spin.setValue(5)
        params_layout.addRow("Cooldown alertu (minuty):", self.alert_cooldown_minutes_spin)

        self.main_layout.addWidget(params_group)

        # Przyciski kontrolne
        control_buttons_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Detektora")
        self.start_button.clicked.connect(self.start_detector)
        control_buttons_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Zatrzymaj Detektor")
        self.stop_button.clicked.connect(self.stop_detector)
        self.stop_button.setEnabled(False)
        control_buttons_layout.addWidget(self.stop_button)
        self.main_layout.addLayout(control_buttons_layout)

        # Wyświetlanie alertów i logów
        self.alerts_display = QTextEdit()
        self.alerts_display.setReadOnly(True)
        self.alerts_display.setMinimumHeight(150)
        self.main_layout.addWidget(QLabel("Alerty:"))
        self.main_layout.addWidget(self.alerts_display)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFixedHeight(80)
        self.main_layout.addWidget(QLabel("Logi:"))
        self.main_layout.addWidget(self.log_display)

        # Połącz sygnały zmian parametrów ze slotem save_settings
        self.baseline_minutes_spin.valueChanged.connect(self.save_settings)
        self.time_window_spin.valueChanged.connect(self.save_settings)
        self.volume_threshold_multiplier_spin.valueChanged.connect(self.save_settings)
        self.price_threshold_percent_spin.valueChanged.connect(self.save_settings)
        self.alert_cooldown_minutes_spin.valueChanged.connect(self.save_settings)
        self.exchange_combo.currentTextChanged.connect(self.save_settings)


    def load_settings(self):
        self.config = configparser.ConfigParser()
        if not os.path.exists(self.config_path):
            self.log_message(f"Plik konfiguracyjny {self.config_path} nie istnieje. Tworzę nowy.")
            self._create_default_config_sections_if_missing()
        else:
            self.config.read(self.config_path)
            self.log_message(f"Załadowano ustawienia z pliku: {self.config_path}")

    def _create_default_config_sections_if_missing(self):
        for exchange_name, details in self.exchange_options.items():
            section_name = details["config_section"]
            if not self.config.has_section(section_name):
                self.config.add_section(section_name)
                self.config.set(section_name, 'api_key', '')
                self.config.set(section_name, 'api_secret', '')
                self.config.set(section_name, 'of_watchlist_pairs', '')
                self.config.set(section_name, 'sd_watchlist_pairs', '')
                self.config.set(section_name, 'sd_baseline_minutes', '15')
                self.config.set(section_name, 'sd_time_window', '5')
                self.config.set(section_name, 'sd_volume_threshold_multiplier', '2.0')
                self.config.set(section_name, 'sd_price_threshold_percent', '0.5')
                self.config.set(section_name, 'sd_alert_cooldown_minutes', '5')

        if not self.config.has_section('global_settings'):
            self.config.add_section('global_settings')
            self.config.set('global_settings', 'default_wpr_length', '14')
            self.config.set('global_settings', 'default_ema_length', '9')
            self.config.set('global_settings', 'default_macd_fast', '12')
            self.config.set('global_settings', 'default_macd_slow', '26')
            self.config.set('global_settings', 'default_macd_signal', '9')
            self.config.set('global_settings', 'scanner_selected_timeframes', ','.join(['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w']))
            self.config.set('global_settings', 'scanner_wr_length', '14')
            self.config.set('global_settings', 'scanner_wr_upper', '-20.0')
            self.config.set('global_settings', 'scanner_wr_lower', '-80.0')
            self.config.set('global_settings', 'scanner_ema_fast', '9')
            self.config.set('global_settings', 'scanner_ema_slow', '21')
            self.config.set('global_settings', 'scanner_check_interval_ms', '5000')
            self.config.set('global_settings', 'selected_exchange', 'Binance (Spot)')

        try:
            with open(self.config_path, 'w') as configfile:
                self.config.write(configfile)
        except Exception as e:
            self.error_message(f"Błąd podczas zapisu domyślnej konfiguracji po braku pliku: {e}")


    def apply_settings_to_ui(self):
        current_exchange_name = self.exchange_combo.currentText()
        selected_exchange_config = self.exchange_options.get(current_exchange_name)

        if selected_exchange_config:
            section_name = selected_exchange_config.get("config_section")
            if section_name and self.config.has_section(section_name):
                sd_settings = self.config[section_name]
                self.baseline_minutes_spin.setValue(self.safe_int_cast(sd_settings.get('sd_baseline_minutes', '15')))
                self.time_window_spin.setValue(self.safe_int_cast(sd_settings.get('sd_time_window', '5')))
                self.volume_threshold_multiplier_spin.setValue(self.safe_float_cast(sd_settings.get('sd_volume_threshold_multiplier', '2.0')))
                self.price_threshold_percent_spin.setValue(self.safe_float_cast(sd_settings.get('sd_price_threshold_percent', '0.5')))
                self.alert_cooldown_minutes_spin.setValue(self.safe_int_cast(sd_settings.get('sd_alert_cooldown_minutes', '5')))

                watchlist_pairs_str = sd_settings.get('sd_watchlist_pairs', "")
                self.watchlist.clear()
                if watchlist_pairs_str:
                    for p_symbol_text in [p.strip() for p in watchlist_pairs_str.split(',') if p.strip()]:
                        item = QListWidgetItem(p_symbol_text)
                        self.watchlist.addItem(item)
                self.log_message(f"Załadowano konfigurację Spike Detector dla {current_exchange_name}.")
            else:
                self.log_message(f"Brak zapisanej konfiguracji Spike Detector dla {current_exchange_name}. Użyto domyślnych.")
                self.baseline_minutes_spin.setValue(15)
                self.time_window_spin.setValue(5)
                self.volume_threshold_multiplier_spin.setValue(2.0)
                self.price_threshold_percent_spin.setValue(0.5)
                self.alert_cooldown_minutes_spin.setValue(5)
                self.watchlist.clear()


        global_settings_section = self.config['global_settings'] if self.config.has_section('global_settings') else {}
        saved_exchange = global_settings_section.get('selected_exchange', 'Binance (Spot)')
        self.exchange_combo.setCurrentText(saved_exchange)


    def save_settings(self):
        current_exchange_name = self.exchange_combo.currentText()
        selected_exchange_config = self.exchange_options.get(current_exchange_name)

        if selected_exchange_config:
            section_name = selected_exchange_config.get("config_section")
            if section_name:
                if not self.config.has_section(section_name):
                    self.config.add_section(section_name)

                sd_settings = self.config[section_name]
                sd_settings['sd_baseline_minutes'] = str(self.baseline_minutes_spin.value())
                sd_settings['sd_time_window'] = str(self.time_window_spin.value())
                sd_settings['sd_volume_threshold_multiplier'] = str(self.volume_threshold_multiplier_spin.value())
                sd_settings['sd_price_threshold_percent'] = str(self.price_threshold_percent_spin.value())
                sd_settings['sd_alert_cooldown_minutes'] = str(self.alert_cooldown_minutes_spin.value())

                watchlist_items = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]
                sd_settings['sd_watchlist_pairs'] = ",".join(watchlist_items)
                self.log_message(f"Przygotowano konfigurację Spike Detector dla {current_exchange_name} do zapisu.")
            else:
                self.log_message("Brak nazwy sekcji konfiguracyjnej dla wybranej giełdy.")
        else:
            self.log_message("Nie wybrano giełdy, pomijam zapis ustawień specyficznych dla Spike Detector.")

        try:
            with open(self.config_path, 'w') as configfile:
                self.config.write(configfile)
            self.log_message(f"Konfiguracja Spike Detector zapisana w {self.config_path}")
        except Exception as e:
            self.error_message(f"Błąd zapisu konf. Spike Detector: {str(e)}")

    def safe_int_cast(self, value_str, default=0):
        try:
            return int(float(value_str))
        except (ValueError, TypeError):
            return default

    def safe_float_cast(self, value_str, default=0.0):
        try:
            return float(value_str)
        except (ValueError, TypeError):
            return default

    def on_exchange_selection_changed(self):
        selected_exchange_name = self.exchange_combo.currentText()
        self.log_message(f"Wybrano giełdę: {selected_exchange_name}")
        self.available_pairs_list.clear()
        self.watchlist.clear()

        self.apply_settings_to_ui()

        self.fetch_markets_for_selected_exchange()

    def fetch_markets_for_selected_exchange(self):
        selected_exchange_name = self.exchange_combo.currentText()
        details = self.exchange_options.get(selected_exchange_name)
        if details:
            exchange_id = details["id_ccxt"]
            market_type = details["type"]

            if exchange_id in self.exchange_fetch_threads and self.exchange_fetch_threads[exchange_id].isRunning():
                self.log_message(f"Pobieranie rynków dla {exchange_id} już w toku.")
                return

            self.log_message(f"Rozpoczynam pobieranie rynków dla {exchange_id} ({market_type})...")

            fetch_thread = FetchMarketsThread(exchange_id, market_type)
            fetch_thread.markets_fetched.connect(self.update_available_pairs)
            fetch_thread.error_occurred.connect(self.error_message)
            fetch_thread.finished.connect(lambda: self.log_message("Rynki załadowane."))
            fetch_thread.start()
            self.exchange_fetch_threads[exchange_id] = fetch_thread

    def update_available_pairs(self, markets):
        self.available_pairs_list.clear()
        self.fetched_markets_data.clear()

        sorted_markets = sorted(markets, key=lambda x: x['symbol'])

        for market in sorted_markets:
            symbol = market['symbol']
            item = QListWidgetItem(symbol)
            item.setData(Qt.ItemDataRole.UserRole, market)
            self.available_pairs_list.addItem(item)
            self.fetched_markets_data[symbol] = market

        self.log_message(f"Załadowano {len(markets)} par.")

    def add_to_watchlist_from_available(self):
        selected_items = self.available_pairs_list.selectedItems()
        if not selected_items:
            self.log_message("Wybierz parę z listy 'Dostępne Pary' do dodania.")
            return

        for item in selected_items:
            symbol = item.text()
            found = False
            for i in range(self.watchlist.count()):
                if self.watchlist.item(i).text() == symbol:
                    found = True
                    break
            if not found:
                watchlist_item = QListWidgetItem(symbol)
                market_data = item.data(Qt.ItemDataRole.UserRole)
                watchlist_item.setData(Qt.ItemDataRole.UserRole, market_data)
                self.watchlist.addItem(watchlist_item)
                self.log_message(f"Dodano {symbol} do watchlisty.")
            else:
                self.log_message(f"Para {symbol} już jest na watchliście.")
        self.save_settings()

    def remove_from_watchlist(self):
        selected_items = self.watchlist.selectedItems()
        if not selected_items:
            self.log_message("Wybierz parę z 'Watchlisty' do usunięcia.")
            return

        for item in selected_items:
            self.watchlist.takeItem(self.watchlist.row(item))
            self.log_message(f"Usunięto {item.text()} z watchlisty.")
        self.save_settings()

    def start_detector(self):
        if self.active_detector_threads:
            self.log_message("Detektor już działa. Najpierw zatrzymaj bieżące detekcje.")
            return

        selected_exchange_name = self.exchange_combo.currentText()
        selected_exchange_details = self.exchange_options.get(selected_exchange_name)

        if not selected_exchange_details:
            self.error_message("Nie wybrano giełdy.")
            return

        exchange_id = selected_exchange_details["id_ccxt"]
        pairs_to_monitor = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]

        if not pairs_to_monitor:
            self.error_message("Watchlista jest pusta. Dodaj pary do monitorowania.")
            return

        detection_params = {
            'baseline_minutes': self.baseline_minutes_spin.value(),
            'time_window': self.time_window_spin.value(),
            'volume_threshold_multiplier': self.volume_threshold_multiplier_spin.value(),
            'price_threshold_percent': self.price_threshold_percent_spin.value(),
            'alert_cooldown_minutes': self.alert_cooldown_minutes_spin.value()
        }

        self.alerts_display.clear()
        self.log_message("Rozpoczynam detekcję pików...")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        detector_thread = SpikeDetectorThread(exchange_id, pairs_to_monitor, detection_params)
        detector_thread.spike_detected.connect(self.display_alert)
        detector_thread.log_message.connect(self.log_message)
        detector_thread.error_occurred.connect(self.error_message)
        detector_thread.start()
        self.active_detector_threads[exchange_id] = detector_thread

    def stop_detector(self):
        if not self.active_detector_threads:
            self.log_message("Detektor nie jest aktywny.")
            return

        self.log_message("Zatrzymuję detektor pików...")
        for exchange_id, thread in list(self.active_detector_threads.items()):
            thread.stop()
            thread.wait(5000) # Poczekaj max 5 sekund na zakończenie wątku
            if thread.isRunning():
                self.log_message(f"Wątek dla {exchange_id} nie zakończył się poprawnie.")
            del self.active_detector_threads[exchange_id]

        self.active_detector_threads.clear()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.log_message("Detektor pików zatrzymany.")

    def display_alert(self, message):
        self.alerts_display.append(f"<font color='red'>{message}</font>")
        self.alerts_display.verticalScrollBar().setValue(self.alerts_display.verticalScrollBar().maximum())

    def log_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"[{current_time}] {message}")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())

    def error_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"<font color='orange'>[{current_time}] BŁĄD: {message}</font>")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())
        QMessageBox.warning(self, "Błąd Detektora", message)

    def closeEvent(self, event):
        reply = QMessageBox.question(self, 'Potwierdzenie Zamknięcia',
                                     "Czy na pewno chcesz zamknąć okno detektora? Aktywny detektor zostanie zatrzymany.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            self.stop_detector()
            self.save_settings()
            event.accept()
        else:
            event.ignore()

if __name__ == '__main__':
    app = qasync.QApplication(sys.argv)
    app.setOrganizationName("MojaFirmaPrzyklad")
    app.setApplicationName(CONFIG_DIR_NAME)

    CONFIG_FILE_PATH = get_config_path_standalone()

    if not os.path.exists(os.path.dirname(CONFIG_FILE_PATH)):
        os.makedirs(os.path.dirname(CONFIG_FILE_PATH), exist_ok=True)

    if not os.path.exists(CONFIG_FILE_PATH):
        config_init = configparser.ConfigParser()
        exchange_options_defaults_for_init = {
            "Binance (Spot)": {"id_ccxt": "binance", "type": "spot", "config_section": "binance_spot_config"},
            "Binance (Futures USDT-M)": {"id_ccxt": "binanceusdm", "type": "future", "config_section": "binance_futures_config"},
            "Bybit (Spot)": {"id_ccxt": "bybit", "type": "spot", "config_section": "bybit_spot_config"},
            "Bybit (Perpetual USDT)": {"id_ccxt": "bybit", "type": "swap", "config_section": "bybit_perp_config"}
        }
        for exchange_name, details in exchange_options_defaults_for_init.items():
            section_name = details["config_section"]
            if not config_init.has_section(section_name):
                config_init.add_section(section_name)
                config_init.set(section_name, 'api_key', '')
                config_init.set(section_name, 'api_secret', '')
                config_init.set(section_name, 'scanner_watchlist_pairs', '')

                config_init.set(section_name, 'of_resample_interval', '1s')
                config_init.set(section_name, 'of_delta_mode', 'CVD (Skumulowana Delta)')
                config_init.set(section_name, 'of_aggregation_level', 'Brak')
                config_init.set(section_name, 'of_ob_source', 'Wybrana giełda')
                config_init.set(section_name, 'of_watchlist_pairs', '')

                config_init.set(section_name, 'sd_baseline_minutes', '15')
                config_init.set(section_name, 'sd_time_window', '5')
                config_init.set(section_name, 'sd_volume_threshold_multiplier', '2.0')
                config_init.set(section_name, 'sd_price_threshold_percent', '0.5')
                config_init.set(section_name, 'sd_alert_cooldown_minutes', '5')
                config_init.set(section_name, 'sd_watchlist_pairs', '')
                for i in range(6):
                    config_init.set(section_name, f'cw_chart_{i}_timeframe', ['1h', '4h', '1d', '15m', '1h', '1w'][i])
                config_init.set(section_name, 'cw_watchlist_pairs', '')
                config_init.set(section_name, 'cw_auto_refresh_enabled', 'False')
                config_init.set(section_name, 'cw_refresh_interval_minutes', '5')


        if not config_init.has_section('global_settings'):
            config_init.add_section('global_settings')
            config_init.set('global_settings', 'default_wpr_length', '14')
            config_init.set('global_settings', 'default_ema_length', '9')
            config_init.set('global_settings', 'default_macd_fast', '12')
            config_init.set('global_settings', 'default_macd_slow', '26')
            config_init.set('global_settings', 'default_macd_signal', '9')

            config_init.set('global_settings', 'scanner_selected_timeframes', ','.join(AVAILABLE_TIMEFRAMES))
            config_init.set('global_settings', 'scanner_wr_length', '14')
            config_init.set('global_settings', 'scanner_wr_upper', '-20.0')
            config_init.set('global_settings', 'scanner_wr_lower', '-80.0')
            config_init.set('global_settings', 'scanner_ema_fast', '9')
            config_init.set('global_settings', 'scanner_ema_slow', '21')
            config_init.set('global_settings', 'scanner_check_interval_ms', '5000')
            config_init.set('global_settings', 'selected_exchange', 'Binance (Spot)')


        try:
            with open(CONFIG_FILE_PATH, 'w') as f:
                config_init.write(f)
        except Exception as e:
            print(f"Błąd podczas inicjalizacji pliku konfiguracyjnego w __main__: {e}")


    test_exchange_options = {
        "Binance (Spot)": {"id_ccxt": "binance", "type": "spot", "default_pairs": ["BTC/USDT"], "config_section": "binance_spot_config"},
        "Binance (Futures USDT-M)": {"id_ccxt": "binanceusdm", "type": "future", "default_pairs": ["BTC/USDT"], "config_section": "binance_futures_config"},
        "Bybit (Spot)": {"id_ccxt": "bybit", "type": "spot", "default_pairs": ["BTC/USDT"], "config_section": "bybit_spot_config"},
        "Bybit (Perpetual USDT)": {"id_ccxt": "bybit", "type": "swap", "default_pairs": ["BTC/USDT:USDT"], "config_section": "bybit_perp_config"}
    }

    window = SpikeDetectorWindow(test_exchange_options)
    window.show()

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        sys.exit(loop.run_forever())
