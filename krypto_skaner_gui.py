import sys
import time
import os
import configparser
import ccxt
import pandas as pd
import pandas_ta as ta
import requests
from PySide6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QWidget,
                             QComboBox, QPushButton, QTableWidget, QTextEdit,
                             QTableWidgetItem, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QHBoxLayout, QCheckBox, QGroupBox,
                             QFormLayout, QDoubleSpinBox, QSpinBox, QListWidget,
                             QListWidgetItem, QSizePolicy, QScrollArea)
from PySide6.QtGui import QAction
from PySide6.QtCore import QThread, Signal, QStandardPaths, Qt

from chart_window import MultiChartWindow

CONFIG_DIR_NAME = "KryptoSkaner"
CONFIG_FILE_NAME = "app_settings.ini"

def get_config_path():
    config_dir = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    app_config_path = os.path.join(config_dir, CONFIG_DIR_NAME)
    if not os.path.exists(app_config_path):
        os.makedirs(app_config_path, exist_ok=True)
    return os.path.join(app_config_path, CONFIG_FILE_NAME)

CONFIG_FILE_PATH = get_config_path()

DEFAULT_WPR_LENGTH, DEFAULT_EMA_WPR_LENGTH, DEFAULT_SCAN_DELAY_MINUTES = 14, 9, 5
AVAILABLE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w']
INITIAL_PAIRS_BYBIT_SPOT = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
INITIAL_PAIRS_BYBIT_PERP = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
INITIAL_PAIRS_BINANCE_SPOT = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT']
INITIAL_PAIRS_BINANCE_FUTURES = ['BTC/USDT', 'ETH/USDT']

TIMEFRAME_DURATIONS_MINUTES = {
    '1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '12h': 720,
    '1d': 1440, '1w': 10080
}
def get_timeframe_duration_for_sort(tf_string): return TIMEFRAME_DURATIONS_MINUTES.get(tf_string.lower(), 0)

def format_large_number(num, currency_symbol=""):
    if num is None: return "N/A"
    abs_num = abs(num); sign = "-" if num < 0 else ""
    if abs_num >= 1_000_000_000_000: val_str = f"{abs_num/1_000_000_000_000:.2f} bln"
    elif abs_num >= 1_000_000_000: val_str = f"{abs_num/1_000_000_000:.2f} mld"
    elif abs_num >= 1_000_000: val_str = f"{abs_num/1_000_000:.2f} mln"
    elif abs_num >= 1_000: val_str = f"{abs_num/1_000:.2f} tys."
    else: val_str = f"{abs_num:,.0f}"
    return f"{sign}{val_str.replace('.', ',')} {currency_symbol}".strip()

def send_telegram_notification(bot_token, chat_id, message, progress_callback):
    if not bot_token or not chat_id: progress_callback.emit("<font color='orange'>Ostrz.: Token Telegram lub Chat ID nieskonfigurowane.</font>"); return
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"; payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
    try:
        response = requests.post(telegram_url, data=payload, timeout=10); response.raise_for_status()
        if response.json().get("ok"): progress_callback.emit("Powiadomienie Telegram wysłane.")
        else: progress_callback.emit(f"<font color='red'>Błąd Telegram: {response.json().get('description', 'Brak szczegółów')}</font>")
    except Exception as e: progress_callback.emit(f"<font color='red'>Błąd Telegram: {str(e)}</font>")

def perform_actual_scan(exchange_id_gui_config_key, api_key, api_secret, pairs_to_scan, selected_timeframes,
                        wpr_period_from_gui, ema_period_from_gui, wpr_operator_cond, wpr_value_cond,
                        ema_wpr_operator_cond, ema_wpr_value_cond, notification_settings,
                        progress_callback, result_callback, error_callback, app_instance):
    progress_callback.emit(f"Rozpoczynanie skanowania dla: {exchange_id_gui_config_key}")
    if not selected_timeframes: error_callback.emit("Nie wybrano interwałów."); return
    sorted_selected_timeframes = sorted(selected_timeframes, key=get_timeframe_duration_for_sort, reverse=True)
    progress_callback.emit(f"Wybrane interwały: {', '.join(sorted_selected_timeframes)}")
    progress_callback.emit(f"Parametry: W%R({wpr_period_from_gui}), EMA({ema_period_from_gui}) | Kryteria: W%R {wpr_operator_cond} {wpr_value_cond}, EMA(W%R) {ema_wpr_operator_cond} {ema_wpr_value_cond}")
    selected_config = app_instance.exchange_options.get(exchange_id_gui_config_key)
    if not selected_config: error_callback.emit(f"Błąd konfiguracji dla {exchange_id_gui_config_key}"); return
    ccxt_exchange_id = selected_config["id_ccxt"]; market_type = selected_config["type"]
    ccxt_options = {};
    if market_type == 'future': ccxt_options['defaultType'] = 'future'
    elif market_type == 'swap': ccxt_options['defaultType'] = 'swap'
    try:
        exchange_params = {'enableRateLimit': True, 'options': ccxt_options, 'timeout': 30000}
        if api_key and api_secret: exchange_params['apiKey'] = api_key; exchange_params['secret'] = api_secret; progress_callback.emit(f"  Inicjalizacja {ccxt_exchange_id} (typ: {market_type}) z kluczami API.")
        else: progress_callback.emit(f"  Inicjalizacja {ccxt_exchange_id} (typ: {market_type}) bez kluczy API.")
        exchange = getattr(ccxt, ccxt_exchange_id)(exchange_params)
        try: exchange.load_markets(); progress_callback.emit(f"  Połączono z {ccxt_exchange_id} i załadowano rynki.")
        except Exception as e_markets: progress_callback.emit(f"  Ostrzeżenie (rynki) {ccxt_exchange_id}: {type(e_markets).__name__} - {str(e_markets)}")
    except Exception as e: error_callback.emit(f"Błąd inicjalizacji giełdy {ccxt_exchange_id}: {type(e).__name__} - {str(e)}"); return
    required_candles = wpr_period_from_gui + ema_period_from_gui + 50
    for i, pair_symbol in enumerate(pairs_to_scan):
        if QThread.currentThread().isInterruptionRequested(): progress_callback.emit("Przerwano analizę par."); return
        progress_callback.emit(f"Analizowanie: {pair_symbol} ({i+1}/{len(pairs_to_scan)})")
        all_tfs_ok = True; wpr_rep, ema_rep, first_tf_ok = None, None, False
        for tf_idx, tf in enumerate(sorted_selected_timeframes):
            if QThread.currentThread().isInterruptionRequested(): progress_callback.emit(f"Przerwano analizę TF dla {pair_symbol}."); return
            try:
                progress_callback.emit(f"  Pobieranie {pair_symbol} @ {tf}...")
                ohlcv = exchange.fetch_ohlcv(pair_symbol, timeframe=tf, limit=required_candles)
                if not ohlcv or len(ohlcv) < (wpr_period_from_gui + ema_period_from_gui -1): progress_callback.emit(f"  Brak danych. Pomijam."); all_tfs_ok = False; break
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                if df.empty: progress_callback.emit(f"  Puste dane. Pomijam."); all_tfs_ok = False; break
                df.ta.willr(length=wpr_period_from_gui, append=True); wpr_col = f'WILLR_{wpr_period_from_gui}'
                if wpr_col not in df.columns or df[wpr_col].isna().all(): progress_callback.emit(f"  Błąd W%R. Pomijam."); all_tfs_ok = False; break
                current_wpr = df[wpr_col].iloc[-1]
                if pd.isna(current_wpr): progress_callback.emit(f"  W%R NaN. Pomijam."); all_tfs_ok = False; break
                ema_series = ta.ema(df[wpr_col].dropna(), length=ema_period_from_gui)
                if ema_series is None or ema_series.empty or ema_series.isna().all(): progress_callback.emit(f"  Błąd EMA(W%R). Pomijam."); all_tfs_ok = False; break
                current_ema = ema_series.iloc[-1]
                if pd.isna(current_ema): progress_callback.emit(f"  EMA(W%R) NaN. Pomijam."); all_tfs_ok = False; break
                wpr_ok = (current_wpr >= wpr_value_cond) if wpr_operator_cond == ">=" else (current_wpr <= wpr_value_cond)
                ema_ok = (current_ema >= ema_wpr_value_cond) if ema_wpr_operator_cond == ">=" else (current_ema <= ema_wpr_value_cond)
                wpr_ok_str = f"<font color='green'>True</font>" if wpr_ok else f"<font color='red'>False</font>"; ema_ok_str = f"<font color='green'>True</font>" if ema_ok else f"<font color='red'>False</font>"
                progress_callback.emit(f"    {tf}: W%R={current_wpr:.2f} ({wpr_ok_str}), EMA={current_ema:.2f} ({ema_ok_str})")
                if not (wpr_ok and ema_ok): progress_callback.emit(f"    <font color='red'>Warunki niespełnione.</font>"); all_tfs_ok = False; break
                else:
                    progress_callback.emit(f"    <font color='green'>Warunki SPEŁNIONE.</font>")
                    if tf_idx == 0: wpr_rep, ema_rep, first_tf_ok = current_wpr, current_ema, True
            except Exception as e: error_callback.emit(f"  Błąd dla {pair_symbol} @ {tf}: {type(e).__name__} - {str(e)}"); all_tfs_ok = False; break
        if all_tfs_ok and first_tf_ok:
            if wpr_rep is not None and ema_rep is not None:
                vol_str, cap_str, rank_str = "N/A", "N/A", "N/A"
                try:
                    ticker = exchange.fetch_ticker(pair_symbol)
                    if ticker and 'quoteVolume' in ticker and ticker['quoteVolume'] is not None:
                        quote_curr = pair_symbol.split('/')[-1].split(':')[0]
                        vol_str = format_large_number(ticker['quoteVolume'], currency_symbol=quote_curr)
                        progress_callback.emit(f"    Wolumen 24h: {vol_str}")
                except Exception as e: progress_callback.emit(f"    Błąd pobierania tickera: {str(e)}")
                result_data = [pair_symbol, wpr_rep, ema_rep, cap_str, vol_str, rank_str]
                result_callback.emit(result_data)
                if notification_settings.get("enabled") and notification_settings.get("method") == "Telegram":
                    tel_token, tel_chat_id = notification_settings.get("telegram_token"), notification_settings.get("telegram_chat_id")
                    msg = (f"🔔 Alert: <b>{pair_symbol}</b>\n"
                           f"Giełda: {exchange_id_gui_config_key}\n"
                           f"W%R({wpr_period_from_gui}): {wpr_rep:.2f}, EMA({ema_period_from_gui}): {ema_rep:.2f}\n"
                           f"Wolumen 24h: {vol_str}")
                    send_telegram_notification(tel_token, tel_chat_id, msg, progress_callback)
            else: error_callback.emit(f"Błąd wewn.: Brak W%R/EMA dla {pair_symbol}.")
        else: progress_callback.emit(f"  {pair_symbol} NIE spełnia kryteriów.\n")
    progress_callback.emit("Cykl skanowania zakończony.")

class ScanThread(QThread):
    progress_signal, result_signal, error_signal, finished_signal = Signal(str), Signal(list), Signal(str), Signal()
    def __init__(self, exchange_id_gui, api_key, api_secret, pairs, tfs, wpr_op, wpr_val, ema_op, ema_val, wpr_p, ema_p, delay, notif, app, parent=None):
        super().__init__(parent)
        (self.exchange_id_gui, self.api_key, self.api_secret, self.pairs, self.tfs, self.wpr_op, self.wpr_val, self.ema_op, self.ema_val, self.wpr_p, self.ema_p, self.delay, self.notif, self.app) = \
        (exchange_id_gui, api_key, api_secret, pairs, tfs, wpr_op, wpr_val, ema_op, ema_val, wpr_p, ema_p, delay, notif, app)
        self._is_running, self.cycle_number = True, 0
    def run(self):
        while self._is_running and not self.isInterruptionRequested():
            self.cycle_number += 1
            self.progress_signal.emit(f"--- Rozpoczynanie cyklu skanowania nr {self.cycle_number} ---")
            if hasattr(self.app, 'clear_results_signal'): self.app.clear_results_signal.emit()
            try:
                if not self.pairs: self.error_signal.emit("Lista par pusta."); break
                if not self.tfs: self.error_signal.emit("Nie wybrano interwałów."); break
                perform_actual_scan(self.exchange_id_gui, self.api_key, self.api_secret, self.pairs, self.tfs, self.wpr_p, self.ema_p, self.wpr_op, self.wpr_val, self.ema_op, self.ema_val, self.notif, self.progress_signal, self.result_signal, self.error_signal, self.app)
                self.progress_signal.emit(f"Cykl {self.cycle_number} zakończony. Następny za {self.delay // 60} min.")
            except Exception as e: self.error_signal.emit(f"Krytyczny błąd w pętli (cykl {self.cycle_number}): {type(e).__name__} - {str(e)}")
            for _ in range(self.delay):
                if self.isInterruptionRequested(): self._is_running = False; break
                time.sleep(1)
            if not self._is_running: break
        self.finished_signal.emit()
    def stop(self): self._is_running = False; self.requestInterruption()

class FetchMarketsThread(QThread):
    markets_fetched_signal, error_signal, finished_signal = Signal(list), Signal(str), Signal()
    def __init__(self, exchange_id_ccxt, market_type_filter, parent=None):
        super().__init__(parent); self.exchange_id_ccxt, self.market_type_filter = exchange_id_ccxt, market_type_filter
    def run(self):
        try:
            exchange = getattr(ccxt, self.exchange_id_ccxt)({'enableRateLimit': True, 'timeout': 30000})
            markets = exchange.load_markets()
            available_pairs = [symbol for symbol, market_data in markets.items() if market_data.get('active', False) and market_data.get('quote', '').upper() == 'USDT' and self.type_matches(market_data)]
            self.markets_fetched_signal.emit(sorted(list(set(available_pairs))))
        except Exception as e: self.error_signal.emit(f"Błąd pobierania par dla {self.exchange_id_ccxt}: {type(e).__name__} - {str(e)}")
        finally: self.finished_signal.emit()
    def type_matches(self, market):
        if self.market_type_filter == market.get('type'): return True
        if self.exchange_id_ccxt == 'binance' and self.market_type_filter == 'future' and market.get('linear') and market.get('type') in ['future', 'swap']: return True
        if self.exchange_id_ccxt == 'bybit' and self.market_type_filter == 'swap' and market.get('type') == 'swap': return True
        return False

class MainWindow(QMainWindow):
    clear_results_signal = Signal()
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Krypto Skaner"); self.setGeometry(100, 100, 1200, 800)
        self.chart_win = None
        self.setup_menu()
        self.scroll_area = QScrollArea(); self.scroll_area.setWidgetResizable(True); self.setCentralWidget(self.scroll_area)
        self.scroll_content_widget = QWidget(); self.main_overall_layout = QVBoxLayout(self.scroll_content_widget); self.scroll_area.setWidget(self.scroll_content_widget)
        self.main_horizontal_layout = QHBoxLayout()

        self.left_column_widget = QWidget(); self.left_column_layout = QVBoxLayout(self.left_column_widget); self.left_column_layout.setAlignment(Qt.AlignTop)
        exchange_api_group = QGroupBox("Giełda i Klucze API"); exchange_api_layout = QVBoxLayout()
        exchange_api_layout.addWidget(QLabel("Wybierz giełdę i rynek:"))
        self.exchange_combo = QComboBox()
        self.exchange_options = {"Binance (Spot)": {"id_ccxt": "binance", "type": "spot", "default_pairs": INITIAL_PAIRS_BINANCE_SPOT, "config_section": "binance_spot_config"},"Binance (Futures USDT-M)": {"id_ccxt": "binance", "type": "future", "default_pairs": INITIAL_PAIRS_BINANCE_FUTURES, "config_section": "binance_futures_config"},"Bybit (Spot)": {"id_ccxt": "bybit", "type": "spot", "default_pairs": INITIAL_PAIRS_BYBIT_SPOT, "config_section": "bybit_spot_config"},"Bybit (Perpetual USDT)": {"id_ccxt": "bybit", "type": "swap", "default_pairs": INITIAL_PAIRS_BYBIT_PERP, "config_section": "bybit_perp_config"}}
        self.exchange_combo.addItems(self.exchange_options.keys()); self.exchange_combo.currentTextChanged.connect(self.on_exchange_selection_changed); exchange_api_layout.addWidget(self.exchange_combo)
        api_keys_form_layout = QFormLayout(); self.api_key_input = QLineEdit(); self.api_key_input.setPlaceholderText("Klucz API (opcjonalny)"); api_keys_form_layout.addRow("Klucz API:", self.api_key_input)
        self.api_secret_input = QLineEdit(); self.api_secret_input.setPlaceholderText("Sekret API (opcjonalny)"); self.api_secret_input.setEchoMode(QLineEdit.Password); api_keys_form_layout.addRow("Sekret API:", self.api_secret_input)
        exchange_api_layout.addLayout(api_keys_form_layout); exchange_api_group.setLayout(exchange_api_layout); self.left_column_layout.addWidget(exchange_api_group)

        self.criteria_groupbox = QGroupBox("Ustawienia Skanowania (Globalne)"); self.criteria_form_layout = QFormLayout()
        self.wpr_period_spinbox = QSpinBox(); self.wpr_period_spinbox.setRange(1, 200); self.wpr_period_spinbox.setValue(DEFAULT_WPR_LENGTH); self.criteria_form_layout.addRow("Okres W%R:", self.wpr_period_spinbox)
        self.wpr_operator_combo = QComboBox(); self.wpr_operator_combo.addItems([">=", "<="]); self.wpr_value_spinbox = QDoubleSpinBox(); self.wpr_value_spinbox.setRange(-100.0, 0.0); self.wpr_value_spinbox.setSingleStep(1.0); self.wpr_value_spinbox.setValue(-20.0); self.wpr_value_spinbox.setDecimals(1)
        wpr_criteria_widget = QWidget(); wpr_h_layout = QHBoxLayout(wpr_criteria_widget); wpr_h_layout.setContentsMargins(0,0,0,0); wpr_h_layout.addWidget(self.wpr_operator_combo); wpr_h_layout.addWidget(self.wpr_value_spinbox); self.criteria_form_layout.addRow("Warunek W%R:", wpr_criteria_widget)
        self.ema_period_spinbox = QSpinBox(); self.ema_period_spinbox.setRange(1, 200); self.ema_period_spinbox.setValue(DEFAULT_EMA_WPR_LENGTH); self.criteria_form_layout.addRow("Okres EMA W%R:", self.ema_period_spinbox)
        self.ema_wpr_operator_combo = QComboBox(); self.ema_wpr_operator_combo.addItems([">=", "<="]); self.ema_wpr_value_spinbox = QDoubleSpinBox(); self.ema_wpr_value_spinbox.setRange(-100.0, 0.0); self.ema_wpr_value_spinbox.setSingleStep(1.0); self.ema_wpr_value_spinbox.setValue(-30.0); self.ema_wpr_value_spinbox.setDecimals(1)
        ema_wpr_criteria_widget = QWidget(); ema_h_layout = QHBoxLayout(ema_wpr_criteria_widget); ema_h_layout.setContentsMargins(0,0,0,0); ema_h_layout.addWidget(self.ema_wpr_operator_combo); ema_h_layout.addWidget(self.ema_wpr_value_spinbox); self.criteria_form_layout.addRow("Warunek EMA W%R:", ema_wpr_criteria_widget)
        self.scan_delay_spinbox = QSpinBox(); self.scan_delay_spinbox.setRange(1, 360); self.scan_delay_spinbox.setValue(DEFAULT_SCAN_DELAY_MINUTES); self.scan_delay_spinbox.setSuffix(" min"); self.criteria_form_layout.addRow("Odstęp (min):", self.scan_delay_spinbox)
        self.criteria_groupbox.setLayout(self.criteria_form_layout); self.left_column_layout.addWidget(self.criteria_groupbox)

        self.per_exchange_settings_groupbox = QGroupBox("Ustawienia Specyficzne dla Giełdy"); per_exchange_layout = QVBoxLayout()
        self.notifications_groupbox = QGroupBox("Powiadomienia:"); notifications_form_layout = QFormLayout()
        self.enable_notifications_checkbox = QCheckBox("Włącz powiadomienia"); self.enable_notifications_checkbox.stateChanged.connect(self.toggle_telegram_settings_visibility); notifications_form_layout.addRow(self.enable_notifications_checkbox)
        self.notification_method_combo = QComboBox(); self.notification_method_combo.addItems(["Brak", "Telegram"]); self.notification_method_combo.currentTextChanged.connect(self.toggle_telegram_settings_visibility); notifications_form_layout.addRow("Metoda:", self.notification_method_combo)
        self.telegram_settings_widget = QWidget(); telegram_form_layout = QFormLayout(self.telegram_settings_widget); telegram_form_layout.setContentsMargins(0,0,0,0)
        self.telegram_token_input = QLineEdit(); self.telegram_token_input.setPlaceholderText("Token bota Telegram"); telegram_form_layout.addRow("Token Bota:", self.telegram_token_input)
        self.telegram_chat_id_input = QLineEdit(); self.telegram_chat_id_input.setPlaceholderText("Twój Chat ID"); telegram_form_layout.addRow("Chat ID:", self.telegram_chat_id_input)
        notifications_form_layout.addRow(self.telegram_settings_widget)
        self.notifications_groupbox.setLayout(notifications_form_layout); per_exchange_layout.addWidget(self.notifications_groupbox)

        self.timeframes_groupbox = QGroupBox("Interwały do skanowania:"); self.timeframes_h_layout = QHBoxLayout()
        self.timeframe_checkboxes = {}
        for tf_text in AVAILABLE_TIMEFRAMES: checkbox = QCheckBox(tf_text); checkbox.setChecked(True); self.timeframe_checkboxes[tf_text] = checkbox; self.timeframes_h_layout.addWidget(checkbox)
        self.timeframes_groupbox.setLayout(self.timeframes_h_layout); per_exchange_layout.addWidget(self.timeframes_groupbox)
        self.per_exchange_settings_groupbox.setLayout(per_exchange_layout); self.left_column_layout.addWidget(self.per_exchange_settings_groupbox)

        self.save_config_button = QPushButton("Zapisz Konfigurację"); self.save_config_button.clicked.connect(self.save_configuration); self.left_column_layout.addWidget(self.save_config_button)
        self.main_horizontal_layout.addWidget(self.left_column_widget, 1)

        self.right_column_widget = QWidget(); self.right_column_layout = QVBoxLayout(self.right_column_widget); self.right_column_layout.setAlignment(Qt.AlignTop)
        self.pairs_management_groupbox = QGroupBox("Zarządzanie Parami"); pairs_vertical_layout = QVBoxLayout()
        lists_and_buttons_layout = QHBoxLayout()
        self.available_pairs_list_widget = QListWidget(); self.available_pairs_list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        pair_buttons_layout = QVBoxLayout(); pair_buttons_layout.addStretch()
        self.add_pair_button = QPushButton("Dodaj >"); self.add_pair_button.clicked.connect(self.add_selected_to_scan); pair_buttons_layout.addWidget(self.add_pair_button)
        self.add_all_pairs_button = QPushButton("Dodaj Wsz. >>"); self.add_all_pairs_button.clicked.connect(self.add_all_to_scan); pair_buttons_layout.addWidget(self.add_all_pairs_button)
        self.remove_pair_button = QPushButton("< Usuń"); self.remove_pair_button.clicked.connect(self.remove_selected_from_scan); pair_buttons_layout.addWidget(self.remove_pair_button)
        self.remove_all_pairs_button = QPushButton("<< Usuń Wsz."); self.remove_all_pairs_button.clicked.connect(self.remove_all_from_scan); pair_buttons_layout.addWidget(self.remove_all_pairs_button)
        pair_buttons_layout.addStretch()
        self.scan_pairs_list_widget = QListWidget(); self.scan_pairs_list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        available_pairs_group = QGroupBox("Dostępne Pary:"); available_pairs_layout = QVBoxLayout(); available_pairs_layout.addWidget(self.available_pairs_list_widget); available_pairs_group.setLayout(available_pairs_layout)
        scan_pairs_group = QGroupBox("Wybrane do Skanowania:"); scan_pairs_layout = QVBoxLayout(); scan_pairs_layout.addWidget(self.scan_pairs_list_widget); scan_pairs_group.setLayout(scan_pairs_layout)
        lists_and_buttons_layout.addWidget(available_pairs_group, 1); lists_and_buttons_layout.addLayout(pair_buttons_layout); lists_and_buttons_layout.addWidget(scan_pairs_group, 1)
        pairs_vertical_layout.addLayout(lists_and_buttons_layout)
        self.refresh_pairs_button = QPushButton("Odśwież Dostępne Pary z Giełdy"); self.refresh_pairs_button.clicked.connect(self.trigger_fetch_markets)
        pairs_vertical_layout.addWidget(self.refresh_pairs_button)
        self.pairs_management_groupbox.setLayout(pairs_vertical_layout); self.right_column_layout.addWidget(self.pairs_management_groupbox)
        start_stop_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Skanowania"); self.start_button.clicked.connect(self.start_scan); start_stop_layout.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop Skanowania"); self.stop_button.clicked.connect(self.stop_scan); self.stop_button.setEnabled(False); start_stop_layout.addWidget(self.stop_button)
        self.right_column_layout.addLayout(start_stop_layout)
        self.results_label = QLabel("Pary spełniające kryteria (aktualny cykl):"); self.right_column_layout.addWidget(self.results_label)
        self.results_table = QTableWidget(); self.results_table.setColumnCount(6); self.results_table.setHorizontalHeaderLabels(["Para", "W%R (Najw. TF)", "EMA (Najw. TF)", "Kapitalizacja", "Wolumen 24h (Giełda)", "Rank (CG)"]);
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 6): self.results_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.right_column_layout.addWidget(self.results_table)
        self.main_horizontal_layout.addWidget(self.right_column_widget, 2)
        self.log_groupbox = QGroupBox("Logi Skanowania"); log_layout_main = QVBoxLayout()
        self.log_output = QTextEdit(); self.log_output.setReadOnly(True); self.log_output.setFixedHeight(150)
        log_layout_main.addWidget(self.log_output); self.log_groupbox.setLayout(log_layout_main)
        self.main_overall_layout.addLayout(self.main_horizontal_layout); self.main_overall_layout.addWidget(self.log_groupbox)
        self.scroll_content_widget.setMinimumSize(1100, 700)
        self.scan_thread = None; self.fetch_markets_thread = None
        self.load_configuration(); self.clear_results_signal.connect(self.clear_results_table_slot)
        self.toggle_telegram_settings_visibility()

    def setup_menu(self):
        menu_bar = self.menuBar()
        tools_menu = menu_bar.addMenu("&Narzędzia")
        open_chart_action = QAction("Otwórz okno analizy wykresów", self)
        open_chart_action.triggered.connect(self.open_chart_window)
        tools_menu.addAction(open_chart_action)

    def open_chart_window(self):
        if self.chart_win is None or not self.chart_win.isVisible():
            self.chart_win = MultiChartWindow(self.exchange_options, CONFIG_FILE_PATH)
            self.chart_win.show()
        else:
            self.chart_win.activateWindow(); self.chart_win.raise_()

    def on_exchange_selection_changed(self, exchange_name_gui):
        self.load_configuration(exchange_name_gui)
        self.available_pairs_list_widget.clear()
        self.trigger_fetch_markets()

    def toggle_telegram_settings_visibility(self, method_text=None):
        if method_text is None: method_text = self.notification_method_combo.currentText()
        is_telegram_selected = (method_text == "Telegram"); notifications_enabled = self.enable_notifications_checkbox.isChecked()
        self.telegram_settings_widget.setVisible(is_telegram_selected and notifications_enabled)

    def clear_results_table_slot(self): self.results_table.setRowCount(0)

    def load_configuration(self, exchange_name_gui_from_signal=None):
        config = configparser.ConfigParser()
        if not os.path.exists(CONFIG_FILE_PATH):
            self.update_log(f"Plik konf. {CONFIG_FILE_PATH} nie istnieje. Domyślne."); self.api_key_input.clear(); self.api_secret_input.clear()
            self.wpr_period_spinbox.setValue(DEFAULT_WPR_LENGTH); self.ema_period_spinbox.setValue(DEFAULT_EMA_WPR_LENGTH)
            self.wpr_operator_combo.setCurrentText(">="); self.wpr_value_spinbox.setValue(-20.0)
            self.ema_wpr_operator_combo.setCurrentText(">="); self.ema_wpr_value_spinbox.setValue(-30.0)
            self.scan_delay_spinbox.setValue(DEFAULT_SCAN_DELAY_MINUTES)
            current_exchange_name_gui = self.exchange_combo.currentText(); selected_exchange_config_template = self.exchange_options.get(current_exchange_name_gui)
            if selected_exchange_config_template:
                self.scan_pairs_list_widget.clear(); self.scan_pairs_list_widget.addItems(selected_exchange_config_template.get("default_pairs", []))
            self.enable_notifications_checkbox.setChecked(False); self.notification_method_combo.setCurrentText("Brak")
            self.telegram_token_input.clear(); self.telegram_chat_id_input.clear()
            for checkbox in self.timeframe_checkboxes.values(): checkbox.setChecked(True)
            self.toggle_telegram_settings_visibility(); return
        config.read(CONFIG_FILE_PATH)
        current_exchange_name_gui = exchange_name_gui_from_signal if exchange_name_gui_from_signal is not None else self.exchange_combo.currentText()
        selected_exchange_config_template = self.exchange_options.get(current_exchange_name_gui)
        if selected_exchange_config_template:
            section_name_exchange = selected_exchange_config_template["config_section"]
            if section_name_exchange in config:
                exch_conf = config[section_name_exchange]
                self.api_key_input.setText(exch_conf.get('api_key', '')); self.api_secret_input.setText(exch_conf.get('api_secret', ''))
                pairs_str = exch_conf.get('scan_pairs', ""); self.scan_pairs_list_widget.clear()
                if pairs_str: self.scan_pairs_list_widget.addItems([p.strip() for p in pairs_str.split(',') if p.strip()])
                else: self.scan_pairs_list_widget.addItems(selected_exchange_config_template.get("default_pairs", []))
                self.enable_notifications_checkbox.setChecked(exch_conf.getboolean('notification_enabled', False))
                self.notification_method_combo.setCurrentText(exch_conf.get('notification_method', "Brak"))
                self.telegram_token_input.setText(exch_conf.get('telegram_token', ""))
                self.telegram_chat_id_input.setText(exch_conf.get('telegram_chat_id', ""))
                selected_tfs_str = exch_conf.get('selected_timeframes', ",".join(AVAILABLE_TIMEFRAMES))
                selected_tfs_list = [tf.strip() for tf in selected_tfs_str.split(',') if tf.strip()]
                for tf_text, checkbox in self.timeframe_checkboxes.items(): checkbox.setChecked(tf_text in selected_tfs_list)
                self.update_log(f"Załadowano konfigurację specyficzną dla {current_exchange_name_gui}.")
            else:
                self.api_key_input.clear(); self.api_secret_input.clear(); self.scan_pairs_list_widget.clear()
                self.scan_pairs_list_widget.addItems(selected_exchange_config_template.get("default_pairs", []))
                self.enable_notifications_checkbox.setChecked(False); self.notification_method_combo.setCurrentText("Brak")
                self.telegram_token_input.clear(); self.telegram_chat_id_input.clear()
                for checkbox in self.timeframe_checkboxes.values(): checkbox.setChecked(True)
                self.update_log(f"Brak zapisanej konfiguracji dla {current_exchange_name_gui}. Użyto domyślnych.")
        else: self.api_key_input.clear(); self.api_secret_input.clear(); self.scan_pairs_list_widget.clear()
        if 'scan_settings' in config:
            settings = config['scan_settings']
            self.wpr_period_spinbox.setValue(settings.getint('wpr_period', DEFAULT_WPR_LENGTH)); self.ema_period_spinbox.setValue(settings.getint('ema_period', DEFAULT_EMA_WPR_LENGTH))
            self.wpr_operator_combo.setCurrentText(settings.get('wpr_operator', ">=")); self.wpr_value_spinbox.setValue(settings.getfloat('wpr_value', -20.0))
            self.ema_wpr_operator_combo.setCurrentText(settings.get('ema_wpr_operator', ">=")); self.ema_wpr_value_spinbox.setValue(settings.getfloat('ema_wpr_value', -30.0))
            self.scan_delay_spinbox.setValue(settings.getint('scan_delay_minutes', DEFAULT_SCAN_DELAY_MINUTES))
            self.update_log("Załadowano globalne ustawienia skanowania.")
        else:
            self.update_log("Brak globalnych ustawień skanowania, używam domyślnych.")
        self.toggle_telegram_settings_visibility()

    def save_configuration(self):
        selected_exchange_name_gui = self.exchange_combo.currentText(); selected_config_template = self.exchange_options.get(selected_exchange_name_gui)
        config = configparser.ConfigParser();
        if os.path.exists(CONFIG_FILE_PATH): config.read(CONFIG_FILE_PATH)
        if selected_config_template:
            section_name_exchange = selected_config_template["config_section"]
            if not config.has_section(section_name_exchange): config.add_section(section_name_exchange)
            config[section_name_exchange]['api_key'] = self.api_key_input.text()
            config[section_name_exchange]['api_secret'] = self.api_secret_input.text()
            scan_pairs_items = [self.scan_pairs_list_widget.item(i).text() for i in range(self.scan_pairs_list_widget.count())]
            config[section_name_exchange]['scan_pairs'] = ",".join(scan_pairs_items)
            config[section_name_exchange]['notification_enabled'] = str(self.enable_notifications_checkbox.isChecked())
            config[section_name_exchange]['notification_method'] = self.notification_method_combo.currentText()
            config[section_name_exchange]['telegram_token'] = self.telegram_token_input.text()
            config[section_name_exchange]['telegram_chat_id'] = self.telegram_chat_id_input.text()
            selected_tfs_list = [tf for tf, cb in self.timeframe_checkboxes.items() if cb.isChecked()]
            config[section_name_exchange]['selected_timeframes'] = ",".join(selected_tfs_list)
            self.update_log(f"Przygotowano konf. specyficzną dla {selected_exchange_name_gui}.")
        else: self.update_log("Nie wybrano giełdy, pomijam zapis ustawień specyficznych dla giełdy.")
        section_name_scan_settings = 'scan_settings'
        if not config.has_section(section_name_scan_settings): config.add_section(section_name_scan_settings)
        config[section_name_scan_settings]['wpr_period'] = str(self.wpr_period_spinbox.value())
        config[section_name_scan_settings]['ema_period'] = str(self.ema_period_spinbox.value())
        config[section_name_scan_settings]['wpr_operator'] = self.wpr_operator_combo.currentText()
        config[section_name_scan_settings]['wpr_value'] = str(self.wpr_value_spinbox.value())
        config[section_name_scan_settings]['ema_wpr_operator'] = self.ema_wpr_operator_combo.currentText()
        config[section_name_scan_settings]['ema_wpr_value'] = str(self.ema_wpr_value_spinbox.value())
        config[section_name_scan_settings]['scan_delay_minutes'] = str(self.scan_delay_spinbox.value())
        self.update_log("Przygotowano globalne ustawienia skanowania.")
        try:
            config_dir = os.path.dirname(CONFIG_FILE_PATH)
            if not os.path.exists(config_dir): os.makedirs(config_dir, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as configfile: config.write(configfile)
            self.update_log(f"Konfiguracja zapisana w {CONFIG_FILE_PATH}"); QMessageBox.information(self, "Zapisano", "Konfiguracja zapisana.")
        except Exception as e: self.update_log(f"<font color='red'>Błąd zapisu konf.: {str(e)}</font>"); QMessageBox.critical(self, "Błąd", f"Błąd zapisu: {str(e)}")

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): self.update_log("Pobieranie par w toku."); return
        selected_exchange_gui = self.exchange_combo.currentText()
        selected_config = self.exchange_options.get(selected_exchange_gui)
        if not selected_config:
            self.log_error(f"Brak konfiguracji dla: {selected_exchange_gui}")
            return
        ccxt_id = selected_config["id_ccxt"]
        market_type = selected_config["type"]
        self.update_log(f"Pobieranie par dla {selected_exchange_gui}...")
        self.refresh_pairs_button.setEnabled(False); self.available_pairs_list_widget.clear()
        self.fetch_markets_thread = FetchMarketsThread(ccxt_id, market_type, self)
        self.fetch_markets_thread.markets_fetched_signal.connect(self.populate_available_pairs)
        self.fetch_markets_thread.error_signal.connect(self.log_error)
        self.fetch_markets_thread.finished_signal.connect(lambda: self.refresh_pairs_button.setEnabled(True))
        self.fetch_markets_thread.start()

    def populate_available_pairs(self, pairs_list):
        self.available_pairs_list_widget.clear(); self.available_pairs_list_widget.addItems(pairs_list)
        self.update_log(f"Załadowano {len(pairs_list)} dostępnych par."); self.refresh_pairs_button.setEnabled(True)
    def add_selected_to_scan(self):
        selected_items = self.available_pairs_list_widget.selectedItems(); current_scan_pairs = [self.scan_pairs_list_widget.item(i).text() for i in range(self.scan_pairs_list_widget.count())]
        for item in selected_items:
            if item.text() not in current_scan_pairs: self.scan_pairs_list_widget.addItem(item.text())
    def add_all_to_scan(self):
        current_scan_pairs = [self.scan_pairs_list_widget.item(i).text() for i in range(self.scan_pairs_list_widget.count())]
        for i in range(self.available_pairs_list_widget.count()):
            item_text = self.available_pairs_list_widget.item(i).text()
            if item_text not in current_scan_pairs: self.scan_pairs_list_widget.addItem(item_text)
    def remove_selected_from_scan(self):
        selected_items = self.scan_pairs_list_widget.selectedItems()
        for item in selected_items: self.scan_pairs_list_widget.takeItem(self.scan_pairs_list_widget.row(item))
    def remove_all_from_scan(self): self.scan_pairs_list_widget.clear()

    def start_scan(self):
        if self.scan_thread and self.scan_thread.isRunning(): self.update_log("<font color='orange'>Skanowanie cykliczne jest już w toku.</font>"); return
        selected_exchange_name_gui = self.exchange_combo.currentText()
        if selected_exchange_name_gui not in self.exchange_options: self.update_log(f"<font color='red'>BŁĄD: Nieprawidłowa opcja giełdy.</font>"); return
        current_pairs_for_scan = [self.scan_pairs_list_widget.item(i).text() for i in range(self.scan_pairs_list_widget.count())]
        api_key = self.api_key_input.text(); api_secret = self.api_secret_input.text()
        selected_timeframes_from_gui = [tf for tf, cb in self.timeframe_checkboxes.items() if cb.isChecked()]
        if not selected_timeframes_from_gui: self.update_log("<font color='red'>BŁĄD: Nie wybrano interwałów!</font>"); return
        if not current_pairs_for_scan: self.update_log(f"<font color='red'>BŁĄD: Brak par na liście do skanowania!</font>"); return
        wpr_operator = self.wpr_operator_combo.currentText(); wpr_value = self.wpr_value_spinbox.value()
        ema_wpr_operator = self.ema_wpr_operator_combo.currentText(); ema_wpr_value = self.ema_wpr_value_spinbox.value()
        wpr_period_from_gui = self.wpr_period_spinbox.value(); ema_period_from_gui = self.ema_period_spinbox.value()
        scan_delay_minutes = self.scan_delay_spinbox.value(); scan_delay_seconds = scan_delay_minutes * 60
        notification_settings_data = {
            "enabled": self.enable_notifications_checkbox.isChecked(), "method": self.notification_method_combo.currentText(),
            "telegram_token": self.telegram_token_input.text(), "telegram_chat_id": self.telegram_chat_id_input.text()
        }
        self.clear_results_signal.emit(); self.update_log(f"Rozpoczynanie cyklicznego skanowania dla: {selected_exchange_name_gui}...")
        self.update_log(f"Odstęp między cyklami: {scan_delay_minutes} min.")
        self.update_log(f"Pary do skanowania: {', '.join(current_pairs_for_scan)}")
        self.scan_thread = ScanThread(selected_exchange_name_gui, api_key, api_secret, current_pairs_for_scan, selected_timeframes_from_gui,wpr_operator, wpr_value, ema_wpr_operator, ema_wpr_value,wpr_period_from_gui, ema_period_from_gui, scan_delay_seconds, notification_settings_data, self)
        self.scan_thread.progress_signal.connect(self.update_log)
        self.scan_thread.result_signal.connect(self.add_result_to_table)
        self.scan_thread.error_signal.connect(self.log_error)
        self.scan_thread.finished_signal.connect(self.scan_finished)
        self.start_button.setEnabled(False); self.stop_button.setEnabled(True)
        self.scan_thread.start()

    def stop_scan(self):
        if self.scan_thread and self.scan_thread.isRunning(): self.scan_thread.stop(); self.update_log("Wysłano żądanie zatrzymania...")
        else: self.update_log("Skanowanie nie jest w toku.")
    def update_log(self, message): self.log_output.append(message)

    def add_result_to_table(self, result_data_list):
        pair_symbol = result_data_list[0]; wpr_val = result_data_list[1]; ema_val = result_data_list[2]
        market_cap_str = result_data_list[3]; volume_24h_str = result_data_list[4]; rank_str = result_data_list[5]
        items = self.results_table.findItems(pair_symbol, Qt.MatchExactly)
        if not items:
            row_position = self.results_table.rowCount(); self.results_table.insertRow(row_position)
            self.results_table.setItem(row_position, 0, QTableWidgetItem(pair_symbol))
            self.results_table.setItem(row_position, 1, QTableWidgetItem(f"{wpr_val:.2f}"))
            self.results_table.setItem(row_position, 2, QTableWidgetItem(f"{ema_val:.2f}"))
            self.results_table.setItem(row_position, 3, QTableWidgetItem(market_cap_str))
            self.results_table.setItem(row_position, 4, QTableWidgetItem(volume_24h_str))
            self.results_table.setItem(row_position, 5, QTableWidgetItem(rank_str))
        else:
            self.results_table.item(items[0].row(), 1).setText(f"{wpr_val:.2f}"); self.results_table.item(items[0].row(), 2).setText(f"{ema_val:.2f}")
            self.results_table.item(items[0].row(), 3).setText(market_cap_str); self.results_table.item(items[0].row(), 4).setText(volume_24h_str)
            self.results_table.item(items[0].row(), 5).setText(rank_str)
        self.update_log(f"<font color='green'>OK: {pair_symbol} (W%R:{wpr_val:.2f},EMA:{ema_val:.2f},Wol:{volume_24h_str})</font>")

    def log_error(self, error_message): self.update_log(f"<font color='red'>BŁĄD: {error_message}</font>")
    def scan_finished(self):
        self.update_log("Wątek cyklicznego skanowania zakończył pracę.")
        self.start_button.setEnabled(True); self.stop_button.setEnabled(False)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setOrganizationName("MojaFirmaPrzyklad")
    app.setApplicationName(CONFIG_DIR_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
