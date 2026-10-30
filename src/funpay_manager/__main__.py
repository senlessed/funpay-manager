import argparse
import logging
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from funpay_manager.app.config import Settings
from funpay_manager.app.worker import BackendThread
from funpay_manager.gui.window import MainWindow
from funpay_manager.storage.database import LocalStorage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="fptm-smoke-") if args.smoke_test else None
    settings = Settings(data_dir=Path(temporary.name)) if temporary else Settings()
    settings.prepare()
    app = QApplication([sys.argv[0]] + (["-platform", "offscreen"] if args.smoke_test else []))
    app.setApplicationName("FunPayTelegramManager")
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(settings.data_dir / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if not args.smoke_test:
            QMessageBox.information(None, "FunPay Manager", "Приложение уже запущено.")
        return 2
    handler = RotatingFileHandler(
        settings.data_dir / "logs" / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("funpay_manager").addHandler(handler)
    logging.getLogger("funpay_manager").setLevel(logging.INFO)
    backend = BackendThread()
    storage = LocalStorage(settings.database)
    window = MainWindow(settings, storage, backend)
    window.quit_requested.connect(window.request_exit)
    backend.finished.connect(app.quit)
    smoke_state = {"passed": False}
    smoke_timeout = QTimer()
    smoke_timeout.setSingleShot(True)
    if args.smoke_test:

        def initialized(name, result):
            if name == "initialize":
                smoke_state["passed"] = True
                window.request_exit()

        backend.completed.connect(initialized)
        backend.failed.connect(lambda _: window.request_exit())
        smoke_timeout.timeout.connect(window.request_exit)
        smoke_timeout.start(15_000)
    else:
        if not args.minimized or not window.tray.isVisible():
            window.show()
        window.first_run = True
    backend.start()
    try:
        exit_code = app.exec()
    finally:
        backend.stop()
        backend.wait()
        window.tray.hide()
        lock.unlock()
        handler.close()
        logging.getLogger("funpay_manager").removeHandler(handler)
        if temporary:
            temporary.cleanup()
    return (0 if smoke_state["passed"] else 1) if args.smoke_test else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
