import signal
import sys
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from screen_duo.ui.main_window import MainWindow


def _apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.Window,                  QColor(28, 28, 28))
    p.setColor(QPalette.WindowText,              QColor(210, 210, 210))
    p.setColor(QPalette.Base,                    QColor(20, 20, 20))
    p.setColor(QPalette.AlternateBase,           QColor(40, 40, 40))
    p.setColor(QPalette.Text,                    QColor(210, 210, 210))
    p.setColor(QPalette.Button,                  QColor(48, 48, 48))
    p.setColor(QPalette.ButtonText,              QColor(210, 210, 210))
    p.setColor(QPalette.Highlight,               QColor(0, 122, 204))
    p.setColor(QPalette.HighlightedText,         QColor(255, 255, 255))
    p.setColor(QPalette.Disabled, QPalette.Text,       QColor(90, 90, 90))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(90, 90, 90))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("screen-duo")
    _apply_dark_theme(app)
    window = MainWindow()
    window.show()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    wakeup = QTimer()
    wakeup.start(200)
    wakeup.timeout.connect(lambda: None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
