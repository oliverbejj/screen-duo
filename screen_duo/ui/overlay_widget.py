import contextlib
import os
import threading

import cv2
import numpy as np
from PySide6.QtCore import Qt, QPoint, QRect, Signal, QObject
from PySide6.QtGui import QImage, QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget

from screen_duo.recording.compositor import OverlayBox

DEFAULT_BOX_W = 160
DEFAULT_BOX_H = 285  # 9:16 portrait — matches phone front camera
MARGIN = 16

_PREVIEW_INTERVAL = 0.066  # ~15 fps


@contextlib.contextmanager
def _suppress_av_log():
    """Redirect C-level fd 2 to /dev/null so libav v4l2 ioctl noise is silent."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


class _FrameSignals(QObject):
    frame_ready = Signal(object)  # numpy ndarray, delivered to main thread


class OverlayWidget(QWidget):
    """
    Transparent overlay that shows the phone-camera feed inside a draggable box.
    Capture runs on a background thread so the GUI thread is never blocked.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)

        self._box_rect = QRect(0, 0, DEFAULT_BOX_W, DEFAULT_BOX_H)
        self._drag_offset = QPoint()
        self._dragging = False
        self._phone_frame: np.ndarray | None = None

        self._sigs = _FrameSignals()
        self._sigs.frame_ready.connect(self._on_frame)
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()

    def set_v4l2_device(self, device: str):
        self.stop()
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._capture_loop, args=(device,), daemon=True
        )
        self._worker.start()

    def stop(self):
        self._stop_event.set()
        if self._worker:
            self._worker.join(timeout=2.0)
            self._worker = None
        self._phone_frame = None
        self.update()

    def _capture_loop(self, device: str):
        with _suppress_av_log():
            cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            return
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if ok:
                    self._sigs.frame_ready.emit(frame)
                self._stop_event.wait(_PREVIEW_INTERVAL)
        finally:
            cap.release()

    def _on_frame(self, frame: np.ndarray):
        self._phone_frame = frame
        self.update()

    def place_default(self, parent_w: int, parent_h: int):
        x = parent_w - DEFAULT_BOX_W - MARGIN
        y = MARGIN
        self._box_rect.moveTo(x, y)

    def overlay_box(self) -> OverlayBox:
        r = self._box_rect
        return OverlayBox(r.x(), r.y(), r.width(), r.height())

    def paintEvent(self, _):
        painter = QPainter(self)
        if self._phone_frame is not None:
            fh, fw = self._phone_frame.shape[:2]
            bw, bh = self._box_rect.width(), self._box_rect.height()
            scale = min(bw / fw, bh / fh)
            dw, dh = int(fw * scale), int(fh * scale)
            dx = self._box_rect.x() + (bw - dw) // 2
            dy = self._box_rect.y() + (bh - dh) // 2
            scaled = cv2.resize(self._phone_frame, (dw, dh))
            rgb = cv2.cvtColor(scaled, cv2.COLOR_BGR2RGB)
            img = QImage(rgb.data, dw, dh, dw * 3, QImage.Format_RGB888).copy()
            painter.drawImage(QRect(dx, dy, dw, dh), img)
        pen = QPen(QColor(255, 255, 255, 200), 2, Qt.SolidLine)
        painter.setPen(pen)
        painter.drawRect(self._box_rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._box_rect.contains(event.pos()):
            self._dragging = True
            self._drag_offset = event.pos() - self._box_rect.topLeft()

    def mouseMoveEvent(self, event):
        if self._dragging:
            new_pos = event.pos() - self._drag_offset
            pw, ph = self.width(), self.height()
            nx = max(0, min(new_pos.x(), pw - self._box_rect.width()))
            ny = max(0, min(new_pos.y(), ph - self._box_rect.height()))
            self._box_rect.moveTo(nx, ny)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
