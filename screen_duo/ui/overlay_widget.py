import cv2
import numpy as np
from PySide6.QtCore import Qt, QPoint, QRect, QTimer
from PySide6.QtGui import QImage, QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget

from screen_duo.recording.compositor import OverlayBox

DEFAULT_BOX_W = 280
DEFAULT_BOX_H = 210
MARGIN = 16


class OverlayWidget(QWidget):
    """
    Transparent widget that draws the phone-camera overlay box on top of the preview.
    The box is draggable. The phone feed is scaled to fill the box.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)

        self._box_rect = QRect(0, 0, DEFAULT_BOX_W, DEFAULT_BOX_H)
        self._drag_offset = QPoint()
        self._dragging = False
        self._phone_frame: np.ndarray | None = None

        self._cap: cv2.VideoCapture | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._grab_frame)

    def set_v4l2_device(self, device: str):
        if self._cap:
            self._cap.release()
        self._cap = cv2.VideoCapture(device)
        self._timer.start(66)  # ~15fps — lightweight preview

    def stop(self):
        self._timer.stop()
        if self._cap:
            self._cap.release()
            self._cap = None

    def place_default(self, parent_w: int, parent_h: int):
        x = parent_w - DEFAULT_BOX_W - MARGIN
        y = MARGIN
        self._box_rect.moveTo(x, y)

    def overlay_box(self) -> OverlayBox:
        r = self._box_rect
        return OverlayBox(r.x(), r.y(), r.width(), r.height())

    def _grab_frame(self):
        if self._cap is None:
            return
        ok, frame = self._cap.read()
        if ok:
            self._phone_frame = frame
            self.update()

    def paintEvent(self, _):
        painter = QPainter(self)

        if self._phone_frame is not None:
            bw, bh = self._box_rect.width(), self._box_rect.height()
            scaled = cv2.resize(self._phone_frame, (bw, bh))
            rgb = cv2.cvtColor(scaled, cv2.COLOR_BGR2RGB)
            img = QImage(rgb.data, bw, bh, bw * 3, QImage.Format_RGB888)
            painter.drawImage(self._box_rect, img)

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
