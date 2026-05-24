#!/usr/bin/env python3
"""Netemango — single-file PySide6 GUI for Linux `tc`/`netem` network impairment.

Features
--------
* Apply delay, jitter, packet loss, and packet corruption via ``tc qdisc``.
* Live throughput chart (pyqtgraph, rolling 60 s) read straight from
  ``/sys/class/net/<iface>/statistics``.
* Wireless quality panel using ``iw dev`` with a clean "not wireless" banner.
* Verify tab that runs ``ping`` through ``QProcess`` and graphs measured
  RTT/jitter/loss so you can confirm the kernel honours your netem rule.
* Live command-preview pane showing the exact ``tc`` invocation being built.
* Rotating file log at ``~/.local/share/delaymorph/app.log``.

This module is intentionally a single file so it can be dropped into a venv
and run directly. Linux only.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import re
import shutil
import signal
import statistics
import subprocess
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import psutil
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, QProcess, QSize, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Netmango"
APP_SLUG = "delaymorph"
LOG_PATH = Path.home() / ".local" / "share" / APP_SLUG / "app.log"
STYLE_QSS = Path(__file__).resolve().parent.parent / "style.qss"
ICON_PATH = Path(__file__).resolve().parent.parent / "assest" / "Netmango.png"

log = logging.getLogger(APP_SLUG)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    log.setLevel(level)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")

    file_h = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=3)
    file_h.setFormatter(fmt)
    log.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stderr)
    stream_h.setFormatter(fmt)
    log.addHandler(stream_h)

    log.debug("Logging initialised (verbose=%s) → %s", verbose, LOG_PATH)


# --------------------------------------------------------------------------- #
# sudo helper
# --------------------------------------------------------------------------- #
class SudoError(RuntimeError):
    """Raised when a sudo command fails. ``stderr`` is the captured text."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def sudo_run(argv: list[str]) -> str:
    """Run ``argv`` as root. Re-auths interactively if the cached ticket expired.

    Returns stdout. Raises :class:`SudoError` with the real stderr on failure.
    """
    cmd = ["sudo", "-n", *argv]
    log.debug("sudo_run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and "password is required" in (proc.stderr or "").lower():
        log.info("sudo ticket expired — re-authenticating on TTY")
        reauth = subprocess.run(["sudo", "-v"])
        if reauth.returncode != 0:
            raise SudoError("sudo authentication failed", proc.stderr)
        proc = subprocess.run(["sudo", "-n", *argv], capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("sudo command failed: %s\nstderr=%s", " ".join(argv),
                  proc.stderr.strip())
        raise SudoError(f"command failed: {' '.join(argv)}", proc.stderr.strip())
    return proc.stdout


# --------------------------------------------------------------------------- #
# Interface helpers
# --------------------------------------------------------------------------- #
def list_interfaces() -> list[str]:
    """Return non-loopback interface names that exist on the host."""
    names = [n for n in psutil.net_if_addrs().keys() if n != "lo"]
    names.sort()
    return names


def default_interface(interfaces: list[str]) -> Optional[str]:
    """Pick the most sensible default interface, preferring Wi-Fi.

    Strategy:
      1. First interface that sysfs marks as wireless.
      2. Otherwise, first interface whose name looks like Wi-Fi
         (``wl*`` from predictable names or legacy ``wlan*``).
      3. Otherwise, the last entry — on most Linux hosts ``ifconfig``/``ip``
         lists Wi-Fi after Ethernet, matching the user's mental model.
    """
    if not interfaces:
        return None
    for n in interfaces:
        if is_wireless(n):
            return n
    for n in interfaces:
        if n.startswith(("wl", "wlan", "wlp")):
            return n
    return interfaces[-1]


def read_iface_bytes(iface: str) -> tuple[int, int]:
    """Return ``(rx_bytes, tx_bytes)`` straight from sysfs."""
    base = Path(f"/sys/class/net/{iface}/statistics")
    rx = int((base / "rx_bytes").read_text().strip())
    tx = int((base / "tx_bytes").read_text().strip())
    return rx, tx


def is_wireless(iface: str) -> bool:
    return Path(f"/sys/class/net/{iface}/wireless").exists()


def current_tc_qdisc(iface: str) -> str:
    """First line of ``tc qdisc show dev <iface>`` or empty string."""
    tc = shutil.which("tc") or "/sbin/tc"
    try:
        out = subprocess.run(
            [tc, "qdisc", "show", "dev", iface],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.splitlines()[0] if out else ""


def has_netem(iface: str) -> bool:
    return "netem" in current_tc_qdisc(iface)


def fmt_rate(bps: float) -> str:
    """Format a bits-per-second rate with auto units."""
    if bps < 1_000:
        return f"{bps:.0f} bit/s"
    for unit, factor in (("kbit/s", 1e3), ("Mbit/s", 1e6), ("Gbit/s", 1e9)):
        if bps < factor * 1_000:
            return f"{bps / factor:.2f} {unit}"
    return f"{bps / 1e9:.2f} Gbit/s"


# --------------------------------------------------------------------------- #
# Toast (non-modal transient label)
# --------------------------------------------------------------------------- #
class Toast(QLabel):
    def __init__(self, parent: QWidget, text: str, msec: int = 3500):
        super().__init__(text, parent)
        self.setObjectName("toast")
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "background: rgba(40,40,40,220); color: #fff; padding: 8px 14px; "
            "border-radius: 6px; font-weight: 600;"
        )
        self.adjustSize()
        p = self.parent()
        if isinstance(p, QWidget):
            geo = p.rect()
            x = geo.center().x() - self.width() // 2
            y = geo.bottom() - self.height() - 30
            self.move(max(10, x), max(10, y))
        self.show()
        QTimer.singleShot(msec, self.deleteLater)


# --------------------------------------------------------------------------- #
# Controls panel (left dock)
# --------------------------------------------------------------------------- #
INFO_TEXT = {
    "delay": (
        "<b>Delay</b><br>Adds a fixed extra latency to every outbound packet "
        "on the chosen interface, on top of whatever the physical link already "
        "has. Implemented as <code>netem delay Nms</code>."
    ),
    "jitter": (
        "<b>Jitter</b><br>Adds <i>variable</i> delay: a base delay plus a "
        "random component, optionally correlated. Implemented as "
        "<code>netem delay BASEms VARms CORR%</code>. Mutually exclusive with "
        "the fixed delay above — pick one or the other."
    ),
    "loss": (
        "<b>Packet Loss</b><br>Drops a percentage of outbound packets at "
        "random before they ever hit the wire. Implemented as "
        "<code>netem loss P%</code>."
    ),
    "corrupt": (
        "<b>Packet Corruption</b><br>Flips a single random bit in the chosen "
        "percentage of packets. The kernel checksum then fails, so the receiver "
        "drops them — which is why <i>corrupt N%</i> usually shows up on the "
        "Verify tab as roughly <i>N%</i> loss. Implemented as "
        "<code>netem corrupt P%</code>."
    ),
}


def info_button(parent: QWidget, key: str) -> QToolButton:
    btn = QToolButton(parent)
    btn.setText("?")
    btn.setToolTip("What does this do?")
    btn.setFixedSize(QSize(22, 22))
    btn.clicked.connect(
        lambda: QMessageBox.information(parent, "About this option", INFO_TEXT[key])
    )
    return btn


class ControlsPanel(QWidget):
    """Left-hand controls. Emits :attr:`changed` whenever anything would
    affect the generated tc command."""

    changed = Signal()
    apply_requested = Signal()
    clear_requested = Signal()
    interface_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # --- interface picker ----------------------------------------------
        iface_box = QGroupBox("Interface")
        iface_form = QFormLayout(iface_box)
        self.iface_combo = QComboBox()
        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("↻")
        self.refresh_btn.setToolTip("Reload interface list")
        self.refresh_btn.clicked.connect(self.populate_interfaces)
        iface_row = QHBoxLayout()
        iface_row.addWidget(self.iface_combo, 1)
        iface_row.addWidget(self.refresh_btn)
        iface_form.addRow("Device:", _wrap(iface_row))
        layout.addWidget(iface_box)

        # --- delay ---------------------------------------------------------
        self.delay_chk = QCheckBox("Enable fixed delay")
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 10_000)
        self.delay_spin.setSuffix(" ms")
        layout.addWidget(self._impairment_box("Delay", "delay", self.delay_chk, [
            ("Latency", self.delay_spin),
        ]))

        # --- jitter --------------------------------------------------------
        self.jitter_chk = QCheckBox("Enable jitter")
        self.base_delay_spin = QSpinBox()
        self.base_delay_spin.setRange(0, 10_000)
        self.base_delay_spin.setSuffix(" ms")
        self.var_delay_spin = QSpinBox()
        self.var_delay_spin.setRange(0, 10_000)
        self.var_delay_spin.setSuffix(" ms")
        self.jitter_corr_spin = QDoubleSpinBox()
        self.jitter_corr_spin.setRange(0.0, 100.0)
        self.jitter_corr_spin.setDecimals(1)
        self.jitter_corr_spin.setSuffix(" %")
        layout.addWidget(self._impairment_box("Jitter", "jitter", self.jitter_chk, [
            ("Base", self.base_delay_spin),
            ("Variance", self.var_delay_spin),
            ("Correlation", self.jitter_corr_spin),
        ]))

        # --- loss ----------------------------------------------------------
        self.loss_chk = QCheckBox("Enable packet loss")
        self.loss_spin = QDoubleSpinBox()
        self.loss_spin.setRange(0.0, 100.0)
        self.loss_spin.setDecimals(2)
        self.loss_spin.setSuffix(" %")
        layout.addWidget(self._impairment_box("Loss", "loss", self.loss_chk, [
            ("Drop rate", self.loss_spin),
        ]))

        # --- corruption ----------------------------------------------------
        self.corrupt_chk = QCheckBox("Enable packet corruption")
        self.corrupt_spin = QDoubleSpinBox()
        self.corrupt_spin.setRange(0.0, 100.0)
        self.corrupt_spin.setDecimals(2)
        self.corrupt_spin.setSuffix(" %")
        layout.addWidget(self._impairment_box(
            "Corruption", "corrupt", self.corrupt_chk,
            [("Corrupt rate", self.corrupt_spin)],
        ))

        # --- buttons -------------------------------------------------------
        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setObjectName("primaryBtn")
        self.apply_btn.clicked.connect(self.apply_requested.emit)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_requested.emit)
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.clear_btn)
        layout.addLayout(btn_row)
        layout.addStretch(1)

        # --- wire change signal --------------------------------------------
        for w in (self.delay_chk, self.jitter_chk, self.loss_chk, self.corrupt_chk):
            w.toggled.connect(self._on_changed)
        for w in (
            self.delay_spin, self.base_delay_spin, self.var_delay_spin,
            self.jitter_corr_spin, self.loss_spin, self.corrupt_spin,
        ):
            w.valueChanged.connect(self._on_changed)
        self.iface_combo.currentTextChanged.connect(self._on_changed)
        self.iface_combo.currentTextChanged.connect(self.interface_changed)

        self.populate_interfaces()

    # ---- helpers ----------------------------------------------------------
    def _impairment_box(
        self, title: str, key: str, chk: QCheckBox, fields: list[tuple[str, QWidget]]
    ) -> QGroupBox:
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        header = QHBoxLayout()
        header.addWidget(chk, 1)
        header.addWidget(info_button(self, key))
        v.addLayout(header)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        for label, widget in fields:
            widget.setEnabled(False)
            form.addRow(label + ":", widget)
        v.addLayout(form)
        chk.toggled.connect(
            lambda on, ws=[w for _, w in fields]: [w.setEnabled(on) for w in ws]
        )
        return box

    def populate_interfaces(self) -> None:
        current = self.iface_combo.currentText()
        ifaces = list_interfaces()
        self.iface_combo.blockSignals(True)
        self.iface_combo.clear()
        self.iface_combo.addItems(ifaces)
        # Restore prior selection if any; otherwise default to Wi-Fi.
        target = current if current else (default_interface(ifaces) or "")
        if target:
            idx = self.iface_combo.findText(target)
            if idx >= 0:
                self.iface_combo.setCurrentIndex(idx)
        self.iface_combo.blockSignals(False)
        self._on_changed()

    def _on_changed(self) -> None:
        self.changed.emit()

    def current_interface(self) -> str:
        return self.iface_combo.currentText().strip()

    def validate(self) -> Optional[str]:
        """Return a human-readable error string, or None when settings are sane."""
        if self.delay_chk.isChecked() and self.jitter_chk.isChecked():
            return (
                "Fixed delay and jitter both add a `delay` clause and conflict.\n"
                "Pick one — jitter alone already lets you set a base latency."
            )
        if not any(c.isChecked() for c in (
            self.delay_chk, self.jitter_chk, self.loss_chk, self.corrupt_chk
        )):
            return "Enable at least one impairment before applying."
        if not self.current_interface():
            return "Select a network interface first."
        return None

    def build_tc_command(self) -> list[str]:
        """Return the full tc invocation as argv (without leading sudo)."""
        iface = self.current_interface() or "<iface>"
        cmd = ["tc", "qdisc", "add", "dev", iface, "root", "netem"]
        if self.delay_chk.isChecked():
            cmd += ["delay", f"{self.delay_spin.value()}ms"]
        elif self.jitter_chk.isChecked():
            cmd += [
                "delay",
                f"{self.base_delay_spin.value()}ms",
                f"{self.var_delay_spin.value()}ms",
                f"{self.jitter_corr_spin.value():g}%",
            ]
        if self.loss_chk.isChecked():
            cmd += ["loss", f"{self.loss_spin.value():g}%"]
        if self.corrupt_chk.isChecked():
            cmd += ["corrupt", f"{self.corrupt_spin.value():g}%"]
        return cmd

    def reset(self) -> None:
        for c in (self.delay_chk, self.jitter_chk, self.loss_chk, self.corrupt_chk):
            c.setChecked(False)
        for s in (
            self.delay_spin, self.base_delay_spin, self.var_delay_spin,
            self.jitter_corr_spin, self.loss_spin, self.corrupt_spin,
        ):
            s.setValue(0)


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w


# --------------------------------------------------------------------------- #
# Throughput tab (pyqtgraph)
# --------------------------------------------------------------------------- #
class ThroughputTab(QWidget):
    WINDOW_S = 300  # rolling history: last 5 minutes

    def __init__(self, get_iface):
        super().__init__()
        self._get_iface = get_iface
        self._t: deque[int] = deque(maxlen=self.WINDOW_S)
        self._rx: deque[float] = deque(maxlen=self.WINDOW_S)
        self._tx: deque[float] = deque(maxlen=self.WINDOW_S)
        self._last: Optional[tuple[int, int]] = None
        self._elapsed = 0

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        reset_window_btn = QPushButton("Reset Window")
        reset_window_btn.setToolTip(
            "Re-frame the chart to show all retained data (does NOT erase samples)."
        )
        reset_window_btn.clicked.connect(self._reset_window)
        reset_data_btn = QPushButton("Reset Data")
        reset_data_btn.setToolTip(
            "Clear all collected samples and restart the trace at t = 0."
        )
        reset_data_btn.clicked.connect(self._reset_data)
        self.readout = QLabel("Downloading: —    Uploading: —")
        self.readout.setStyleSheet("font-family: monospace; font-size: 13px;")
        controls.addWidget(reset_window_btn)
        controls.addWidget(reset_data_btn)
        controls.addStretch(1)
        controls.addWidget(self.readout)
        layout.addLayout(controls)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#1e1f22")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Throughput", units="bit/s")
        self.plot.setLabel("bottom", "Time", units="s")
        # Anchor the x-axis at t = 0 on launch; pyqtgraph will then auto-range
        # as new samples arrive (and stop auto-ranging if the user pans/zooms).
        self.plot.setXRange(0, self.WINDOW_S, padding=0)
        self.plot.addLegend()
        self.rx_curve = self.plot.plot(pen=pg.mkPen("#5DADE2", width=2), name="Downloading")
        self.tx_curve = self.plot.plot(pen=pg.mkPen("#F39C12", width=2), name="Uploading")
        layout.addWidget(self.plot, 1)

        # Single, persistent timer — connect once.
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _reset_window(self) -> None:
        """Snap the view back to fit the retained data; keep samples intact."""
        self.plot.enableAutoRange(axis="xy", enable=True)

    def _reset_data(self) -> None:
        """Erase all collected samples and restart at t = 0."""
        self._t.clear()
        self._rx.clear()
        self._tx.clear()
        self._last = None
        self._elapsed = 0
        self.rx_curve.setData([], [])
        self.tx_curve.setData([], [])
        self.readout.setText("Downloading: —    Uploading: —")
        self.plot.setXRange(0, self.WINDOW_S, padding=0)
        self.plot.enableAutoRange(axis="xy", enable=True)

    def _tick(self) -> None:
        iface = self._get_iface()
        if not iface:
            return
        try:
            rx, tx = read_iface_bytes(iface)
        except (FileNotFoundError, OSError) as exc:
            log.debug("throughput read failed for %s: %s", iface, exc)
            return
        self._elapsed += 1
        if self._last is None:
            self._last = (rx, tx)
            return
        rx_rate = max(0, rx - self._last[0]) * 8  # bytes/s → bits/s
        tx_rate = max(0, tx - self._last[1]) * 8
        self._last = (rx, tx)
        self._t.append(self._elapsed)
        self._rx.append(rx_rate)
        self._tx.append(tx_rate)
        self.rx_curve.setData(list(self._t), list(self._rx))
        self.tx_curve.setData(list(self._t), list(self._tx))
        self.readout.setText(
            f"Downloading: {fmt_rate(rx_rate):>12}    Uploading: {fmt_rate(tx_rate):>12}"
        )


# --------------------------------------------------------------------------- #
# Verify tab (ping)
# --------------------------------------------------------------------------- #
PING_RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


class VerifyTab(QWidget):
    """Run ``ping`` with QProcess and graph measured RTT / jitter / loss."""

    WINDOW = 300  # rolling history: last 5 minutes of samples

    def __init__(self, get_configured_summary):
        super().__init__()
        self._get_configured_summary = get_configured_summary
        self.proc: Optional[QProcess] = None
        self.sent = 0
        self.recv = 0
        self.rtts: deque[float] = deque(maxlen=self.WINDOW)
        self.t: deque[int] = deque(maxlen=self.WINDOW)
        self._elapsed = 0

        layout = QVBoxLayout(self)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Target:"))
        self.target_edit = QLineEdit("1.1.1.1")
        self.target_edit.setMaximumWidth(180)
        ctl.addWidget(self.target_edit)
        ctl.addWidget(QLabel("Interval:"))
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.2, 5.0)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setValue(1.0)
        self.interval_spin.setSuffix(" s")
        ctl.addWidget(self.interval_spin)
        self.start_btn = QPushButton("Start ping")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.clicked.connect(self.start)
        ctl.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        ctl.addWidget(self.stop_btn)
        ctl.addStretch(1)
        self.reset_window_btn = QPushButton("Reset Window")
        self.reset_window_btn.setToolTip(
            "Re-frame the chart to show all retained samples (does NOT erase data)."
        )
        self.reset_window_btn.clicked.connect(self._reset_window)
        ctl.addWidget(self.reset_window_btn)
        self.reset_data_btn = QPushButton("Reset Data")
        self.reset_data_btn.setToolTip(
            "Erase all collected samples (RTT, loss, log) and restart at t = 0."
        )
        self.reset_data_btn.clicked.connect(self._reset)
        ctl.addWidget(self.reset_data_btn)
        layout.addLayout(ctl)

        readout = QGroupBox("Configured vs measured")
        grid = QFormLayout(readout)
        self.configured_lbl = QLabel("—")
        self.configured_lbl.setStyleSheet("font-family: monospace;")
        self.rtt_lbl = QLabel("—")
        self.jitter_lbl = QLabel("—")
        self.loss_lbl = QLabel("—")
        for lbl in (self.rtt_lbl, self.jitter_lbl, self.loss_lbl):
            lbl.setStyleSheet("font-family: monospace; font-size: 13px;")
        grid.addRow("Configured impairment:", self.configured_lbl)
        grid.addRow("Measured avg RTT:", self.rtt_lbl)
        grid.addRow("Measured jitter (stdev):", self.jitter_lbl)
        grid.addRow("Measured loss:", self.loss_lbl)
        layout.addWidget(readout)

        self.plot = pg.PlotWidget()
        self.plot.setBackground("#1e1f22")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        # RTT is always in milliseconds — disable SI auto-prefix so the axis
        # never silently turns into e.g. "kms" when RTTs grow large.
        self.plot.setLabel("left", "RTT (ms)")
        left_axis = self.plot.getAxis("left")
        left_axis.enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "Ping #")
        # Anchor x at 0 on launch; auto-range takes over as samples arrive.
        self.plot.setXRange(0, self.WINDOW, padding=0)
        self.curve = self.plot.plot(pen=pg.mkPen("#2ecc71", width=2))
        layout.addWidget(self.plot, 1)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setMaximumHeight(120)
        self.log_view.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.log_view)

        # Periodic readout refresh (decoupled from ping output rate).
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(500)
        self.ui_timer.timeout.connect(self._refresh_readout)
        self.ui_timer.start()

    def start(self) -> None:
        if self.proc is not None:
            return
        target = self.target_edit.text().strip()
        if not target:
            return
        self._reset()
        ping = shutil.which("ping") or "/bin/ping"
        argv = [ping, "-O", "-i", f"{self.interval_spin.value():g}", target]
        log.info("starting verify ping: %s", " ".join(argv))
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.start(argv[0], argv[1:])
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def stop(self) -> None:
        if self.proc is None:
            return
        log.info("stopping verify ping")
        self.proc.terminate()
        if not self.proc.waitForFinished(1500):
            self.proc.kill()
            self.proc.waitForFinished(500)

    def _on_finished(self, *_args) -> None:
        self.proc = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _reset(self) -> None:
        self.sent = 0
        self.recv = 0
        self.rtts.clear()
        self.t.clear()
        self._elapsed = 0
        self.curve.setData([], [])
        self.log_view.clear()
        self.plot.setXRange(0, self.WINDOW, padding=0)
        self.plot.enableAutoRange(axis="xy", enable=True)

    def _reset_window(self) -> None:
        """Snap the view back to fit the retained data; keep samples intact."""
        self.plot.enableAutoRange(axis="xy", enable=True)

    def _on_output(self) -> None:
        assert self.proc is not None
        chunk = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        for raw in chunk.splitlines():
            line = raw.strip()
            if not line:
                continue
            self.log_view.appendPlainText(line)
            if line.startswith("no answer yet for icmp_seq="):
                self.sent += 1
                continue
            m = PING_RTT_RE.search(line)
            if m:
                self.sent += 1
                self.recv += 1
                rtt = float(m.group(1))
                self._elapsed += 1
                self.t.append(self._elapsed)
                self.rtts.append(rtt)
                self.curve.setData(list(self.t), list(self.rtts))

    def _refresh_readout(self) -> None:
        self.configured_lbl.setText(self._get_configured_summary() or "none")
        if self.rtts:
            avg = sum(self.rtts) / len(self.rtts)
            jit = statistics.pstdev(self.rtts) if len(self.rtts) > 1 else 0.0
            self.rtt_lbl.setText(f"{avg:6.2f} ms  (last {len(self.rtts)} samples)")
            self.jitter_lbl.setText(f"{jit:6.2f} ms")
        else:
            self.rtt_lbl.setText("—")
            self.jitter_lbl.setText("—")
        if self.sent > 0:
            loss = 100.0 * (self.sent - self.recv) / self.sent
            self.loss_lbl.setText(f"{loss:5.1f}%   ({self.recv}/{self.sent} replies)")
        else:
            self.loss_lbl.setText("—")

    def shutdown(self) -> None:
        """Ensure no zombie ping process when the app closes."""
        if self.proc is None:
            return
        try:
            self.proc.readyReadStandardOutput.disconnect(self._on_output)
            self.proc.finished.disconnect(self._on_finished)
        except (TypeError, RuntimeError):
            pass
        self.proc.terminate()
        if not self.proc.waitForFinished(1000):
            self.proc.kill()
            self.proc.waitForFinished(500)
        self.proc = None


# --------------------------------------------------------------------------- #
# Command-preview tab
# --------------------------------------------------------------------------- #
class CommandPreviewTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        info = QLabel(
            "Live preview of the exact <code>tc</code> command that will be run "
            "when you click <b>Apply</b>. Updates as you edit the controls on the left."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family: monospace; font-size: 13px;")
        layout.addWidget(self.view, 1)

    def set_command(self, argv: list[str], error: Optional[str] = None) -> None:
        if error:
            self.view.setPlainText(
                f"# {error}\n# (Apply is disabled until this is resolved.)"
            )
            return
        self.view.setPlainText("sudo " + " ".join(argv))


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Window title is rendered by the OS title bar (often centered).
        # The in-app header below already shows the brand, so leave the OS
        # title empty to avoid the duplicate "Netmango" up top.
        self.setWindowTitle("")
        self.resize(1180, 740)

        self.controls = ControlsPanel()
        dock = QDockWidget("Controls", self)
        dock.setObjectName("controlsDock")
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(self.controls)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # Every interface we have ever touched with `tc qdisc add` in this
        # session. Used to guarantee cleanup on close/crash even if the user
        # switched interfaces after applying a rule.
        self._touched_ifaces: set[str] = set()

        self.tabs = QTabWidget()
        self.throughput_tab = ThroughputTab(self.controls.current_interface)
        self.verify_tab = VerifyTab(self._configured_summary)
        self.preview_tab = CommandPreviewTab()
        # Order: Monitoring → Throughput → Log Preview
        self.tabs.addTab(self.verify_tab, "Monitoring")
        self.tabs.addTab(self.throughput_tab, "Throughput")
        self.tabs.addTab(self.preview_tab, "Log Preview")
        self.setCentralWidget(self.tabs)

        # --- Top header: logo + title, centered, hugging the top edge -----
        header = QWidget()
        header.setFixedHeight(24)
        header.setContentsMargins(0, 0, 0, 0)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header_row.addStretch(1)
        if ICON_PATH.is_file():
            icon_lbl = QLabel()
            pix = QPixmap(str(ICON_PATH))
            if not pix.isNull():
                icon_lbl.setPixmap(
                    pix.scaledToHeight(19, Qt.SmoothTransformation)
                )
            header_row.addWidget(icon_lbl, 0, Qt.AlignVCenter)
        title_lbl = QLabel(APP_NAME)
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 600;")
        header_row.addWidget(title_lbl, 0, Qt.AlignVCenter)
        header_row.addStretch(1)
        self.setMenuWidget(header)

        self.status_label = QLabel("No netem rule applied.")
        self.statusBar().addWidget(self.status_label, 1)

        # Wiring
        self.controls.changed.connect(self._refresh_preview)
        self.controls.apply_requested.connect(self.apply_settings)
        self.controls.clear_requested.connect(self.clear_settings)
        self.controls.interface_changed.connect(lambda _: self._refresh_status())

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(2000)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start()

        self._refresh_preview()
        self._refresh_status()

    def _configured_summary(self) -> str:
        c = self.controls
        bits = []
        if c.delay_chk.isChecked():
            bits.append(f"delay {c.delay_spin.value()}ms")
        if c.jitter_chk.isChecked():
            bits.append(
                f"delay {c.base_delay_spin.value()}ms ±{c.var_delay_spin.value()}ms"
                f" ({c.jitter_corr_spin.value():g}%)"
            )
        if c.loss_chk.isChecked():
            bits.append(f"loss {c.loss_spin.value():g}%")
        if c.corrupt_chk.isChecked():
            bits.append(f"corrupt {c.corrupt_spin.value():g}%")
        return ", ".join(bits)

    def _refresh_preview(self) -> None:
        err = self.controls.validate()
        argv = self.controls.build_tc_command()
        self.preview_tab.set_command(argv, error=err)
        self.controls.apply_btn.setEnabled(err is None)

    def _refresh_status(self) -> None:
        iface = self.controls.current_interface()
        if not iface:
            self.status_label.setText("No interface selected.")
            return
        rule = current_tc_qdisc(iface)
        if "netem" in rule:
            self.status_label.setText(f"[{iface}]  {rule}")
        else:
            self.status_label.setText(f"[{iface}]  no netem qdisc active")

    def apply_settings(self) -> None:
        err = self.controls.validate()
        if err:
            QMessageBox.warning(self, "Cannot apply", err)
            return
        iface = self.controls.current_interface()
        argv = self.controls.build_tc_command()

        try:
            if has_netem(iface):
                sudo_run(["tc", "qdisc", "del", "dev", iface, "root"])
        except SudoError as exc:
            self._error_toast(f"Failed to remove existing qdisc: {exc.stderr or exc}")
            return

        try:
            sudo_run(argv)
            self._touched_ifaces.add(iface)
            log.info("applied: tc %s", " ".join(argv[1:]))
            Toast(self, f"Applied: {self._configured_summary()}")
        except SudoError as exc:
            self._error_toast(f"tc failed: {exc.stderr or exc}")
        finally:
            self._refresh_status()

    def clear_settings(self) -> None:
        iface = self.controls.current_interface()
        if not iface:
            self.controls.reset()
            return
        if has_netem(iface):
            try:
                sudo_run(["tc", "qdisc", "del", "dev", iface, "root"])
                Toast(self, f"Cleared netem on {iface}")
            except SudoError as exc:
                self._error_toast(f"Could not clear qdisc: {exc.stderr or exc}")
        else:
            Toast(self, f"No netem qdisc on {iface}")
        self.controls.reset()
        self._refresh_status()

    def _error_toast(self, msg: str) -> None:
        log.error(msg)
        Toast(self, msg, msec=5000)

    def cleanup(self) -> None:
        """Idempotent teardown: stop ping, strip netem from every touched iface
        (plus the currently-selected one as a safety net)."""
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        log.info("shutting down")
        try:
            self.verify_tab.shutdown()
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            log.warning("verify_tab shutdown error: %s", exc)

        ifaces = set(self._touched_ifaces)
        current = self.controls.current_interface()
        if current:
            ifaces.add(current)
        for iface in ifaces:
            try:
                if has_netem(iface):
                    sudo_run(["tc", "qdisc", "del", "dev", iface, "root"])
                    log.info("removed netem qdisc on %s", iface)
            except SudoError as exc:
                log.warning("could not remove qdisc on %s: %s",
                            iface, exc.stderr or exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("cleanup error on %s: %s", iface, exc)

    def closeEvent(self, event) -> None:
        self.cleanup()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=APP_SLUG, description=f"{APP_NAME} — netem GUI")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    return p.parse_args(argv)


def _load_qss(app: QApplication) -> None:
    if STYLE_QSS.is_file():
        try:
            app.setStyleSheet(STYLE_QSS.read_text())
        except OSError as exc:
            log.warning("could not load %s: %s", STYLE_QSS, exc)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    setup_logging(args.verbose)

    if sys.platform != "linux":
        print(f"{APP_NAME} only supports Linux (tc/netem).", file=sys.stderr)
        return 2

    # Prime the sudo ticket up-front; failures surface real stderr.
    pre = subprocess.run(["sudo", "-v"])
    if pre.returncode != 0:
        print("sudo authentication failed — cannot manage tc qdiscs.", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(" ")
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    _load_qss(app)

    win = MainWindow()
    win.show()

    # Belt-and-braces cleanup: also fires on normal interpreter exit and on
    # SIGINT/SIGTERM (Ctrl+C in the launching terminal, `kill <pid>`, etc.).
    atexit.register(win.cleanup)

    def _signal_quit(signum, _frame):
        log.info("received signal %s, quitting", signum)
        win.cleanup()
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _signal_quit)
        except (ValueError, OSError):
            pass  # e.g. SIGHUP missing on some platforms

    log.info("%s ready", APP_NAME)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
