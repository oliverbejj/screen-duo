import contextlib
import os
import threading

import cv2
from PySide6.QtCore import Qt, QPoint, QRect, Signal, QObject
from PySide6.QtGui import QImage, QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import QWidget

from screen_duo.recording.compositor import OverlayBox

DEFAULT_BOX_W = 160
DEFAULT_BOX_H = 285  # 9:16 portrait — matches phone front camera
MARGIN = 16
HANDLE_SIZE = 10
MIN_BOX = 80


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
    frame_ready = Signal(QImage)


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
        self._last_painted_rect = QRect()
        self._drag_offset = QPoint()
        self._dragging = False
        self._phone_frame: QImage | None = None
        self._frame_pending = False  # True while a frame is queued but not yet painted

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
        self._frame_pending = False
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
        self._frame_pending = False
        self.update()

    def _capture_loop(self, device: str):
        with _suppress_av_log():
            cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            return
        cap.set(cv2.CAP_PROP_FPS, 30)
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    continue
                if self._frame_pending:
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = rgb.shape[:2]
                qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
                self._frame_pending = True
                self._sigs.frame_ready.emit(qimg)
        finally:
            cap.release()

    def _on_frame(self, qimg: QImage):
        # Track the previous box so a drag/resize between frames clears the old region.
        prev = QRect(self._last_painted_rect) if not self._last_painted_rect.isNull() else QRect()
        self._phone_frame = qimg
        self._frame_pending = False
        # Only invalidate the box region (handles + size label live inside it) —
        # repainting the whole overlay was the main paint cost.
        dirty = QRect(self._box_rect)
        if not prev.isNull() and prev != dirty:
            dirty = dirty.united(prev)
        self.update(dirty)

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
        self._last_painted_rect = QRect(self._box_rect)

        if self._phone_frame is not None:
            iw, ih = self._phone_frame.width(), self._phone_frame.height()
            bw, bh = self._box_rect.width(), self._box_rect.height()
            scale = min(bw / iw, bh / ih)
            dw, dh = int(iw * scale), int(ih * scale)
            dx = self._box_rect.x() + (bw - dw) // 2
            dy = self._box_rect.y() + (bh - dh) // 2
            # Qt scales the image to the target rect internally — no cv2 on the main thread
            painter.drawImage(QRect(dx, dy, dw, dh), self._phone_frame)

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
