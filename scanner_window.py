import sys
import os
import configparser
import datetime
import time
import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from collections import deque
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QLineEdit,
                             QPushButton, QComboBox, QSpinBox, QDoubleSpinBox,
                             QTextEdit, QListWidget, QListWidgetItem, QGroupBox,
                             QMessageBox, QTabWidget, QScrollArea, QSizePolicy,
                             QFormLayout, QCheckBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QStandardPaths, QEvent
from PyQt6.QtGui import QFont, QIcon, QAction


# --- Konfiguracja ścieżek ---
CONFIG_DIR_NAME = "KryptoSkaner"
CONFIG_FILE_NAME = "app_settings.ini"

def get_config_path_standalone():
    config_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    app_config_path = os.path.join(config_dir, CONFIG_DIR_NAME)
    if not os.path.exists(app_config_path):
        os.makedirs(app_config_path, exist_ok=True)
    return os.path.join(app_config_path, CONFIG_FILE_NAME)

CONFIG_FILE_PATH = get_config_path_standalone()

# Dostępne interwały czasowe
AVAILABLE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w']

# Mapowanie interwałów na minuty dla sortowania (od największego do najmniejszego)
TIMEFRAME_DURATIONS_MINUTES = {
    '1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '12h': 720, '1d': 1440, '1w': 10080
}
def get_timeframe_duration_for_sort(tf_string):
    return TIMEFRAME_DURATIONS_MINUTES.get(tf_string.lower(), 0)

# Funkcja formatująca duże liczby (z pierwotnego krypto_skaner_gui.py)
def format_large_number(num, currency_symbol=""):
    if num is None: return "N/A"
    abs_num = abs(num)
    sign = "-" if num < 0 else ""
    if abs_num >= 1_000_000_000_000: val_str = f"{abs_num / 1_000_000_000_000:.2f} bln"
    elif abs_num >= 1_000_000_000: val_str = f"{abs_num / 1_000_000_000:.2f} mld"
    elif abs_num >= 1_000_000: val_str = f"{abs_num / 1_000_000:.2f} mln"
    elif abs_num >= 1_000: val_str = f"{abs_num / 1_000:.2f} tys."
    else: val_str = f"{abs_num:,.0f}"
    return f"{sign}{val_str.replace('.',',')} {currency_symbol}".strip()

# Funkcje wysyłania powiadomień (z pierwotnego krypto_skaner_gui.py)
def send_telegram_notification(bot_token, chat_id, message, progress_callback):
    if not bot_token or not chat_id:
        progress_callback.emit("<font color='orange'>Ostrzeżenie: Token Telegram lub Chat ID nieskonfigurowane.</font>")
        return
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
    try:
        response = requests.post(telegram_url, data=payload, timeout=10)
        response.raise_for_status() # Wyrzuć błąd dla statusów 4xx/5xx
        if response.json().get("ok"):
            progress_callback.emit("Powiadomienie Telegram wysłane.")
        else:
            progress_callback.emit(f"<font color='red'>Błąd Telegram: {response.json().get('description','Brak szczegółów')}</font>")
    except Exception as e:
        progress_callback.emit(f"<font color='red'>Błąd Telegram: {str(e)}</font>")

def send_email_notification(sender_email, sender_password, receiver_email, smtp_server, smtp_port, subject, body, progress_callback):
    if not sender_email or not sender_password or not receiver_email:
        progress_callback.emit("<font color='orange'>Ostrzeżenie: Adres/hasło/odbiorca e-mail nieskonfigurowani.</font>")
        return

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender_email
    message["To"] = receiver_email

    part = MIMEText(body, "html")
    message.attach(part)

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls() # Użyj TLS
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, message.as_string())
        progress_callback.emit("Powiadomienie e-mail wysłane.")
    except Exception as e:
        progress_callback.emit(f"<font color='red'>Błąd E-mail: {str(e)}</font>")


# --- KLASY POMOCNICZE ---

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

        if self.exchange_id == 'binanceusdm' and self.market_type_filter == 'future':
            if market.get('linear') and market_type in ['future', 'swap']:
                return True

        if self.exchange_id == 'bybit' and self.market_type_filter == 'swap' and market_type == 'swap':
            return True

        return False

    def stop(self):
        self.is_running = False


# Nowy wątek zarządzający cyklicznym skanowaniem (ScanLoopThread z pierwotnej wersji)
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

    # Przeniesiona funkcja perform_actual_scan jako metoda wewnętrzna
    def _perform_actual_scan_logic(self):
        self.progress_signal.emit(f"Rozpoczynanie skanowania dla: {self.exchange_id_gui}")
        if not self.tfs:
            self.error_signal.emit("Nie wybrano interwałów."); return

        sorted_selected_timeframes = sorted(self.tfs, key=get_timeframe_duration_for_sort, reverse=True)

        self.progress_signal.emit(f"Wybrane interwały: {', '.join(sorted_selected_timeframes)}")
        self.progress_signal.emit(f"Parametry: W%R({self.wpr_p}), EMA({self.ema_p}) | Kryteria: W%R {self.wpr_op} {self.wpr_val}, EMA(W%R) {self.ema_op} {self.ema_val}")

        selected_config = self.exchange_options_data.get(self.exchange_id_gui)
        if not selected_config:
            self.error_signal.emit(f"Błąd konfiguracji dla {self.exchange_id_gui}"); return

        ccxt_exchange_id = selected_config["id_ccxt"]
        market_type = selected_config["type"]
        ccxt_options = {}

        if market_type == 'future':
            ccxt_options['defaultType'] = 'future'
        elif market_type == 'swap':
            ccxt_options['defaultType'] = 'swap'

        exchange = None
        try:
            exchange_params = {'enableRateLimit': True, 'options': ccxt_options, 'timeout': 30000}
            if self.api_key and self.api_secret:
                exchange_params['apiKey'] = self.api_key
                exchange_params['secret'] = self.api_secret
                self.progress_signal.emit(f"  Inicjalizacja {ccxt_exchange_id} (typ: {market_type}) z kluczami API.")
            else:
                self.progress_signal.emit(f"  Inicjalizacja {ccxt_exchange_id} (typ: {market_type}) bez kluczy API.")

            exchange = getattr(ccxt, ccxt_exchange_id)(exchange_params)

            try:
                exchange.load_markets()
                self.progress_signal.emit(f"  Połączono z {ccxt_exchange_id} i załadowano rynki.")
            except Exception as e_markets:
                self.progress_signal.emit(f"  Ostrzeżenie (rynki) {ccxt_exchange_id}: {type(e_markets).__name__} - {str(e_markets)}")

        except Exception as e:
            self.error_signal.emit(f"Błąd inicjalizacji giełdy {ccxt_exchange_id}: {type(e).__name__} - {str(e)}"); return

        required_candles = self.wpr_p + self.ema_p + 50

        for i, pair_symbol in enumerate(self.pairs):
            if self.isInterruptionRequested():
                self.progress_signal.emit("Przerwano analizę par."); break

            self.progress_signal.emit(f"Analizowanie: {pair_symbol} ({i+1}/{len(self.pairs)})")

            all_tfs_ok = True
            wpr_rep, ema_rep, first_tf_ok = None, None, False
            volume_24h_str, market_cap_str, rank_str = "N/A", "N/A", "N/A"

            for tf_idx, tf in enumerate(sorted_selected_timeframes):
                if self.isInterruptionRequested():
                    self.progress_signal.emit(f"Przerwano analizę TF dla {pair_symbol}."); all_tfs_ok = False; break

                try:
                    self.progress_signal.emit(f"  Pobieranie {pair_symbol} @ {tf}...")
                    ohlcv = exchange.fetch_ohlcv(pair_symbol, timeframe=tf, limit=required_candles)

                    if not ohlcv or len(ohlcv) < (self.wpr_p + self.ema_p - 1):
                        self.progress_signal.emit(f"  Brak danych lub za mało danych dla {pair_symbol} @ {tf}. Pomijam.")
                        all_tfs_ok = False; break

                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                    if df.empty:
                        self.progress_signal.emit(f"  Puste dane. Pomijam {pair_symbol} @ {tf}."); all_tfs_ok=False; break

                    df.ta.willr(length=self.wpr_p, append=True)
                    wpr_col=f'WILLR_{self.wpr_p}'

                    if wpr_col not in df.columns or df[wpr_col].isna().all():
                        self.progress_signal.emit(f"  Błąd W%R. Pomijam {pair_symbol} @ {tf}."); all_tfs_ok=False; break

                    current_wpr=df[wpr_col].iloc[-1]
                    if pd.isna(current_wpr):
                        self.progress_signal.emit(f"  W%R NaN. Pomijam {pair_symbol} @ {tf}."); all_tfs_ok=False; break

                    ema_series=ta.ema(df[wpr_col].dropna(),length=self.ema_p)
                    if ema_series is None or ema_series.empty or ema_series.isna().all():
                        self.progress_signal.emit(f"  Błąd EMA(W%R). Pomijam {pair_symbol} @ {tf}."); all_tfs_ok=False; break

                    current_ema=ema_series.iloc[-1]
                    if pd.isna(current_ema):
                        self.progress_signal.emit(f"  EMA(W%R) NaN. Pomijam {pair_symbol} @ {tf}."); all_tfs_ok=False; break

                    wpr_ok=(current_wpr >= self.wpr_val) if self.wpr_op == ">=" else (current_wpr <= self.wpr_val)
                    ema_ok=(current_ema >= self.ema_val) if self.ema_op == ">=" else (current_ema <= self.ema_val)

                    wpr_ok_str=f"<font color='green'>True</font>" if wpr_ok else f"<font color='red'>False</font>"
                    ema_ok_str=f"<font color='green'>True</font>" if ema_ok else f"<font color='red'>False</font>"

                    self.progress_signal.emit(f"    {tf}: W%R={current_wpr:.2f} ({wpr_ok_str}), EMA={current_ema:.2f} ({ema_ok_str})")

                    if not (wpr_ok and ema_ok):
                        self.progress_signal.emit(f"    <font color='red'>Warunki niespełnione.</font>"); all_tfs_ok=False; break
                    else:
                        self.progress_signal.emit(f"    <font color='green'>Warunki SPEŁNIONE.</font>");
                        if tf_idx == 0:
                            wpr_rep, ema_rep, first_tf_ok = current_wpr, current_ema, True

                except Exception as e:
                    self.error_signal.emit(f"  Błąd dla {pair_symbol} @ {tf}: {type(e).__name__} - {str(e)}"); all_tfs_ok=False; break

            if all_tfs_ok and first_tf_ok:
                if wpr_rep is not None and ema_rep is not None:
                    try:
                        ticker = exchange.fetch_ticker(pair_symbol)
                        if ticker and 'quoteVolume' in ticker and ticker['quoteVolume'] is not None:
                            quote_curr = pair_symbol.split('/')[-1].split(':')[0]
                            vol_str = format_large_number(ticker['quoteVolume'], currency_symbol=quote_curr)
                            self.progress_signal.emit(f"    Wolumen 24h: {vol_str}")
                            volume_24h_str = vol_str
                    except Exception as e:
                        self.progress_signal.emit(f"    Błąd pobierania tickera: {str(e)}")

                    self.result_signal.emit({
                        'symbol': pair_symbol,
                        'all_tfs_ok': True,
                        'wr_value': wpr_rep,
                        'ema_value': ema_rep,
                        'volume_24h_str': volume_24h_str,
                        'market_cap_str': market_cap_str,
                        'rank_str': rank_str
                    })

                    # Logika wysyłania powiadomień (przeniesiona z pierwotnej wersji)
                    if self.notification_settings.get("enabled"):
                        message_body = (f"🔔 Alert: <b>{pair_symbol}</b>\n"
                                        f"Giełda: {self.exchange_id_gui}\n"
                                        f"W%R({self.wpr_p}): {wpr_rep:.2f}, EMA({self.ema_p}): {ema_rep:.2f}\n"
                                        f"Wolumen 24h: {volume_24h_str}")

                        if self.notification_settings.get("method") == "Telegram":
                            tel_token = self.notification_settings.get("telegram_token")
                            tel_chat_id = self.notification_settings.get("telegram_chat_id")
                            send_telegram_notification(tel_token, tel_chat_id, message_body, self.progress_signal)
                        elif self.notification_settings.get("method") == "Email":
                            sender_email = self.notification_settings.get("email_address")
                            sender_password = self.notification_settings.get("email_password")
                            receiver_email = self.notification_settings.get("email_address") # Wysyłamy na ten sam adres
                            smtp_server = self.notification_settings.get("smtp_server")
                            smtp_port = self.notification_settings.get("smtp_port")
                            subject = f"Krypto Skaner Alert: {pair_symbol} na {self.exchange_id_gui}"
                            send_email_notification(sender_email, sender_password, receiver_email, smtp_server, int(smtp_port), subject, message_body, self.progress_signal)
                else:
                    self.error_signal.emit(f"Błąd wewn.: Brak W%R/EMA dla {pair_symbol}.")
            else:
                self.result_signal.emit({
                    'symbol': pair_symbol,
                    'all_tfs_ok': False
                })
                self.progress_signal.emit(f"  {pair_symbol} NIE spełnia kryteriów.\n")

        self.progress_signal.emit("Cykl skanowania zakończony.")


    def run(self):
        while self._is_running and not self.isInterruptionRequested():
            self.cycle_number += 1
            self.progress_signal.emit(f"--- Rozpoczynanie cyklu skanowania nr {self.cycle_number} ---")

            self.result_signal.emit({'clear_results_table': True})

            if not self.pairs:
                self.error_signal.emit("Lista par do skanowania jest pusta."); break

            try:
                self._perform_actual_scan_logic() # Wywołanie metody
            except Exception as e:
                self.error_signal.emit(f"Krytyczny błąd w pętli skanowania (cykl {self.cycle_number}): {type(e).__name__} - {str(e)}")

            self.progress_signal.emit(f"Cykl {self.cycle_number} zakończony. Następny za {self.delay_seconds // 60} min.")

            for _ in range(self.delay_seconds):
                if self.isInterruptionRequested():
                    self.progress_signal.emit("Oczekiwanie przerwane.")
                    self._is_running = False
                    break
                time.sleep(1)

            if not self._is_running:
                break

        self.finished_signal.emit()

    def stop(self):
        self.progress_signal.emit("Wysłano żądanie zatrzymania skanowania...")
        self._is_running = False
        self.requestInterruption()


# --- GŁÓWNA KLASA OKNA SKANERA ---

class ScannerWindow(QMainWindow):
    def __init__(self, exchange_options: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Krypto Skaner - Narzędzie Skanera")
        self.setGeometry(150, 150, 1000, 850) # Zwiększono domyślną wysokość

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
        # Utwórz QScrollArea jako główny widget centralny
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.setCentralWidget(self.scroll_area)

        # Utwórz widget, który będzie zawierał całą zawartość i będzie przewijany
        self.scroll_content_widget = QWidget()
        self.main_layout = QVBoxLayout(self.scroll_content_widget) # Główny layout w tym widgetcie
        self.scroll_area.setWidget(self.scroll_content_widget) # Ustaw jako zawartość ScrollArea

        # Górny panel - Wybór giełdy i symbolu
        top_panel = QGroupBox("Wybór Giełdy i Pary")
        top_layout = QHBoxLayout()
        top_panel.setLayout(top_layout)

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

        # Watchlista
        watchlist_group = QGroupBox("Watchlista")
        watchlist_layout = QVBoxLayout()
        watchlist_group.setLayout(watchlist_layout)

        self.watchlist = QListWidget()
        self.watchlist.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.watchlist.setMinimumHeight(150)
        watchlist_layout.addWidget(self.watchlist)

        # Przyciski do zarządzania watchlistą
        watchlist_buttons_layout = QHBoxLayout()
        self.add_selected_button = QPushButton("Dodaj wybrane >")
        self.add_selected_button.clicked.connect(self.add_to_watchlist_from_available)
        watchlist_buttons_layout.addWidget(self.add_selected_button)

        self.add_all_button = QPushButton("Dodaj wszystkie >>")
        self.add_all_button.clicked.connect(self.add_all_to_watchlist)
        watchlist_buttons_layout.addWidget(self.add_all_button)

        self.remove_selected_button = QPushButton("< Usuń wybrane")
        self.remove_selected_button.clicked.connect(self.remove_from_watchlist)
        watchlist_buttons_layout.addWidget(self.remove_selected_button)

        self.remove_all_button = QPushButton("<< Usuń wszystkie")
        self.remove_all_button.clicked.connect(self.remove_all_from_watchlist)
        watchlist_buttons_layout.addWidget(self.remove_all_button)

        watchlist_layout.addLayout(watchlist_buttons_layout)

        self.main_layout.addWidget(watchlist_group)

        # Parametry skanera
        params_group = QGroupBox("Parametry Skanera")
        params_layout = QFormLayout()
        params_group.setLayout(params_layout)

        # Interwały do skanowania (checkboxes)
        timeframes_group = QGroupBox("Interwały do skanowania:")
        timeframes_h_layout = QHBoxLayout()
        timeframes_group.setLayout(timeframes_h_layout)
        self.timeframe_checkboxes = {}
        for tf_text in AVAILABLE_TIMEFRAMES:
            checkbox = QCheckBox(tf_text)
            checkbox.setChecked(True)
            checkbox.stateChanged.connect(self.save_settings)
            self.timeframe_checkboxes[tf_text] = checkbox
            timeframes_h_layout.addWidget(checkbox)
        params_layout.addRow(timeframes_group)

        self.wr_length_spin = QSpinBox()
        self.wr_length_spin.setRange(5, 200)
        self.wr_length_spin.setValue(14)
        self.wr_length_spin.valueChanged.connect(self.save_settings)
        params_layout.addRow("W%R Długość:", self.wr_length_spin)

        # Warunek W%R
        wpr_criteria_widget = QWidget()
        wpr_h_layout = QHBoxLayout(wpr_criteria_widget)
        wpr_h_layout.setContentsMargins(0, 0, 0, 0)
        self.wpr_operator_combo = QComboBox()
        self.wpr_operator_combo.addItems([">=", "<="])
        self.wpr_operator_combo.currentTextChanged.connect(self.save_settings)
        wpr_h_layout.addWidget(self.wpr_operator_combo)
        self.wpr_value_spin = QDoubleSpinBox()
        self.wpr_value_spin.setRange(-100.0, 0.0)
        self.wpr_value_spin.setSingleStep(1.0)
        self.wpr_value_spin.setValue(-20.0)
        self.wpr_value_spin.setDecimals(1)
        self.wpr_value_spin.valueChanged.connect(self.save_settings)
        wpr_h_layout.addWidget(self.wpr_value_spin)
        params_layout.addRow("W%R Warunek:", wpr_criteria_widget)

        self.ema_wpr_length_spin = QSpinBox()
        self.ema_wpr_length_spin.setRange(5, 200)
        self.ema_wpr_length_spin.setValue(9)
        self.ema_wpr_length_spin.valueChanged.connect(self.save_settings)
        params_layout.addRow("EMA(W%R) Długość:", self.ema_wpr_length_spin)

        # Warunek EMA(W%R)
        ema_wpr_criteria_widget = QWidget()
        ema_wpr_h_layout = QHBoxLayout(ema_wpr_criteria_widget)
        ema_wpr_h_layout.setContentsMargins(0, 0, 0, 0)
        self.ema_wpr_operator_combo = QComboBox()
        self.ema_wpr_operator_combo.addItems([">=", "<="])
        self.ema_wpr_operator_combo.currentTextChanged.connect(self.save_settings)
        ema_wpr_h_layout.addWidget(self.ema_wpr_operator_combo)
        self.ema_wpr_value_spin = QDoubleSpinBox()
        self.ema_wpr_value_spin.setRange(-100.0, 0.0)
        self.ema_wpr_value_spin.setSingleStep(1.0)
        self.ema_wpr_value_spin.setValue(-30.0)
        self.ema_wpr_value_spin.setDecimals(1)
        self.ema_wpr_value_spin.valueChanged.connect(self.save_settings)
        ema_wpr_h_layout.addWidget(self.ema_wpr_value_spin)
        params_layout.addRow("EMA(W%R) Warunek:", ema_wpr_criteria_widget)

        self.scan_delay_spin = QSpinBox()
        self.scan_delay_spin.setRange(1, 360)
        self.scan_delay_spin.setValue(5)
        self.scan_delay_spin.setSuffix(" min")
        self.scan_delay_spin.valueChanged.connect(self.save_settings)
        params_layout.addRow("Odstęp między cyklami (min):", self.scan_delay_spin)

        # Nowa sekcja Powiadomień (specyficzna dla skanera, ale używająca globalnych danych)
        notifications_settings_group = QGroupBox("Ustawienia Powiadomień")
        notifications_settings_layout = QFormLayout()
        notifications_settings_group.setLayout(notifications_settings_layout)

        self.enable_notifications_checkbox = QCheckBox("Włącz powiadomienia")
        self.enable_notifications_checkbox.setChecked(False)
        self.enable_notifications_checkbox.stateChanged.connect(self.save_settings)
        self.enable_notifications_checkbox.stateChanged.connect(self.toggle_notification_method_visibility)
        notifications_settings_layout.addRow(self.enable_notifications_checkbox)

        self.notification_method_combo = QComboBox()
        self.notification_method_combo.addItems(["Brak", "Telegram", "Email"])
        self.notification_method_combo.currentTextChanged.connect(self.save_settings)
        self.notification_method_combo.currentTextChanged.connect(self.toggle_notification_method_visibility)
        notifications_settings_layout.addRow("Metoda Powiadomienia:", self.notification_method_combo)

        params_layout.addRow(notifications_settings_group)

        self.main_layout.addWidget(params_group)

        # Przyciski kontrolne
        control_buttons_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Skanowania")
        self.start_button.clicked.connect(self.start_scanning)
        control_buttons_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Zatrzymaj Skanowanie")
        self.stop_button.clicked.connect(self.stop_scanning)
        self.stop_button.setEnabled(False)
        control_buttons_layout.addWidget(self.stop_button)
        self.main_layout.addLayout(control_buttons_layout)

        # Wyniki skanowania
        results_group = QGroupBox("Wyniki Skanowania")
        results_layout = QVBoxLayout()
        results_group.setLayout(results_layout)

        self.results_display = QTextEdit()
        self.results_display.setReadOnly(True)
        self.results_display.setMinimumHeight(150)
        results_layout.addWidget(self.results_display)
        self.main_layout.addWidget(results_group)

        # Logi dla skanera (w tym oknie)
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFixedHeight(80)
        self.main_layout.addWidget(QLabel("Logi Skanera:"))
        self.main_layout.addWidget(self.log_display)

        self.toggle_notification_method_visibility()

    def toggle_notification_method_visibility(self):
        is_enabled = self.enable_notifications_checkbox.isChecked()
        self.notification_method_combo.setVisible(is_enabled)

        parent_layout = self.notification_method_combo.parentWidget().layout()
        if isinstance(parent_layout, QFormLayout):
            row_index = parent_layout.indexOf(self.notification_method_combo)
            if row_index != -1:
                label_item = parent_layout.itemAt(row_index, QFormLayout.ItemRole.LabelRole)
                if label_item and label_item.widget():
                    label_item.widget().setVisible(is_enabled)


    def load_settings(self):
        self.config = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE_PATH):
            self.config.read(CONFIG_FILE_PATH)
            self.log_message("Załadowano ustawienia z pliku.")
        else:
            self.log_message("Brak pliku konfiguracyjnego. Użyto domyślnych ustawień.")
            self._create_default_config_sections_if_missing()

    def _create_default_config_sections_if_missing(self):
        for exchange_name, details in self.exchange_options.items():
            section_name = details["config_section"]
            if not self.config.has_section(section_name):
                self.config.add_section(section_name)
                self.config.set(section_name, 'api_key', '')
                self.config.set(section_name, 'api_secret', '')
                self.config.set(section_name, 'scanner_watchlist_pairs', '')

                self.config.set(section_name, 'of_resample_interval', '1s')
                self.config.set(section_name, 'of_delta_mode', 'CVD (Skumulowana Delta)')
                self.config.set(section_name, 'of_aggregation_level', 'Brak')
                self.config.set(section_name, 'of_ob_source', 'Wybrana giełda')
                self.config.set(section_name, 'of_watchlist_pairs', '')

                self.config.set(section_name, 'sd_baseline_minutes', '15')
                self.config.set(section_name, 'sd_time_window', '5')
                self.config.set(section_name, 'sd_volume_threshold_multiplier', '2.0')
                self.config.set(section_name, 'sd_price_threshold_percent', '0.5')
                self.config.set(section_name, 'sd_alert_cooldown_minutes', '5')
                self.config.set(section_name, 'sd_watchlist_pairs', '')
                for i in range(6):
                    self.config.set(section_name, f'cw_chart_{i}_timeframe', ['1h', '4h', '1d', '15m', '1h', '1w'][i])
                self.config.set(section_name, 'cw_watchlist_pairs', '')
                self.config.set(section_name, 'cw_auto_refresh_enabled', 'False')
                self.config.set(section_name, 'cw_refresh_interval_minutes', '5')


        if not self.config.has_section('global_settings'):
            self.config.add_section('global_settings')
            self.config.set('global_settings', 'default_wpr_length', '14')
            self.config.set('global_settings', 'default_ema_length', '9')
            self.config.set('global_settings', 'default_macd_fast', '12')
            self.config.set('global_settings', 'default_macd_slow', '26')
            self.config.set('global_settings', 'default_macd_signal', '9')

            self.config.set('global_settings', 'scanner_selected_timeframes', ','.join(AVAILABLE_TIMEFRAMES))
            self.config.set('global_settings', 'scanner_wr_length', '14')
            self.config.set('global_settings', 'scanner_wr_operator', '>=')
            self.config.set('global_settings', 'scanner_wr_value', '-20.0')
            self.config.set('global_settings', 'scanner_ema_wpr_length', '9')
            self.config.set('global_settings', 'scanner_ema_wpr_operator', '>=')
            self.config.set('global_settings', 'scanner_ema_wpr_value', '-30.0')
            self.config.set('global_settings', 'scanner_scan_delay_minutes', '5')
            self.config.set('global_settings', 'selected_exchange', 'Binance (Spot)')
            # Ustawienia powiadomień
            self.config.set('global_settings', 'notification_telegram_token', '')
            self.config.set('global_settings', 'notification_telegram_chat_id', '')
            self.config.set('global_settings', 'notification_email_address', '')
            self.config.set('global_settings', 'notification_email_password', '')
            self.config.set('global_settings', 'notification_smtp_server', 'smtp.gmail.com')
            self.config.set('global_settings', 'notification_smtp_port', '587')
            self.config.set('global_settings', 'scanner_notification_enabled', 'False')
            self.config.set('global_settings', 'scanner_notification_method', 'Brak')

        try:
            with open(CONFIG_FILE_PATH, 'w') as configfile:
                self.config.write(configfile)
        except Exception as e:
            self.error_message(f"Błąd podczas zapisu domyślnej konfiguracji po braku pliku: {e}")


    def apply_settings_to_ui(self):
        global_settings = self.config['global_settings'] if self.config.has_section('global_settings') else {}

        # Ustawienie wybranej giełdy w ComboBox
        saved_exchange = global_settings.get('selected_exchange', 'Binance (Spot)')
        self.exchange_combo.setCurrentText(saved_exchange)

        # Ustawienia interwałów (checkboxes)
        selected_timeframes_str = global_settings.get('scanner_selected_timeframes', ','.join(AVAILABLE_TIMEFRAMES))
        selected_timeframes_list = [tf.strip() for tf in selected_timeframes_str.split(',') if tf.strip()]
        for tf_text, checkbox in self.timeframe_checkboxes.items():
            checkbox.setChecked(tf_text in selected_timeframes_list)

        self.wr_length_spin.setValue(self.safe_int_cast(global_settings.get('scanner_wr_length', '14')))
        self.wpr_operator_combo.setCurrentText(global_settings.get('scanner_wr_operator', '>='))
        self.wpr_value_spin.setValue(self.safe_float_cast(global_settings.get('scanner_wr_value', '-20.0')))

        self.ema_wpr_length_spin.setValue(self.safe_int_cast(global_settings.get('scanner_ema_wpr_length', '9')))
        self.ema_wpr_operator_combo.setCurrentText(global_settings.get('scanner_ema_wpr_operator', '>='))
        self.ema_wpr_value_spin.setValue(self.safe_float_cast(global_settings.get('scanner_ema_wpr_value', '-30.0')))

        self.scan_delay_spin.setValue(self.safe_int_cast(global_settings.get('scanner_scan_delay_minutes', '5')))

        # Ustawienia powiadomień dla skanera
        self.enable_notifications_checkbox.setChecked(global_settings.getboolean('scanner_notification_enabled', False))
        self.notification_method_combo.setCurrentText(global_settings.get('scanner_notification_method', 'Brak'))
        self.toggle_notification_method_visibility()

        self.watchlist.clear()

    def save_settings(self):
        if not self.config.has_section('global_settings'):
            self.config.add_section('global_settings')
        global_settings = self.config['global_settings']

        selected_timeframes_list = [tf for tf, cb in self.timeframe_checkboxes.items() if cb.isChecked()]
        global_settings['scanner_selected_timeframes'] = ','.join(selected_timeframes_list)

        global_settings['scanner_wr_length'] = str(self.wr_length_spin.value())
        global_settings['scanner_wr_operator'] = self.wpr_operator_combo.currentText()
        global_settings['scanner_wr_value'] = str(self.wpr_value_spin.value())

        global_settings['scanner_ema_wpr_length'] = str(self.ema_wpr_length_spin.value())
        global_settings['scanner_ema_wpr_operator'] = self.ema_wpr_operator_combo.currentText()
        global_settings['scanner_ema_wpr_value'] = str(self.ema_wpr_value_spin.value())

        global_settings['scanner_scan_delay_minutes'] = str(self.scan_delay_spin.value())
        global_settings['selected_exchange'] = self.exchange_combo.currentText()

        # Zapisz ustawienia powiadomień dla skanera
        global_settings['scanner_notification_enabled'] = str(self.enable_notifications_checkbox.isChecked())
        global_settings['scanner_notification_method'] = self.notification_method_combo.currentText()

        current_exchange_name = self.exchange_combo.currentText()
        selected_exchange_config = self.exchange_options.get(current_exchange_name)
        if selected_exchange_config:
            section_name = selected_exchange_config["config_section"]
            if not self.config.has_section(section_name):
                self.config.add_section(section_name)

            watchlist_pairs = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]
            self.config.set(section_name, 'scanner_watchlist_pairs', ",".join(watchlist_pairs))

        try:
            with open(CONFIG_FILE_PATH, 'w') as configfile:
                self.config.write(configfile)
            self.log_message(f"Ustawienia skanera zapisane w {CONFIG_FILE_PATH}")
        except Exception as e:
            self.error_message(f"Błąd zapisu ustawień skanera: {e}")


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

        self.fetch_markets_for_selected_exchange()

        current_exchange_config = self.exchange_options.get(selected_exchange_name)
        if current_exchange_config:
            section_name = current_exchange_config["config_section"]
            if self.config.has_section(section_name):
                watchlist_pairs_str = self.config.get(section_name, 'scanner_watchlist_pairs', fallback='')
                if watchlist_pairs_str:
                    pairs_to_add = [p.strip() for p in watchlist_pairs_str.split(',') if p.strip()]
                    for pair_symbol in pairs_to_add:
                        item = QListWidgetItem(pair_symbol)
                        item.setData(Qt.ItemDataRole.UserRole, pair_symbol)
                        self.watchlist.addItem(item)
                elif current_exchange_config.get("default_pairs"):
                    for pair_symbol in current_exchange_config["default_pairs"]:
                        item = QListWidgetItem(pair_symbol)
                        item.setData(Qt.ItemDataRole.UserRole, pair_symbol)
                        self.watchlist.addItem(item)


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

        self.log_message(f"Załadowano {len(markets)} dostępnych par.")

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

    def add_all_to_watchlist(self):
        pairs_to_add = []
        for i in range(self.available_pairs_list.count()):
            item = self.available_pairs_list.item(i)
            symbol = item.text()
            found = False
            for j in range(self.watchlist.count()):
                if self.watchlist.item(j).text() == symbol:
                    found = True
                    break
            if not found:
                pairs_to_add.append(item)

        for item in pairs_to_add:
            watchlist_item = QListWidgetItem(item.text())
            watchlist_item.setData(Qt.ItemDataRole.UserRole, item.data(Qt.ItemDataRole.UserRole))
            self.watchlist.addItem(watchlist_item)

        if pairs_to_add:
            self.log_message(f"Dodano {len(pairs_to_add)} par do watchlisty.")
            self.save_settings()
        else:
            self.log_message("Wszystkie dostępne pary są już na watchliście.")

    def remove_from_watchlist(self):
        selected_items = self.watchlist.selectedItems()
        if not selected_items:
            self.log_message("Wybierz parę z 'Watchlisty' do usunięcia.")
            return

        for item in selected_items:
            self.watchlist.takeItem(self.watchlist.row(item))
            self.log_message(f"Usunięto {item.text()} z watchlisty.")
        self.save_settings()

    def remove_all_from_watchlist(self):
        if self.watchlist.count() == 0:
            self.log_message("Watchlista jest już pusta.")
            return

        removed_count = self.watchlist.count()
        self.watchlist.clear()
        self.log_message(f"Usunięto wszystkie {removed_count} par z watchlisty.")
        self.save_settings()

    def start_scanning(self):
        if self.current_scan_thread and self.current_scan_thread.isRunning():
            self.log_message("Skanowanie cykliczne już trwa. Najpierw zatrzymaj bieżące skanowanie.")
            return

        selected_exchange_name = self.exchange_combo.currentText()
        selected_exchange_details = self.exchange_options.get(selected_exchange_name)

        if not selected_exchange_details:
            self.error_message("Nie wybrano giełdy.")
            return

        # Pobieranie kluczy API z pliku konfiguracyjnego (dla perform_actual_scan)
        config_parser_temp = configparser.ConfigParser()
        config_parser_temp.read(CONFIG_FILE_PATH)
        api_key = config_parser_temp.get(selected_exchange_details['config_section'], 'api_key', fallback='')
        api_secret = config_parser_temp.get(selected_exchange_details['config_section'], 'api_secret', fallback='')

        # Pobierz globalne ustawienia powiadomień
        notification_settings = {}
        if config_parser_temp.has_section('global_settings'):
            global_settings = config_parser_temp['global_settings']
            notification_settings = {
                "enabled": self.enable_notifications_checkbox.isChecked(),
                "method": self.notification_method_combo.currentText(),
                "telegram_token": global_settings.get('notification_telegram_token', ''),
                "telegram_chat_id": global_settings.get('notification_telegram_chat_id', ''),
                "email_address": global_settings.get('notification_email_address', ''),
                "email_password": global_settings.get('notification_email_password', ''),
                "smtp_server": global_settings.get('notification_smtp_server', 'smtp.gmail.com'),
                "smtp_port": global_settings.get('notification_smtp_port', '587')
            }

        pairs_to_scan = [self.watchlist.item(i).text() for i in range(self.watchlist.count())]
        if not pairs_to_scan:
            self.error_message("Watchlista jest pusta. Dodaj pary do skanowania.")
            return

        selected_timeframes = [tf for tf, cb in self.timeframe_checkboxes.items() if cb.isChecked()]
        if not selected_timeframes:
            self.error_message("Wybierz przynajmniej jeden interwał do skanowania.")
            return

        scan_params = {
            'selected_exchange_name_gui': selected_exchange_name,
            'api_key': api_key,
            'api_secret': api_secret,
            'pairs_to_scan': pairs_to_scan,
            'selected_timeframes': selected_timeframes,
            'wpr_period_from_gui': self.wr_length_spin.value(),
            'wpr_operator_cond': self.wpr_operator_combo.currentText(),
            'wpr_value_cond': self.wpr_value_spin.value(),
            'ema_period_from_gui': self.ema_wpr_length_spin.value(),
            'ema_wpr_operator_cond': self.ema_wpr_operator_combo.currentText(),
            'ema_wpr_value_cond': self.ema_wpr_value_spin.value(),
            'scan_delay_minutes': self.scan_delay_spin.value(),
            'scan_delay_seconds': self.scan_delay_spin.value() * 60,
        }

        self.results_display.clear()
        self.log_message("Rozpoczynam cykliczne skanowanie...")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.current_scan_thread = ScanLoopThread(
            scan_params['selected_exchange_name_gui'], scan_params['api_key'], scan_params['api_secret'],
            scan_params['pairs_to_scan'], scan_params['selected_timeframes'],
            scan_params['wpr_operator_cond'], scan_params['wpr_value_cond'],
            scan_params['ema_wpr_operator_cond'], scan_params['ema_wpr_value_cond'],
            scan_params['wpr_period_from_gui'], scan_params['ema_period_from_gui'],
            scan_params['scan_delay_seconds'], self.exchange_options, notification_settings
        )
        self.current_scan_thread.progress_signal.connect(self.log_message)
        self.current_scan_thread.result_signal.connect(self.process_scan_data_or_clear)
        self.current_scan_thread.error_signal.connect(self.error_message)
        self.current_scan_thread.finished_signal.connect(self.on_full_scan_finished)
        self.current_scan_thread.start()

    def stop_scanning(self):
        if not self.current_scan_thread or not self.current_scan_thread.isRunning():
            self.log_message("Skanowanie nie jest aktywne.")
            return

        self.log_message("Zatrzymuję cykliczne skanowanie...")
        self.current_scan_thread.stop()
        if self.current_scan_thread and self.current_scan_thread.isRunning():
            self.current_scan_thread.wait(5000)
            if self.current_scan_thread.isRunning():
                self.error_message("Główny wątek skanowania nie zakończył się po żądaniu zatrzymania.")


    def process_scan_data_or_clear(self, data):
        if data.get('clear_results_table'):
            self.results_display.clear()
            return

        symbol = data['symbol']
        all_tfs_ok = data['all_tfs_ok']

        if all_tfs_ok:
            wr_value = data['wr_value']
            ema_value = data['ema_value']
            volume_24h_str = data['volume_24h_str']
            message = (f"OK: <b>{symbol}</b> (W%R:{wr_value:.2f},EMA:{ema_value:.2f},Wol:{volume_24h_str})")
            self.results_display.append(f"<font color='green'>{message}</font>")

        self.results_display.verticalScrollBar().setValue(self.results_display.verticalScrollBar().maximum())

    def on_full_scan_finished(self):
        self.log_message("Cykliczne skanowanie całkowicie zakończyło pracę.")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.current_scan_thread = None

    def log_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"[{current_time}] {message}")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())

    def error_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"<font color='orange'>[{current_time}] BŁĄD: {message}</font>")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())
        QMessageBox.warning(self, "Błąd Skanera", message)

    def closeEvent(self, event: QEvent):
        reply = QMessageBox.question(self, 'Potwierdzenie Zamknięcia',
                                     "Czy na pewno chcesz zamknąć okno skanera? Aktywne skanowanie zostanie zatrzymane.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, # Naprawiono literówkę
                                     QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            self.stop_scanning()
            if self.current_scan_thread and self.current_scan_thread.isRunning():
                self.current_scan_thread.wait(5000)
                if self.current_scan_thread.isRunning():
                    self.log_message("Wątek skanowania nie zakończył się poprawnie przed zamknięciem okna.")

            self.save_settings()
            event.accept()
        else:
            event.ignore()

if __name__ == '__main__':
    import qasync
    app = qasync.QApplication(sys.argv)
    app.setOrganizationName("MojaFirmaPrzyklad")
    app.setApplicationName(CONFIG_DIR_NAME)

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
            config_init.set('global_settings', 'scanner_wr_operator', '>=')
            config_init.set('global_settings', 'scanner_wr_value', '-20.0')
            config_init.set('global_settings', 'scanner_ema_wpr_length', '9')
            config_init.set('global_settings', 'scanner_ema_wpr_operator', '>=')
            config_init.set('global_settings', 'scanner_ema_wpr_value', '-30.0')
            config_init.set('global_settings', 'scanner_scan_delay_minutes', '5')
            config_init.set('global_settings', 'selected_exchange', 'Binance (Spot)')
            # Ustawienia powiadomień
            config_init.set('global_settings', 'notification_telegram_token', '')
            config_init.set('global_settings', 'notification_telegram_chat_id', '')
            config_init.set('global_settings', 'notification_email_address', '')
            config_init.set('global_settings', 'notification_email_password', '')
            config_init.set('global_settings', 'notification_smtp_server', 'smtp.gmail.com')
            config_init.set('global_settings', 'notification_smtp_port', '587')
            config_init.set('global_settings', 'scanner_notification_enabled', 'False')
            config_init.set('global_settings', 'scanner_notification_method', 'Brak')


        try:
            with open(CONFIG_FILE_PATH, 'w') as f:
                config_init.write(f)
        except Exception as e:
            print(f"Błąd podczas inicjalizacji pliku konfiguracyjnego w __main__: {e}")


    window = MainWindow()
    window.show()

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        sys.exit(loop.run_forever())
