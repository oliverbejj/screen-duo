import sys
from PySide6.QtWidgets import QApplication
from screen_duo.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("screen-duo")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
