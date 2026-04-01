"""
fpga_plot.py

Real-time plotting widget for FPGA sensor/feedback signals.
Provides two views — identical to the LabVIEW "Bead" (time domain) and
"Plot 0" (frequency domain / PSD) tabs:

  * **Time trace**  — scrolling strip-chart of selected indicator channels.
  * **PSD**         — live power spectral density computed from the most
                      recent history window, displayed on a log-log scale.

Can be embedded as a tab in fpga_gui or run standalone:
    python fpga_plot.py
"""

from __future__ import annotations

import collections
import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import numpy as np
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
    NavigationToolbar2QT as NavigationToolbar,
)
from matplotlib.figure import Figure


# Indicators worth plotting — sensor readings and feedback outputs
PLOT_CHANNELS: list[dict] = [
    {"name": "AI Z plot",              "label": "Z sensor",           "color": "#1f77b4"},
    {"name": "AI Z before chamber plot", "label": "Z before chamber", "color": "#aec7e8"},
    {"name": "fb Z plot",              "label": "Z feedback",         "color": "#ff7f0e"},
    {"name": "fb Z before chamber plot", "label": "Z fb before",     "color": "#ffbb78"},
    {"name": "tot_fb Z plot",          "label": "Z total feedback",   "color": "#e6550d"},
    {"name": "AI Y plot",              "label": "Y sensor",           "color": "#2ca02c"},
    {"name": "AI Y before chamber plot", "label": "Y before chamber", "color": "#98df8a"},
    {"name": "fb Y plot",              "label": "Y feedback",         "color": "#d62728"},
    {"name": "fb Y before chamber plot", "label": "Y fb before",     "color": "#ff9896"},
    {"name": "tot_fb Y plot",          "label": "Y total feedback",   "color": "#a63603"},
    {"name": "AI X plot",              "label": "X sensor",           "color": "#9467bd"},
    {"name": "AI X before chamber plot", "label": "X before chamber", "color": "#c5b0d5"},
    {"name": "fb X plot",              "label": "X feedback",         "color": "#8c564b"},
    {"name": "fb X before chamber plot", "label": "X fb before",     "color": "#c49c94"},
    {"name": "tot_fb X plot",          "label": "X total feedback",   "color": "#7b4173"},
    {"name": "accum out z1",           "label": "Accum Z1",           "color": "#17becf"},
    {"name": "accum out z2",           "label": "Accum Z2",           "color": "#bcbd22"},
]


class FPGAPlotWidget(QWidget):
    """Real-time time-domain + PSD widget for FPGA indicators."""

    def __init__(self, parent=None, max_points: int = 500,
                 sample_rate: float = 5.0):
        """
        Parameters
        ----------
        max_points  : ring-buffer length (number of poll samples stored)
        sample_rate : approximate samples per second (= 1000 / poll_interval_ms).
                      Used only for the PSD frequency axis.
        """
        super().__init__(parent)
        self._max_points = max_points
        self._sample_rate = sample_rate
        self._t0 = time.monotonic()

        # Ring buffers: {channel_name: deque of (t, value)}
        self._buffers: dict[str, collections.deque] = {
            ch["name"]: collections.deque(maxlen=max_points)
            for ch in PLOT_CHANNELS
        }
        self._checkboxes: dict[str, QCheckBox] = {}
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)

        # --- Left: channel checkboxes + settings ---
        left = QVBoxLayout()
        grp = QGroupBox("Channels")
        grid = QGridLayout()
        for i, ch in enumerate(PLOT_CHANNELS):
            cb = QCheckBox(ch["label"])
            cb.setChecked(i < 3)  # default: first 3 checked
            cb.stateChanged.connect(self._replot)
            self._checkboxes[ch["name"]] = cb
            grid.addWidget(cb, i, 0)
        grp.setLayout(grid)
        left.addWidget(grp)

        # History length
        hist_row = QHBoxLayout()
        hist_row.addWidget(QLabel("History:"))
        self._hist_spin = QSpinBox()
        self._hist_spin.setRange(50, 10000)
        self._hist_spin.setValue(self._max_points)
        self._hist_spin.setSuffix(" pts")
        self._hist_spin.valueChanged.connect(self._resize_buffers)
        hist_row.addWidget(self._hist_spin)
        left.addLayout(hist_row)

        # PSD window type
        win_row = QHBoxLayout()
        win_row.addWidget(QLabel("Window:"))
        self._window_combo = QComboBox()
        self._window_combo.addItems(["hanning", "hamming", "blackman", "boxcar"])
        self._window_combo.currentIndexChanged.connect(self._replot)
        win_row.addWidget(self._window_combo)
        left.addLayout(win_row)

        left.addStretch()
        layout.addLayout(left, 1)

        # --- Right: tabbed plots ---
        self._tabs = QTabWidget()

        # Time domain tab
        time_widget = QWidget()
        time_layout = QVBoxLayout(time_widget)
        self._time_fig = Figure(figsize=(8, 4), tight_layout=True)
        self._time_ax = self._time_fig.add_subplot(111)
        self._time_ax.set_xlabel("Time (s)")
        self._time_ax.set_ylabel("Value")
        self._time_ax.grid(True, alpha=0.3)
        self._time_canvas = FigureCanvas(self._time_fig)
        self._time_toolbar = NavigationToolbar(self._time_canvas, self)
        time_layout.addWidget(self._time_toolbar)
        time_layout.addWidget(self._time_canvas)
        self._tabs.addTab(time_widget, "Bead")

        # PSD tab
        psd_widget = QWidget()
        psd_layout = QVBoxLayout(psd_widget)
        self._psd_fig = Figure(figsize=(8, 4), tight_layout=True)
        self._psd_ax = self._psd_fig.add_subplot(111)
        self._psd_ax.set_xlabel("Frequency (Hz)")
        self._psd_ax.set_ylabel("PSD")
        self._psd_ax.set_xscale("log")
        self._psd_ax.set_yscale("log")
        self._psd_ax.grid(True, alpha=0.3, which="both")
        self._psd_canvas = FigureCanvas(self._psd_fig)
        self._psd_toolbar = NavigationToolbar(self._psd_canvas, self)
        psd_layout.addWidget(self._psd_toolbar)
        psd_layout.addWidget(self._psd_canvas)
        self._tabs.addTab(psd_widget, "Plot 0")

        right = QVBoxLayout()
        right.addWidget(self._tabs)
        layout.addLayout(right, 4)

    # ------------------------------------------------------------------
    # Public API — called by the GUI on each monitor poll
    # ------------------------------------------------------------------

    def push_values(self, values: dict[str, float]) -> None:
        """Append a new sample for each tracked channel and redraw."""
        t = time.monotonic() - self._t0
        for ch in PLOT_CHANNELS:
            name = ch["name"]
            if name in values:
                self._buffers[name].append((t, values[name]))
        self._replot()

    def clear(self) -> None:
        """Clear all history buffers."""
        for buf in self._buffers.values():
            buf.clear()
        self._t0 = time.monotonic()
        self._replot()

    def set_sample_rate(self, rate: float) -> None:
        """Update the assumed sample rate (for PSD freq axis)."""
        self._sample_rate = rate

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resize_buffers(self, new_max: int) -> None:
        self._max_points = new_max
        for name in list(self._buffers):
            old = self._buffers[name]
            new_buf: collections.deque = collections.deque(old, maxlen=new_max)
            self._buffers[name] = new_buf

    def _active_channels(self) -> list[dict]:
        """Return channels whose checkbox is checked and have data."""
        active = []
        for ch in PLOT_CHANNELS:
            cb = self._checkboxes.get(ch["name"])
            if cb is not None and cb.isChecked() and len(self._buffers[ch["name"]]) >= 2:
                active.append(ch)
        return active

    def _replot(self, *_args) -> None:
        self._replot_time()
        self._replot_psd()

    def _replot_time(self) -> None:
        ax = self._time_ax
        ax.clear()
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Value")
        ax.grid(True, alpha=0.3)

        for ch in self._active_channels():
            buf = self._buffers[ch["name"]]
            ts = np.array([p[0] for p in buf])
            vs = np.array([p[1] for p in buf])
            ax.plot(ts, vs, label=ch["label"], color=ch["color"], linewidth=1)

        if self._active_channels():
            ax.legend(fontsize=7, loc="upper left")
        self._time_canvas.draw_idle()

    def _replot_psd(self) -> None:
        ax = self._psd_ax
        ax.clear()
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("PSD")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.grid(True, alpha=0.3, which="both")

        active = self._active_channels()
        if not active:
            self._psd_canvas.draw_idle()
            return

        # Determine effective sample rate from timestamps
        for ch in active:
            buf = self._buffers[ch["name"]]
            if len(buf) < 8:
                continue
            vs = np.array([p[1] for p in buf])
            ts = np.array([p[0] for p in buf])
            n = len(vs)

            # Estimate sample rate from actual timestamps
            dt = (ts[-1] - ts[0]) / (n - 1) if n > 1 else 1.0 / self._sample_rate
            fs = 1.0 / dt if dt > 0 else self._sample_rate

            # Apply window
            win_name = self._window_combo.currentText()
            if win_name == "hanning":
                win = np.hanning(n)
            elif win_name == "hamming":
                win = np.hamming(n)
            elif win_name == "blackman":
                win = np.blackman(n)
            else:
                win = np.ones(n)

            # Avoid divide-by-zero
            win_norm = np.sum(win ** 2)
            if win_norm == 0:
                continue

            # Compute one-sided PSD via FFT
            windowed = (vs - np.mean(vs)) * win
            fft_vals = np.fft.rfft(windowed)
            psd = (2.0 / (fs * win_norm)) * np.abs(fft_vals) ** 2
            freqs = np.fft.rfftfreq(n, d=1.0 / fs)

            # Skip DC
            if len(freqs) > 1:
                ax.plot(freqs[1:], psd[1:],
                        label=ch["label"], color=ch["color"], linewidth=1)

        if active:
            ax.legend(fontsize=7, loc="upper right")
        self._psd_canvas.draw_idle()


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("FPGA Plot — Standalone")
    win.setCentralWidget(FPGAPlotWidget())
    win.resize(1000, 500)
    win.show()
    sys.exit(app.exec_())
