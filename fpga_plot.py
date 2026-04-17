"""
fpga_plot.py

Real-time 3×3 diagnostic plot grid for FPGA feedback signals, using
**pyqtgraph** for GPU-accelerated rendering (~60 fps even at 10 ms poll).

Layout (rows = axes, columns = signal type):

          Sensor (AI)      Feedback (fb)     Total Feedback (tot_fb)
    X  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
       │  AI X plot   │  │  fb X plot   │  │ tot_fb X plot│
    Y  ├──────────────┤  ├──────────────┤  ├──────────────┤
       │  AI Y plot   │  │  fb Y plot   │  │ tot_fb Y plot│
    Z  ├──────────────┤  ├──────────────┤  ├──────────────┤
       │  AI Z plot   │  │  fb Z plot   │  │ tot_fb Z plot│
       └──────────────┘  └──────────────┘  └──────────────┘

PSD is on-demand only (Compute PSD button opens a matplotlib dialog).
"""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QSettings, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# Keep matplotlib only for the rare PSD dialog
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# ── pyqtgraph global config ──────────────────────────────────────────────

pg.setConfigOptions(antialias=False, useOpenGL=True)

# ── 3×3 grid definition ──────────────────────────────────────────────────

GRID_ROWS = ["X", "Y", "Z"]
GRID_COLS = [
    {"key": "sensor",   "label": "Sensor (AI)",       "template": "AI {a} plot",     "color": None},
    {"key": "feedback", "label": "Feedback",           "template": "fb {a} plot",     "color": None},
    {"key": "total",    "label": "Total Feedback",     "template": "tot_fb {a} plot", "color": None},
]

# Per-cell colours  (row, col) -> pen colour
_COLORS = {
    ("X", "sensor"):   "#9467bd",  ("X", "feedback"):  "#8c564b",  ("X", "total"):  "#7b4173",
    ("Y", "sensor"):   "#2ca02c",  ("Y", "feedback"):  "#d62728",  ("Y", "total"):  "#a63603",
    ("Z", "sensor"):   "#1f77b4",  ("Z", "feedback"):  "#ff7f0e",  ("Z", "total"):  "#e6550d",
}

# Flat list of register names the monitor must read (exported for fpga_core)
ALL_PLOT_NAMES: list[str] = []
for _axis in GRID_ROWS:
    for _col in GRID_COLS:
        _name = _col["template"].format(a=_axis)
        ALL_PLOT_NAMES.append(_name)


# ── Ring buffer (pre-allocated numpy) ─────────────────────────────────────

class _RingBuffer:
    """Fixed-size circular buffer backed by a numpy array."""

    __slots__ = ("_buf", "_cap", "_len", "_head")

    def __init__(self, capacity: int) -> None:
        self._buf = np.zeros(capacity, dtype=np.float64)
        self._cap = capacity
        self._len = 0
        self._head = 0       # next write position

    def append(self, value: float) -> None:
        self._buf[self._head] = value
        self._head = (self._head + 1) % self._cap
        if self._len < self._cap:
            self._len += 1

    def get_array(self) -> np.ndarray:
        """Return data in chronological order (oldest → newest)."""
        if self._len < self._cap:
            return self._buf[:self._len].copy()
        return np.concatenate((self._buf[self._head:], self._buf[:self._head]))

    def clear(self) -> None:
        self._buf[:] = 0.0
        self._len = 0
        self._head = 0

    def __len__(self) -> int:
        return self._len


# ── PSD dialog (on-demand, matplotlib) ────────────────────────────────────

_SHORT_COL = {"sensor": "Sensor", "feedback": "FB", "total": "Total FB"}

# Ordered list of (reg_name, axis, col_key) for all 9 channels
_ALL_CHANNELS: list[tuple[str, str, str]] = [
    (col["template"].format(a=axis), axis, col["key"])
    for axis in GRID_ROWS
    for col in GRID_COLS
]

_SETTINGS = QSettings("MooreLab", "usphere-FPGA")


class PSDDialog(QDialog):
    """On-demand PSD with per-channel visibility checkboxes."""

    def __init__(self, times: np.ndarray,
                 data: dict[str, np.ndarray], parent=None):
        super().__init__(parent)
        self.setWindowTitle("PSD")
        self.resize(900, 560)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self._times = times
        self._data = data
        self._checks: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)

        # ── top bar: window selector ──────────────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("Window:"))
        self._win_combo = QComboBox()
        self._win_combo.addItems(["hanning", "hamming", "blackman", "boxcar"])
        self._win_combo.currentIndexChanged.connect(self._recompute)
        top.addWidget(self._win_combo)
        top.addStretch()
        layout.addLayout(top)

        # ── channel selector (3×3 grid) ───────────────────────────────
        grp = QGroupBox("Channels")
        grid = QGridLayout(grp)
        grid.setHorizontalSpacing(16)
        # Header row
        for c, col_def in enumerate(GRID_COLS):
            lbl = QLabel(f"<b>{_SHORT_COL[col_def['key']]}</b>")
            lbl.setAlignment(Qt.AlignCenter)
            grid.addWidget(lbl, 0, c + 1)
        for r, axis in enumerate(GRID_ROWS):
            grid.addWidget(QLabel(f"<b>{axis}</b>"), r + 1, 0)
            for c, col_def in enumerate(GRID_COLS):
                reg_name = col_def["template"].format(a=axis)
                cb = QCheckBox()
                cb.setChecked(
                    _SETTINGS.value(f"psd_ch/{reg_name}", True, type=bool))
                cb.toggled.connect(self._on_check_toggled)
                grid.addWidget(cb, r + 1, c + 1, alignment=Qt.AlignHCenter)
                self._checks[reg_name] = cb
        layout.addWidget(grp)

        # ── matplotlib plot ───────────────────────────────────────────
        self._fig = Figure(figsize=(9, 4), tight_layout=True)
        self._ax = self._fig.add_subplot(111)
        self._canvas = FigureCanvas(self._fig)
        layout.addWidget(self._canvas, stretch=1)

        self._recompute()

    def _on_check_toggled(self) -> None:
        for reg_name, cb in self._checks.items():
            _SETTINGS.setValue(f"psd_ch/{reg_name}", cb.isChecked())
        self._recompute()

    def _recompute(self) -> None:
        ax = self._ax
        ax.clear()
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("PSD")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.grid(True, alpha=0.3, which="both")

        n = len(self._times)
        if n < 16:
            self._canvas.draw()
            return

        dt = (self._times[-1] - self._times[0]) / (n - 1)
        fs = 1.0 / dt if dt > 0 else 100.0

        win_name = self._win_combo.currentText()
        win_fn = {"hanning": np.hanning, "hamming": np.hamming,
                  "blackman": np.blackman}.get(win_name)
        win = win_fn(n) if win_fn else np.ones(n)
        win_norm = np.sum(win ** 2)
        if win_norm == 0:
            self._canvas.draw()
            return

        for reg_name, axis, col_key in _ALL_CHANNELS:
            cb = self._checks.get(reg_name)
            if cb is not None and not cb.isChecked():
                continue
            vs = self._data.get(reg_name)
            if vs is None or len(vs) != n:
                continue
            windowed = (vs - np.mean(vs)) * win
            fft_vals = np.fft.rfft(windowed)
            psd = (2.0 / (fs * win_norm)) * np.abs(fft_vals) ** 2
            freqs = np.fft.rfftfreq(n, d=1.0 / fs)
            color = _COLORS.get((axis, col_key))
            label = reg_name.replace(" plot", "")
            if len(freqs) > 1:
                ax.plot(freqs[1:], psd[1:], label=label,
                        color=color, linewidth=1)

        ax.legend(fontsize=7, loc="upper right")
        self._canvas.draw()


# ── Main embeddable widget ────────────────────────────────────────────────

class FPGAPlotWidget(QWidget):
    """3×3 pyqtgraph grid (X/Y/Z × sensor/feedback/total_feedback).

    Embed this directly in a tab.  Data is pushed via push_values();
    a QTimer redraws at ~60 fps by updating curve data in-place.
    """

    HISTORY_SEC = 5.0     # visible window
    BUF_CAPACITY = 5000   # ring-buffer size (enough for 5 s @ 1 kHz)
    REFRESH_MS = 16       # ~60 fps redraw timer

    def __init__(self, parent=None):
        super().__init__(parent)
        self._t0 = time.monotonic()
        self._dirty = False

        # Time ring buffer
        self._times = _RingBuffer(self.BUF_CAPACITY)

        # Per-cell state
        self._bufs: dict[str, _RingBuffer] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._plots: dict[str, pg.PlotItem] = {}

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._redraw)
        self._timer.start(self.REFRESH_MS)

    # ── Build ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)

        # Toolbar row
        top = QHBoxLayout()
        psd_btn = QPushButton("Compute PSD")
        psd_btn.clicked.connect(self._show_psd)
        top.addWidget(psd_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        top.addWidget(clear_btn)
        top.addStretch()
        layout.addLayout(top)

        # pyqtgraph graphics layout  (3 rows × 3 cols)
        self._gw = pg.GraphicsLayoutWidget()
        self._gw.setBackground("w")

        for r, axis in enumerate(GRID_ROWS):
            for c, col_def in enumerate(GRID_COLS):
                reg_name = col_def["template"].format(a=axis)
                color = _COLORS[(axis, col_def["key"])]

                title = f"{axis} {col_def['label']}"
                p = self._gw.addPlot(row=r, col=c, title=title)
                p.setLabel("bottom", "Time", units="s")
                p.showGrid(x=True, y=True, alpha=0.25)
                p.setDownsampling(auto=True, mode="peak")
                p.setClipToView(True)
                p.enableAutoRange(axis="y")
                p.setAutoVisible(y=True)

                pen = pg.mkPen(color=color, width=1)
                curve = p.plot(pen=pen)

                self._plots[reg_name] = p
                self._curves[reg_name] = curve
                self._bufs[reg_name] = _RingBuffer(self.BUF_CAPACITY)

        layout.addWidget(self._gw, stretch=1)

    # ── Public API ────────────────────────────────────────────────────

    def push_values(self, values: dict[str, float]) -> None:
        """Buffer one sample per channel. No drawing here."""
        t = time.monotonic() - self._t0
        self._times.append(t)
        for reg_name, buf in self._bufs.items():
            buf.append(values.get(reg_name, 0.0))
        self._dirty = True

    def clear(self) -> None:
        self._times.clear()
        for buf in self._bufs.values():
            buf.clear()
        self._t0 = time.monotonic()
        self._dirty = True

    # ── Timer-driven redraw ───────────────────────────────────────────

    def _redraw(self) -> None:
        if not self._dirty:
            return
        self._dirty = False

        t_arr = self._times.get_array()
        if len(t_arr) == 0:
            return

        t_now = t_arr[-1]
        t_min = t_now - self.HISTORY_SEC
        mask = t_arr >= t_min
        t_vis = t_arr[mask]

        for reg_name, curve in self._curves.items():
            v_arr = self._bufs[reg_name].get_array()
            curve.setData(t_vis, v_arr[mask])

        # Set x-range uniformly across all subplots
        for p in self._plots.values():
            p.setXRange(t_min, t_now, padding=0)

    # ── On-demand PSD ─────────────────────────────────────────────────

    def _show_psd(self) -> None:
        times = self._times.get_array()
        if len(times) < 16:
            return
        data = {name: buf.get_array() for name, buf in self._bufs.items()
                if len(buf) >= 16}
        dlg = PSDDialog(times, data, parent=self)
        dlg.show()


# ── Standalone test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication, QMainWindow

    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("FPGA Plot — Standalone")
    w = FPGAPlotWidget()
    win.setCentralWidget(w)
    win.resize(1200, 700)
    win.show()

    # Feed dummy sine data for testing
    _counter = [0]
    def _feed():
        _counter[0] += 1
        t = _counter[0] * 0.01
        vals = {}
        for axis in GRID_ROWS:
            for col in GRID_COLS:
                name = col["template"].format(a=axis)
                vals[name] = np.sin(2 * np.pi * t + hash(name) % 7)
        w.push_values(vals)

    timer = QTimer()
    timer.timeout.connect(_feed)
    timer.start(10)

    sys.exit(app.exec_())
