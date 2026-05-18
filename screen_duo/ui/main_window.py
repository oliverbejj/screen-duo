import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap, QColor
from PySide6.QtWidgets import (
    QComboBox,
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
from screen_duo.devices.screen_capture import list_displays, Display
from screen_duo.recording.session import RecordingSession, State
from screen_duo.ui.overlay_widget import OverlayWidget


class _Signals(QObject):
    state_changed = Signal(object)
    progress = Signal(str)
    done = Signal(str)
    error = Signal(str)


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
        screen = self.screen().geometry()
        self.setGeometry(screen)
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

        self._session: RecordingSession | None = None
        self._displays: list[Display] = []
        self._flash = FlashWidget()

        self._build_ui()
        self._refresh_all()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Top bar
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

        # Preview area
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

        # Controls
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
        root.addLayout(controls)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

        # Screen preview timer
        self._preview_timer = QTimer()
        self._preview_timer.timeout.connect(self._update_screen_preview)
        self._preview_timer.start(200)  # 5fps preview

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.setGeometry(self._preview_container.rect())
        self._overlay.place_default(self._preview_container.width(), self._preview_container.height())

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
        live = phone_capture.check_device_live(device)
        if live:
            self._phone_label.setText(f"Live: {device}")
            self._phone_label.setStyleSheet("color: green;")
            self._overlay.set_v4l2_device(device)
            self._active_v4l2 = device
        else:
            self._phone_label.setText(f"No feed: {device}")
            self._phone_label.setStyleSheet("color: orange;")
            self._overlay.stop()
            self._active_v4l2 = None

    def _update_screen_preview(self):
        if not self._displays:
            return
        idx = self._display_combo.currentIndex()
        if idx < 0 or idx >= len(self._displays):
            return

        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        d = self._displays[idx]
        if d.width > 0:
            pixmap = screen.grabWindow(0, d.x, d.y, d.width, d.height)
        else:
            pixmap = screen.grabWindow(0)

        scaled = pixmap.scaled(
            self._screen_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._screen_label.setPixmap(scaled)

    def _selected_display(self):
        idx = self._display_combo.currentIndex()
        if 0 <= idx < len(self._displays):
            return self._displays[idx]
        return None

    def _on_record(self):
        display = self._selected_display()
        if not display:
            QMessageBox.warning(self, "No display", "Select a display first.")
            return

        if not getattr(self, "_active_v4l2", None):
            QMessageBox.warning(self, "No camera", "No live camera feed found.\nStart OBS with the virtual camera enabled first.")
            return

        self._session = RecordingSession(display, self._active_v4l2)
        self._session.overlay_box = self._overlay.overlay_box()
        self._session.on_state_change = lambda s: self._signals.state_changed.emit(s)
        self._session.on_progress = lambda msg: self._signals.progress.emit(msg)

        def _run():
            try:
                self._session.start(flash_callback=self._flash.flash)
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
                target=lambda: self._session.resume(flash_callback=self._flash.flash),
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

        if compositing:
            self._status_bar.showMessage("Processing…")
        elif recording:
            self._status_bar.showMessage("Recording…")
        elif paused:
            self._status_bar.showMessage("Paused")
        elif idle:
            self._status_bar.showMessage("Ready")

    def _on_progress(self, msg: str):
        self._status_bar.showMessage(msg)

    def _on_done(self, path: str):
        self._status_bar.showMessage(f"Saved: {path}")
        QMessageBox.information(self, "Done", f"Recording saved to:\n{path}")
        self._session = None

    def _on_error(self, msg: str):
        self._status_bar.showMessage(f"Error: {msg}")
        QMessageBox.critical(self, "Error", msg)
        self._session = None
