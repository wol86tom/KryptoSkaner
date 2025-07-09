import sys
import os
import configparser
import datetime
import time
import ccxt
import ccxt.pro as ccxtpro
import asyncio
import pandas as pd
import pandas_ta as ta
import numpy as np
from collections import deque
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QLineEdit,
                             QPushButton, QComboBox, QSpinBox, QDoubleSpinBox,
                             QTextEdit, QListWidget, QListWidgetItem, QGroupBox,
                             QMessageBox, QTabWidget, QScrollArea, QSizePolicy,
                             QFormLayout, QCheckBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QStandardPaths, QEvent
from PyQt6.QtGui import QFont, QIcon, QAction

# Importy dla okien narzędzi
from chart_window import ChartWindow
from spike_detector_window import SpikeDetectorWindow
from order_flow_window import OrderFlowWindow

# Import dla nowego okna skanera
from scanner_window import ScannerWindow

# Dodaj import qasync
import qasync

# --- Konfiguracja ścieżek ---
CONFIG_DIR_NAME = "KryptoSkaner"
CONFIG_FILE_NAME = "app_settings.ini"

def get_config_path():
    config_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    app_config_path = os.path.join(config_dir, CONFIG_DIR_NAME)
    if not os.path.exists(app_config_path):
        os.makedirs(app_config_path, exist_ok=True)
    return os.path.join(app_config_path, CONFIG_FILE_NAME)

CONFIG_FILE_PATH = get_config_path()

# Globalne stałe
AVAILABLE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w']


# --- Główne okno ustawień aplikacji ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Krypto Skaner - Ustawienia Globalne")
        self.setGeometry(100, 100, 1000, 800) # Zwiększamy rozmiar, by pomieścić więcej ustawień

        self.exchange_options = {
            "Binance (Spot)": {"id_ccxt": "binance", "type": "spot", "default_pairs": ["BTC/USDT", "ETH/USDT"], "config_section": "binance_spot_config"},
            "Binance (Futures USDT-M)": {"id_ccxt": "binanceusdm", "type": "future", "default_pairs": ["BTC/USDT", "ETH/USDT"], "config_section": "binance_futures_config"},
            "Bybit (Spot)": {"id_ccxt": "bybit", "type": "spot", "default_pairs": ["BTC/USDT", "ETH/USDT"], "config_section": "bybit_spot_config"},
            "Bybit (Perpetual USDT)": {"id_ccxt": "bybit", "type": "swap", "default_pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT"], "config_section": "bybit_perp_config"}
        }

        self.init_ui()
        self.load_settings()
        self.apply_settings_to_ui()

    def init_ui(self):
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        # Tworzenie paska menu
        self.create_menu_bar()

        self.tab_widget = QTabWidget()
        self.main_layout.addWidget(self.tab_widget)

        self.setup_global_settings_tab()

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFixedHeight(100)
        self.main_layout.addWidget(self.log_display)

        self.status_bar = self.statusBar()
        self.status_bar.showMessage("Gotowy.")

    def create_menu_bar(self):
        menu_bar = self.menuBar()

        # Menu "Narzędzia"
        tools_menu = menu_bar.addMenu("Narzędzia")

        # Akcja dla okna Skanera
        self.scanner_window_action = QAction("Skaner Krypto", self)
        self.scanner_window_action.triggered.connect(self.open_scanner_window)
        tools_menu.addAction(self.scanner_window_action)

        # Akcja dla okna ChartWindow
        self.chart_window_action = QAction("Analiza Wykresów", self)
        self.chart_window_action.triggered.connect(self.open_chart_window)
        tools_menu.addAction(self.chart_window_action)

        # Akcja dla okna SpikeDetectorWindow
        self.spike_detector_action = QAction("Detektor Pików Cenowych", self)
        self.spike_detector_action.triggered.connect(self.open_spike_detector_window)
        tools_menu.addAction(self.spike_detector_action)

        # Akcja dla okna OrderFlowWindow
        self.order_flow_action = QAction("Analiza Order Flow", self)
        self.order_flow_action.triggered.connect(self.open_order_flow_window)
        tools_menu.addAction(self.order_flow_action)

    def open_scanner_window(self):
        if not hasattr(self, '_scanner_window') or not self._scanner_window.isVisible():
            self._scanner_window = ScannerWindow(self.exchange_options)
            self._scanner_window.show()
        else:
            self._scanner_window.activateWindow()
            self._scanner_window.raise_()

    def open_chart_window(self):
        if not hasattr(self, '_chart_window') or not self._chart_window.isVisible():
            self._chart_window = ChartWindow(self.exchange_options, CONFIG_FILE_PATH)
            self._chart_window.show()
        else:
            self._chart_window.activateWindow()
            self._chart_window.raise_()

    def open_spike_detector_window(self):
        if not hasattr(self, '_spike_detector_window') or not self._spike_detector_window.isVisible():
            self._spike_detector_window = SpikeDetectorWindow(self.exchange_options)
            self._spike_detector_window.show()
        else:
            self._spike_detector_window.activateWindow()
            self._spike_detector_window.raise_()

    def open_order_flow_window(self):
        if not hasattr(self, '_order_flow_window') or not self._order_flow_window.isVisible():
            self._order_flow_window = OrderFlowWindow(self.exchange_options)
            self._order_flow_window.show()
        else:
            self._order_flow_window.activateWindow()
            self._order_flow_window.raise_()

    def setup_global_settings_tab(self):
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        self.tab_widget.addTab(settings_tab, "Ustawienia Ogólne i API")

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_area.setWidget(scroll_content)
        settings_layout.addWidget(scroll_area)

        # Globalne ustawienia wskaźników
        indicator_defaults_group = QGroupBox("Domyślne Parametry Wskaźników")
        indicator_defaults_layout = QVBoxLayout()
        indicator_defaults_group.setLayout(indicator_defaults_layout)

        wpr_ema_group = QGroupBox("Williams %R i EMA")
        wpr_ema_layout = QFormLayout()
        wpr_ema_group.setLayout(wpr_ema_layout)

        self.default_wpr_length_spin = QSpinBox()
        self.default_wpr_length_spin.setRange(1, 200)
        self.default_wpr_length_spin.setValue(14)
        wpr_ema_layout.addRow("W%R Długość:", self.default_wpr_length_spin)

        self.default_ema_length_spin = QSpinBox()
        self.default_ema_length_spin.setRange(1, 200)
        self.default_ema_length_spin.setValue(9)
        wpr_ema_layout.addRow("EMA Długość:", self.default_ema_length_spin)

        indicator_defaults_layout.addWidget(wpr_ema_group)

        macd_group = QGroupBox("MACD")
        macd_layout = QFormLayout()
        macd_group.setLayout(macd_layout)

        self.default_macd_fast_spin = QSpinBox()
        self.default_macd_fast_spin.setRange(1, 200)
        self.default_macd_fast_spin.setValue(12)
        macd_layout.addRow("MACD Fast:", self.default_macd_fast_spin)

        self.default_macd_slow_spin = QSpinBox()
        self.default_macd_slow_spin.setRange(1, 200)
        self.default_macd_slow_spin.setValue(26)
        macd_layout.addRow("MACD Slow:", self.default_macd_slow_spin)

        self.default_macd_signal_spin = QSpinBox()
        self.default_macd_signal_spin.setRange(1, 200)
        self.default_macd_signal_spin.setValue(9)
        macd_layout.addRow("MACD Signal:", self.default_macd_signal_spin)

        indicator_defaults_layout.addWidget(macd_group)
        scroll_layout.addWidget(indicator_defaults_group)

        # --- NOWE SEKCJE DLA USTAWIEŃ MODUŁÓW ---
        # Skaner
        scanner_settings_group = QGroupBox("Ustawienia Skanera")
        scanner_settings_layout = QFormLayout()
        scanner_settings_group.setLayout(scanner_settings_layout)

        self.scanner_default_timeframes_label = QLabel("Interwały (domyślne):")
        scanner_settings_layout.addRow(self.scanner_default_timeframes_label) # To będzie aktualizowane programowo

        self.scanner_wr_length_label = QLabel("W%R Długość:")
        scanner_settings_layout.addRow(self.scanner_wr_length_label)

        self.scanner_wr_operator_label = QLabel("W%R Operator:")
        scanner_settings_layout.addRow(self.scanner_wr_operator_label)

        self.scanner_wr_value_label = QLabel("W%R Wartość:")
        scanner_settings_layout.addRow(self.scanner_wr_value_label)

        self.scanner_ema_wpr_length_label = QLabel("EMA(W%R) Długość:")
        scanner_settings_layout.addRow(self.scanner_ema_wpr_length_label)

        self.scanner_ema_wpr_operator_label = QLabel("EMA(W%R) Operator:")
        scanner_settings_layout.addRow(self.scanner_ema_wpr_operator_label)

        self.scanner_ema_wpr_value_label = QLabel("EMA(W%R) Wartość:")
        scanner_settings_layout.addRow(self.scanner_ema_wpr_value_label)

        self.scanner_scan_delay_label = QLabel("Odstęp między cyklami (min):")
        scanner_settings_layout.addRow(self.scanner_scan_delay_label)

        self.scanner_notification_enabled_label = QLabel("Powiadomienia włączone:")
        scanner_settings_layout.addRow(self.scanner_notification_enabled_label)

        self.scanner_notification_method_label = QLabel("Metoda powiadomień:")
        scanner_settings_layout.addRow(self.scanner_notification_method_label)

        scroll_layout.addWidget(scanner_settings_group)

        # Okno analizy wykresów
        chart_settings_group = QGroupBox("Ustawienia Analizy Wykresów")
        chart_settings_layout = QFormLayout()
        chart_settings_group.setLayout(chart_settings_layout)

        self.chart_auto_refresh_label = QLabel("Auto-odświeżanie włączone:")
        chart_settings_layout.addRow(self.chart_auto_refresh_label)
        self.chart_refresh_interval_label = QLabel("Interwał odświeżania (min):")
        chart_settings_layout.addRow(self.chart_refresh_interval_label)
        self.chart_default_timeframes_label = QLabel("Domyślne interwały wykresów:")
        chart_settings_layout.addRow(self.chart_default_timeframes_label)

        scroll_layout.addWidget(chart_settings_group)

        # Detektor pików
        spike_detector_settings_group = QGroupBox("Ustawienia Detektora Pików")
        spike_detector_settings_layout = QFormLayout()
        spike_detector_settings_group.setLayout(spike_detector_settings_layout)

        self.sd_baseline_minutes_label = QLabel("Okres bazowy (minuty):")
        spike_detector_settings_layout.addRow(self.sd_baseline_minutes_label)
        self.sd_time_window_label = QLabel("Okno piku (sekundy):")
        spike_detector_settings_layout.addRow(self.sd_time_window_label)
        self.sd_volume_threshold_multiplier_label = QLabel("Mnożnik progu wolumenu:")
        spike_detector_settings_layout.addRow(self.sd_volume_threshold_multiplier_label)
        self.sd_price_threshold_percent_label = QLabel("Próg zmiany ceny (%):")
        spike_detector_settings_layout.addRow(self.sd_price_threshold_percent_label)
        self.sd_alert_cooldown_minutes_label = QLabel("Cooldown alertu (minuty):")
        spike_detector_settings_layout.addRow(self.sd_alert_cooldown_minutes_label)

        scroll_layout.addWidget(spike_detector_settings_group)

        # Analiza Order Flow
        order_flow_settings_group = QGroupBox("Ustawienia Analizy Order Flow")
        order_flow_settings_layout = QFormLayout()
        order_flow_settings_group.setLayout(order_flow_settings_layout)

        self.of_resample_interval_label = QLabel("Interwał resamplingu:")
        order_flow_settings_layout.addRow(self.of_resample_interval_label)
        self.of_delta_mode_label = QLabel("Tryb delty:")
        order_flow_settings_layout.addRow(self.of_delta_mode_label)
        self.of_aggregation_level_label = QLabel("Agregacja Order Book:")
        order_flow_settings_layout.addRow(self.of_aggregation_level_label)
        self.of_ob_source_label = QLabel("Źródło Order Book:")
        order_flow_settings_layout.addRow(self.of_ob_source_label)

        scroll_layout.addWidget(order_flow_settings_group)
        # --- KONIEC NOWYCH SEKCJI DLA USTAWIEŃ MODUŁÓW ---


        # Sekcja ustawień API (pozostaje na dole, ale bez przycisków zapisu)
        api_settings_group = QGroupBox("Ustawienia API dla Giełd")
        api_settings_layout = QVBoxLayout()
        api_settings_group.setLayout(api_settings_layout)
        scroll_layout.addWidget(api_settings_group) # Przeniesiono na koniec listy GroupBoxów

        for exchange_name, details in self.exchange_options.items():
            group_box = QGroupBox(exchange_name)
            form_layout = QFormLayout()
            group_box.setLayout(form_layout)

            api_key_label = QLabel("API Key:")
            api_key_input = QLineEdit()
            api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            api_key_input.setObjectName(f"{details['config_section']}_api_key")
            form_layout.addRow(api_key_label, api_key_input)

            api_secret_label = QLabel("API Secret:")
            api_secret_input = QLineEdit()
            api_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
            api_secret_input.setObjectName(f"{details['config_section']}_api_secret")
            form_layout.addRow(api_secret_label, api_secret_input)

            api_settings_layout.addWidget(group_box)

        # Ustawienia Powiadomień Globalnych (pozostaje na dole)
        notifications_group = QGroupBox("Ustawienia Powiadomień Globalnych")
        notifications_layout = QVBoxLayout()
        notifications_group.setLayout(notifications_layout)

        telegram_settings_group = QGroupBox("Ustawienia Telegram")
        telegram_layout = QFormLayout()
        telegram_settings_group.setLayout(telegram_layout)

        self.telegram_token_input = QLineEdit()
        self.telegram_token_input.setPlaceholderText("Wprowadź token bota Telegram")
        telegram_layout.addRow("Token Bota:", self.telegram_token_input)

        self.telegram_chat_id_input = QLineEdit()
        self.telegram_chat_id_input.setPlaceholderText("Wprowadź swój Chat ID Telegram")
        telegram_layout.addRow("Chat ID:", self.telegram_chat_id_input)
        notifications_layout.addWidget(telegram_settings_group)

        email_settings_group = QGroupBox("Ustawienia Email")
        email_layout = QFormLayout()
        email_settings_group.setLayout(email_layout)

        self.email_address_input = QLineEdit()
        self.email_address_input.setPlaceholderText("Wprowadź adres e-mail nadawcy")
        email_layout.addRow("Adres E-mail:", self.email_address_input)

        self.email_password_input = QLineEdit()
        self.email_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.email_password_input.setPlaceholderText("Wprowadź hasło do e-maila lub hasło aplikacji")
        email_layout.addRow("Hasło E-mail:", self.email_password_input)

        self.smtp_server_input = QLineEdit()
        self.smtp_server_input.setPlaceholderText("Wprowadź serwer SMTP (np. smtp.gmail.com)")
        email_layout.addRow("Serwer SMTP:", self.smtp_server_input)

        self.smtp_port_spin = QSpinBox()
        self.smtp_port_spin.setRange(1, 65535)
        self.smtp_port_spin.setValue(587)
        email_layout.addRow("Port SMTP:", self.smtp_port_spin)
        notifications_layout.addWidget(email_settings_group)

        scroll_layout.addWidget(notifications_group)

        # Przycisk Zapisz Ustawienia (teraz dla wszystkich ustawień)
        save_load_buttons_layout = QHBoxLayout()
        self.save_all_settings_button = QPushButton("Zapisz Ustawienia")
        self.save_all_settings_button.clicked.connect(self.save_settings)
        save_load_buttons_layout.addWidget(self.save_all_settings_button)
        settings_layout.addLayout(save_load_buttons_layout) # Dodaj do głównego layoutu zakładki

        settings_layout.addStretch(1) # Rozciągnij, aby przycisk był na dole

    def load_settings(self):
        self.config = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE_PATH):
            self.config.read(CONFIG_FILE_PATH)
            self.log_message("Załadowano ustawienia z pliku.")
        else:
            self.log_message("Plik konfiguracyjny nie istnieje. Użyto domyślnych ustawień i utworzono nowy plik.")

        # Upewnij się, że wszystkie sekcje dla giełd istnieją
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


        # Upewnij się, że sekcja 'global_settings' istnieje
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
            self.config.set('global_settings', 'notification_telegram_token', '')
            self.config.set('global_settings', 'notification_telegram_chat_id', '')
            self.config.set('global_settings', 'notification_email_address', '')
            self.config.set('global_settings', 'notification_email_password', '')
            self.config.set('global_settings', 'notification_smtp_server', 'smtp.gmail.com')
            self.config.set('global_settings', 'notification_smtp_port', '587')
            self.config.set('global_settings', 'scanner_notification_enabled', 'False')
            self.config.set('global_settings', 'scanner_notification_method', 'Brak')


        # Zapisz (utwórz/zaktualizuj) plik, aby zawierał wszystkie sekcje
        try:
            with open(CONFIG_FILE_PATH, 'w') as configfile:
                self.config.write(configfile)
        except Exception as e:
            self.error_message(f"Błąd podczas zapisu domyślnej konfiguracji: {e}")


    def apply_settings_to_ui(self):
        # Ustawienia API
        for exchange_name, details in self.exchange_options.items():
            section_name = details["config_section"]
            api_key_input = self.findChild(QLineEdit, f"{section_name}_api_key")
            api_secret_input = self.findChild(QLineEdit, f"{section_name}_api_secret")
            if api_key_input:
                api_key_input.setText(self.config.get(section_name, 'api_key', fallback=''))
            if api_secret_input:
                api_secret_input.setText(self.config.get(section_name, 'api_secret', fallback=''))

        # Globalne ustawienia wskaźników
        global_settings = self.config['global_settings']
        self.default_wpr_length_spin.setValue(self.safe_int_cast(global_settings.get('default_wpr_length', '14')))
        self.default_ema_length_spin.setValue(self.safe_int_cast(global_settings.get('default_ema_length', '9')))
        self.default_macd_fast_spin.setValue(self.safe_int_cast(global_settings.get('default_macd_fast', '12')))
        self.default_macd_slow_spin.setValue(self.safe_int_cast(global_settings.get('default_macd_slow', '26')))
        self.default_macd_signal_spin.setValue(self.safe_int_cast(global_settings.get('default_macd_signal', '9')))

        # Ustawienia powiadomień - TERAZ POPRAWNIE WŁADUJEMY WARTOŚCI
        self.telegram_token_input.setText(global_settings.get('notification_telegram_token', ''))
        self.telegram_chat_id_input.setText(global_settings.get('notification_telegram_chat_id', ''))
        self.email_address_input.setText(global_settings.get('notification_email_address', ''))
        self.email_password_input.setText(global_settings.get('notification_email_password', ''))
        self.smtp_server_input.setText(global_settings.get('notification_smtp_server', 'smtp.gmail.com'))
        self.smtp_port_spin.setValue(self.safe_int_cast(global_settings.get('notification_smtp_port', '587')))

        # --- ŁADOWANIE USTAWIEŃ DLA KAŻDEGO MODUŁU (WYŚWIETLANIE) ---
        # Skaner
        self.scanner_default_timeframes_label.setText(global_settings.get('scanner_selected_timeframes', ','.join(AVAILABLE_TIMEFRAMES)))
        self.scanner_wr_length_label.setText(global_settings.get('scanner_wr_length', '14'))
        self.scanner_wr_operator_label.setText(global_settings.get('scanner_wr_operator', '>='))
        self.scanner_wr_value_label.setText(global_settings.get('scanner_wr_value', '-20.0'))
        self.scanner_ema_wpr_length_label.setText(global_settings.get('scanner_ema_wpr_length', '9'))
        self.scanner_ema_wpr_operator_label.setText(global_settings.get('scanner_ema_wpr_operator', '>='))
        self.scanner_ema_wpr_value_label.setText(global_settings.get('scanner_ema_wpr_value', '-30.0'))
        self.scanner_scan_delay_label.setText(global_settings.get('scanner_scan_delay_minutes', '5') + " min")
        self.scanner_notification_enabled_label.setText(global_settings.get('scanner_notification_enabled', 'False'))
        self.scanner_notification_method_label.setText(global_settings.get('scanner_notification_method', 'Brak'))

        # Okno Analizy Wykresów
        self.chart_auto_refresh_label.setText(global_settings.get('cw_auto_refresh_enabled', 'False'))
        self.chart_refresh_interval_label.setText(global_settings.get('cw_refresh_interval_minutes', '5') + " min")
        # Domyślne interwały dla wykresów są per-giełda, więc w globalnych ustawieniach pokażemy ogólną informację
        self.chart_default_timeframes_label.setText("Domyślne dla wykresów")

        # Detektor Pików
        self.sd_baseline_minutes_label.setText(global_settings.get('sd_baseline_minutes', '15') + " min")
        self.sd_time_window_label.setText(global_settings.get('sd_time_window', '5') + " s")
        self.sd_volume_threshold_multiplier_label.setText(global_settings.get('sd_volume_threshold_multiplier', '2.0') + " x")
        self.sd_price_threshold_percent_label.setText(global_settings.get('sd_price_threshold_percent', '0.5') + " %")
        self.sd_alert_cooldown_minutes_label.setText(global_settings.get('sd_alert_cooldown_minutes', '5') + " min")

        # Analiza Order Flow
        self.of_resample_interval_label.setText(global_settings.get('of_resample_interval', '1s'))
        self.of_delta_mode_label.setText(global_settings.get('of_delta_mode', 'CVD (Skumulowana Delta)'))
        self.of_aggregation_level_label.setText(global_settings.get('of_aggregation_level', 'Brak'))
        self.of_ob_source_label.setText(global_settings.get('of_ob_source', 'Wybrana giełda'))


    def save_settings(self):
        # Zapisz globalne ustawienia wskaźników
        if not self.config.has_section('global_settings'):
            self.config.add_section('global_settings')
        global_settings = self.config['global_settings']
        global_settings['default_wpr_length'] = str(self.default_wpr_length_spin.value())
        global_settings['default_ema_length'] = str(self.default_ema_length_spin.value())
        global_settings['default_macd_fast'] = str(self.default_macd_fast_spin.value())
        global_settings['default_macd_slow'] = str(self.default_macd_slow_spin.value())
        global_settings['default_macd_signal'] = str(self.default_macd_signal_spin.value())

        # Zapisz ustawienia API
        for exchange_name, details in self.exchange_options.items():
            section_name = details["config_section"]
            api_key_input = self.findChild(QLineEdit, f"{section_name}_api_key")
            api_secret_input = self.findChild(QLineEdit, f"{section_name}_api_secret")
            if api_key_input and api_secret_input:
                if not self.config.has_section(section_name):
                    self.config.add_section(section_name)
                self.config.set(section_name, 'api_key', api_key_input.text())
                self.config.set(section_name, 'api_secret', api_secret_input.text())

        # Zapisz ustawienia powiadomień
        global_settings['notification_telegram_token'] = self.telegram_token_input.text()
        global_settings['notification_telegram_chat_id'] = self.telegram_chat_id_input.text()
        global_settings['notification_email_address'] = self.email_address_input.text()
        global_settings['notification_email_password'] = self.email_password_input.text()
        global_settings['notification_smtp_server'] = self.smtp_server_input.text()
        global_settings['notification_smtp_port'] = str(self.smtp_port_spin.value())

        try:
            with open(CONFIG_FILE_PATH, 'w') as configfile:
                self.config.write(configfile)
            self.log_message(f"Ustawienia zapisane w {CONFIG_FILE_PATH}")
            QMessageBox.information(self, "Zapisano", "Ustawienia zostały zapisane pomyślnie.")
            # Odśwież widok po zapisie, aby pokazać, że dane zostały zaktualizowane
            self.apply_settings_to_ui()
        except Exception as e:
            self.error_message(f"Błąd zapisu ustawień: {e}")
            QMessageBox.critical(self, "Błąd Zapisu", f"Nie udało się zapisać ustawień: {str(e)}")

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

    def log_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"[{current_time}] {message}")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())

    def error_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"<font color='orange'>[{current_time}] BŁĄD: {message}</font>")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())
        QMessageBox.warning(self, "Błąd", message)

    def closeEvent(self, event: QEvent):
        reply = QMessageBox.question(self, 'Potwierdzenie Zamknięcia',
                                     "Czy na pewno chcesz zamknąć aplikację? Niezapisane ustawienia mogą zostać utracone.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            if hasattr(self, '_scanner_window') and self._scanner_window.isVisible():
                self._scanner_window.close()
            if hasattr(self, '_chart_window') and self._chart_window.isVisible():
                self._chart_window.close()
            if hasattr(self, '_spike_detector_window') and self._spike_detector_window.isVisible():
                self._spike_detector_window.close()
            if hasattr(self, '_order_flow_window') and self._order_flow_window.isVisible():
                self._order_flow_window.close()

            event.accept()
        else:
            event.ignore()

if __name__ == '__main__':
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
