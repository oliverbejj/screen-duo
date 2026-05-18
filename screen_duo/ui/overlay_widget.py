import contextlib
import os
import threading

import cv2
import numpy as np
from PySide6.QtCore import Qt, QPoint, QRect, Signal, QObject
from PySide6.QtGui import QImage, QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import QWidget

from screen_duo.recording.compositor import OverlayBox

DEFAULT_BOX_W = 160
DEFAULT_BOX_H = 285  # 9:16 portrait — matches phone front camera
MARGIN = 16
HANDLE_SIZE = 10
MIN_BOX = 80

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
    frame_ready = Signal(object)


class OverlayWidget(QWidget):
    """
    Transparent overlay showing the phone-camera feed in a draggable, resizable box.
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

        self._resizing = False
        self._resize_handle = ""
        self._resize_start_pos = QPoint()
        self._resize_start_rect = QRect()

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

    def _handle_rects(self) -> dict[str, QRect]:
        r = self._box_rect
        h = HANDLE_SIZE
        return {
            'tl': QRect(r.x(),             r.y(),              h, h),
            'tr': QRect(r.right() - h + 1, r.y(),              h, h),
            'bl': QRect(r.x(),             r.bottom() - h + 1, h, h),
            'br': QRect(r.right() - h + 1, r.bottom() - h + 1, h, h),
        }

    def _cursor_for_handle(self, handle: str) -> Qt.CursorShape:
        if handle in ('tl', 'br'):
            return Qt.SizeFDiagCursor
        return Qt.SizeBDiagCursor

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

        # Box border
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._box_rect)

        # Corner resize handles
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 210))
        for rect in self._handle_rects().values():
            painter.drawRect(rect)

        # Size label (bottom-right corner of box)
        size_text = f"{self._box_rect.width()}×{self._box_rect.height()}"
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        label_rect = QRect(
            self._box_rect.right() - 68,
            self._box_rect.bottom() - 18,
            66, 16,
        )
        painter.fillRect(label_rect, QColor(0, 0, 0, 120))
        painter.setPen(QColor(200, 200, 200, 200))
        painter.drawText(label_rect, Qt.AlignCenter, size_text)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        for name, rect in self._handle_rects().items():
            if rect.contains(event.pos()):
                self._resizing = True
                self._resize_handle = name
                self._resize_start_pos = event.pos()
                self._resize_start_rect = QRect(self._box_rect)
                return
        if self._box_rect.contains(event.pos()):
            self._dragging = True
            self._drag_offset = event.pos() - self._box_rect.topLeft()

    def mouseMoveEvent(self, event):
        if self._resizing:
            self._do_resize(event.pos())
            return
        if self._dragging:
            new_pos = event.pos() - self._drag_offset
            pw, ph = self.width(), self.height()
            nx = max(0, min(new_pos.x(), pw - self._box_rect.width()))
            ny = max(0, min(new_pos.y(), ph - self._box_rect.height()))
            self._box_rect.moveTo(nx, ny)
            self.update()
            return
        # Cursor feedback on hover
        for name, rect in self._handle_rects().items():
            if rect.contains(event.pos()):
                self.setCursor(self._cursor_for_handle(name))
                return
        if self._box_rect.contains(event.pos()):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()

    def _do_resize(self, pos: QPoint):
        dx = pos.x() - self._resize_start_pos.x()
        dy = pos.y() - self._resize_start_pos.y()
        r = QRect(self._resize_start_rect)
        pw, ph = self.width(), self.height()
        handle = self._resize_handle

        if 'l' in handle:
            r.setLeft(max(0, min(r.left() + dx, r.right() - MIN_BOX)))
        if 'r' in handle:
            r.setRight(min(pw - 1, max(r.right() + dx, r.left() + MIN_BOX)))
        if 't' in handle:
            r.setTop(max(0, min(r.top() + dy, r.bottom() - MIN_BOX)))
        if 'b' in handle:
            r.setBottom(min(ph - 1, max(r.bottom() + dy, r.top() + MIN_BOX)))

        self._box_rect = r
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self._resizing = False
            self._resize_handle = ""
