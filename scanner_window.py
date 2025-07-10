# ZREFAKTORYZOWANY I POPRAWIONY PLIK: scanner_window.py

import sys
import os
import configparser
import datetime
import time
import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
from collections import deque
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QLineEdit,
                             QPushButton, QComboBox, QSpinBox, QDoubleSpinBox,
                             QTextEdit, QListWidget, QListWidgetItem, QGroupBox,
                             QMessageBox, QTabWidget, QScrollArea, QSizePolicy,
                             QFormLayout, QCheckBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QEvent
from PyQt6.QtGui import QFont

import utils

CONFIG_FILE_PATH = utils.get_config_path()
AVAILABLE_TIMEFRAMES = utils.AVAILABLE_TIMEFRAMES


class ScanLoopThread(QThread):
    progress_signal = Signal(str)
    result_signal = Signal(dict)
    error_signal = Signal(str)
    finished_signal = Signal()

    def __init__(self, exchange_id_gui, api_key, api_secret, pairs, tfs,
                 wpr_op, wpr_val, ema_op, ema_val, wpr_p, ema_p, delay_seconds,
                 exchange_options_data, notification_settings, parent=None):
        super().__init__(parent)
        self.exchange_id_gui = exchange_id_gui
        self.api_key = api_key
        self.api_secret = api_secret
        self.pairs = pairs
        self.tfs = tfs
        self.wpr_op = wpr_op
        self.wpr_val = wpr_val
        self.ema_op = ema_op
        self.ema_val = ema_val
        self.wpr_p = wpr_p
        self.ema_p = ema_p
        self.delay_seconds = delay_seconds
        self.exchange_options_data = exchange_options_data
        self.notification_settings = notification_settings
        self._is_running = True
        self.cycle_number = 0

    def _perform_actual_scan_logic(self):
        self.progress_signal.emit(f"Rozpoczynanie skanowania dla: {self.exchange_id_gui}")
        if not self.tfs:
            self.error_signal.emit("Nie wybrano interwałów."); return

        sorted_selected_timeframes = sorted(self.tfs, key=utils.get_timeframe_duration_for_sort, reverse=True)

        self.progress_signal.emit(f"Wybrane interwały: {', '.join(sorted_selected_timeframes)}")
        self.progress_signal.emit(f"Parametry: W%R({self.wpr_p}), EMA({self.ema_p}) | Kryteria: W%R {self.wpr_op} {self.wpr_val}, EMA(W%R) {self.ema_op} {self.ema_val}")

        selected_config = self.exchange_options_data.get(self.exchange_id_gui)
        if not selected_config:
            self.error_signal.emit(f"Błąd konfiguracji dla {self.exchange_id_gui}"); return

        ccxt_exchange_id = selected_config["id_ccxt"]
        market_type = selected_config["type"]
        ccxt_options = {'defaultType': market_type} if market_type in ['future', 'swap'] else {}

        exchange = None
        try:
            exchange_params = {'enableRateLimit': True, 'options': ccxt_options, 'timeout': 30000}
            if self.api_key and self.api_secret:
                exchange_params['apiKey'] = self.api_key
                exchange_params['secret'] = self.api_secret

            exchange = getattr(ccxt, ccxt_exchange_id)(exchange_params)
            exchange.load_markets()
            self.progress_signal.emit(f"  Połączono z {ccxt_exchange_id} i załadowano rynki.")
        except Exception as e:
            self.error_signal.emit(f"Błąd inicjalizacji giełdy {ccxt_exchange_id}: {e}"); return

        required_candles = self.wpr_p + self.ema_p + 50

        for i, pair_symbol in enumerate(self.pairs):
            if not self._is_running: break

            self.progress_signal.emit(f"Analizowanie: {pair_symbol} ({i+1}/{len(self.pairs)})")
            all_tfs_ok, wpr_rep, ema_rep, first_tf_ok = True, None, None, False
            volume_24h_str = "N/A"

            for tf_idx, tf in enumerate(sorted_selected_timeframes):
                if not self._is_running: all_tfs_ok = False; break
                try:
                    ohlcv = exchange.fetch_ohlcv(pair_symbol, timeframe=tf, limit=required_candles)
                    if not ohlcv or len(ohlcv) < (self.wpr_p + self.ema_p - 1): all_tfs_ok = False; break
                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                    if df.empty: all_tfs_ok=False; break
                    df.ta.willr(length=self.wpr_p, append=True)
                    wpr_col=f'WILLR_{self.wpr_p}'
                    if wpr_col not in df.columns or df[wpr_col].isna().all(): all_tfs_ok=False; break
                    current_wpr = df[wpr_col].iloc[-1]
                    if pd.isna(current_wpr): all_tfs_ok=False; break
                    ema_series = ta.ema(df[wpr_col].dropna(),length=self.ema_p)
                    if ema_series is None or ema_series.empty or ema_series.isna().all(): all_tfs_ok=False; break
                    current_ema = ema_series.iloc[-1]
                    if pd.isna(current_ema): all_tfs_ok=False; break
                    wpr_ok = (current_wpr >= self.wpr_val) if self.wpr_op == ">=" else (current_wpr <= self.wpr_val)
                    ema_ok = (current_ema >= self.ema_val) if self.ema_op == ">=" else (current_ema <= self.ema_val)
                    if not (wpr_ok and ema_ok): all_tfs_ok=False; break
                    else:
                        if tf_idx == 0: wpr_rep, ema_rep, first_tf_ok = current_wpr, current_ema, True
                except Exception as e: self.error_signal.emit(f"  Błąd dla {pair_symbol} @ {tf}: {e}"); all_tfs_ok=False; break

            if all_tfs_ok and first_tf_ok:
                try:
                    ticker = exchange.fetch_ticker(pair_symbol)
                    if ticker and 'quoteVolume' in ticker and ticker['quoteVolume'] is not None:
                        quote_curr = pair_symbol.split('/')[-1].split(':')[0]
                        volume_24h_str = utils.format_large_number(ticker['quoteVolume'], currency_symbol=quote_curr)
                except Exception as e: self.progress_signal.emit(f"  Błąd pobierania tickera: {e}")

                self.result_signal.emit({'symbol': pair_symbol, 'all_tfs_ok': True, 'wr_value': wpr_rep, 'ema_value': ema_rep, 'volume_24h_str': volume_24h_str})
                if self.notification_settings.get("enabled"):
                    message_body = (f"🔔 Alert: <b>{pair_symbol}</b>\n"
                                    f"Giełda: {self.exchange_id_gui}\n"
                                    f"W%R({self.wpr_p}): {wpr_rep:.2f}, EMA({self.ema_p}): {ema_rep:.2f}\n"
                                    f"Wolumen 24h: {volume_24h_str}")
                    if self.notification_settings.get("method") == "Telegram":
                        utils.send_telegram_notification(self.notification_settings.get("telegram_token"), self.notification_settings.get("telegram_chat_id"), message_body, self.progress_signal)
                    elif self.notification_settings.get("method") == "Email":
                        utils.send_email_notification(self.notification_settings.get("email_address"), self.notification_settings.get("email_password"), self.notification_settings.get("email_address"), self.notification_settings.get("smtp_server"), int(self.notification_settings.get("smtp_port")), f"Krypto Skaner Alert: {pair_symbol}", message_body, self.progress_signal)
        self.progress_signal.emit("Cykl skanowania zakończony.")

    def run(self):
        while self._is_running:
            self.cycle_number += 1
            self.progress_signal.emit(f"--- Rozpoczynanie cyklu skanowania nr {self.cycle_number} ---")
            self.result_signal.emit({'clear_results_table': True})

            if not self.pairs:
                self.error_signal.emit("Lista par do skanowania jest pusta."); break

            try:
                if self._is_running: self._perform_actual_scan_logic()
            except Exception as e:
                self.error_signal.emit(f"Krytyczny błąd w pętli skanowania (cykl {self.cycle_number}): {e}")

            if not self._is_running: break

            self.progress_signal.emit(f"Cykl {self.cycle_number} zakończony. Następny za {self.delay_seconds // 60} min.")

            # ---- POPRAWKA ----
            # Zamiast jednego długiego snu, robimy pętlę z krótkimi przerwami,
            # sprawdzając co sekundę, czy mamy zakończyć pracę.
            for _ in range(self.delay_seconds):
                if not self._is_running:
                    break
                # Używamy msleep (milisekundy), które jest częścią QThread i jest przerywalne
                self.msleep(1000)
            # ---- KONIEC POPRAWKI ----

        self.finished_signal.emit()

    def stop(self):
        self.progress_signal.emit("Wysłano żądanie zatrzymania skanowania...")
        self._is_running = False

# --- GŁÓWNA KLASA OKNA SKANERA ---
class ScannerWindow(QMainWindow):
    # ... reszta klasy pozostaje bez zmian ...
    def __init__(self, exchange_options: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Krypto Skaner - Narzędzie Skanera")
        self.setGeometry(150, 150, 1000, 850)

        self.exchange_options = exchange_options
        self.current_scan_thread = None
        self.fetched_markets_data = {}
        self.exchange_fetch_threads = {}

        self.config_path = CONFIG_FILE_PATH
        self.config = configparser.ConfigParser()

        self.init_ui()
        self.load_settings()
        self.apply_settings_to_ui()
        self.on_exchange_selection_changed()

    def init_ui(self):
        self.central_widget = QWidget()
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.setCentralWidget(self.scroll_area)

        self.scroll_content_widget = QWidget()
        self.main_layout = QVBoxLayout(self.scroll_content_widget)
        self.scroll_area.setWidget(self.scroll_content_widget)

        top_panel = QGroupBox("Wybór Giełdy i Pary")
        top_layout = QHBoxLayout(top_panel)
        self.exchange_combo = QComboBox()
        self.exchange_combo.addItems(self.exchange_options.keys())
        self.exchange_combo.currentTextChanged.connect(self.on_exchange_selection_changed)
        top_layout.addWidget(QLabel("Giełda:"))
        top_layout.addWidget(self.exchange_combo)
        self.available_pairs_list = QListWidget()
        self.available_pairs_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.available_pairs_list.itemDoubleClicked.connect(self.add_to_watchlist_from_available)
        top_layout.addWidget(QLabel("Dostępne Pary:"))
        top_layout.addWidget(self.available_pairs_list)
        self.main_layout.addWidget(top_panel)

        watchlist_group = QGroupBox("Watchlista")
        watchlist_layout = QVBoxLayout(watchlist_group)
        self.watchlist = QListWidget()
        self.watchlist.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.watchlist.setMinimumHeight(150)
        watchlist_layout.addWidget(self.watchlist)
        watchlist_buttons_layout = QHBoxLayout()
        self.add_selected_button = QPushButton("Dodaj wybrane >"); self.add_selected_button.clicked.connect(self.add_to_watchlist_from_available)
        watchlist_buttons_layout.addWidget(self.add_selected_button)
        self.add_all_button = QPushButton("Dodaj wszystkie >>"); self.add_all_button.clicked.connect(self.add_all_to_watchlist)
        watchlist_buttons_layout.addWidget(self.add_all_button)
        self.remove_selected_button = QPushButton("< Usuń wybrane"); self.remove_selected_button.clicked.connect(self.remove_from_watchlist)
        watchlist_buttons_layout.addWidget(self.remove_selected_button)
        self.remove_all_button = QPushButton("<< Usuń wszystkie"); self.remove_all_button.clicked.connect(self.remove_all_from_watchlist)
        watchlist_buttons_layout.addWidget(self.remove_all_button)
        watchlist_layout.addLayout(watchlist_buttons_layout)
        self.main_layout.addWidget(watchlist_group)

        params_group = QGroupBox("Parametry Skanera"); params_layout = QFormLayout(params_group)
        timeframes_group = QGroupBox("Interwały do skanowania:"); timeframes_h_layout = QHBoxLayout(timeframes_group)
        self.timeframe_checkboxes = {}
        for tf_text in AVAILABLE_TIMEFRAMES:
            checkbox = QCheckBox(tf_text); checkbox.setChecked(True); checkbox.stateChanged.connect(self.save_settings); self.timeframe_checkboxes[tf_text] = checkbox; timeframes_h_layout.addWidget(checkbox)
        params_layout.addRow(timeframes_group)
        self.wr_length_spin = QSpinBox(); self.wr_length_spin.setRange(5, 200); self.wr_length_spin.setValue(14); self.wr_length_spin.valueChanged.connect(self.save_settings); params_layout.addRow("W%R Długość:", self.wr_length_spin)
        wpr_criteria_widget = QWidget(); wpr_h_layout = QHBoxLayout(wpr_criteria_widget); wpr_h_layout.setContentsMargins(0, 0, 0, 0)
        self.wpr_operator_combo = QComboBox(); self.wpr_operator_combo.addItems([">=", "<="]); self.wpr_operator_combo.currentTextChanged.connect(self.save_settings); wpr_h_layout.addWidget(self.wpr_operator_combo)
        self.wpr_value_spin = QDoubleSpinBox(); self.wpr_value_spin.setRange(-100.0, 0.0); self.wpr_value_spin.setSingleStep(1.0); self.wpr_value_spin.setValue(-20.0); self.wpr_value_spin.setDecimals(1); self.wpr_value_spin.valueChanged.connect(self.save_settings); wpr_h_layout.addWidget(self.wpr_value_spin)
        params_layout.addRow("W%R Warunek:", wpr_criteria_widget)
        self.ema_wpr_length_spin = QSpinBox(); self.ema_wpr_length_spin.setRange(5, 200); self.ema_wpr_length_spin.setValue(9); self.ema_wpr_length_spin.valueChanged.connect(self.save_settings); params_layout.addRow("EMA(W%R) Długość:", self.ema_wpr_length_spin)
        ema_wpr_criteria_widget = QWidget(); ema_wpr_h_layout = QHBoxLayout(ema_wpr_criteria_widget); ema_wpr_h_layout.setContentsMargins(0, 0, 0, 0)
        self.ema_wpr_operator_combo = QComboBox(); self.ema_wpr_operator_combo.addItems([">=", "<="]); self.ema_wpr_operator_combo.currentTextChanged.connect(self.save_settings); ema_wpr_h_layout.addWidget(self.ema_wpr_operator_combo)
        self.ema_wpr_value_spin = QDoubleSpinBox(); self.ema_wpr_value_spin.setRange(-100.0, 0.0); self.ema_wpr_value_spin.setSingleStep(1.0); self.ema_wpr_value_spin.setValue(-30.0); self.ema_wpr_value_spin.setDecimals(1); self.ema_wpr_value_spin.valueChanged.connect(self.save_settings); ema_wpr_h_layout.addWidget(self.ema_wpr_value_spin)
        params_layout.addRow("EMA(W%R) Warunek:", ema_wpr_criteria_widget)
        self.scan_delay_spin = QSpinBox(); self.scan_delay_spin.setRange(1, 360); self.scan_delay_spin.setValue(5); self.scan_delay_spin.setSuffix(" min"); self.scan_delay_spin.valueChanged.connect(self.save_settings); params_layout.addRow("Odstęp między cyklami (min):", self.scan_delay_spin)
        notifications_settings_group = QGroupBox("Ustawienia Powiadomień"); notifications_settings_layout = QFormLayout(notifications_settings_group)
        self.enable_notifications_checkbox = QCheckBox("Włącz powiadomienia"); self.enable_notifications_checkbox.setChecked(False); self.enable_notifications_checkbox.stateChanged.connect(self.save_settings); self.enable_notifications_checkbox.stateChanged.connect(self.toggle_notification_method_visibility); notifications_settings_layout.addRow(self.enable_notifications_checkbox)
        self.notification_method_combo = QComboBox(); self.notification_method_combo.addItems(["Brak", "Telegram", "Email"]); self.notification_method_combo.currentTextChanged.connect(self.save_settings); self.notification_method_combo.currentTextChanged.connect(self.toggle_notification_method_visibility); notifications_settings_layout.addRow("Metoda Powiadomienia:", self.notification_method_combo)
        params_layout.addRow(notifications_settings_group); self.main_layout.addWidget(params_group)

        control_buttons_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Skanowania"); self.start_button.clicked.connect(self.start_scanning); control_buttons_layout.addWidget(self.start_button)
        self.stop_button = QPushButton("Zatrzymaj Skanowanie"); self.stop_button.clicked.connect(self.stop_scanning); self.stop_button.setEnabled(False); control_buttons_layout.addWidget(self.stop_button)
        self.main_layout.addLayout(control_buttons_layout)

        results_group = QGroupBox("Wyniki Skanowania"); results_layout = QVBoxLayout(results_group)
        self.results_display = QTextEdit(); self.results_display.setReadOnly(True); self.results_display.setMinimumHeight(150); results_layout.addWidget(self.results_display); self.main_layout.addWidget(results_group)
        self.log_display = QTextEdit(); self.log_display.setReadOnly(True); self.log_display.setFixedHeight(80); self.main_layout.addWidget(QLabel("Logi Skanera:")); self.main_layout.addWidget(self.log_display)
        self.toggle_notification_method_visibility()

    def toggle_notification_method_visibility(self):
        is_enabled = self.enable_notifications_checkbox.isChecked()
        self.notification_method_combo.setVisible(is_enabled)
        parent_layout = self.notification_method_combo.parentWidget().layout()
        if isinstance(parent_layout, QFormLayout):
            for i in range(parent_layout.rowCount()):
                if parent_layout.itemAt(i, QFormLayout.ItemRole.FieldRole).widget() is self.notification_method_combo:
                    parent_layout.itemAt(i, QFormLayout.ItemRole.LabelRole).widget().setVisible(is_enabled)
                    break

    def load_settings(self):
        self.config.read(CONFIG_FILE_PATH); self.log_message("Załadowano ustawienia z pliku.")

    def apply_settings_to_ui(self):
        if not self.config.has_section('global_settings'): return
        gs = self.config['global_settings']
        self.exchange_combo.setCurrentText(gs.get('selected_exchange', 'Binance (Spot)'))
        selected_tfs_list = [tf.strip() for tf in gs.get('scanner_selected_timeframes', ','.join(AVAILABLE_TIMEFRAMES)).split(',') if tf.strip()]
        for tf, cb in self.timeframe_checkboxes.items(): cb.setChecked(tf in selected_tfs_list)
        self.wr_length_spin.setValue(utils.safe_int_cast(gs.get('scanner_wr_length', '14')))
        self.wpr_operator_combo.setCurrentText(gs.get('scanner_wr_operator', '>='))
        self.wpr_value_spin.setValue(utils.safe_float_cast(gs.get('scanner_wr_value', '-20.0')))
        self.ema_wpr_length_spin.setValue(utils.safe_int_cast(gs.get('scanner_ema_wpr_length', '9')))
        self.ema_wpr_operator_combo.setCurrentText(gs.get('scanner_ema_wpr_operator', '>='))
        self.ema_wpr_value_spin.setValue(utils.safe_float_cast(gs.get('scanner_ema_wpr_value', '-30.0')))
        self.scan_delay_spin.setValue(utils.safe_int_cast(gs.get('scanner_scan_delay_minutes', '5')))
        self.enable_notifications_checkbox.setChecked(gs.getboolean('scanner_notification_enabled', False))
        self.notification_method_combo.setCurrentText(gs.get('scanner_notification_method', 'Brak'))
        self.toggle_notification_method_visibility(); self.watchlist.clear()

    def save_settings(self):
        if not self.config.has_section('global_settings'): self.config.add_section('global_settings')
        gs = self.config['global_settings']
        gs['scanner_selected_timeframes'] = ','.join([tf for tf, cb in self.timeframe_checkboxes.items() if cb.isChecked()])
        gs['scanner_wr_length'] = str(self.wr_length_spin.value()); gs['scanner_wr_operator'] = self.wpr_operator_combo.currentText(); gs['scanner_wr_value'] = str(self.wpr_value_spin.value())
        gs['scanner_ema_wpr_length'] = str(self.ema_wpr_length_spin.value()); gs['scanner_ema_wpr_operator'] = self.ema_wpr_operator_combo.currentText(); gs['scanner_ema_wpr_value'] = str(self.ema_wpr_value_spin.value())
        gs['scanner_scan_delay_minutes'] = str(self.scan_delay_spin.value()); gs['selected_exchange'] = self.exchange_combo.currentText()
        gs['scanner_notification_enabled'] = str(self.enable_notifications_checkbox.isChecked()); gs['scanner_notification_method'] = self.notification_method_combo.currentText()
        config = self.exchange_options.get(self.exchange_combo.currentText())
        if config:
            section = config["config_section"];
            if not self.config.has_section(section): self.config.add_section(section)
            self.config.set(section, 'scanner_watchlist_pairs', ",".join([self.watchlist.item(i).text() for i in range(self.watchlist.count())]))
        try:
            with open(CONFIG_FILE_PATH, 'w') as f: self.config.write(f)
        except Exception as e: self.error_message(f"Błąd zapisu ustawień: {e}")

    def on_exchange_selection_changed(self):
        self.available_pairs_list.clear(); self.watchlist.clear()
        self.fetch_markets_for_selected_exchange()
        config = self.exchange_options.get(self.exchange_combo.currentText())
        if config and self.config.has_section(config["config_section"]):
            pairs_str = self.config.get(config["config_section"], 'scanner_watchlist_pairs', fallback='')
            if pairs_str: self.watchlist.addItems([p.strip() for p in pairs_str.split(',') if p.strip()])
            elif config.get("default_pairs"): self.watchlist.addItems(config["default_pairs"])

    def fetch_markets_for_selected_exchange(self):
        details = self.exchange_options.get(self.exchange_combo.currentText())
        if details:
            if details["id_ccxt"] in self.exchange_fetch_threads and self.exchange_fetch_threads[details["id_ccxt"]].isRunning(): return
            fetch_thread = utils.FetchMarketsThread(details["id_ccxt"], details["type"]); fetch_thread.markets_fetched.connect(self.update_available_pairs); fetch_thread.error_occurred.connect(self.error_message); fetch_thread.finished.connect(lambda: self.log_message("Rynki załadowane.")); fetch_thread.start(); self.exchange_fetch_threads[details["id_ccxt"]] = fetch_thread

    def update_available_pairs(self, markets):
        self.available_pairs_list.clear(); self.fetched_markets_data.clear()
        for market in markets:
            item = QListWidgetItem(market['symbol']); item.setData(Qt.ItemDataRole.UserRole, market); self.available_pairs_list.addItem(item); self.fetched_markets_data[market['symbol']] = market

    def add_to_watchlist_from_available(self):
        for item in self.available_pairs_list.selectedItems():
            if not self.watchlist.findItems(item.text(), Qt.MatchFlag.MatchExactly): self.watchlist.addItem(QListWidgetItem(item.text()))
        self.save_settings()

    def add_all_to_watchlist(self):
        for i in range(self.available_pairs_list.count()):
            symbol = self.available_pairs_list.item(i).text()
            if not self.watchlist.findItems(symbol, Qt.MatchFlag.MatchExactly): self.watchlist.addItem(QListWidgetItem(symbol))
        self.save_settings()

    def remove_from_watchlist(self):
        for item in self.watchlist.selectedItems(): self.watchlist.takeItem(self.watchlist.row(item))
        self.save_settings()

    def remove_all_from_watchlist(self):
        self.watchlist.clear(); self.save_settings()

    def start_scanning(self):
        if self.current_scan_thread and self.current_scan_thread.isRunning(): self.log_message("Skanowanie już trwa."); return
        details = self.exchange_options.get(self.exchange_combo.currentText())
        if not details: self.error_message("Nie wybrano giełdy."); return
        api_key = self.config.get(details['config_section'], 'api_key', fallback=''); api_secret = self.config.get(details['config_section'], 'api_secret', fallback='')
        notification_settings = {}
        if self.config.has_section('global_settings'):
            gs = self.config['global_settings']
            notification_settings = {"enabled": self.enable_notifications_checkbox.isChecked(), "method": self.notification_method_combo.currentText(), "telegram_token": gs.get('notification_telegram_token'), "telegram_chat_id": gs.get('notification_telegram_chat_id'), "email_address": gs.get('notification_email_address'), "email_password": gs.get('notification_email_password'), "smtp_server": gs.get('notification_smtp_server'), "smtp_port": gs.get('notification_smtp_port', '587')}
        pairs_to_scan = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]
        if not pairs_to_scan: self.error_message("Watchlista jest pusta."); return
        selected_tfs = [tf for tf, cb in self.timeframe_checkboxes.items() if cb.isChecked()]
        if not selected_tfs: self.error_message("Wybierz przynajmniej jeden interwał."); return
        self.results_display.clear(); self.log_message("Rozpoczynam cykliczne skanowanie..."); self.start_button.setEnabled(False); self.stop_button.setEnabled(True)
        self.current_scan_thread = ScanLoopThread(
            self.exchange_combo.currentText(), api_key, api_secret, pairs_to_scan, selected_tfs,
            self.wpr_operator_combo.currentText(), self.wpr_value_spin.value(),
            self.ema_wpr_operator_combo.currentText(), self.ema_wpr_value_spin.value(),
            self.wr_length_spin.value(), self.ema_wpr_length_spin.value(),
            self.scan_delay_spin.value() * 60, self.exchange_options, notification_settings)
        self.current_scan_thread.progress_signal.connect(self.log_message); self.current_scan_thread.result_signal.connect(self.process_scan_data_or_clear); self.current_scan_thread.error_signal.connect(self.error_message); self.current_scan_thread.finished_signal.connect(self.on_full_scan_finished)
        self.current_scan_thread.start()

    def stop_scanning(self):
        if self.current_scan_thread and self.current_scan_thread.isRunning():
            self.log_message("Zatrzymuję cykliczne skanowanie..."); self.current_scan_thread.stop(); self.current_scan_thread.wait(2000)

    def process_scan_data_or_clear(self, data):
        if data.get('clear_results_table'): self.results_display.clear(); return
        if data['all_tfs_ok']:
            msg = (f"OK: <b>{data['symbol']}</b> (W%R:{data['wr_value']:.2f}, EMA:{data['ema_value']:.2f}, Wol:{data['volume_24h_str']})")
            self.results_display.append(f"<font color='green'>{msg}</font>")

    def on_full_scan_finished(self):
        self.log_message("Cykliczne skanowanie zakończone."); self.start_button.setEnabled(True); self.stop_button.setEnabled(False); self.current_scan_thread = None

    def log_message(self, message):
        self.log_display.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}")

    def error_message(self, message):
        self.log_display.append(f"<font color='orange'>[{datetime.datetime.now().strftime('%H:%M:%S')}] BŁĄD: {message}</font>"); QMessageBox.warning(self, "Błąd Skanera", message)

    def closeEvent(self, event: QEvent):
        reply = QMessageBox.question(self, 'Potwierdzenie Zamknięcia', "Czy na pewno chcesz zamknąć okno skanera? Aktywne skanowanie zostanie zatrzymane.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes: self.stop_scanning(); self.save_settings(); event.accept()
        else: event.ignore()
