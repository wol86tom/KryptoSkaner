import sys, os, configparser, datetime, time, ccxt, pandas as pd, pandas_ta as ta, pyqtgraph as pg, numpy as np
from PyQt6.QtWidgets import (QMainWindow, QVBoxLayout, QWidget, QComboBox, QGridLayout, QLabel, QListWidget, QPushButton, QHBoxLayout, QGroupBox, QApplication, QMessageBox, QListWidgetItem, QFormLayout, QSpinBox, QStackedWidget, QCheckBox, QScrollArea, QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal, QTimer, QEvent, QPointF, QRectF
from PyQt6.QtGui import QPainter, QPen, QFont, QBrush

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')

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
            p.end()
            return

        if len(self._data) > 1:
            avg_interval = np.mean([self._data[i]['x'] - self._data[i-1]['x'] for i in range(1, len(self._data))])
            w = avg_interval * 0.4
        else:
            w = 1.0

        for d in self._data:
            t, open_val, high_val, low_val, close_val = d['x'], d['open'], d['high'], d['low'], d['close']

            # Draw wick
            p.setPen(pg.mkPen('k')) # Always black pen for wick
            p.drawLine(QPointF(t, low_val), QPointF(t, high_val))

            # Draw body
            if open_val < close_val:
                p.setBrush(pg.mkBrush('g')) # Green for bullish
            else:
                p.setBrush(pg.mkBrush('r')) # Red for bearish

            # Set pen to black for body outline
            p.setPen(pg.mkPen('k')) # Black pen for body outline

            rect_top = min(open_val, close_val)
            rect_height = abs(close_val - open_val)
            p.drawRect(QRectF(t - w, rect_top, w * 2, rect_height))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        if not self._data:
            return QRectF()

        x_values = [d['x'] for d in self._data]
        y_lows = [d['low'] for d in self._data]
        y_highs = [d['high'] for d in self._data]

        x_min = min(x_values)
        x_max = max(x_values)
        y_min = np.min(y_lows)
        y_max = np.max(y_highs)

        if len(self._data) > 1:
            avg_interval = np.mean([self._data[i]['x'] - self._data[i-1]['x'] for i in range(1, len(self._data))])
            w = avg_interval * 0.4
        else:
            w = 1.0

        return QRectF(x_min - w, y_min, (x_max - x_min) + 2 * w, y_max - y_min)


class FetchChartMarketsThread(QThread):
    markets_fetched_signal = Signal(list)
    error_signal = Signal(str)
    finished_signal = Signal()

    def __init__(self, exchange_id_ccxt, market_type_filter, parent=None):
        super().__init__(parent)
        self.exchange_id_ccxt = exchange_id_ccxt
        self.market_type_filter = market_type_filter

    def run(self):
        # print(f"[DEBUG_MARKET_FETCH]: Rozpoczynam pobieranie rynków dla giełdy: {self.exchange_id_ccxt}, typ: {self.market_type_filter}") # Log removed for cleaner output during normal operation
        try:
            exchange = getattr(ccxt, self.exchange_id_ccxt)({'enableRateLimit': True, 'timeout': 30000})
            markets = exchange.load_markets()
            # print(f"[DEBUG_MARKET_FETCH]: Pobrane rynki z {self.exchange_id_ccxt}. Liczba rynków: {len(markets)}") # Log removed

            available_market_data = []
            for m in markets.values():
                if self.type_matches(m):
                    available_market_data.append({
                        'symbol': m['symbol'],
                        'id': m['id'],
                        'type': m['type']
                    })
            self.markets_fetched_signal.emit(sorted(available_market_data, key=lambda x: x['symbol']))
        except Exception as e:
            self.error_signal.emit(f"Błąd pobierania par dla {self.exchange_id_ccxt}: {type(e).__name__} - {str(e)}")
        finally:
            self.finished_signal.emit()

    def type_matches(self, market):
        if not (market.get('active', False) and market.get('quote', '').upper() == 'USDT'):
            return False

        market_type = market.get('type')

        if self.market_type_filter == market_type:
            return True

        if self.exchange_id_ccxt == 'binanceusdm' and self.market_type_filter == 'future':
            if market.get('linear') and market_type in ['future', 'swap']:
                return True

        if self.exchange_id_ccxt == 'bybit' and self.market_type_filter == 'swap' and market_type == 'swap':
            return True

        return False

class FetchChartDataThread(QThread):
    data_ready_signal = Signal(object, object)
    error_signal = Signal(str, object)
    finished_signal = Signal(object)

    def __init__(self, exchange, pair_symbol, timeframe, indicator_name, indicator_params, chart_widget, parent=None):
        super().__init__(parent)
        self.exchange = exchange
        self.pair_symbol = pair_symbol
        self.timeframe = timeframe
        self.indicator_name = indicator_name
        self.indicator_params = indicator_params
        self.chart_widget = chart_widget

    def run(self):
        try:
            ohlcv = self.exchange.fetch_ohlcv(self.pair_symbol, timeframe=self.timeframe, limit=300)
            if not ohlcv:
                raise ccxt.NetworkError(f"Giełda nie zwróciła danych OHLCV dla {self.pair_symbol} na {self.timeframe}.")

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

            if self.indicator_name == "Williams %R":
                wpr_p = self.indicator_params.get('wpr_period', DEFAULT_WPR_LENGTH)
                ema_p = self.indicator_params.get('ema_period', DEFAULT_EMA_WPR_LENGTH)
                wpr_col = f'WILLR_{wpr_p}'
                df.ta.willr(length=wpr_p, append=True)
                if wpr_col in df and df[wpr_col].notna().any():
                    df[f'WPR_EMA_{ema_p}'] = ta.ema(df[wpr_col], length=ema_p)
            elif self.indicator_name == "RSI":
                rsi_p = self.indicator_params.get('rsi_period', DEFAULT_RSI_LENGTH)
                df.ta.rsi(length=rsi_p, append=True)
            elif self.indicator_name == "MACD":
                fast_p = self.indicator_params.get('fast', DEFAULT_MACD_FAST)
                slow_p = self.indicator_params.get('slow', DEFAULT_MACD_SLOW)
                signal_p = self.indicator_params.get('signal', DEFAULT_MACD_SIGNAL)
                df.ta.macd(fast=fast_p, slow=slow_p, signal=signal_p, append=True)

            self.data_ready_signal.emit(df, self.chart_widget)
        except Exception as e:
            error_message = f"Błąd w wątku pobierania danych dla {self.pair_symbol} ({self.timeframe}): {type(e).__name__} - {str(e)}"
            self.error_signal.emit(error_message, self.chart_widget)
        finally:
            self.finished_signal.emit(self.chart_widget)

class MeasurablePlotItem(pg.PlotItem):
    sigMeasureStart = Signal(object)
    sigMeasureUpdate = Signal(object)
    sigMeasureEnd = Signal(object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.measuring = False

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton and ev.modifiers() == Qt.KeyboardModifier.ControlModifier:
            ev.accept()
            self.measuring = True
            self.sigMeasureStart.emit(self.vb.mapSceneToView(ev.pos()))
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self.measuring:
            self.sigMeasureUpdate.emit(self.vb.mapSceneToView(ev.pos()))
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton and self.measuring:
            ev.accept()
            self.measuring = False
            self.sigMeasureEnd.emit(self.vb.mapSceneToView(ev.pos()))
        else:
            super().mouseReleaseEvent(ev)

class SingleChartWidget(QWidget):
    mouse_moved_signal = Signal(float)
    sigDoubleClicked = Signal()

    def __init__(self, chart_id, parent=None):
        super().__init__(parent)
        self.chart_id = chart_id
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(2, 2, 2, 2)
        self.layout.setSpacing(2)

        top_layout = QHBoxLayout()
        self.chart_title_label = QLabel(f"Wykres {self.chart_id + 1}")

        font = QFont()
        font.setPointSize(9)
        self.chart_title_label.setFont(font)

        self.timeframe_combo = QComboBox()
        self.timeframe_combo.addItems(['1m', '5m', '15m', '1h', '4h', '12h', '1d', '1w'])
        top_layout.addWidget(self.chart_title_label)
        top_layout.addStretch()
        top_layout.addWidget(QLabel("Interwał:"))
        top_layout.addWidget(self.timeframe_combo)

        self.plot_item_price = MeasurablePlotItem(axisItems={'bottom': pg.DateAxisItem()})
        self.plot_widget = pg.PlotWidget(plotItem=self.plot_item_price)
        self.indicator_plot_item = pg.PlotItem(axisItems={'bottom': pg.DateAxisItem()})
        self.indicator_widget = pg.PlotWidget(plotItem=self.indicator_plot_item)
        self.indicator_plot_item.setXLink(self.plot_item_price)

        self.layout.addLayout(top_layout)
        self.layout.addWidget(self.plot_widget)
        self.layout.addWidget(self.indicator_widget)
        self.layout.setStretchFactor(self.plot_widget, 3)
        self.layout.setStretchFactor(self.indicator_widget, 1)

        self.data_frame = None
        self.current_indicator_name = ""
        self.pair_name = ""

        self.candlestick_item = CandlestickItem()
        self.plot_widget.addItem(self.candlestick_item)

        pen = pg.mkPen(color=(100, 100, 100), style=Qt.PenStyle.DashLine)
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        self.v_line_indicator = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.plot_widget.addItem(self.v_line, ignoreBounds=True)
        self.plot_widget.addItem(self.h_line, ignoreBounds=True)
        self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)

        self.price_mouse_proxy = pg.SignalProxy(self.plot_widget.scene().sigMouseMoved, rateLimit=60, slot=self.mouse_moved)
        self.indicator_mouse_proxy = pg.SignalProxy(self.indicator_widget.scene().sigMouseMoved, rateLimit=60, slot=self.mouse_moved)

        self.plot_widget.scene().installEventFilter(self)
        self.indicator_widget.scene().installEventFilter(self)

        self.measure_line = pg.PlotDataItem(pen=pg.mkPen(color='blue', style=Qt.PenStyle.DashLine, width=2))
        self.measure_text = pg.TextItem(anchor=(0, 1), color=(0,0,200), fill=(255, 255, 255, 180))
        self.plot_widget.addItem(self.measure_line)
        self.plot_widget.addItem(self.measure_text)
        self.measure_text.setVisible(False)
        self.plot_item_price.sigMeasureStart.connect(self.measure_start)
        self.plot_item_price.sigMeasureUpdate.connect(self.measure_update)
        self.plot_item_price.sigMeasureEnd.connect(self.measure_end)

    def eventFilter(self, watched_object, event):
        if event.type() == QEvent.Type.Leave:
            if watched_object is self.plot_widget.scene() or watched_object is self.indicator_widget.scene():
                self.mouse_left()
        return super().eventFilter(watched_object, event)

    def mouseDoubleClickEvent(self, event): self.sigDoubleClicked.emit(); super().mouseDoubleClickEvent(event)

    def mouse_moved(self, event):
        pos = event[0]
        if self.plot_widget.sceneBoundingRect().contains(pos) or self.indicator_widget.sceneBoundingRect().contains(pos):
            mouse_point = self.plot_widget.getPlotItem().vb.mapSceneToView(pos)
            x_val, y_val = mouse_point.x(), mouse_point.y()

            self.h_line.setPos(y_val)
            self.h_line.show()
            self.mouse_moved_signal.emit(x_val)

            date_str = datetime.datetime.fromtimestamp(x_val).strftime('%Y-%m-%d %H:%M:%S')
            self.chart_title_label.setText(f"<b>{self.pair_name}</b> | Data: {date_str} | Cena: {y_val:.4f}")
        else:
            self.mouse_left()

    def mouse_left(self):
        self.h_line.hide()
        self.v_line.hide()
        self.v_line_indicator.hide()
        self.chart_title_label.setText(f"<b>{self.pair_name}</b>")

    def update_v_line(self, x_pos):
        self.v_line.setPos(x_pos); self.v_line.show()
        self.v_line_indicator.setPos(x_pos); self.v_line_indicator.show()

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
        elif dist_to_low < snap_threshold and dist_to_low < dist_to_high: snapped_y = candle_low
        elif abs(mouse_y - nearest_candle['open']) < snap_threshold and abs(mouse_y - nearest_candle['open']) < min(dist_to_high, dist_to_low): snapped_y = nearest_candle['open']
        elif abs(mouse_y - nearest_candle['close']) < snap_threshold and abs(mouse_y - nearest_candle['close']) < min(dist_to_high, dist_to_low): snapped_y = nearest_candle['close']
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

    def update_chart_and_indicator(self, df, indicator_name, pair_name):
        self.data_frame = df
        self.current_indicator_name = indicator_name
        self.pair_name = pair_name
        self.chart_title_label.setText(f"<b>{self.pair_name}</b>")

        if not df.empty:
            candlestick_data = []
            for d_idx, row in df.iterrows():
                timestamp = d_idx.timestamp()
                candlestick_data.append({'x': timestamp, 'open': row['open'], 'high': row['high'], 'low': row['low'], 'close': row['close']})
            self.candlestick_item.setData(candlestick_data)
        else:
            self.candlestick_item.setData([])

        self.redraw_indicator()
        self.plot_widget.autoRange()
        self.indicator_widget.autoRange()
        self.mouse_left()

    def redraw_indicator(self):
        self.indicator_widget.clear(); self.indicator_widget.addItem(self.v_line_indicator, ignoreBounds=True)
        if self.data_frame is None or self.data_frame.empty: return
        indicator_name = self.current_indicator_name; timestamps = self.data_frame.index.astype('int64') // 10**9
        if indicator_name == "Williams %R":
            wpr_col = next((c for c in self.data_frame if c.startswith('WILLR_')), None); ema_col = next((c for c in self.data_frame if c.startswith('WPR_EMA_')), None)
            if wpr_col is not None: self.indicator_widget.plot(x=timestamps, y=self.data_frame[wpr_col], pen='b', name="W%R")
            if ema_col is not None: self.indicator_widget.plot(x=timestamps, y=self.data_frame[ema_col], pen=pg.mkPen('orange', width=2), name="EMA on W%R")
            self.indicator_widget.addLine(y=-20, pen=pg.mkPen('r', style=Qt.PenStyle.DashLine)); self.indicator_widget.addLine(y=-80, pen=pg.mkPen('g', style=Qt.PenStyle.DashLine))
        elif indicator_name == "RSI":
            rsi_col = next((c for c in self.data_frame if c.startswith('RSI_')), None)
            if rsi_col is not None: self.indicator_widget.plot(x=timestamps, y=self.data_frame[rsi_col], pen='g', name="RSI"); self.indicator_widget.addLine(y=70, pen=pg.mkPen('r', style=Qt.PenStyle.DashLine)); self.indicator_widget.addLine(y=30, pen=pg.mkPen('g', style=Qt.PenStyle.DashLine))
        elif indicator_name == "MACD":
            macd_col = next((c for c in self.data_frame if c.startswith('MACD_')), None); macdh_col = next((c for c in self.data_frame if c.startswith('MACDh_')), None); macds_col = next((c for c in self.data_frame if c.startswith('MACDs_')), None)
            if all([macd_col, macdh_col, macds_col]):
                self.indicator_widget.plot(x=timestamps, y=self.data_frame[macd_col], pen='b', name='MACD'); self.indicator_widget.plot(x=timestamps, y=self.data_frame[macds_col], pen='r', name='Signal')
                brushes = ['g' if v > 0 else 'r' for v in self.data_frame[macdh_col]]; width = 0.8 * (timestamps[1] - timestamps[0] if len(timestamps) > 1 else 1)
                self.indicator_widget.addItem(pg.BarGraphItem(x=timestamps, height=self.data_frame[macdh_col], width=width, brushes=brushes))
        self.indicator_widget.autoRange()

class MultiChartWindow(QMainWindow):
    def __init__(self, exchange_options, config_path, parent=None):
        super().__init__(parent)
        self.exchange_options = exchange_options
        self.config_path = config_path
        self.setWindowTitle("Okno Analizy Wykresów")
        self.setGeometry(150, 150, 1400, 800)
        self.maximized_chart = None
        self.current_pair = ""
        self.is_loading = False

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.trigger_chart_updates)

        self.setup_ui() # Call to setup UI elements
        self.fetch_markets_thread = None
        self.chart_data_threads = {}

        # Load settings and trigger initial market fetch after UI setup
        self.load_settings()
        self.on_global_indicator_changed() # Ensure indicator parameters are correctly set up
        self.trigger_fetch_markets()

    # --- Methods for getting indicator parameters and exchange configuration ---
    def get_exchange(self):
        selected_exchange_gui = self.chart_exchange_combo.currentText()
        selected_config = self.exchange_options.get(selected_exchange_gui)
        if not selected_config:
            QMessageBox.critical(self, "Błąd Giełdy", "Nie wybrano konfiguracji giełdy lub konfiguracja jest niekompletna.")
            return None

        ccxt_id, market_type = selected_config["id_ccxt"], selected_config["type"]

        config = configparser.ConfigParser()
        config.read(self.config_path)

        api_key, api_secret = '', ''
        config_section_name = selected_config.get("config_section")
        if config_section_name and config.has_section(config_section_name):
            api_key = config.get(config_section_name, 'api_key', fallback='')
            api_secret = config.get(config_section_name, 'api_secret', fallback='')

        try:
            exchange_class = getattr(ccxt, ccxt_id)
            exchange = exchange_class({
                'apiKey': api_key,
                'secret': api_secret,
                'enableRateLimit': True,
                'options': {'defaultType': market_type} if market_type in ['future', 'swap'] else {},
                'timeout': 30000
            })
            return exchange
        except Exception as e:
            QMessageBox.critical(self, "Błąd Giełdy", f"Nie można zainicjalizować {ccxt_id}: {str(e)}")
            return None

    def get_indicator_name(self):
        return self.global_indicator_combo.currentText()

    def get_indicator_params(self):
        indicator_name = self.get_indicator_name()
        params = {}
        if indicator_name == "Williams %R":
            params['wpr_period'] = self.wpr_period_spin.value()
            params['ema_period'] = self.ema_period_spin.value()
        elif indicator_name == "RSI":
            params['rsi_period'] = self.rsi_period_spin.value()
        elif indicator_name == "MACD":
            params['fast'] = self.macd_fast_spin.value()
            params['slow'] = self.macd_slow_spin.value()
            params['signal'] = self.macd_signal_spin.value()
        return params

    # --- Refactored UI Setup Methods ---
    def setup_ui(self):
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)
        self.setup_sidebar()
        self.setup_charts_grid()

    def setup_sidebar(self):
        self.sidebar_scroll_area = QScrollArea()
        self.sidebar_scroll_area.setWidgetResizable(True)
        self.sidebar_scroll_area.setFixedWidth(320)

        self.sidebar_widget = QWidget()
        self.sidebar_layout = QVBoxLayout(self.sidebar_widget)

        self.sidebar_layout.addWidget(QLabel("<b>Panel Kontrolny</b>"))
        self.sidebar_layout.addWidget(QLabel("Giełda:"))
        self.chart_exchange_combo = QComboBox()
        if self.exchange_options:
            self.chart_exchange_combo.addItems(self.exchange_options.keys())
        self.chart_exchange_combo.currentTextChanged.connect(self.on_exchange_changed)
        self.sidebar_layout.addWidget(self.chart_exchange_combo)

        # Indicator Group
        indicator_group = QGroupBox("Globalny Wskaźnik")
        indicator_layout = QFormLayout()
        self.global_indicator_combo = QComboBox()
        self.global_indicator_combo.addItems(["Williams %R", "RSI", "MACD"])
        indicator_layout.addRow("Wskaźnik:", self.global_indicator_combo)
        self.params_stacked_widget = QStackedWidget()

        # WPR Params
        wpr_params_widget = QWidget()
        wpr_layout = QFormLayout(wpr_params_widget)
        wpr_layout.setContentsMargins(0,0,0,0)
        self.wpr_period_spin = QSpinBox(); self.wpr_period_spin.setRange(1,200); wpr_layout.addRow("Okres W%R:", self.wpr_period_spin)
        self.ema_period_spin = QSpinBox(); self.ema_period_spin.setRange(1,200); wpr_layout.addRow("Okres EMA:", self.ema_period_spin)
        self.params_stacked_widget.addWidget(wpr_params_widget)

        # RSI Params
        rsi_params_widget = QWidget()
        rsi_layout = QFormLayout(rsi_params_widget)
        rsi_layout.setContentsMargins(0,0,0,0)
        self.rsi_period_spin = QSpinBox(); self.rsi_period_spin.setRange(1,200); rsi_layout.addRow("Okres RSI:", self.rsi_period_spin)
        self.params_stacked_widget.addWidget(rsi_params_widget)

        # MACD Params
        macd_params_widget = QWidget()
        macd_layout = QFormLayout(macd_params_widget)
        macd_layout.setContentsMargins(0,0,0,0)
        self.macd_fast_spin = QSpinBox(); self.macd_fast_spin.setRange(1,200); macd_layout.addRow("Okres Fast:", self.macd_fast_spin)
        self.macd_slow_spin = QSpinBox(); self.macd_slow_spin.setRange(1,200); macd_layout.addRow("Okres Slow:", self.macd_slow_spin)
        self.macd_signal_spin = QSpinBox(); self.macd_signal_spin.setRange(1,200); macd_layout.addRow("Okres Signal:", self.macd_signal_spin)
        self.params_stacked_widget.addWidget(macd_params_widget)

        indicator_layout.addRow(self.params_stacked_widget)
        indicator_group.setLayout(indicator_layout)
        self.sidebar_layout.addWidget(indicator_group)

        # Pairs Management Group
        self.pairs_group = QGroupBox("Zarządzanie Listą Par")
        pairs_layout = QVBoxLayout()
        available_pairs_group = QGroupBox("Dostępne Pary:")
        available_layout = QVBoxLayout(available_pairs_group)
        self.available_pairs_list_widget = QListWidget()
        self.available_pairs_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        available_layout.addWidget(self.available_pairs_list_widget)

        buttons_layout = QGridLayout()
        self.add_pair_button = QPushButton(">"); self.add_pair_button.setToolTip("Dodaj zaznaczone"); buttons_layout.addWidget(self.add_pair_button, 0, 0)
        self.add_all_pairs_button = QPushButton(">>"); self.add_all_pairs_button.setToolTip("Dodaj wszystkie"); buttons_layout.addWidget(self.add_all_pairs_button, 0, 1)
        self.remove_pair_button = QPushButton("<"); self.remove_pair_button.setToolTip("Usuń zaznaczone"); buttons_layout.addWidget(self.remove_pair_button, 1, 0)
        self.remove_all_pairs_button = QPushButton("<<"); self.remove_all_pairs_button.setToolTip("Usuń wszystkie"); buttons_layout.addWidget(self.remove_all_pairs_button, 1, 1)

        watchlist_group = QGroupBox("Moja lista obserwowanych:")
        watchlist_layout = QVBoxLayout(watchlist_group)
        self.watchlist_widget = QListWidget()
        self.watchlist_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        watchlist_layout.addWidget(self.watchlist_widget)

        pairs_layout.addWidget(available_pairs_group)
        pairs_layout.addLayout(buttons_layout)
        pairs_layout.addWidget(watchlist_group)
        self.refresh_pairs_button = QPushButton("Odśwież Dostępne Pary")
        self.refresh_pairs_button.clicked.connect(self.trigger_fetch_markets)
        pairs_layout.addWidget(self.refresh_pairs_button)
        self.pairs_group.setLayout(pairs_layout)
        self.sidebar_layout.addWidget(self.pairs_group)

        self.sidebar_layout.addStretch()

        # Auto Refresh Group
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

        # Connect signals for sidebar controls
        self.auto_refresh_checkbox.stateChanged.connect(self.toggle_auto_refresh)
        self.refresh_interval_spinbox.valueChanged.connect(self.update_refresh_interval)
        self.load_charts_button.clicked.connect(self.trigger_chart_updates)
        self.global_indicator_combo.currentTextChanged.connect(self.on_global_indicator_changed)
        self.global_indicator_combo.currentTextChanged.connect(self.save_settings)
        for spinbox in [self.wpr_period_spin, self.ema_period_spin, self.rsi_period_spin, self.macd_fast_spin, self.macd_slow_spin, self.macd_signal_spin]:
            spinbox.valueChanged.connect(self.save_settings)
        self.add_pair_button.clicked.connect(self.add_to_watchlist)
        self.remove_pair_button.clicked.connect(self.remove_from_watchlist)
        self.add_all_pairs_button.clicked.connect(self.add_all_to_watchlist)
        self.remove_all_pairs_button.clicked.connect(self.remove_all_from_watchlist)

    def setup_charts_grid(self):
        self.charts_area_widget = QWidget()
        self.charts_grid_layout = QGridLayout(self.charts_area_widget)
        self.charts = []
        for idx, (i, j) in enumerate([(i, j) for i in range(2) for j in range(3)]):
            chart_widget = SingleChartWidget(chart_id=idx)
            chart_widget.timeframe_combo.currentTextChanged.connect(self.save_settings)
            chart_widget.mouse_moved_signal.connect(self.sync_crosshairs)
            chart_widget.sigDoubleClicked.connect(lambda cw=chart_widget: self.toggle_maximize_chart(cw))
            self.charts_grid_layout.addWidget(chart_widget, i, j)
            self.charts.append(chart_widget)
        self.main_layout.addWidget(self.charts_area_widget, 1)

    # --- Refactored Settings (Load/Save) Methods ---
    def load_settings(self):
        config = configparser.ConfigParser()
        if not os.path.exists(self.config_path):
            self._load_default_chart_settings()
            return

        config.read(self.config_path)
        self._load_chart_indicator_settings(config)
        self._load_auto_refresh_settings(config)
        self._load_watchlist_settings(config)
        self._load_chart_timeframe_settings(config)

    def _load_default_chart_settings(self):
        """Loads default chart settings if config file is not found."""
        self.wpr_period_spin.setValue(DEFAULT_WPR_LENGTH)
        self.ema_period_spin.setValue(DEFAULT_EMA_WPR_LENGTH)
        self.rsi_period_spin.setValue(DEFAULT_RSI_LENGTH)
        self.macd_fast_spin.setValue(DEFAULT_MACD_FAST)
        self.macd_slow_spin.setValue(DEFAULT_MACD_SLOW)
        self.macd_signal_spin.setValue(DEFAULT_MACD_SIGNAL)
        self.refresh_interval_spinbox.setValue(DEFAULT_REFRESH_MINUTES)
        self.auto_refresh_checkbox.setChecked(False)
        self.watchlist_widget.clear()
        for chart_widget in self.charts:
            chart_widget.timeframe_combo.setCurrentText('1h') # Default timeframe for charts

    def _load_chart_indicator_settings(self, config):
        if config.has_section('chart_indicator_settings'):
            settings = config['chart_indicator_settings']
            self.global_indicator_combo.setCurrentText(settings.get('indicator_type', 'Williams %R').replace('%%', '%'))
            self.wpr_period_spin.setValue(settings.getint('wpr_period', DEFAULT_WPR_LENGTH))
            self.ema_period_spin.setValue(settings.getint('ema_period', DEFAULT_EMA_WPR_LENGTH))
            self.rsi_period_spin.setValue(settings.getint('rsi_period', DEFAULT_RSI_LENGTH))
            self.macd_fast_spin.setValue(settings.getint('macd_fast', DEFAULT_MACD_FAST))
            self.macd_slow_spin.setValue(settings.getint('macd_slow', DEFAULT_MACD_SLOW))
            self.macd_signal_spin.setValue(settings.getint('macd_signal', DEFAULT_MACD_SIGNAL))

    def _load_auto_refresh_settings(self, config):
        if config.has_section('auto_refresh_settings'):
            refresh_settings = config['auto_refresh_settings']
            self.auto_refresh_checkbox.setChecked(refresh_settings.getboolean('enabled', False))
            self.refresh_interval_spinbox.setValue(refresh_settings.getint('interval_minutes', DEFAULT_REFRESH_MINUTES))

    def _load_watchlist_settings(self, config):
        current_exchange_name_gui = self.chart_exchange_combo.currentText()
        selected_exchange_config = self.exchange_options.get(current_exchange_name_gui)
        self.watchlist_widget.clear() # Always clear before loading

        if selected_exchange_config:
            section_name = selected_exchange_config.get("config_section")
            if section_name and config.has_section(section_name):
                watchlist_pairs_str = config.get(section_name, 'watchlist_pairs', fallback='')
                if watchlist_pairs_str:
                    for p_symbol_text in [p.strip() for p in watchlist_pairs_str.split(',') if p.strip()]:
                        item = QListWidgetItem(p_symbol_text)
                        # We need to re-fetch market data for watchlist items to get full market_data for UserRole
                        # This part might need improvement for robust data persistence if `id` or `type` is critical
                        # For now, just adding symbol string to list, if that's enough
                        self.watchlist_widget.addItem(item)
            # Note: For full market_data persistence for watchlist, you'd need to save/load 'id' and 'type' too.

    def _load_chart_timeframe_settings(self, config):
        if config.has_section('chart_settings'):
            settings = config['chart_settings']
            for i, chart_widget in enumerate(self.charts):
                tf = settings.get(f'chart_{i}_timeframe', '1h')
                chart_widget.timeframe_combo.setCurrentText(tf)


    def save_settings(self, _=None):
        config = configparser.ConfigParser()
        if os.path.exists(self.config_path):
            config.read(self.config_path)

        self._save_chart_indicator_settings(config)
        self._save_auto_refresh_settings(config)
        self._save_watchlist_settings(config)
        self._save_chart_timeframe_settings(config)

        try:
            with open(self.config_path, 'w') as configfile:
                config.write(configfile)
            # print("Ustawienia wykresów zapisano pomyślnie.") # Removed verbose print
        except Exception as e:
            print(f"Błąd zapisu ustawień wykresów: {e}")

    def _save_chart_indicator_settings(self, config):
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

    def _save_auto_refresh_settings(self, config):
        if not config.has_section('auto_refresh_settings'):
            config.add_section('auto_refresh_settings')
        refresh_settings = config['auto_refresh_settings']
        refresh_settings['enabled'] = str(self.auto_refresh_checkbox.isChecked())
        refresh_settings['interval_minutes'] = str(self.refresh_interval_spinbox.value())

    def _save_watchlist_settings(self, config):
        current_exchange_name_gui = self.chart_exchange_combo.currentText()
        selected_exchange_config = self.exchange_options.get(current_exchange_name_gui)
        if selected_exchange_config:
            section_name = selected_exchange_config.get("config_section")
            if section_name:
                if not config.has_section(section_name):
                    config.add_section(section_name)
                watchlist_items = [self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())]
                config.set(section_name, 'watchlist_pairs', ",".join(watchlist_items))
        # Note: Same as _load_watchlist_settings, only symbol string is saved/loaded for watchlist.

    def _save_chart_timeframe_settings(self, config):
        if not config.has_section('chart_settings'):
            config.add_section('chart_settings')
        for i, chart_widget in enumerate(self.charts):
            config.set('chart_settings', f'chart_{i}_timeframe', chart_widget.timeframe_combo.currentText())

    # --- Existing (mostly unchanged) functional methods ---
    def toggle_auto_refresh(self, state):
        if Qt.CheckState(state) == Qt.CheckState.Checked:
            self.update_refresh_interval()
            self.refresh_timer.start()
            self.load_charts_button.setEnabled(False)
            self.trigger_chart_updates()
        else:
            self.refresh_timer.stop()
            for thread_id, thread in list(self.chart_data_threads.items()):
                if thread.isRunning():
                    thread.exit()
                    thread.wait(500)
                del self.chart_data_threads[thread_id]
            self.chart_data_threads = {}
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

        for thread_id, thread in list(self.chart_data_threads.items()):
            if thread.isRunning():
                thread.exit()
                thread.wait(500)
            del self.chart_data_threads[thread_id]
        self.chart_data_threads = {}

        delay = 0
        for chart_widget in self.charts:
            QTimer.singleShot(delay, lambda cw=chart_widget: self.start_single_fetch_thread(cw))
            delay += 250

    def on_chart_data_thread_finished(self, finished_chart_widget=None):
        if finished_chart_widget and finished_chart_widget.chart_id in self.chart_data_threads:
            del self.chart_data_threads[finished_chart_widget.chart_id]

        if not self.chart_data_threads:
            self.is_loading = False
            if not self.auto_refresh_checkbox.isChecked():
                self.load_charts_button.setEnabled(True)
                self.load_charts_button.setText("Wczytaj Wykresy dla wybranej pary")

    def start_single_fetch_thread(self, chart_widget):
        exchange = self.get_exchange();
        if not exchange: self.on_chart_data_thread_finished(chart_widget); return
        if not hasattr(self, 'current_pair') or not self.current_pair: return

        indicator_name = self.get_indicator_name(); indicator_params = self.get_indicator_params()

        thread = FetchChartDataThread(exchange, self.current_pair, chart_widget.timeframe_combo.currentText(), indicator_name, indicator_params, chart_widget, self)

        thread.data_ready_signal.connect(lambda df, cw, ind=indicator_name, p=self.current_pair: cw.update_chart_and_indicator(df, ind, p))
        thread.error_signal.connect(lambda msg, cw=chart_widget: cw.chart_title_label.setText(f"Błąd: {msg}"))
        thread.finished_signal.connect(self.on_chart_data_thread_finished)

        self.chart_data_threads[chart_widget.chart_id] = thread
        thread.start()

    def on_exchange_changed(self, exchange_name_gui):
        # When exchange changes, load settings specific to this exchange (including watchlist)
        self.load_settings()
        # Clear available pairs and re-fetch them for the new exchange
        self.available_pairs_list_widget.clear()
        self.trigger_fetch_markets()

    def trigger_fetch_markets(self):
        if self.fetch_markets_thread and self.fetch_markets_thread.isRunning(): return

        self.chart_exchange_combo.setEnabled(False)

        selected_exchange_gui = self.chart_exchange_combo.currentText()
        selected_config = self.exchange_options.get(selected_exchange_gui)
        if not selected_config:
            self.on_fetch_markets_finished()
            return

        ccxt_id, market_type = selected_config["id_ccxt"], selected_config["type"]

        self.refresh_pairs_button.setEnabled(False)
        self.refresh_pairs_button.setText("Pobieranie...")

        self.fetch_markets_thread = FetchChartMarketsThread(ccxt_id, market_type, self)
        self.fetch_markets_thread.markets_fetched_signal.connect(self.populate_available_pairs)
        self.fetch_markets_thread.error_signal.connect(lambda msg: QMessageBox.critical(self, "Błąd API", msg))
        self.fetch_markets_thread.finished_signal.connect(self.on_fetch_markets_finished)
        self.fetch_markets_thread.start()

    def populate_available_pairs(self, pairs_list):
        # print(f"[DEBUG_POPULATE_PAIRS]: Otrzymano {len(pairs_list)} rynków do populacji.") # Removed verbose print
        # for i, market in enumerate(pairs_list[:5]): # Removed verbose print
        #     print(f"  [DEBUG_POPULATE_PAIRS]: Rynek {i}: {market}") # Removed verbose print

        # Filter out pairs already in watchlist before populating available list
        current_watchlist_items = {self.watchlist_widget.item(i).text() for i in range(self.watchlist_widget.count())}
        self.available_pairs_list_widget.clear()

        added_count = 0
        for market in pairs_list: # Note: 'pairs_list' now contains dicts, not just symbols
            symbol = market['symbol']
            if symbol not in current_watchlist_items:
                item = QListWidgetItem(symbol)
                item.setData(Qt.ItemDataRole.UserRole, market) # Store full market data
                self.available_pairs_list_widget.addItem(item)
                added_count += 1
        # print(f"[DEBUG_POPULATE_PAIRS]: Dodano {added_count} par do listy dostępnych.") # Removed verbose print

    def on_fetch_markets_finished(self):
        self.chart_exchange_combo.setEnabled(True)
        self.refresh_pairs_button.setEnabled(True)
        self.refresh_pairs_button.setText("Odśwież Dostępne Pary")

    def add_to_watchlist(self):
        selected_items = self.available_pairs_list_widget.selectedItems()
        for item in selected_items:
            # When moving, create a new QListWidgetItem with the same data
            new_item = QListWidgetItem(item.text())
            new_item.setData(Qt.ItemDataRole.UserRole, item.data(Qt.ItemDataRole.UserRole))
            self.watchlist_widget.addItem(new_item)
            self.available_pairs_list_widget.takeItem(self.available_pairs_list_widget.row(item))
        self.watchlist_widget.sortItems()
        self.save_settings()

    def remove_from_watchlist(self):
        selected_items = self.watchlist_widget.selectedItems()
        for item in selected_items:
            # When moving back, check if it's already in available to avoid duplicates
            item_text = item.text()
            is_already_available = False
            for i in range(self.available_pairs_list_widget.count()):
                if self.available_pairs_list_widget.item(i).text() == item_text:
                    is_already_available = True
                    break

            if not is_already_available:
                new_item = QListWidgetItem(item.text())
                new_item.setData(Qt.ItemDataRole.UserRole, item.data(Qt.ItemDataRole.UserRole)) # Keep original data
                self.available_pairs_list_widget.addItem(new_item)
            self.watchlist_widget.takeItem(self.watchlist_widget.row(item))
        self.available_pairs_list_widget.sortItems()
        self.save_settings()

    def add_all_to_watchlist(self):
        # Move all items from available to watchlist
        items_to_add = []
        for i in range(self.available_pairs_list_widget.count()):
            item = self.available_pairs_list_widget.item(i)
            new_item = QListWidgetItem(item.text())
            new_item.setData(Qt.ItemDataRole.UserRole, item.data(Qt.ItemDataRole.UserRole))
            items_to_add.append(new_item)

        for item in items_to_add:
            self.watchlist_widget.addItem(item)
        self.available_pairs_list_widget.clear() # Clear the original list

        self.watchlist_widget.sortItems()
        self.save_settings()

    def remove_all_from_watchlist(self):
        # Move all items from watchlist to available
        items_to_add = []
        for i in range(self.watchlist_widget.count()):
            item = self.watchlist_widget.item(i)
            # Check for duplicates before adding back
            item_text = item.text()
            is_already_available = False
            for j in range(self.available_pairs_list_widget.count()):
                if self.available_pairs_list_widget.item(j).text() == item_text:
                    is_already_available = True
                    break
            if not is_already_available:
                new_item = QListWidgetItem(item.text())
                new_item.setData(Qt.ItemDataRole.UserRole, item.data(Qt.ItemDataRole.UserRole))
                items_to_add.append(new_item)

        for item in items_to_add:
            self.available_pairs_list_widget.addItem(item)
        self.watchlist_widget.clear() # Clear the original list

        self.available_pairs_list_widget.sortItems()
        self.save_settings()


if __name__ == '__main__':
    app = QApplication(sys.argv)

    CONFIG_FILE_PATH = os.path.join(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation),
        "KryptoSkaner", "app_settings.ini"
    )
    if not os.path.exists(os.path.dirname(CONFIG_FILE_PATH)):
        os.makedirs(os.path.dirname(CONFIG_FILE_PATH), exist_ok=True)

    if not os.path.exists(CONFIG_FILE_PATH):
        with open(CONFIG_FILE_PATH, 'w') as f:
            f.write("# Pusty plik konfiguracyjny\n")

    # This test_options should mirror the options in krypto_skaner_gui.py
    # for consistent behavior when running chart_window.py directly for testing.
    test_options = {
        "Binance (Spot)": {"id_ccxt": "binance", "type": "spot", "config_section":"binance_spot_config"},
        "Binance (Futures USDT-M)": {"id_ccxt": "binanceusdm", "type": "future", "config_section":"binance_futures_config"},
        "Bybit (Spot)": {"id_ccxt": "bybit", "type": "spot", "config_section":"bybit_spot_config"},
        "Bybit (Perpetual USDT)": {"id_ccxt": "bybit", "type": "swap", "config_section":"bybit_perp_config"},
    }

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE_PATH)
    # Ensure all default config sections for test_options exist if running standalone
    for _, details in test_options.items():
        section_name = details["config_section"]
        if not config.has_section(section_name):
            config.add_section(section_name)
            # Add placeholder API keys if they don't exist
            config.set(section_name, "api_key", "YOUR_API_KEY_PLACEHOLDER")
            config.set(section_name, "api_secret", "YOUR_SECRET_PLACEHOLDER")
            # Set default watchlist for standalone testing
            if section_name == "binance_spot_config":
                config.set(section_name, "watchlist_pairs", "BTC/USDT,ETH/USDT")
            elif section_name == "binance_futures_config":
                config.set(section_name, "watchlist_pairs", "BTC/USDT,ETH/USDT")
            elif section_name == "bybit_spot_config":
                config.set(section_name, "watchlist_pairs", "BTC/USDT,ETH/USDT")
            elif section_name == "bybit_perp_config":
                config.set(section_name, "watchlist_pairs", "BTC/USDT:USDT,ETH/USDT:USDT")

    if not config.has_section('chart_settings'):
        config.add_section('chart_settings')
        config.set('chart_settings', 'chart_0_timeframe', '1h')
        config.set('chart_settings', 'chart_1_timeframe', '4h')
        config.set('chart_settings', 'chart_2_timeframe', '1d')
        config.set('chart_settings', 'chart_3_timeframe', '15m')
        config.set('chart_settings', 'chart_4_timeframe', '1h')
        config.set('chart_settings', 'chart_5_timeframe', '1w')

    if not config.has_section('chart_indicator_settings'):
        config.add_section('chart_indicator_settings')
        config.set('chart_indicator_settings', 'indicator_type', 'Williams %%R') # Double % to escape for configparser

    with open(CONFIG_FILE_PATH, 'w') as configfile:
        config.write(configfile)


    window = MultiChartWindow(test_options, CONFIG_FILE_PATH)
    window.show()
    sys.exit(app.exec())
