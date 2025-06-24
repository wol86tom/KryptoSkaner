# Ostateczna, stabilna wersja pliku chart_window.py
import sys
import os
import configparser
import datetime
import time
import ccxt
import pandas as pd
import pandas_ta as ta
import pyqtgraph as pg
import numpy as np
from PySide6.QtWidgets import (QMainWindow, QVBoxLayout, QWidget,
                             QComboBox, QGridLayout, QLabel, QListWidget,
                             QPushButton, QHBoxLayout, QGroupBox, QApplication,
                             QMessageBox, QListWidgetItem, QFormLayout,
                             QSpinBox, QStackedWidget, QCheckBox, QScrollArea)
from PySide6.QtCore import QThread, Signal, Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QPainter, QPen

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')

DEFAULT_WPR_LENGTH = 14
DEFAULT_EMA_WPR_LENGTH = 9
DEFAULT_RSI_LENGTH = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_REFRESH_MINUTES = 5

# --- STABILNA, POPRAWIONA KLASA DO RYSOWANIA ŚWIEC ---
class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data):
        pg.GraphicsObject.__init__(self)
        self.data = data  # data = list of dicts with 'x', 'o', 'h', 'l', 'c'
        self.generatePicture()

    def generatePicture(self):
        self.picture = pg.QtGui.QPicture()
        p = pg.QtGui.QPainter(self.picture)

        if len(self.data) > 1:
            # Poprawione obliczanie szerokości świecy
            w = (self.data[1]['x'] - self.data[0]['x']) * 0.4
        else:
            w = 1.0

        for d in self.data:
            t, open_val, high_val, low_val, close_val = d['x'], d['open'], d['high'], d['low'], d['close']
            p.setPen(pg.mkPen('k'))
            p.drawLine(QPointF(t, low_val), QPointF(t, high_val))
            if open_val > close_val:
                p.setBrush(pg.mkBrush('r'))
            else:
                p.setBrush(pg.mkBrush('g'))
            p.drawRect(QRectF(t - w, open_val, w * 2, close_val - open_val))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        if not self.data: return QRectF()
        # Poprawione obliczanie granic
        x_min = self.data[0]['x']
        x_max = self.data[-1]['x']
        y_min = min(d['low'] for d in self.data)
        y_max = max(d['high'] for d in self.data)
        return QRectF(x_min, y_min, x_max - x_min, y_max - y_min)


class FetchChartMarketsThread(QThread):
    markets_fetched_signal = Signal(list); error_signal = Signal(str); finished_signal = Signal()
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
        return False

class FetchChartDataThread(QThread):
    data_ready_signal = Signal(object, object); error_signal = Signal(str, object); finished_signal = Signal(object)
    def __init__(self, exchange, pair_symbol, timeframe, indicator_name, indicator_params, chart_widget, parent=None):
        super().__init__(parent)
        self.exchange, self.pair_symbol, self.timeframe = exchange, pair_symbol, timeframe
        self.indicator_name, self.indicator_params = indicator_name, indicator_params
        self.chart_widget = chart_widget
    def run(self):
        try:
            ohlcv = self.exchange.fetch_ohlcv(self.pair_symbol, timeframe=self.timeframe, limit=300)
            if not ohlcv:
                raise ccxt.NetworkError(f"Giełda nie zwróciła danych dla {self.pair_symbol} na {self.timeframe}.")

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms'); df.set_index('timestamp', inplace=True)

            if self.indicator_name == "Williams %R":
                wpr_p, ema_p = self.indicator_params.get('wpr_period'), self.indicator_params.get('ema_period')
                wpr_col = f'WILLR_{wpr_p}'; df.ta.willr(length=wpr_p, append=True)
                if wpr_col in df and df[wpr_col].notna().any(): df[f'WPR_EMA_{ema_p}'] = ta.ema(df[wpr_col], length=ema_p)
            elif self.indicator_name == "RSI": df.ta.rsi(length=self.indicator_params.get('rsi_period'), append=True)
            elif self.indicator_name == "MACD":
                fast_p, slow_p, signal_p = self.indicator_params.get('fast'), self.indicator_params.get('slow'), self.indicator_params.get('signal')
                df.ta.macd(fast=fast_p, slow=slow_p, signal=signal_p, append=True)

            self.data_ready_signal.emit(df, self.chart_widget)
        except Exception as e:
            error_message = f"Błąd w wątku dla {self.pair_symbol} ({self.timeframe}): {str(e)}"
            self.error_signal.emit(error_message, self.chart_widget)
        finally:
            self.finished_signal.emit(self)

class MeasurablePlotItem(pg.PlotItem):
    sigMeasureStart = Signal(object); sigMeasureUpdate = Signal(object); sigMeasureEnd = Signal(object)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.measuring = False
    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton and ev.modifiers() == Qt.ControlModifier:
            ev.accept(); self.measuring = True; self.sigMeasureStart.emit(self.vb.mapSceneToView(ev.pos()))
        else: super().mousePressEvent(ev)
    def mouseMoveEvent(self, ev):
        if self.measuring: self.sigMeasureUpdate.emit(self.vb.mapSceneToView(ev.pos()))
        super().mouseMoveEvent(ev)
    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.RightButton and self.measuring:
            ev.accept(); self.measuring = False; self.sigMeasureEnd.emit(self.vb.mapSceneToView(ev.pos()))
        else: super().mouseReleaseEvent(ev)

class SingleChartWidget(QWidget):
    mouse_moved_signal = Signal(float); sigDoubleClicked = Signal()
    def __init__(self, chart_id, parent=None):
        super().__init__(parent); self.chart_id = chart_id
        self.layout = QVBoxLayout(self); self.layout.setContentsMargins(2, 2, 2, 2); self.layout.setSpacing(2)
        controls_layout = QHBoxLayout(); self.timeframe_combo = QComboBox(); self.timeframe_combo.addItems(['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w'])
        controls_layout.addWidget(QLabel(f"Wykres {self.chart_id + 1} - Interwał:")); controls_layout.addWidget(self.timeframe_combo); controls_layout.addStretch()

        self.plot_item_price = MeasurablePlotItem(axisItems={'bottom': pg.DateAxisItem()})
        self.plot_widget = pg.PlotWidget(plotItem=self.plot_item_price)
        self.indicator_plot_item = pg.PlotItem(axisItems={'bottom': pg.DateAxisItem()})
        self.indicator_widget = pg.PlotWidget(plotItem=self.indicator_plot_item)
        self.indicator_plot_item.setXLink(self.plot_item_price)

        self.layout.addLayout(controls_layout); self.layout.addWidget(self.plot_widget); self.layout.addWidget(self.indicator_widget)
        self.layout.setStretchFactor(self.plot_widget, 3); self.layout.setStretchFactor(self.indicator_widget, 1)
        self.data_frame = None; self.current_indicator_name = ""

        pen = pg.mkPen(color=(100, 100, 100), style=Qt.DashLine)
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pen); self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        self.v_line_indicator = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.plot_widget.addItem(self.v_line, ignoreBounds=True); self.plot_widget.addItem(self.h_line, ignoreBounds=True)
        self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)

        self.price_mouse_proxy = pg.SignalProxy(self.plot_widget.scene().sigMouseMoved, rateLimit=60, slot=self.mouse_moved)
        self.indicator_mouse_proxy = pg.SignalProxy(self.indicator_widget.scene().sigMouseMoved, rateLimit=60, slot=self.mouse_moved)

        self.measure_line = pg.PlotDataItem(pen=pg.mkPen(color='blue', style=Qt.DashLine, width=2))
        self.measure_text = pg.TextItem(anchor=(0, 1), color=(0,0,200), fill=(255, 255, 255, 180))
        self.plot_widget.addItem(self.measure_line); self.plot_widget.addItem(self.measure_text); self.measure_text.setVisible(False)
        self.plot_item_price.sigMeasureStart.connect(self.measure_start); self.plot_item_price.sigMeasureUpdate.connect(self.measure_update); self.plot_item_price.sigMeasureEnd.connect(self.measure_end)
    def mouseDoubleClickEvent(self, event): self.sigDoubleClicked.emit(); super().mouseDoubleClickEvent(event)
    def mouse_moved(self, event):
        pos = event[0]
        if self.plot_widget.sceneBoundingRect().contains(pos):
            mouse_point = self.plot_widget.getPlotItem().vb.mapSceneToView(pos)
            self.h_line.setPos(mouse_point.y()); self.h_line.show()
            self.mouse_moved_signal.emit(mouse_point.x())
        elif self.indicator_widget.sceneBoundingRect().contains(pos):
            mouse_point = self.indicator_widget.getPlotItem().vb.mapSceneToView(pos)
            self.h_line.hide(); self.mouse_moved_signal.emit(mouse_point.x())
    def update_v_line(self, x_pos):
        self.v_line.setPos(x_pos); self.v_line_indicator.setPos(x_pos)
    def get_snapped_pos(self, pos):
        if self.data_frame is None or self.data_frame.empty: return pos
        try:
            nearest_idx = self.data_frame.index.get_indexer([pd.to_datetime(pos.x(), unit='s')], method='nearest')[0]
            nearest_candle = self.data_frame.iloc[nearest_idx]
        except (IndexError, KeyError): return pos
        snapped_x = nearest_candle.name.timestamp(); mouse_y, candle_high, candle_low = pos.y(), nearest_candle['high'], nearest_candle['low']
        visible_y_range = self.plot_widget.getPlotItem().vb.viewRange()[1]; snap_threshold = (visible_y_range[1] - visible_y_range[0]) * 0.05
        dist_to_high, dist_to_low = abs(mouse_y - candle_high), abs(mouse_y - candle_low); snapped_y = mouse_y
        if dist_to_high < snap_threshold and dist_to_high < dist_to_low: snapped_y = candle_high
        elif dist_to_low < snap_threshold: snapped_y = candle_low
        return QPointF(snapped_x, snapped_y)
    def measure_start(self, pos):
        snapped_pos = self.get_snapped_pos(pos); self.start_measure_pos = snapped_pos
        self.measure_text.setText(""); self.measure_text.setVisible(True)
    def measure_update(self, pos):
        if not hasattr(self, 'start_measure_pos') or self.start_measure_pos is None: return
        snapped_pos = self.get_snapped_pos(pos)
        self.measure_line.setData([self.start_measure_pos.x(), snapped_pos.x()], [self.start_measure_pos.y(), snapped_pos.y()])
        dx = snapped_pos.x() - self.start_measure_pos.x(); dy = snapped_pos.y() - self.start_measure_pos.y()
        percent_change = (dy / self.start_measure_pos.y()) * 100 if self.start_measure_pos.y() != 0 else 0
        time_diff = datetime.timedelta(seconds=int(dx)); bar_count = 0
        if self.data_frame is not None and not self.data_frame.empty:
            try:
                start_idx = self.data_frame.index.get_indexer([pd.to_datetime(self.start_measure_pos.x(), unit='s')], method='nearest')[0]
                end_idx = self.data_frame.index.get_indexer([pd.to_datetime(snapped_pos.x(), unit='s')], method='nearest')[0]
                bar_count = abs(end_idx - start_idx)
            except Exception: pass
        text = f"Δ Cena: {dy:,.4f}\nΔ Procent: {percent_change:.2f}%\nCzas: {str(time_diff)}\nŚwiece: {bar_count}"
        self.measure_text.setText(text); self.measure_text.setPos(snapped_pos)
    def measure_end(self, pos):
        self.start_measure_pos = None; self.measure_line.setData([], []); self.measure_text.setVisible(False)
    def update_chart_and_indicator(self, df, indicator_name):
        self.data_frame = df; self.current_indicator_name = indicator_name
        self.plot_widget.clear(); self.indicator_widget.clear(); self.plot_widget.setTitle("")
        self.plot_widget.addItem(self.v_line, ignoreBounds=True); self.plot_widget.addItem(self.h_line, ignoreBounds=True)
        self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)
        self.plot_widget.addItem(self.measure_line); self.plot_widget.addItem(self.measure_text)

        if not df.empty:
            candlestick_data = []
            for d_idx, row in df.iterrows():
                # Przekształcanie timestampu na sekundy
                timestamp = d_idx.timestamp()
                candlestick_data.append({'x': timestamp, 'open': row['open'], 'high': row['high'], 'low': row['low'], 'close': row['close']})

            item = CandlestickItem(candlestick_data)
            self.plot_widget.addItem(item)

        self.redraw_indicator(); self.plot_widget.autoRange(); self.indicator_widget.autoRange()
    def redraw_indicator(self):
        self.indicator_widget.clear(); self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)
        if self.data_frame is None or self.data_frame.empty: return
        indicator_name = self.current_indicator_name; timestamps = self.data_frame.index.astype('int64') // 10**9
        if indicator_name == "Williams %R":
            wpr_col, ema_col = next((c for c in self.data_frame if c.startswith('WILLR_')), None), next((c for c in self.data_frame if c.startswith('WPR_EMA_')), None)
            if wpr_col is not None: self.indicator_widget.plot(x=timestamps, y=self.data_frame[wpr_col], pen='b', name="W%R")
            if ema_col is not None: self.indicator_widget.plot(x=timestamps, y=self.data_frame[ema_col], pen=pg.mkPen('orange', width=2), name="EMA on W%R")
            self.indicator_widget.addLine(y=-20, pen=pg.mkPen('r', style=Qt.DashLine)); self.indicator_widget.addLine(y=-80, pen=pg.mkPen('g', style=Qt.DashLine))
        elif indicator_name == "RSI":
            rsi_col = next((c for c in self.data_frame if c.startswith('RSI_')), None)
            if rsi_col is not None: self.indicator_widget.plot(x=timestamps, y=self.data_frame[rsi_col], pen='g', name="RSI"); self.indicator_widget.addLine(y=70, pen=pg.mkPen('r', style=Qt.DashLine)); self.indicator_widget.addLine(y=30, pen=pg.mkPen('g', style=Qt.DashLine))
        elif indicator_name == "MACD":
            macd_col, macdh_col, macds_col = next((c for c in self.data_frame if c.startswith('MACD_')), None), next((c for c in self.data_frame if c.startswith('MACDh_')), None), next((c for c in self.data_frame if c.startswith('MACDs_')), None)
            if all([macd_col, macdh_col, macds_col]):
                self.indicator_widget.plot(x=timestamps, y=self.data_frame[macd_col], pen='b', name='MACD'); self.indicator_widget.plot(x=timestamps, y=self.data_frame[macds_col], pen='r', name='Signal')
                brushes = ['g' if v > 0 else 'r' for v in self.data_frame[macdh_col]]; width = 0.8 * (timestamps[1] - timestamps[0] if len(timestamps) > 1 else 1)
                self.indicator_widget.addItem(pg.BarGraphItem(x=timestamps, height=self.data_frame[macdh_col], width=width, brushes=brushes))

class MultiChartWindow(QMainWindow):
    def __init__(self, exchange_options, config_path, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options; self.config_path = config_path
        self.setWindowTitle("Okno Analizy Wykresów"); self.setGeometry(150, 150, 1400, 800)
        self.maximized_chart = None; self.current_pair = ""
        self.is_loading = False

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.trigger_chart_updates)

        self.setup_ui()
        self.fetch_markets_thread = None; self.chart_data_threads = {}
        self.load_settings(); self.on_global_indicator_changed()
        self.trigger_fetch_markets()
    def setup_ui(self):
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget); self.main_layout = QHBoxLayout(self.central_widget)
        self.setup_sidebar(); self.setup_charts_grid()
    def setup_sidebar(self):
        self.sidebar_scroll_area = QScrollArea()
        self.sidebar_scroll_area.setWidgetResizable(True)
        self.sidebar_scroll_area.setFixedWidth(320)

        self.sidebar_widget = QWidget()
        self.sidebar_layout = QVBoxLayout(self.sidebar_widget)

        self.sidebar_layout.addWidget(QLabel("<b>Panel Kontrolny</b>")); self.sidebar_layout.addWidget(QLabel("Giełda:")); self.chart_exchange_combo = QComboBox()
        if self.exchange_options: self.chart_exchange_combo.addItems(self.exchange_options.keys())
        self.chart_exchange_combo.currentTextChanged.connect(self.on_exchange_changed); self.sidebar_layout.addWidget(self.chart_exchange_combo)

        indicator_group = QGroupBox("Globalny Wskaźnik"); indicator_layout = QFormLayout(); self.global_indicator_combo = QComboBox(); self.global_indicator_combo.addItems(["Williams %R", "RSI", "MACD"]); indicator_layout.addRow("Wskaźnik:", self.global_indicator_combo)
        self.params_stacked_widget = QStackedWidget(); wpr_params_widget = QWidget(); wpr_layout = QFormLayout(wpr_params_widget); wpr_layout.setContentsMargins(0,0,0,0)
        self.wpr_period_spin = QSpinBox(); self.wpr_period_spin.setRange(1,200); wpr_layout.addRow("Okres W%R:", self.wpr_period_spin)
        self.ema_period_spin = QSpinBox(); self.ema_period_spin.setRange(1,200); wpr_layout.addRow("Okres EMA:", self.ema_period_spin)
        self.params_stacked_widget.addWidget(wpr_params_widget); rsi_params_widget = QWidget(); rsi_layout = QFormLayout(rsi_params_widget); rsi_layout.setContentsMargins(0,0,0,0)
        self.rsi_period_spin = QSpinBox(); self.rsi_period_spin.setRange(1,200); rsi_layout.addRow("Okres RSI:", self.rsi_period_spin)
        self.params_stacked_widget.addWidget(rsi_params_widget); macd_params_widget = QWidget(); macd_layout = QFormLayout(macd_params_widget); macd_layout.setContentsMargins(0,0,0,0)
        self.macd_fast_spin = QSpinBox(); self.macd_fast_spin.setRange(1,200); macd_layout.addRow("Okres Fast:", self.macd_fast_spin)
        self.macd_slow_spin = QSpinBox(); self.macd_slow_spin.setRange(1,200); macd_layout.addRow("Okres Slow:", self.macd_slow_spin)
        self.macd_signal_spin = QSpinBox(); self.macd_signal_spin.setRange(1,200); macd_layout.addRow("Okres Signal:", self.macd_signal_spin)
        self.params_stacked_widget.addWidget(macd_params_widget); indicator_layout.addRow(self.params_stacked_widget); indicator_group.setLayout(indicator_layout); self.sidebar_layout.addWidget(indicator_group)

        self.pairs_group = QGroupBox("Zarządzanie Listą Par"); pairs_layout = QVBoxLayout()
        available_pairs_group = QGroupBox("Dostępne Pary:"); available_layout = QVBoxLayout(available_pairs_group)
        self.available_pairs_list_widget = QListWidget(); self.available_pairs_list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        available_layout.addWidget(self.available_pairs_list_widget)
        buttons_layout = QGridLayout(); self.add_pair_button = QPushButton(">"); self.add_pair_button.setToolTip("Dodaj zaznaczone"); buttons_layout.addWidget(self.add_pair_button, 0, 0)
        self.add_all_pairs_button = QPushButton(">>"); self.add_all_pairs_button.setToolTip("Dodaj wszystkie"); buttons_layout.addWidget(self.add_all_pairs_button, 0, 1)
        self.remove_pair_button = QPushButton("<"); self.remove_pair_button.setToolTip("Usuń zaznaczone"); buttons_layout.addWidget(self.remove_pair_button, 1, 0)
        self.remove_all_pairs_button = QPushButton("<<"); self.remove_all_pairs_button.setToolTip("Usuń wszystkie"); buttons_layout.addWidget(self.remove_all_pairs_button, 1, 1)
        watchlist_group = QGroupBox("Moja lista obserwowanych:"); watchlist_layout = QVBoxLayout(watchlist_group)
        self.watchlist_widget = QListWidget(); self.watchlist_widget.setSelectionMode(QListWidget.SingleSelection)
        watchlist_layout.addWidget(self.watchlist_widget)
        pairs_layout.addWidget(available_pairs_group); pairs_layout.addLayout(buttons_layout); pairs_layout.addWidget(watchlist_group)
        self.refresh_pairs_button = QPushButton("Odśwież Dostępne Pary"); self.refresh_pairs_button.clicked.connect(self.trigger_fetch_markets)
        pairs_layout.addWidget(self.refresh_pairs_button); self.pairs_group.setLayout(pairs_layout); self.sidebar_layout.addWidget(self.pairs_group)

        self.sidebar_layout.addStretch()

        refresh_group = QGroupBox("Automatyczne odświeżanie")
        refresh_layout = QFormLayout(refresh_group)
        self.auto_refresh_checkbox = QCheckBox("Włącz auto-odświeżanie")
        self.refresh_interval_spinbox = QSpinBox()
        self.refresh_interval_spinbox.setRange(1, 120)
        self.refresh_interval_spinbox.setValue(DEFAULT_REFRESH_MINUTES)
        self.refresh_interval_spinbox.setSuffix(" min")
        refresh_layout.addRow(self.auto_refresh_checkbox)
        refresh_layout.addRow("Interwał:", self.refresh_interval_spinbox)
        self.sidebar_layout.addWidget(refresh_group)

        self.load_charts_button = QPushButton("Wczytaj Wykresy dla wybranej pary")
        self.sidebar_layout.addWidget(self.load_charts_button)

        self.sidebar_scroll_area.setWidget(self.sidebar_widget)
        self.main_layout.addWidget(self.sidebar_scroll_area)

        self.auto_refresh_checkbox.stateChanged.connect(self.toggle_auto_refresh)
        self.refresh_interval_spinbox.valueChanged.connect(self.update_refresh_interval)
        self.load_charts_button.clicked.connect(self.trigger_chart_updates)

        self.global_indicator_combo.currentTextChanged.connect(self.on_global_indicator_changed); self.global_indicator_combo.currentTextChanged.connect(self.save_settings)
        for spinbox in [self.wpr_period_spin, self.ema_period_spin, self.rsi_period_spin, self.macd_fast_spin, self.macd_slow_spin, self.macd_signal_spin]: spinbox.valueChanged.connect(self.save_settings)
        self.add_pair_button.clicked.connect(self.add_to_watchlist); self.remove_pair_button.clicked.connect(self.remove_from_watchlist)
        self.add_all_pairs_button.clicked.connect(self.add_all_to_watchlist); self.remove_all_pairs_button.clicked.connect(self.remove_all_from_watchlist)

    def setup_charts_grid(self):
        self.charts_area_widget = QWidget(); self.charts_grid_layout = QGridLayout(self.charts_area_widget); self.charts = []
        for idx, (i, j) in enumerate([(i, j) for i in range(2) for j in range(3)]):
            chart_widget = SingleChartWidget(chart_id=idx)
            chart_widget.timeframe_combo.currentTextChanged.connect(self.save_settings)
            chart_widget.mouse_moved_signal.connect(self.sync_crosshairs)
            chart_widget.sigDoubleClicked.connect(lambda cw=chart_widget: self.toggle_maximize_chart(cw))
            self.charts_grid_layout.addWidget(chart_widget, i, j); self.charts.append(chart_widget)
        self.main_layout.addWidget(self.charts_area_widget, 1)

    def toggle_auto_refresh(self, state):
        if Qt.CheckState(state) == Qt.CheckState.Checked:
            self.update_refresh_interval()
            self.refresh_timer.start()
            self.load_charts_button.setEnabled(False)
            self.trigger_chart_updates()
        else:
            self.refresh_timer.stop()
            self.load_charts_button.setEnabled(True)

    def update_refresh_interval(self):
        minutes = self.refresh_interval_spinbox.value()
        interval_ms = minutes * 60 * 1000
        self.refresh_timer.setInterval(interval_ms)

    def toggle_maximize_chart(self, chart_to_toggle):
        if self.maximized_chart is None:
            self.maximized_chart = chart_to_toggle
            for chart in self.charts:
                if chart is not self.maximized_chart: chart.hide()
        else:
            for chart in self.charts: chart.show()
            self.maximized_chart = None

    def sync_crosshairs(self, x_pos):
        for chart in self.charts: chart.update_v_line(x_pos)

    def on_global_indicator_changed(self, indicator_name=None):
        name = indicator_name or self.global_indicator_combo.currentText()
        if name == "Williams %R": self.params_stacked_widget.setCurrentIndex(0)
        elif name == "RSI": self.params_stacked_widget.setCurrentIndex(1)
        elif name == "MACD": self.params_stacked_widget.setCurrentIndex(2)

    def trigger_chart_updates(self):
        if self.is_loading:
            return

        current_item = self.watchlist_widget.currentItem()
        if not current_item:
            if self.auto_refresh_checkbox.isChecked():
                return
            else:
                QMessageBox.warning(self, "Brak wyboru", "Wybierz parę z listy obserwowanych.")
                return

        self.is_loading = True
        self.current_pair = current_item.text()

        if not self.auto_refresh_checkbox.isChecked():
            self.load_charts_button.setEnabled(False)
            self.load_charts_button.setText(f"Wczytywanie {self.current_pair}...")

        self.chart_data_threads.clear()
        delay = 0
        for chart_widget in self.charts:
            QTimer.singleShot(delay, lambda cw=chart_widget: self.start_single_fetch_thread(cw))
            delay += 250

    def on_chart_data_thread_finished(self, finished_thread=None):
        if finished_thread and finished_thread.chart_widget.chart_id in self.chart_data_threads:
            del self.chart_data_threads[finished_thread.chart_widget.chart_id]

        if not self.chart_data_threads:
            self.is_loading = False
            if not self.auto_refresh_checkbox.isChecked():
                self.load_charts_button.setEnabled(True)
                self.load_charts_button.setText("Wczytaj Wykresy dla wybranej pary")

    def start_single_fetch_thread(self, chart_widget):
        exchange = self.get_exchange();
        if not exchange: self.on_chart_data_thread_finished(None); return
        if not hasattr(self, 'current_pair') or not self.current_pair: return

        indicator_name = self.get_indicator_name(); indicator_params = self.get_indicator_params()

        thread = FetchChartDataThread(exchange, self.current_pair, chart_widget.timeframe_combo.currentText(), indicator_name, indicator_params, chart_widget, self)

        thread.data_ready_signal.connect(lambda df, cw, ind=indicator_name: cw.update_chart_and_indicator(df, ind))
        thread.error_signal.connect(lambda msg, cw=chart_widget: cw.plot_widget.setTitle(f"Błąd: {msg}", color='r'))
        thread.finished.connect(lambda th=thread: self.on_chart_data_thread_finished(th))

        self.chart_data_threads[chart_widget.chart_id] = thread
        thread.start()

    def on_exchange_changed(self, exchange_name_gui):
        self.load_settings(); self.available_pairs_list_widget.clear(); self.trigger_fetch_markets()

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return
        selected_exchange_gui = self.chart_exchange_combo.currentText(); selected_config = self.exchange_options.get(selected_exchange_gui)
        if not selected_config: return
        ccxt_id, market_type = selected_config["id_ccxt"], selected_config["type"]
        self.refresh_pairs_button.setEnabled(False); self.refresh_pairs_button.setText("Pobieranie...")
        self.fetch_markets_thread = FetchChartMarketsThread(ccxt_id, market_type, self)
        self.fetch_markets_thread.markets_fetched_signal.connect(self.populate_available_pairs)
        self.fetch_markets_thread.error_signal.connect(lambda msg: QMessageBox.critical(self, "Błąd API", msg))
        self.fetch_markets_thread.finished_signal.connect(self.on_fetch_markets_finished); self.fetch_markets_thread.start()

    def populate_available_pairs(self, pairs_list):
        self.available_pairs_list_widget.clear()
        watchlist_pairs = {self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())}
        self.available_pairs_list_widget.addItems([p for p in pairs_list if p not in watchlist_pairs])

    def on_fetch_markets_finished(self):
        self.refresh_pairs_button.setEnabled(True); self.refresh_pairs_button.setText("Odśwież Dostępne Pary")

    def add_to_watchlist(self):
        for item in self.available_pairs_list_widget.selectedItems():
            self.watchlist_widget.addItem(self.available_pairs_list_widget.takeItem(self.available_pairs_list_widget.row(item)))
        self.watchlist_widget.sortItems(); self.save_settings()

    def remove_from_watchlist(self):
        for item in self.watchlist_widget.selectedItems():
            self.available_pairs_list_widget.addItem(self.watchlist_widget.takeItem(self.watchlist_widget.row(item)))
        self.available_pairs_list_widget.sortItems(); self.save_settings()

    def add_all_to_watchlist(self):
        while self.available_pairs_list_widget.count() > 0:
            self.watchlist_widget.addItem(self.available_pairs_list_widget.takeItem(0))
        self.watchlist_widget.sortItems(); self.save_settings()

    def remove_all_from_watchlist(self):
        while self.watchlist_widget.count() > 0:
            self.available_pairs_list_widget.addItem(self.watchlist_widget.takeItem(0))
        self.available_pairs_list_widget.sortItems(); self.save_settings()

    def get_exchange(self):
        selected_exchange_gui = self.chart_exchange_combo.currentText(); selected_config = self.exchange_options.get(selected_exchange_gui)
        if not selected_config: return None
        ccxt_id, market_type = selected_config["id_ccxt"], selected_config["type"]
        config = configparser.ConfigParser(); config.read(self.config_path)
        api_key, api_secret = '', ''
        config_section_name = selected_config.get("config_section")
        if config_section_name and config.has_section(config_section_name):
            api_key = config.get(config_section_name, 'api_key', fallback=''); api_secret = config.get(config_section_name, 'api_secret', fallback='')
        try:
            exchange = getattr(ccxt, ccxt_id)({'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True, 'options': {'defaultType': market_type} if market_type in ['future', 'swap'] else {}})
            return exchange
        except Exception as e: QMessageBox.critical(self, "Błąd Giełdy", f"Nie można zainicjalizować {ccxt_id}: {str(e)}"); return None

    def get_indicator_name(self): return self.global_indicator_combo.currentText()

    def get_indicator_params(self):
        indicator_name = self.get_indicator_name(); params = {}
        if indicator_name == "Williams %R": params = {'wpr_period': self.wpr_period_spin.value(), 'ema_period': self.ema_period_spin.value()}
        elif indicator_name == "RSI": params = {'rsi_period': self.rsi_period_spin.value()}
        elif indicator_name == "MACD": params = {'fast': self.macd_fast_spin.value(), 'slow': self.macd_slow_spin.value(), 'signal': self.macd_signal_spin.value()}
        return params

    def load_settings(self):
        config = configparser.ConfigParser();
        if not os.path.exists(self.config_path):
            self.wpr_period_spin.setValue(DEFAULT_WPR_LENGTH); self.ema_period_spin.setValue(DEFAULT_EMA_WPR_LENGTH)
            self.rsi_period_spin.setValue(DEFAULT_RSI_LENGTH); self.macd_fast_spin.setValue(DEFAULT_MACD_FAST)
            self.macd_slow_spin.setValue(DEFAULT_MACD_SLOW); self.macd_signal_spin.setValue(DEFAULT_MACD_SIGNAL)
            return
        config.read(self.config_path)
        if config.has_section('chart_indicator_settings'):
            settings = config['chart_indicator_settings']
            self.global_indicator_combo.setCurrentText(settings.get('indicator_type', 'Williams %R'))
            self.wpr_period_spin.setValue(settings.getint('wpr_period', DEFAULT_WPR_LENGTH))
            self.ema_period_spin.setValue(settings.getint('ema_period', DEFAULT_EMA_WPR_LENGTH))
            self.rsi_period_spin.setValue(settings.getint('rsi_period', DEFAULT_RSI_LENGTH))
            self.macd_fast_spin.setValue(settings.getint('macd_fast', DEFAULT_MACD_FAST))
            self.macd_slow_spin.setValue(settings.getint('macd_slow', DEFAULT_MACD_SLOW))
            self.macd_signal_spin.setValue(settings.getint('macd_signal', DEFAULT_MACD_SIGNAL))
        current_exchange_name_gui = self.chart_exchange_combo.currentText(); selected_exchange_config = self.exchange_options.get(current_exchange_name_gui)
        self.watchlist_widget.clear()
        if selected_exchange_config:
            section_name = selected_exchange_config["config_section"]
            if config.has_section(section_name):
                watchlist_pairs_str = config.get(section_name, 'watchlist_pairs', fallback=config.get(section_name, 'scan_pairs', fallback=''))
                if watchlist_pairs_str: self.watchlist_widget.addItems([p.strip() for p in watchlist_pairs_str.split(',') if p.strip()])
        if config.has_section('chart_settings'):
            settings = config['chart_settings']
            for i, chart_widget in enumerate(self.charts):
                tf = settings.get(f'chart_{i}_timeframe', '1h'); chart_widget.timeframe_combo.setCurrentText(tf)

    def save_settings(self, _=None):
        config = configparser.ConfigParser()
        if os.path.exists(self.config_path):
            config.read(self.config_path)

        current_exchange_name_gui = self.chart_exchange_combo.currentText()
        selected_exchange_config = self.exchange_options.get(current_exchange_name_gui)
        if selected_exchange_config:
            section_name = selected_exchange_config["config_section"]
            if not config.has_section(section_name):
                config.add_section(section_name)
            watchlist_items = [self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())]
            config.set(section_name, 'watchlist_pairs', ",".join(watchlist_items))

        if not config.has_section('chart_settings'):
            config.add_section('chart_settings')
        for i, chart_widget in enumerate(self.charts):
            config.set('chart_settings', f'chart_{i}_timeframe', chart_widget.timeframe_combo.currentText())

        if not config.has_section('chart_indicator_settings'):
            config.add_section('chart_indicator_settings')
        settings = config['chart_indicator_settings']
        settings['indicator_type'] = self.global_indicator_combo.currentText().replace('%', '%%')
        settings['wpr_period'] = str(self.wpr_period_spin.value())
        settings['ema_period'] = str(self.ema_period_spin.value())
        settings['rsi_period'] = str(self.rsi_period_spin.value())
        settings['macd_fast'] = str(self.macd_fast_spin.value())
        settings['macd_slow'] = str(self.macd_slow_spin.value())
        settings['macd_signal'] = str(self.macd_signal_spin.value())

        try:
            with open(self.config_path, 'w') as configfile:
                config.write(configfile)
        except Exception as e:
            print(f"Błąd zapisu ustawień wykresów: {e}")

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    cfg_path = "app_settings.ini"
    if not os.path.exists(cfg_path):
        with open(cfg_path, 'w') as f:
            f.write("# Pusty plik konfiguracyjny\n")

    test_options = {
        "Binance (Spot)": {"id_ccxt": "binance", "type": "spot", "config_section":"binance_spot_config"}
    }
    window = MultiChartWindow(test_options, cfg_path)
    window.show()
    sys.exit(app.exec())
