# OSTATECZNA WERSJA PLIKU: utils.py

import os
import configparser
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from PyQt6.QtCore import QThread, pyqtSignal as Signal, QStandardPaths
import ccxt

# --- Konfiguracja ścieżek i stałe ---
CONFIG_DIR_NAME = "KryptoSkaner"
CONFIG_FILE_NAME = "app_settings.ini"
AVAILABLE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w']

def get_config_path():
    """Zwraca pełną, systemową ścieżkę do pliku konfiguracyjnego."""
    config_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    app_config_path = os.path.join(config_dir, CONFIG_DIR_NAME)
    if not os.path.exists(app_config_path):
        os.makedirs(app_config_path, exist_ok=True)
    return os.path.join(app_config_path, CONFIG_FILE_NAME)

# --- Funkcje pomocnicze ---

def safe_int_cast(value_str, default=0):
    """Bezpiecznie konwertuje string na int."""
    try:
        return int(float(value_str))
    except (ValueError, TypeError):
        return default

def safe_float_cast(value_str, default=0.0):
    """Bezpiecznie konwertuje string na float."""
    try:
        return float(value_str)
    except (ValueError, TypeError):
        return default

def format_large_number(num, currency_symbol=""):
    """Formatuje duże liczby do czytelnego formatu (mln, mld, etc.)."""
    if num is None: return "N/A"
    abs_num = abs(num)
    sign = "-" if num < 0 else ""
    if abs_num >= 1_000_000_000_000:
        val_str = f"{abs_num / 1_000_000_000_000:.2f} bln"
    elif abs_num >= 1_000_000_000:
        val_str = f"{abs_num / 1_000_000_000:.2f} mld"
    elif abs_num >= 1_000_000:
        val_str = f"{abs_num / 1_000_000:.2f} mln"
    elif abs_num >= 1_000:
        val_str = f"{abs_num / 1_000:.2f} tys."
    else:
        val_str = f"{abs_num:,.0f}"
    return f"{sign}{val_str.replace('.',',')} {currency_symbol}".strip()

TIMEFRAME_DURATIONS_MINUTES = {
    '1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '12h': 720, '1d': 1440, '1w': 10080
}

def get_timeframe_duration_for_sort(tf_string):
    """Zwraca długość interwału w minutach na potrzeby sortowania."""
    return TIMEFRAME_DURATIONS_MINUTES.get(tf_string.lower(), 0)

# --- Funkcje do wysyłania powiadomień ---

def send_telegram_notification(bot_token, chat_id, message, progress_callback):
    """Wysyła powiadomienie na Telegram."""
    if not bot_token or not chat_id:
        progress_callback.emit("<font color='orange'>Ostrzeżenie: Token Telegram lub Chat ID nieskonfigurowane.</font>")
        return
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
    try:
        response = requests.post(telegram_url, data=payload, timeout=10)
        response.raise_for_status()
        if response.json().get("ok"):
            progress_callback.emit("Powiadomienie Telegram wysłane.")
        else:
            progress_callback.emit(f"<font color='red'>Błąd Telegram: {response.json().get('description','Brak szczegółów')}</font>")
    except Exception as e:
        progress_callback.emit(f"<font color='red'>Błąd Telegram: {str(e)}</font>")

def send_email_notification(sender_email, sender_password, receiver_email, smtp_server, smtp_port, subject, body, progress_callback):
    """Wysyła powiadomienie e-mail."""
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
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, message.as_string())
            progress_callback.emit("Powiadomienie e-mail wysłane.")
    except Exception as e:
        progress_callback.emit(f"<font color='red'>Błąd E-mail: {str(e)}</font>")

# --- Wspólne klasy QThread ---

class FetchMarketsThread(QThread):
    """Wątek do asynchronicznego pobierania listy rynków z giełdy."""
    markets_fetched = Signal(list)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self, exchange_id, market_type_filter, parent=None):
        super().__init__(parent)
        self.exchange_id = exchange_id
        self.market_type_filter = market_type_filter

    def run(self):
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
            exchange = exchange_class({'enableRateLimit': True})
            markets = exchange.load_markets()

            available_market_data = []
            for m in markets.values():
                if self.type_matches(m):
                    available_market_data.append({
                        'symbol': m['symbol'],
                        'id': m['id'],
                        'type': m['type']
                    })
            self.markets_fetched.emit(sorted(available_market_data, key=lambda x: x['symbol']))
        except Exception as e:
            self.error_occurred.emit(f"Błąd pobierania par dla {self.exchange_id}: {e}")
        finally:
            self.finished.emit()

    def type_matches(self, market: dict) -> bool:
        """Sprawdza, czy rynek pasuje do kryteriów (aktywny, USDT quote, typ rynku)."""
        if not (market.get('active', False) and market.get('quote', '').upper() == 'USDT'):
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
