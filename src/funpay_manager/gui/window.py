from datetime import UTC, datetime

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from funpay_manager import __version__
from funpay_manager.app.autostart import Autostart
from funpay_manager.app.config import Settings
from funpay_manager.app.worker import BackendThread
from funpay_manager.storage.database import LocalStorage


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, settings: Settings, storage: LocalStorage, backend: BackendThread):
        super().__init__()
        self.settings, self.storage, self.backend = settings, storage, backend
        self.exiting = False
        self.initialized = False
        self.setWindowTitle("FunPay Telegram Manager")
        self.resize(620, 500)
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        title = QLabel("FunPay Telegram Manager")
        title.setStyleSheet("font-size: 24px; font-weight: 600; padding: 12px 0;")
        layout.addWidget(title)
        notice = QLabel(
            "Локальная Windows-основа · " + __version__ + "\n"
            "Подключение аккаунтов и мастер настройки появятся на следующем этапе."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        for name, state in (
            ("FunPay", "Не подключён"),
            ("Telegram", "Не подключён"),
            ("Синхронизация", "Недоступна до подключения аккаунтов"),
            ("Активные чаты", "Нет данных"),
            ("Автоподнятие", "Выключено · модуль ещё не реализован"),
            ("Последняя синхронизация", "Не выполнялась"),
        ):
            form.addRow(name, QLabel(state))
        self.database_status = QLabel("Подготовка…")
        form.addRow("Локальная база", self.database_status)
        layout.addLayout(form)
        self.dark = QCheckBox("Тёмная тема")
        self.dark.setEnabled(False)
        self.dark.toggled.connect(self.change_theme)
        layout.addWidget(self.dark)
        self.backup_button = QPushButton("Создать резервную копию базы")
        self.backup_button.setEnabled(False)
        self.backup_button.clicked.connect(self.backup)
        layout.addWidget(self.backup_button)
        self.restore_button = QPushButton("Восстановить базу из копии…")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self.restore)
        layout.addWidget(self.restore_button)
        self.autostart = Autostart()
        self.startup = QCheckBox("Запускать вместе с Windows (в трей)")
        layout.addWidget(self.startup)
        self.startup_note = QLabel()
        self.startup_note.setWordWrap(True)
        layout.addWidget(self.startup_note)
        self.repair_startup = QPushButton("Обновить путь автозапуска")
        self.repair_startup.clicked.connect(lambda: self.change_startup(True))
        layout.addWidget(self.repair_startup)
        self.startup.toggled.connect(self.change_startup)
        self.refresh_startup()
        logs = QPushButton("Открыть папку логов")
        logs.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(settings.data_dir / "logs")))
        )
        layout.addWidget(logs)
        self.status = QLabel("Запуск локальных компонентов…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("FunPay Telegram Manager · аккаунты не подключены")
        menu = QMenu(self)
        menu.addAction("Открыть", self.reveal)
        menu.addAction("Статус", self.reveal)
        menu.addAction("Настройки", self.reveal)
        menu.addAction("Логи", logs.click)
        menu.addSeparator()
        menu.addAction("Полностью выйти", self.quit_requested.emit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        backend.ready.connect(self.initialize)
        backend.completed.connect(self.completed)
        backend.failed.connect(self.failed)

    def reveal(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.reveal()

    def initialize(self) -> None:
        async def prepare():
            await self.storage.migrate()
            return await self.storage.get_setting("dark_theme", False)

        self.backend.submit("initialize", prepare)

    def completed(self, name: str, result) -> None:
        if name == "initialize":
            self.initialized = True
            self.database_status.setText("Готова · SQLite WAL")
            self.dark.blockSignals(True)
            self.dark.setChecked(bool(result))
            self.dark.blockSignals(False)
            self.apply_theme(bool(result))
            self.dark.setEnabled(True)
            self.backup_button.setEnabled(True)
            self.restore_button.setEnabled(True)
            self.status.setText("Локальные компоненты готовы. Аккаунты не подключены.")
        elif name == "backup":
            self.backup_button.setEnabled(True)
            self.status.setText("Резервная копия сохранена: " + str(result))
        elif name == "restore":
            self.centralWidget().setEnabled(True)
            self.initialize()
            QMessageBox.information(
                self,
                "Восстановление",
                "База восстановлена.\nПредыдущее состояние сохранено: " + str(result),
            )

    def failed(self, message: str) -> None:
        self.centralWidget().setEnabled(True)
        self.status.setText(message)
        self.backup_button.setEnabled(self.initialized)

    def refresh_startup(self) -> None:
        self.startup.blockSignals(True)
        try:
            current = self.autostart.read()
            self.startup.setChecked(current is not None)
            supported = self.autostart.executable is not None
            self.startup.setEnabled(supported or current is not None)
            stale = supported and current is not None and current != self.autostart.command()
            self.repair_startup.setVisible(bool(stale))
            self.startup_note.setText(
                "EXE перемещён или запись устарела. Обновите путь автозапуска."
                if stale
                else "Автозапуск доступен в собранном EXE."
                if not supported
                else ""
            )
        except (OSError, ValueError):
            self.startup_note.setText("Не удалось прочитать настройки автозапуска Windows.")
            self.repair_startup.hide()
        finally:
            self.startup.blockSignals(False)

    def change_startup(self, enabled: bool) -> None:
        try:
            if enabled:
                self.autostart.enable()
            else:
                self.autostart.disable()
        except (OSError, ValueError):
            QMessageBox.warning(self, "Автозапуск", "Не удалось изменить автозапуск.")
        self.refresh_startup()

    def restore(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Резервная копия", "", "SQLite (*.db)")
        if not source:
            return
        if (
            QMessageBox.question(
                self,
                "Восстановить базу?",
                "Текущие настройки и история будут заменены выбранной копией.\n"
                "Предварительно будет сохранена страховочная копия текущей базы.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.centralWidget().setEnabled(False)

        async def perform():
            from pathlib import Path

            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            safety = self.settings.data_dir / "backups" / f"before-restore-{timestamp}.db"
            await self.storage.restore(Path(source), safety)
            return safety

        self.backend.submit("restore", perform)

    def change_theme(self, enabled: bool) -> None:
        self.apply_theme(enabled)
        self.backend.submit("settings", lambda: self.storage.set_setting("dark_theme", enabled))

    def apply_theme(self, enabled: bool) -> None:
        self.setStyleSheet(
            "QWidget { background: #20242d; color: #edf1f7; }"
            "QPushButton { background: #35415a; padding: 10px; border-radius: 5px; }"
            if enabled
            else "QPushButton { padding: 10px; }"
        )

    def backup(self) -> None:
        self.backup_button.setEnabled(False)

        async def perform():
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.settings.data_dir / "backups" / f"funpay-{timestamp}.db"
            await self.storage.backup(path)
            return path

        self.backend.submit("backup", perform)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.exiting:
            event.accept()
        elif self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
        else:
            event.ignore()
            self.quit_requested.emit()

    def request_exit(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        self.setEnabled(False)
        self.status.setText("Завершение фоновых задач…")
        self.backend.stop()

    def show_first_run(self) -> None:
        QMessageBox.information(
            self,
            "Первый запуск",
            "Программа бесплатная и работает локально.\n"
            "Эта версия содержит Windows-основу и диагностику SQLite.\n"
            "Мастер подключения Telegram и FunPay пока не реализован.\n"
            "Автоматизации будут работать только при запущенной программе.",
        )
