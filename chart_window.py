# ZREFAKTORYZOWANY I OSTATECZNIE POPRAWIONY PLIK: chart_window.py

import sys, os, configparser, datetime, time, ccxt, pandas as pd, pandas_ta as ta, pyqtgraph as pg, numpy as np
from PyQt6.QtWidgets import (QMainWindow, QVBoxLayout, QWidget, QComboBox, QGridLayout, QLabel, QListWidget, QPushButton, QHBoxLayout, QGroupBox, QApplication, QMessageBox, QListWidgetItem, QFormLayout, QSpinBox, QStackedWidget, QCheckBox, QScrollArea, QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QEvent, QPointF, QRectF
from PyQt6.QtGui import QPainter, QPen, QFont, QBrush

import utils

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')

# Stałe
DEFAULT_WPR_LENGTH = 14
DEFAULT_EMA_WPR_LENGTH = 9
DEFAULT_RSI_LENGTH = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_REFRESH_MINUTES = 5

class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data=[]):
        pg.GraphicsObject.__init__(self)
        self._data = []
        self.setData(data)

    def setData(self, data):
        self._data = data
        self.generatePicture()
        self.prepareGeometryChange()
        self.update()

    def generatePicture(self):
        self.picture = pg.QtGui.QPicture()
        p = pg.QtGui.QPainter(self.picture)
        if not self._data:
            p.end(); return

        if len(self._data) > 1:
            w = np.mean([self._data[i]['x'] - self._data[i-1]['x'] for i in range(1, len(self._data))]) * 0.4
        else:
            w = 1.0

        for d in self._data:
            t, open_val, high_val, low_val, close_val = d['x'], d['open'], d['high'], d['low'], d['close']
            p.setPen(pg.mkPen('k'))
            p.drawLine(QPointF(t, low_val), QPointF(t, high_val))
            p.setBrush(pg.mkBrush('g' if open_val < close_val else 'r'))
            p.setPen(pg.mkPen('k', width=1)) # Cienka czarna ramka
            rect_top = min(open_val, close_val)
            rect_height = abs(close_val - open_val)
            p.drawRect(QRectF(t - w, rect_top, w * 2, rect_height))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        if not self._data: return QRectF()
        x_min = min(d['x'] for d in self._data)
        x_max = max(d['x'] for d in self._data)
        y_min = np.min([d['low'] for d in self._data])
        y_max = np.max([d['high'] for d in self._data])

        if len(self._data) > 1:
            w = np.mean([self._data[i]['x'] - self._data[i-1]['x'] for i in range(1, len(self._data))]) * 0.4
        else:
            w = 1.0
        return QRectF(x_min - w, y_min, (x_max - x_min) + 2 * w, y_max - y_min)


class FetchChartDataThread(QThread):
    data_ready_signal = Signal(object, object)
    error_signal = Signal(str, object)
    finished_signal = Signal(object)

    def __init__(self, exchange, pair_symbol, timeframe, indicator_name, indicator_params, chart_widget, parent=None):
        super().__init__(parent)
        self.exchange = exchange; self.pair_symbol = pair_symbol; self.timeframe = timeframe
        self.indicator_name = indicator_name; self.indicator_params = indicator_params; self.chart_widget = chart_widget

    def run(self):
        try:
            ohlcv = self.exchange.fetch_ohlcv(self.pair_symbol, timeframe=self.timeframe, limit=300)
            if not ohlcv: raise ccxt.NetworkError(f"Brak danych OHLCV dla {self.pair_symbol} na {self.timeframe}.")
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df.set_index('timestamp', inplace=True)
            if self.indicator_name == "Williams %R":
                df.ta.willr(length=self.indicator_params.get('wpr_period', DEFAULT_WPR_LENGTH), append=True)
                wpr_col = next((c for c in df.columns if c.startswith('WILLR_')), None)
                if wpr_col: df[f'WPR_EMA_{self.indicator_params.get("ema_period", DEFAULT_EMA_WPR_LENGTH)}'] = ta.ema(df[wpr_col], length=self.indicator_params.get('ema_period', DEFAULT_EMA_WPR_LENGTH))
            elif self.indicator_name == "RSI": df.ta.rsi(length=self.indicator_params.get('rsi_period', DEFAULT_RSI_LENGTH), append=True)
            elif self.indicator_name == "MACD": df.ta.macd(fast=self.indicator_params.get('fast', DEFAULT_MACD_FAST), slow=self.indicator_params.get('slow', DEFAULT_MACD_SLOW), signal=self.indicator_params.get('signal', DEFAULT_MACD_SIGNAL), append=True)
            self.data_ready_signal.emit(df, self.chart_widget)
        except Exception as e: self.error_signal.emit(f"Błąd danych dla {self.pair_symbol}: {e}", self.chart_widget)
        finally: self.finished_signal.emit(self.chart_widget)

class MeasurablePlotItem(pg.PlotItem):
    sigMeasureStart = Signal(object); sigMeasureUpdate = Signal(object); sigMeasureEnd = Signal(object)
    def __init__(self, *args, **kwargs): super().__init__(*args, **kwargs); self.measuring = False
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton and ev.modifiers() == Qt.KeyboardModifier.ControlModifier:
            ev.accept(); self.measuring = True; self.sigMeasureStart.emit(self.vb.mapSceneToView(ev.pos()))
        else: super().mousePressEvent(ev)
    def mouseMoveEvent(self, ev):
        if self.measuring: self.sigMeasureUpdate.emit(self.vb.mapSceneToView(ev.pos()))
        super().mouseMoveEvent(ev)
    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton and self.measuring:
            ev.accept(); self.measuring = False; self.sigMeasureEnd.emit(self.vb.mapSceneToView(ev.pos()))
        else: super().mouseReleaseEvent(ev)

class SingleChartWidget(QWidget):
    mouse_moved_signal = Signal(float); sigDoubleClicked = Signal()
    def __init__(self, chart_id, parent=None):
        super().__init__(parent)
        self.chart_id = chart_id; self.layout = QVBoxLayout(self); self.layout.setContentsMargins(2, 2, 2, 2); self.layout.setSpacing(2)
        top_layout = QHBoxLayout(); self.chart_title_label = QLabel(f"Wykres {self.chart_id + 1}"); self.chart_title_label.setFont(QFont("Arial", 9))
        self.timeframe_combo = QComboBox(); self.timeframe_combo.addItems(utils.AVAILABLE_TIMEFRAMES)
        top_layout.addWidget(self.chart_title_label); top_layout.addStretch(); top_layout.addWidget(QLabel("Interwał:")); top_layout.addWidget(self.timeframe_combo)
        self.plot_item_price = MeasurablePlotItem(axisItems={'bottom': pg.DateAxisItem()}); self.plot_widget = pg.PlotWidget(plotItem=self.plot_item_price)
        self.indicator_plot_item = pg.PlotItem(axisItems={'bottom': pg.DateAxisItem()}); self.indicator_widget = pg.PlotWidget(plotItem=self.indicator_plot_item); self.indicator_plot_item.setXLink(self.plot_item_price)
        self.layout.addLayout(top_layout); self.layout.addWidget(self.plot_widget, 3); self.layout.addWidget(self.indicator_widget, 1)
        self.data_frame = None; self.current_indicator_name = ""; self.pair_name = ""
        self.candlestick_item = CandlestickItem(); self.plot_widget.addItem(self.candlestick_item)
        pen = pg.mkPen(color=(100, 100, 100), style=Qt.PenStyle.DashLine); self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pen); self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pen); self.v_line_indicator = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.plot_widget.addItem(self.v_line, ignoreBounds=True); self.plot_widget.addItem(self.h_line, ignoreBounds=True); self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)

        # POPRAWKA: Przypisujemy obiekty SignalProxy do atrybutów self, aby nie zostały usunięte przez garbage collector
        self.price_mouse_proxy = pg.SignalProxy(self.plot_widget.scene().sigMouseMoved, rateLimit=60, slot=self.mouse_moved)
        self.indicator_mouse_proxy = pg.SignalProxy(self.indicator_widget.scene().sigMouseMoved, rateLimit=60, slot=self.mouse_moved)

        self.plot_widget.scene().installEventFilter(self); self.indicator_widget.scene().installEventFilter(self)
        self.measure_line = pg.PlotDataItem(pen=pg.mkPen(color='blue', style=Qt.PenStyle.DashLine, width=2)); self.measure_text = pg.TextItem(anchor=(0, 1), color=(0,0,200), fill=(255, 255, 255, 180))
        self.plot_widget.addItem(self.measure_line); self.plot_widget.addItem(self.measure_text); self.measure_text.setVisible(False)
        self.plot_item_price.sigMeasureStart.connect(self.measure_start); self.plot_item_price.sigMeasureUpdate.connect(self.measure_update); self.plot_item_price.sigMeasureEnd.connect(self.measure_end)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Leave: self.mouse_left()
        return super().eventFilter(obj, event)
    def mouseDoubleClickEvent(self, event): self.sigDoubleClicked.emit(); super().mouseDoubleClickEvent(event)
    def mouse_moved(self, event):
        pos = event[0]
        if self.plot_widget.sceneBoundingRect().contains(pos) or self.indicator_widget.sceneBoundingRect().contains(pos):
            mouse_point = self.plot_widget.getPlotItem().vb.mapSceneToView(pos)
            self.h_line.setPos(mouse_point.y()); self.h_line.show()
            self.mouse_moved_signal.emit(mouse_point.x())
            date_str = datetime.datetime.fromtimestamp(mouse_point.x()).strftime('%Y-%m-%d %H:%M') if mouse_point.x() > 0 else "N/A"
            self.chart_title_label.setText(f"<b>{self.pair_name}</b> | Data: {date_str} | Cena: {mouse_point.y():.4f}")
    def mouse_left(self): self.h_line.hide(); self.v_line.hide(); self.v_line_indicator.hide(); self.chart_title_label.setText(f"<b>{self.pair_name}</b>")
    def update_v_line(self, x_pos): self.v_line.setPos(x_pos); self.v_line.show(); self.v_line_indicator.setPos(x_pos); self.v_line_indicator.show()
    def measure_start(self, pos): self.start_measure_pos = pos; self.measure_text.setText(""); self.measure_text.setVisible(True)
    def measure_update(self, pos):
        if not hasattr(self, 'start_measure_pos'): return
        self.measure_line.setData([self.start_measure_pos.x(), pos.x()], [self.start_measure_pos.y(), pos.y()])
        dx = pos.x() - self.start_measure_pos.x(); dy = pos.y() - self.start_measure_pos.y()
        percent_change = (dy / self.start_measure_pos.y()) * 100 if self.start_measure_pos.y() != 0 else 0
        text = f"Δ Cena: {dy:,.4f} ({percent_change:.2f}%)\nCzas: {datetime.timedelta(seconds=int(dx))}"
        self.measure_text.setText(text); self.measure_text.setPos(pos)
    def measure_end(self, pos): self.start_measure_pos = None; self.measure_line.setData([], []); self.measure_text.setVisible(False)
    def update_chart_and_indicator(self, df, indicator_name, pair_name):
        self.data_frame = df; self.current_indicator_name = indicator_name; self.pair_name = pair_name
        self.chart_title_label.setText(f"<b>{self.pair_name}</b>")
        if not df.empty:
            candlestick_data = [{'x': d.timestamp(), 'open': r['open'], 'high': r['high'], 'low': r['low'], 'close': r['close']} for d, r in df.iterrows()]
            self.candlestick_item.setData(candlestick_data)
        else: self.candlestick_item.setData([])
        self.redraw_indicator(); self.plot_widget.autoRange(); self.indicator_widget.autoRange()
    def redraw_indicator(self):
        self.indicator_widget.clear(); self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)
        if self.data_frame is None or self.data_frame.empty: return
        timestamps = self.data_frame.index.astype('int64') // 10**9
        if self.current_indicator_name == "Williams %R":
            wpr_col = next((c for c in self.data_frame if c.startswith('WILLR_')), None); ema_col = next((c for c in self.data_frame if c.startswith('WPR_EMA_')), None)
            if wpr_col: self.indicator_widget.plot(x=timestamps, y=self.data_frame[wpr_col], pen='b', name="W%R")
            if ema_col: self.indicator_widget.plot(x=timestamps, y=self.data_frame[ema_col], pen=pg.mkPen('orange', width=2), name="EMA on W%R")
            self.indicator_widget.addLine(y=-20, pen=pg.mkPen('r', style=Qt.PenStyle.DashLine)); self.indicator_widget.addLine(y=-80, pen=pg.mkPen('g', style=Qt.PenStyle.DashLine))
        elif self.current_indicator_name == "RSI":
            rsi_col = next((c for c in self.data_frame if c.startswith('RSI_')), None)
            if rsi_col: self.indicator_widget.plot(x=timestamps, y=self.data_frame[rsi_col], pen='g', name="RSI"); self.indicator_widget.addLine(y=70, pen=pg.mkPen('r', style=Qt.PenStyle.DashLine)); self.indicator_widget.addLine(y=30, pen=pg.mkPen('g', style=Qt.PenStyle.DashLine))
        elif self.current_indicator_name == "MACD":
            macd_col, macdh_col, macds_col = [next((c for c in self.data_frame if c.startswith(prefix)), None) for prefix in ['MACD_', 'MACDh_', 'MACDs_']]
            if all([macd_col, macdh_col, macds_col]):
                self.indicator_widget.plot(x=timestamps, y=self.data_frame[macd_col], pen='b', name='MACD'); self.indicator_widget.plot(x=timestamps, y=self.data_frame[macds_col], pen='r', name='Signal')
                brushes = ['g' if v > 0 else 'r' for v in self.data_frame[macdh_col]]; width = 0.8 * (timestamps[1] - timestamps[0] if len(timestamps) > 1 else 1)
                self.indicator_widget.addItem(pg.BarGraphItem(x=timestamps, height=self.data_frame[macdh_col], width=width, brushes=brushes))
        self.indicator_widget.autoRange()

class ChartWindow(QMainWindow):
    def __init__(self, exchange_options, config_path, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options; self.config_path = config_path
        self.setWindowTitle("Analiza Wykresów"); self.setGeometry(150, 150, 1400, 800)
        self.maximized_chart = None; self.current_pair = ""; self.is_loading = False
        self.refresh_timer = QTimer(self); self.refresh_timer.timeout.connect(self.trigger_chart_updates)
        self.setup_ui(); self.fetch_markets_thread = None; self.chart_data_threads = {}
        self.load_settings()

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    def get_exchange(self):
        selected_config = self.exchange_options.get(self.chart_exchange_combo.currentText())
        if not selected_config: return None
        config = configparser.ConfigParser(); config.read(self.config_path)
        section = selected_config.get("config_section")
        api_key, api_secret = (config.get(section, 'api_key', fallback=''), config.get(section, 'api_secret', fallback='')) if section and config.has_section(section) else ('', '')
        try:
            return getattr(ccxt, selected_config["id_ccxt"])({'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True, 'options': {'defaultType': selected_config["type"]}, 'timeout': 30000})
        except Exception as e: QMessageBox.critical(self, "Błąd Giełdy", f"Nie można zainicjalizować {selected_config['id_ccxt']}: {e}"); return None
    def get_indicator_params(self):
        name = self.global_indicator_combo.currentText(); params = {}
        if name == "Williams %R": params.update({'wpr_period': self.wpr_period_spin.value(), 'ema_period': self.ema_period_spin.value()})
        elif name == "RSI": params['rsi_period'] = self.rsi_period_spin.value()
        elif name == "MACD": params.update({'fast': self.macd_fast_spin.value(), 'slow': self.macd_slow_spin.value(), 'signal': self.macd_signal_spin.value()})
        return params
    def setup_ui(self):
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget); self.main_layout = QHBoxLayout(self.central_widget)
        self.setup_sidebar(); self.setup_charts_grid()
    def setup_sidebar(self):
        sidebar_scroll = QScrollArea(); sidebar_scroll.setWidgetResizable(True); sidebar_scroll.setFixedWidth(320)
        sidebar_widget = QWidget(); self.sidebar_layout = QVBoxLayout(sidebar_widget)
        self.sidebar_layout.addWidget(QLabel("<b>Panel Kontrolny</b>"))
        self.sidebar_layout.addWidget(QLabel("Giełda:")); self.chart_exchange_combo = QComboBox(); self.chart_exchange_combo.addItems(self.exchange_options.keys()); self.chart_exchange_combo.currentTextChanged.connect(self.on_exchange_changed); self.sidebar_layout.addWidget(self.chart_exchange_combo)

        indicator_group = QGroupBox("Globalny Wskaźnik"); indicator_layout = QFormLayout(indicator_group)
        self.global_indicator_combo = QComboBox(); self.global_indicator_combo.addItems(["Williams %R", "RSI", "MACD"]); indicator_layout.addRow("Wskaźnik:", self.global_indicator_combo)
        self.params_stacked_widget = QStackedWidget()
        wpr_widget = QWidget(); wpr_layout = QFormLayout(wpr_widget); self.wpr_period_spin = QSpinBox(); self.ema_period_spin = QSpinBox(); wpr_layout.addRow("Okres W%R:", self.wpr_period_spin); wpr_layout.addRow("Okres EMA:", self.ema_period_spin); self.params_stacked_widget.addWidget(wpr_widget)
        rsi_widget = QWidget(); rsi_layout = QFormLayout(rsi_widget); self.rsi_period_spin = QSpinBox(); rsi_layout.addRow("Okres RSI:", self.rsi_period_spin); self.params_stacked_widget.addWidget(rsi_widget)
        macd_widget = QWidget(); macd_layout = QFormLayout(macd_widget); self.macd_fast_spin = QSpinBox(); self.macd_slow_spin = QSpinBox(); self.macd_signal_spin = QSpinBox(); macd_layout.addRow("Fast:", self.macd_fast_spin); macd_layout.addRow("Slow:", self.macd_slow_spin); macd_layout.addRow("Signal:", self.macd_signal_spin); self.params_stacked_widget.addWidget(macd_widget)
        for s in [self.wpr_period_spin, self.ema_period_spin, self.rsi_period_spin, self.macd_fast_spin, self.macd_slow_spin, self.macd_signal_spin]: s.setRange(1, 200)
        indicator_layout.addRow(self.params_stacked_widget); self.sidebar_layout.addWidget(indicator_group)

        pairs_group = QGroupBox("Zarządzanie Listą Par"); pairs_layout = QVBoxLayout(pairs_group)
        available_group = QGroupBox("Dostępne Pary:"); available_layout = QVBoxLayout(available_group); self.available_pairs_list_widget = QListWidget(); self.available_pairs_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); available_layout.addWidget(self.available_pairs_list_widget)
        buttons_layout = QGridLayout(); self.add_pair_button = QPushButton(">"); self.add_all_pairs_button = QPushButton(">>"); self.remove_pair_button = QPushButton("<"); self.remove_all_pairs_button = QPushButton("<<"); buttons_layout.addWidget(self.add_pair_button, 0, 0); buttons_layout.addWidget(self.add_all_pairs_button, 0, 1); buttons_layout.addWidget(self.remove_pair_button, 1, 0); buttons_layout.addWidget(self.remove_all_pairs_button, 1, 1)
        watchlist_group = QGroupBox("Moja lista obserwowanych:"); watchlist_layout = QVBoxLayout(watchlist_group); self.watchlist_widget = QListWidget(); watchlist_layout.addWidget(self.watchlist_widget)
        pairs_layout.addWidget(available_group); pairs_layout.addLayout(buttons_layout); pairs_layout.addWidget(watchlist_group); self.refresh_pairs_button = QPushButton("Odśwież Dostępne Pary"); pairs_layout.addWidget(self.refresh_pairs_button); self.sidebar_layout.addWidget(pairs_group)

        refresh_group = QGroupBox("Automatyczne odświeżanie"); refresh_layout = QFormLayout(refresh_group)
        self.auto_refresh_checkbox = QCheckBox("Włącz"); self.refresh_interval_spinbox = QSpinBox(); self.refresh_interval_spinbox.setRange(1, 120); self.refresh_interval_spinbox.setSuffix(" min"); refresh_layout.addRow(self.auto_refresh_checkbox); refresh_layout.addRow("Interwał:", self.refresh_interval_spinbox); self.sidebar_layout.addWidget(refresh_group)

        self.load_charts_button = QPushButton("Wczytaj Wykresy"); self.sidebar_layout.addWidget(self.load_charts_button); self.sidebar_layout.addStretch()
        sidebar_scroll.setWidget(sidebar_widget); self.main_layout.addWidget(sidebar_scroll)

        self.auto_refresh_checkbox.stateChanged.connect(self.toggle_auto_refresh); self.refresh_interval_spinbox.valueChanged.connect(self.update_refresh_interval); self.load_charts_button.clicked.connect(self.trigger_chart_updates); self.global_indicator_combo.currentTextChanged.connect(self.on_global_indicator_changed); self.refresh_pairs_button.clicked.connect(self.trigger_fetch_markets)
        for spinbox in [self.wpr_period_spin, self.ema_period_spin, self.rsi_period_spin, self.macd_fast_spin, self.macd_slow_spin, self.macd_signal_spin]: spinbox.valueChanged.connect(self.save_settings)
        self.global_indicator_combo.currentTextChanged.connect(self.save_settings)
        self.add_pair_button.clicked.connect(self.add_to_watchlist); self.remove_pair_button.clicked.connect(self.remove_from_watchlist); self.add_all_pairs_button.clicked.connect(self.add_all_to_watchlist); self.remove_all_pairs_button.clicked.connect(self.remove_all_from_watchlist)
    def setup_charts_grid(self):
        self.charts_area_widget = QWidget(); self.charts_grid_layout = QGridLayout(self.charts_area_widget)
        self.charts = []
        for idx in range(6):
            chart = SingleChartWidget(chart_id=idx); chart.timeframe_combo.currentTextChanged.connect(self.save_settings)
            chart.mouse_moved_signal.connect(self.sync_crosshairs); chart.sigDoubleClicked.connect(lambda cw=chart: self.toggle_maximize_chart(cw))
            self.charts_grid_layout.addWidget(chart, idx // 3, idx % 3); self.charts.append(chart)
        self.main_layout.addWidget(self.charts_area_widget, 1)

    def load_settings(self):
        config = configparser.ConfigParser(); config.read(self.config_path)
        if not config.has_section('chart_indicator_settings'):
            self.trigger_fetch_markets()
            return

        s = config['chart_indicator_settings']

        self.chart_exchange_combo.blockSignals(True)
        self.global_indicator_combo.blockSignals(True)

        self.global_indicator_combo.setCurrentText(s.get('indicator_type', 'Williams %R').replace('%%', '%'))
        self.wpr_period_spin.setValue(s.getint('wpr_period', DEFAULT_WPR_LENGTH)); self.ema_period_spin.setValue(s.getint('ema_period', DEFAULT_EMA_WPR_LENGTH)); self.rsi_period_spin.setValue(s.getint('rsi_period', DEFAULT_RSI_LENGTH)); self.macd_fast_spin.setValue(s.getint('macd_fast', DEFAULT_MACD_FAST)); self.macd_slow_spin.setValue(s.getint('macd_slow', DEFAULT_MACD_SLOW)); self.macd_signal_spin.setValue(s.getint('macd_signal', DEFAULT_MACD_SIGNAL))
        self.chart_exchange_combo.setCurrentText(s.get('selected_exchange', self.chart_exchange_combo.itemText(0)))

        self.chart_exchange_combo.blockSignals(False)
        self.global_indicator_combo.blockSignals(False)

        section_name = self.exchange_options.get(self.chart_exchange_combo.currentText(), {}).get("config_section")
        if section_name and config.has_section(section_name):
            self.watchlist_widget.clear(); pairs_str = config.get(section_name, 'cw_watchlist_pairs', fallback=''); self.watchlist_widget.addItems([p.strip() for p in pairs_str.split(',') if p.strip()])
        if config.has_section('chart_settings'):
            s_chart = config['chart_settings']; [c.timeframe_combo.setCurrentText(s_chart.get(f'chart_{i}_timeframe', '1h')) for i, c in enumerate(self.charts)]

        self.trigger_fetch_markets()

    def save_settings(self, _=None):
        config = configparser.ConfigParser(); config.read(self.config_path)
        if not config.has_section('chart_indicator_settings'): config.add_section('chart_indicator_settings')
        s = config['chart_indicator_settings']; s['indicator_type'] = self.global_indicator_combo.currentText().replace('%', '%%'); s['wpr_period'] = str(self.wpr_period_spin.value()); s['ema_period'] = str(self.ema_period_spin.value()); s['rsi_period'] = str(self.rsi_period_spin.value()); s['macd_fast'] = str(self.macd_fast_spin.value()); s['macd_slow'] = str(self.macd_slow_spin.value()); s['macd_signal'] = str(self.macd_signal_spin.value())
        s['selected_exchange'] = self.chart_exchange_combo.currentText()

        section_name = self.exchange_options.get(self.chart_exchange_combo.currentText(), {}).get("config_section")
        if section_name:
            if not config.has_section(section_name): config.add_section(section_name)
            config.set(section_name, 'cw_watchlist_pairs', ",".join([self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())]))

        if not config.has_section('chart_settings'): config.add_section('chart_settings')
        [config.set('chart_settings', f'chart_{i}_timeframe', c.timeframe_combo.currentText()) for i, c in enumerate(self.charts)]
        with open(self.config_path, 'w') as f: config.write(f)

    def on_exchange_changed(self, exchange_name):
        self.watchlist_widget.clear(); self.available_pairs_list_widget.clear()
        self.trigger_fetch_markets()
        self.save_settings()

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return
        self.chart_exchange_combo.setEnabled(False); self.refresh_pairs_button.setEnabled(False); self.refresh_pairs_button.setText("Pobieranie...")
        config = self.exchange_options.get(self.chart_exchange_combo.currentText())
        if not config: self.on_fetch_markets_finished(); return
        self.fetch_markets_thread = utils.FetchMarketsThread(config["id_ccxt"], config["type"], self)
        self.fetch_markets_thread.markets_fetched.connect(self.populate_available_pairs)
        self.fetch_markets_thread.error_occurred.connect(lambda msg: QMessageBox.critical(self, "Błąd API", msg))
        self.fetch_markets_thread.finished.connect(self.on_fetch_markets_finished)
        self.fetch_markets_thread.start()

    def on_fetch_markets_finished(self):
        self.chart_exchange_combo.setEnabled(True); self.refresh_pairs_button.setEnabled(True); self.refresh_pairs_button.setText("Odśwież Dostępne Pary")

    def toggle_auto_refresh(self, state):
        if Qt.CheckState(state) == Qt.CheckState.Checked: self.update_refresh_interval(); self.refresh_timer.start(); self.load_charts_button.setEnabled(False); self.trigger_chart_updates()
        else: self.refresh_timer.stop(); [t.exit() for t in self.chart_data_threads.values() if t.isRunning()]; self.chart_data_threads.clear(); self.load_charts_button.setEnabled(True)
    def update_refresh_interval(self): self.refresh_timer.setInterval(self.refresh_interval_spinbox.value() * 60 * 1000)
    def toggle_maximize_chart(self, chart):
        if self.maximized_chart is None: self.maximized_chart = chart; [c.hide() for c in self.charts if c is not self.maximized_chart]
        else: [c.show() for c in self.charts]; self.maximized_chart = None
    def sync_crosshairs(self, x_pos): [c.update_v_line(x_pos) for c in self.charts]
    def on_global_indicator_changed(self, name=None): name = name or self.global_indicator_combo.currentText(); self.params_stacked_widget.setCurrentIndex({"Williams %R": 0, "RSI": 1, "MACD": 2}.get(name, 0))
    def trigger_chart_updates(self):
        if self.is_loading: return
        current_item = self.watchlist_widget.currentItem()
        if not current_item:
            if not self.auto_refresh_checkbox.isChecked(): QMessageBox.warning(self, "Brak wyboru", "Wybierz parę z listy."); return
        self.is_loading = True; self.current_pair = current_item.text() if current_item else self.current_pair
        if not self.current_pair: self.is_loading = False; return
        if not self.auto_refresh_checkbox.isChecked(): self.load_charts_button.setEnabled(False); self.load_charts_button.setText(f"Wczytywanie {self.current_pair}...")
        [t.exit() for t in self.chart_data_threads.values() if t.isRunning()]; self.chart_data_threads.clear()
        [QTimer.singleShot(i * 250, lambda cw=c: self.start_single_fetch_thread(cw)) for i, c in enumerate(self.charts)]
    def on_chart_data_thread_finished(self, chart_widget=None):
        if chart_widget and chart_widget.chart_id in self.chart_data_threads: del self.chart_data_threads[chart_widget.chart_id]
        if not self.chart_data_threads: self.is_loading = False; self.load_charts_button.setEnabled(True); self.load_charts_button.setText("Wczytaj Wykresy")
    def start_single_fetch_thread(self, chart):
        exchange = self.get_exchange()
        if not exchange: self.on_chart_data_thread_finished(chart); return
        thread = FetchChartDataThread(exchange, self.current_pair, chart.timeframe_combo.currentText(), self.global_indicator_combo.currentText(), self.get_indicator_params(), chart, self)
        thread.data_ready_signal.connect(lambda df, cw, p=self.current_pair, ind=self.global_indicator_combo.currentText(): cw.update_chart_and_indicator(df, ind, p))
        thread.error_signal.connect(lambda msg, cw=chart: cw.chart_title_label.setText(f"<font color='red'>Błąd: {msg[:80]}</font>"))
        thread.finished_signal.connect(self.on_chart_data_thread_finished)
        self.chart_data_threads[chart.chart_id] = thread; thread.start()
    def populate_available_pairs(self, pairs):
        watchlist = {self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())}
        self.available_pairs_list_widget.clear()
        [self.available_pairs_list_widget.addItem(p['symbol']) for p in pairs if p['symbol'] not in watchlist]
    def add_to_watchlist(self): [self.watchlist_widget.addItem(i.text()) for i in self.available_pairs_list_widget.selectedItems()]; [self.available_pairs_list_widget.takeItem(self.available_pairs_list_widget.row(i)) for i in self.available_pairs_list_widget.selectedItems()]; self.save_settings()
    def remove_from_watchlist(self): [self.available_pairs_list_widget.addItem(i.text()) for i in self.watchlist_widget.selectedItems()]; [self.watchlist_widget.takeItem(self.watchlist_widget.row(i)) for i in self.watchlist_widget.selectedItems()]; self.available_pairs_list_widget.sortItems(); self.save_settings()
    def add_all_to_watchlist(self): self.watchlist_widget.addItems([self.available_pairs_list_widget.item(i).text() for i in range(self.available_pairs_list_widget.count())]); self.available_pairs_list_widget.clear(); self.save_settings()
    def remove_all_from_watchlist(self): self.available_pairs_list_widget.addItems([self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())]); self.watchlist_widget.clear(); self.available_pairs_list_widget.sortItems(); self.save_settings()
