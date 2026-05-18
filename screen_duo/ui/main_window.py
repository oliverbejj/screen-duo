import os
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from screen_duo.devices import phone_capture
from screen_duo.devices.iphone_bridge import find_loopback_device
from screen_duo.devices.screen_capture import list_displays, Display
from screen_duo.devices.webrtc_server import WebRTCServer
from screen_duo.recording.session import RecordingSession, State
from screen_duo.ui.overlay_widget import OverlayWidget


class _Signals(QObject):
    state_changed = Signal(object)
    progress = Signal(str)
    done = Signal(str)
    error = Signal(str)
    camera_probe_done = Signal(str, bool)   # (device, is_live)
    webrtc_connected = Signal()
    webrtc_disconnected = Signal()
    flash = Signal()


class FlashWidget(QWidget):
    """Full-screen white flash for the clapper."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        p = self.palette()
        p.setColor(self.backgroundRole(), QColor("white"))
        self.setPalette(p)
        self.setAutoFillBackground(True)

    def flash(self):
        screen = self.screen()
        if screen is None:
            return
        self.setGeometry(screen.geometry())
        self.showFullScreen()
        QTimer.singleShot(100, self.hide)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("screen-duo")
        self.setMinimumSize(900, 600)

        self._signals = _Signals()
        self._signals.state_changed.connect(self._on_state_change)
        self._signals.progress.connect(self._on_progress)
        self._signals.done.connect(self._on_done)
        self._signals.error.connect(self._on_error)
        self._signals.camera_probe_done.connect(self._on_camera_probe_done)
        self._signals.webrtc_connected.connect(self._on_webrtc_connected)
        self._signals.webrtc_disconnected.connect(self._on_webrtc_disconnected)
        self._pending_probe: str = ""

        self._session: RecordingSession | None = None
        self._displays: list[Display] = []
        self._flash = FlashWidget()
        self._signals.flash.connect(self._flash.flash)
        self._webrtc: WebRTCServer | None = None
        self._active_v4l2: str = ""

        self._record_elapsed: float = 0.0
        self._seg_start: float = 0.0
        self._elapsed_timer = QTimer()
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        self._build_ui()
        self._refresh_all()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── top bar: display + camera selectors ──────────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("Display:"))
        self._display_combo = QComboBox()
        self._display_combo.setMinimumWidth(220)
        top.addWidget(self._display_combo)

        top.addWidget(QLabel("Camera:"))
        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumWidth(140)
        self._camera_combo.currentTextChanged.connect(self._on_camera_selected)
        top.addWidget(self._camera_combo)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh_all)
        top.addWidget(self._refresh_btn)
        top.addStretch()

        self._phone_label = QLabel("Camera: —")
        top.addWidget(self._phone_label)
        root.addLayout(top)

        # ── iPhone row: WebRTC connect ────────────────────────────────────────
        iphone_row = QHBoxLayout()
        self._iphone_btn = QPushButton("Connect iPhone")
        self._iphone_btn.setFixedWidth(140)
        self._iphone_btn.clicked.connect(self._on_iphone_btn)
        iphone_row.addWidget(self._iphone_btn)

        # Cert install URL (HTTP, first-time only)
        self._cert_label = QLabel()
        self._cert_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._cert_label.setStyleSheet("font-family: monospace; font-size: 11px; color: #aaa;")
        self._cert_label.hide()
        iphone_row.addWidget(self._cert_label)

        self._cert_copy_btn = QPushButton("Copy")
        self._cert_copy_btn.setFixedWidth(48)
        self._cert_copy_btn.clicked.connect(lambda: self._copy_url(self._cert_label, self._cert_copy_btn))
        self._cert_copy_btn.hide()
        iphone_row.addWidget(self._cert_copy_btn)

        sep = QLabel("·")
        sep.setStyleSheet("color: #555;")
        self._iphone_sep = sep
        sep.hide()
        iphone_row.addWidget(sep)

        # Camera URL (HTTPS, used every time)
        self._cam_label = QLabel()
        self._cam_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._cam_label.setStyleSheet("font-family: monospace; font-size: 11px; color: #ccc;")
        self._cam_label.hide()
        iphone_row.addWidget(self._cam_label)

        self._cam_copy_btn = QPushButton("Copy")
        self._cam_copy_btn.setFixedWidth(48)
        self._cam_copy_btn.clicked.connect(lambda: self._copy_url(self._cam_label, self._cam_copy_btn))
        self._cam_copy_btn.hide()
        iphone_row.addWidget(self._cam_copy_btn)

        self._iphone_status = QLabel("—")
        self._iphone_status.setStyleSheet("color: gray; margin-left: 8px;")
        iphone_row.addWidget(self._iphone_status)
        iphone_row.addStretch()
        root.addLayout(iphone_row)

        # ── preview area ──────────────────────────────────────────────────────
        self._preview_container = QWidget()
        self._preview_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._preview_container.setStyleSheet("background: #111;")
        preview_layout = QVBoxLayout(self._preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        self._screen_label = QLabel()
        self._screen_label.setAlignment(Qt.AlignCenter)
        self._screen_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        preview_layout.addWidget(self._screen_label)

        self._overlay = OverlayWidget(self._preview_container)
        self._overlay.setGeometry(self._preview_container.rect())
        root.addWidget(self._preview_container, stretch=1)

        # ── record controls ───────────────────────────────────────────────────
        controls = QHBoxLayout()
        self._record_btn = QPushButton("Record")
        self._record_btn.setFixedHeight(40)
        self._record_btn.clicked.connect(self._on_record)
        controls.addWidget(self._record_btn)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setFixedHeight(40)
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self._on_pause)
        controls.addWidget(self._pause_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setFixedHeight(40)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        controls.addWidget(self._stop_btn)

        controls.addStretch()
        self._timer_label = QLabel()
        self._timer_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #e53935; min-width: 80px;"
        )
        self._timer_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._timer_label.hide()
        controls.addWidget(self._timer_label)
        root.addLayout(controls)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

        # Screen preview at 5 fps — fast enough to orient, slow enough to stay cheap
        self._preview_timer = QTimer()
        self._preview_timer.timeout.connect(self._update_screen_preview)
        self._preview_timer.start(200)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.setGeometry(self._preview_container.rect())
        self._overlay.place_default(self._preview_container.width(), self._preview_container.height())

    # ── display / camera refresh ──────────────────────────────────────────────

    def _refresh_all(self):
        self._refresh_displays()
        self._refresh_cameras()

    def _refresh_displays(self):
        self._displays = list_displays()
        self._display_combo.clear()
        for d in self._displays:
            self._display_combo.addItem(str(d))

    def _refresh_cameras(self):
        devices = phone_capture.list_v4l2_devices()
        self._camera_combo.blockSignals(True)
        self._camera_combo.clear()
        for dev in devices:
            self._camera_combo.addItem(dev)
        self._camera_combo.blockSignals(False)
        if devices:
            self._on_camera_selected(self._camera_combo.currentText())

    def _on_camera_selected(self, device: str):
        if not device:
            return
        self._pending_probe = device
        self._phone_label.setText(f"Checking {device}…")
        self._phone_label.setStyleSheet("color: gray;")
        threading.Thread(
            target=lambda: self._signals.camera_probe_done.emit(
                device, phone_capture.check_device_live(device)
            ),
            daemon=True,
        ).start()

    def _on_camera_probe_done(self, device: str, live: bool):
        if device != self._pending_probe:
            return  # stale result
        if live:
            self._phone_label.setText(f"Live: {device}")
            self._phone_label.setStyleSheet("color: green;")
            self._overlay.set_v4l2_device(device)
            self._active_v4l2 = device
        else:
            self._phone_label.setText(f"No feed: {device}")
            self._phone_label.setStyleSheet("color: orange;")
            self._overlay.stop()
            self._active_v4l2 = ""

    # ── screen preview ────────────────────────────────────────────────────────

    def _update_screen_preview(self):
        if not self._displays:
            return
        idx = self._display_combo.currentIndex()
        if idx < 0 or idx >= len(self._displays):
            return
        d = self._displays[idx]
        if d.width == 0:  # Wayland sentinel — grabWindow not supported
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        pixmap = screen.grabWindow(0, d.x, d.y, d.width, d.height)
        if pixmap.isNull():
            return
        scaled = pixmap.scaled(
            self._screen_label.size(),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self._screen_label.setPixmap(scaled)

    def _selected_display(self):
        idx = self._display_combo.currentIndex()
        if 0 <= idx < len(self._displays):
            return self._displays[idx]
        return None

    # ── iPhone / WebRTC ───────────────────────────────────────────────────────

    def _on_iphone_btn(self):
        if self._webrtc:
            self._stop_webrtc()
        else:
            self._start_webrtc()

    def _start_webrtc(self):
        device = find_loopback_device()
        if not device:
            QMessageBox.warning(
                self, "No loopback device",
                "No v4l2loopback device found.\nRun: sudo modprobe v4l2loopback",
            )
            return
        self._webrtc = WebRTCServer(device)
        self._webrtc.on_connected = lambda: self._signals.webrtc_connected.emit()
        self._webrtc.on_disconnected = lambda: self._signals.webrtc_disconnected.emit()
        cert_url, camera_url = self._webrtc.start()

        self._cert_label.setText(f"Trust cert (once): {cert_url}")
        self._cam_label.setText(f"Open in Safari: {camera_url}")
        for w in (self._cert_label, self._cert_copy_btn,
                  self._iphone_sep, self._cam_label, self._cam_copy_btn):
            w.show()

        self._iphone_btn.setText("Disconnect")
        self._iphone_status.setText("Waiting for iPhone…")
        self._iphone_status.setStyleSheet("color: gray;")

    def _on_webrtc_connected(self):
        device = self._webrtc.v4l2_device if self._webrtc else ""
        if not device:
            return
        # Bypass the probe — we know the device is live (ffmpeg is already writing)
        self._pending_probe = ""
        self._overlay.set_v4l2_device(device)
        self._active_v4l2 = device
        self._phone_label.setText(f"Live: {device}")
        self._phone_label.setStyleSheet("color: green;")
        self._iphone_status.setText("Connected ●")
        self._iphone_status.setStyleSheet("color: #00c853;")
        # Sync the camera combo without triggering a probe
        devices = phone_capture.list_v4l2_devices()
        self._camera_combo.blockSignals(True)
        self._camera_combo.clear()
        for dev in devices:
            self._camera_combo.addItem(dev)
        idx = self._camera_combo.findText(device)
        if idx >= 0:
            self._camera_combo.setCurrentIndex(idx)
        self._camera_combo.blockSignals(False)

    def _on_webrtc_disconnected(self):
        self._iphone_status.setText("Disconnected")
        self._iphone_status.setStyleSheet("color: orange;")
        self._overlay.stop()
        self._active_v4l2 = ""
        self._phone_label.setText("Camera: —")
        self._phone_label.setStyleSheet("")

    def _stop_webrtc(self):
        if self._webrtc:
            srv = self._webrtc
            self._webrtc = None
            threading.Thread(target=srv.stop, daemon=True).start()
        for w in (self._cert_label, self._cert_copy_btn,
                  self._iphone_sep, self._cam_label, self._cam_copy_btn):
            w.hide()
        self._iphone_btn.setText("Connect iPhone")
        self._iphone_status.setText("—")
        self._iphone_status.setStyleSheet("color: gray;")
        self._overlay.stop()
        self._active_v4l2 = ""
        self._phone_label.setText("Camera: —")
        self._phone_label.setStyleSheet("")
        QTimer.singleShot(300, self._refresh_cameras)

    def _copy_url(self, label: QLabel, btn: QPushButton):
        # Extract just the URL part (strip the descriptive prefix)
        text = label.text()
        url = text.split(": ", 1)[-1] if ": " in text else text
        QApplication.clipboard().setText(url)
        original = btn.text()
        btn.setText("✓")
        QTimer.singleShot(1500, lambda: btn.setText(original))

    # ── recording ─────────────────────────────────────────────────────────────

    def _on_record(self):
        display = self._selected_display()
        if not display:
            QMessageBox.warning(self, "No display", "Select a display first.")
            return
        if not self._active_v4l2:
            QMessageBox.warning(
                self, "No camera",
                "No live camera feed.\nConnect your iPhone using the button above.",
            )
            return

        # Stop preview before starting ffmpeg — both can't hold the v4l2 device
        self._overlay.stop()

        self._session = RecordingSession(display, self._active_v4l2)
        self._session.overlay_box = self._overlay.overlay_box()
        self._session.on_state_change = lambda s: self._signals.state_changed.emit(s)
        self._session.on_progress = lambda msg: self._signals.progress.emit(msg)

        def _run():
            try:
                self._session.start(flash_callback=lambda: self._signals.flash.emit())
            except Exception as e:
                self._signals.error.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_pause(self):
        if not self._session:
            return
        if self._session.state == State.RECORDING:
            threading.Thread(target=self._session.pause, daemon=True).start()
        else:
            threading.Thread(
                target=lambda: self._session.resume(flash_callback=lambda: self._signals.flash.emit()),
                daemon=True,
            ).start()

    def _on_stop(self):
        if not self._session:
            return

        def _run():
            try:
                out = self._session.stop()
                self._signals.done.emit(out)
            except Exception as e:
                self._signals.error.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_state_change(self, state: State):
        recording = state == State.RECORDING
        paused = state == State.PAUSED
        compositing = state == State.COMPOSITING
        idle = state == State.IDLE

        self._record_btn.setEnabled(idle)
        self._pause_btn.setEnabled(recording or paused)
        self._stop_btn.setEnabled(recording or paused)
        self._pause_btn.setText("Resume" if paused else "Pause")

        # Overlay: stop during recording, restart when idle
        if recording:
            self._overlay.stop()
        elif idle and self._active_v4l2:
            self._overlay.set_v4l2_device(self._active_v4l2)

        # Recording timer
        if recording:
            self._seg_start = time.time()
            self._elapsed_timer.start(500)
            self._timer_label.setStyleSheet(
                "font-size: 18px; font-weight: bold; color: #e53935; min-width: 80px;"
            )
            self._timer_label.show()
        elif paused:
            self._record_elapsed += time.time() - self._seg_start
            self._elapsed_timer.stop()
            self._timer_label.setStyleSheet(
                "font-size: 18px; font-weight: bold; color: #f57c00; min-width: 80px;"
            )
            self._timer_label.setText(f"⏸ {self._fmt_elapsed(self._record_elapsed)}")
        elif compositing:
            if self._elapsed_timer.isActive():
                self._record_elapsed += time.time() - self._seg_start
            self._elapsed_timer.stop()
            self._timer_label.setStyleSheet("font-size: 14px; color: #888; min-width: 80px;")
            self._timer_label.setText("Processing…")
        elif idle:
            self._elapsed_timer.stop()
            self._timer_label.hide()
            self._record_elapsed = 0.0

        if compositing:
            self._status_bar.showMessage("Processing…")
        elif recording:
            self._status_bar.showMessage("Recording…")
        elif paused:
            self._status_bar.showMessage("Paused")
        elif idle:
            self._status_bar.showMessage("Ready")

    def _update_elapsed(self):
        total = self._record_elapsed + (time.time() - self._seg_start)
        self._timer_label.setText(f"● {self._fmt_elapsed(total)}")

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _on_progress(self, msg: str):
        self._status_bar.showMessage(msg)

    def _on_done(self, path: str):
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Save Recording", path, "MP4 Video (*.mp4)"
        )
        if save_path:
            if not save_path.endswith(".mp4"):
                save_path += ".mp4"
            if save_path != path:
                os.rename(path, save_path)
            final = save_path
        else:
            final = path
        self._status_bar.showMessage(f"Saved: {final}")
        self._session = None

    def _on_error(self, msg: str):
        self._elapsed_timer.stop()
        self._timer_label.hide()
        self._record_elapsed = 0.0
        self._status_bar.showMessage(f"Error: {msg}")
        QMessageBox.critical(self, "Error", msg)
        self._session = None

    def closeEvent(self, event):
        self._preview_timer.stop()
        self._overlay.stop()
        event.accept()
        QApplication.quit()
