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
import logging
import re
import shutil
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

APP_NAME = "Netemango"
APP_SLUG = "delaymorph"
LOG_PATH = Path.home() / ".local" / "share" / APP_SLUG / "app.log"
STYLE_QSS = Path(__file__).resolve().parent.parent / "style.qss"

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


def read_iface_bytes(iface: str) -> tuple[int, int]:
    """Return ``(rx_bytes, tx_bytes)`` straight from sysfs."""
    base = Path(f"/sys/class/net/{iface}/statistics")
    rx = int((base / "rx_bytes").read_text().strip())
    tx = int((base / "tx_bytes").read_text().strip())
    return rx, tx


def is_wireless(iface: str) -> bool:
    return Path(f"/sys/class/net/{iface}/wireless").exists()


def parse_iw_link(iface: str) -> dict[str, str]:
    """Parse ``iw dev <iface> link`` into a flat dict. Empty if not associated."""
    iw = shutil.which("iw")
    if not iw:
        return {}
    try:
        out = subprocess.run(
            [iw, "dev", iface, "link"], capture_output=True, text=True, timeout=2
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("iw dev failed: %s", exc)
        return {}
    if "Not connected" in out:
        return {}
    fields: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


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
        self.iface_combo.blockSignals(True)
        self.iface_combo.clear()
        self.iface_combo.addItems(list_interfaces())
        if current:
            idx = self.iface_combo.findText(current)
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
    WINDOW_S = 60

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
        self.enable_chk = QCheckBox("Live update")
        self.enable_chk.setChecked(True)
        clear_btn = QPushButton("Reset")
        clear_btn.clicked.connect(self._reset_history)
        self.readout = QLabel("RX: —    TX: —")
        self.readout.setStyleSheet("font-family: monospace; font-size: 13px;")
        controls.addWidget(self.enable_chk)
        controls.addWidget(clear_btn)
        controls.addStretch(1)
        controls.addWidget(self.readout)
        layout.addLayout(controls)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#1e1f22")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Throughput", units="bit/s")
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.addLegend()
        self.rx_curve = self.plot.plot(pen=pg.mkPen("#5DADE2", width=2), name="RX")
        self.tx_curve = self.plot.plot(pen=pg.mkPen("#F39C12", width=2), name="TX")
        layout.addWidget(self.plot, 1)

        # Single, persistent timer — connect once.
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _reset_history(self) -> None:
        self._t.clear()
        self._rx.clear()
        self._tx.clear()
        self._last = None
        self._elapsed = 0
        self.rx_curve.setData([], [])
        self.tx_curve.setData([], [])
        self.readout.setText("RX: —    TX: —")

    def _tick(self) -> None:
        if not self.enable_chk.isChecked():
            return
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
            f"RX: {fmt_rate(rx_rate):>12}    TX: {fmt_rate(tx_rate):>12}"
        )


# --------------------------------------------------------------------------- #
# Quality tab
# --------------------------------------------------------------------------- #
class QualityTab(QWidget):
    def __init__(self, get_iface):
        super().__init__()
        self._get_iface = get_iface

        layout = QVBoxLayout(self)
        self.banner = QLabel()
        self.banner.setStyleSheet(
            "background: #3a2f17; color: #f1c40f; padding: 8px; border-radius: 4px;"
        )
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)

        self.form_box = QGroupBox("Wireless link")
        form = QFormLayout(self.form_box)
        self.ssid_lbl = QLabel("—")
        self.freq_lbl = QLabel("—")
        self.signal_lbl = QLabel("—")
        self.bitrate_lbl = QLabel("—")
        for lbl in (self.ssid_lbl, self.freq_lbl, self.signal_lbl, self.bitrate_lbl):
            lbl.setStyleSheet("font-family: monospace;")
        form.addRow("SSID:", self.ssid_lbl)
        form.addRow("Frequency:", self.freq_lbl)
        form.addRow("Signal:", self.signal_lbl)
        form.addRow("TX bitrate:", self.bitrate_lbl)
        layout.addWidget(self.form_box)
        layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()

    def _tick(self) -> None:
        iface = self._get_iface()
        if not iface:
            self.banner.setText("Select an interface to inspect link quality.")
            self.banner.show()
            self.form_box.setEnabled(False)
            return
        if not is_wireless(iface):
            self.banner.setText(
                f"<b>{iface}</b> is not a wireless interface — no signal data available."
            )
            self.banner.show()
            self.form_box.setEnabled(False)
            return
        fields = parse_iw_link(iface)
        if not fields:
            self.banner.setText(
                f"<b>{iface}</b> is wireless but not associated to an AP."
            )
            self.banner.show()
            self.form_box.setEnabled(False)
            return
        self.banner.hide()
        self.form_box.setEnabled(True)
        self.ssid_lbl.setText(fields.get("SSID", "—"))
        self.freq_lbl.setText(fields.get("freq", "—"))
        self.signal_lbl.setText(fields.get("signal", "—"))
        self.bitrate_lbl.setText(fields.get("tx bitrate", "—"))


# --------------------------------------------------------------------------- #
# Verify tab (ping)
# --------------------------------------------------------------------------- #
PING_RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


class VerifyTab(QWidget):
    """Run ``ping`` with QProcess and graph measured RTT / jitter / loss."""

    WINDOW = 60

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
        self.plot.setLabel("left", "RTT", units="ms")
        self.plot.setLabel("bottom", "Ping #")
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
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 740)

        self.controls = ControlsPanel()
        dock = QDockWidget("Controls", self)
        dock.setObjectName("controlsDock")
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setWidget(self.controls)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        self.tabs = QTabWidget()
        self.throughput_tab = ThroughputTab(self.controls.current_interface)
        self.quality_tab = QualityTab(self.controls.current_interface)
        self.verify_tab = VerifyTab(self._configured_summary)
        self.preview_tab = CommandPreviewTab()
        self.tabs.addTab(self.throughput_tab, "Throughput")
        self.tabs.addTab(self.quality_tab, "Quality")
        self.tabs.addTab(self.verify_tab, "Verify")
        self.tabs.addTab(self.preview_tab, "Command Preview")
        self.setCentralWidget(self.tabs)

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

    def closeEvent(self, event) -> None:
        log.info("shutting down")
        self.verify_tab.shutdown()
        iface = self.controls.current_interface()
        if iface and has_netem(iface):
            try:
                sudo_run(["tc", "qdisc", "del", "dev", iface, "root"])
            except SudoError as exc:
                log.warning("could not remove qdisc on exit: %s", exc.stderr or exc)
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
    app.setApplicationName(APP_NAME)
    _load_qss(app)

    win = MainWindow()
    win.show()
    log.info("%s ready", APP_NAME)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
