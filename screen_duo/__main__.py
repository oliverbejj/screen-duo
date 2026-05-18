import signal
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from screen_duo.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("screen-duo")
    window = MainWindow()
    window.show()

    # Qt's event loop blocks Python signal delivery; a periodic timer unblocks it.
    # SIG_DFL lets the OS terminate the process immediately on Ctrl-C.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    wakeup = QTimer()
    wakeup.start(200)
    wakeup.timeout.connect(lambda: None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
