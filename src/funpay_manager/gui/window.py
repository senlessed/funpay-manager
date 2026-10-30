from datetime import UTC, datetime

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
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
from funpay_manager.app.services import ApplicationServices
from funpay_manager.app.worker import BackendThread
from funpay_manager.gui.funpay_setup import FunPaySetupDialog
from funpay_manager.gui.telegram_setup import TelegramSetupDialog
from funpay_manager.storage.database import LocalStorage


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, settings: Settings, storage: LocalStorage, backend: BackendThread):
        super().__init__()
        self.settings, self.storage, self.backend = settings, storage, backend
        self.exiting = False
        self.initialized = False
        self.first_run = False
        self.setup_dialog = None
        self.binding = None
        self.network_enabled = False
        self.funpay_dialog = None
        self.services = ApplicationServices(storage, backend.completed.emit)
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
            "Telegram: чаты, локальная история и очередь ответов. Отправка по умолчанию выключена."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        for name, state in (("Автоподнятие", "Выключено · модуль ещё не реализован"),):
            form.addRow(name, QLabel(state))
        self.chat_count = QLabel("Нет данных")
        form.addRow("Чаты в доступном списке", self.chat_count)
        self.sync_status = QLabel("Ожидание запуска")
        self.sync_status.setWordWrap(True)
        form.addRow("Синхронизация", self.sync_status)
        self.last_sync = QLabel("Не выполнялась")
        form.addRow("Последняя успешная", self.last_sync)
        self.notification_status = QLabel("Ожидание событий")
        self.notification_status.setWordWrap(True)
        form.addRow("Уведомления", self.notification_status)
        self.database_status = QLabel("Подготовка…")
        form.addRow("Локальная база", self.database_status)
        layout.addLayout(form)
        self.telegram_status = QLabel("Telegram: не настроен")
        self.telegram_status.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("Telegram", self.telegram_status)
        self.funpay_status = QLabel("Не настроен")
        self.funpay_status.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("FunPay", self.funpay_status)
        self.funpay_button = QPushButton("Подключить FunPay")
        self.funpay_button.setEnabled(False)
        self.funpay_button.clicked.connect(self.show_funpay_setup)
        layout.addWidget(self.funpay_button)
        self.setup_button = QPushButton("Настроить Telegram")
        self.setup_button.setEnabled(False)
        self.setup_button.clicked.connect(self.show_setup)
        layout.addWidget(self.setup_button)
        self.reconnect = QPushButton("Переподключить Telegram")
        self.reconnect.setEnabled(False)
        self.reconnect.clicked.connect(self.reconnect_telegram)
        layout.addWidget(self.reconnect)
        self.sending = QCheckBox("Разрешить отправку сообщений из очереди в FunPay")
        self.sending.setEnabled(False)
        self.sending.toggled.connect(self.change_sending)
        layout.addWidget(self.sending)
        self.sync_enabled = QCheckBox("Фоновая синхронизация чатов FunPay")
        self.notifications_enabled = QCheckBox(
            "Уведомления о новых сообщениях и доставке в личный Telegram"
        )
        for control in (self.sync_enabled, self.notifications_enabled):
            control.setEnabled(False)
            control.toggled.connect(self.change_background)
            layout.addWidget(control)
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
        menu.addAction("Приостановить синхронизацию", lambda: self.sync_enabled.setChecked(False))
        menu.addAction("Возобновить синхронизацию", lambda: self.sync_enabled.setChecked(True))
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
            expected = await self.storage.get_setting("funpay_account")
            async with self.storage.connect() as db:
                async with db.execute(
                    "SELECT cursor FROM sync_state WHERE component=?",
                    (f"fp-last-success:{expected['id']}" if expected else "none",),
                ) as cursor:
                    last = await cursor.fetchone()
            if self.network_enabled:
                await self.services.start_telegram()
            return {
                "dark_theme": await self.storage.get_setting("dark_theme", False),
                "binding": await self.storage.get_setting("telegram_binding"),
                "funpay": await self.storage.get_setting("funpay_account"),
                "sending": await self.storage.get_setting("sending_enabled", False),
                "sync": await self.storage.get_setting("sync_enabled", True),
                "notifications": await self.storage.get_setting("notifications_enabled", True),
                "last_sync": float(last[0]) if last else None,
            }

        self.backend.submit("initialize", prepare)

    def completed(self, name: str, result) -> None:
        if name == "initialize":
            self.initialized = True
            self.database_status.setText("Готова · SQLite WAL")
            self.dark.blockSignals(True)
            self.dark.setChecked(bool(result["dark_theme"]))
            self.dark.blockSignals(False)
            self.apply_theme(bool(result["dark_theme"]))
            self.binding = result["binding"]
            self.funpay_button.setEnabled(True)
            self.set_background_controls(result)
            if result["last_sync"]:
                self.last_sync.setText(
                    datetime.fromtimestamp(result["last_sync"], UTC).strftime(
                        "%d.%m.%Y %H:%M:%S UTC"
                    )
                )
            else:
                self.last_sync.setText("Не выполнялась")
            self.sending.blockSignals(True)
            self.sending.setChecked(bool(result["sending"]))
            self.sending.blockSignals(False)
            self.sending.setEnabled(result["funpay"] is not None and self.binding is not None)
            self.reconnect.setEnabled(self.binding is not None)
            account = result["funpay"]
            self.funpay_status.setText(
                f"Сохранён аккаунт {account['username']}" if account else "Не настроен"
            )
            self.setup_button.setEnabled(self.binding is None)
            self.telegram_status.setText(
                f"Telegram: @{self.binding['username']} · владелец {self.binding['owner_id']}\n"
                "Привязка сохранена."
                if self.binding
                else "Telegram: не настроен"
            )
            self.dark.setEnabled(True)
            self.backup_button.setEnabled(True)
            self.restore_button.setEnabled(True)
            self.status.setText(
                "Компоненты готовы. Синхронизация ограничена доступным списком и историей FunPay."
            )
            if self.first_run:
                self.first_run = False
                if not self.binding:
                    QTimer.singleShot(0, self.show_setup)
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
        elif name == "telegram-status":
            self.telegram_status.setText("Telegram: " + str(result))
        elif name == "sync-status":
            if isinstance(result, dict):
                self.sync_status.setText(result["message"])
                if "chats_count" in result:
                    self.chat_count.setText(str(result["chats_count"]))
                if result.get("last_success"):
                    self.last_sync.setText(
                        datetime.fromtimestamp(result["last_success"], UTC).strftime(
                            "%d.%m.%Y %H:%M:%S UTC"
                        )
                    )
                if result.get("auth_paused"):
                    for control in (self.sending, self.sync_enabled):
                        control.blockSignals(True)
                        control.setChecked(False)
                        control.blockSignals(False)
            else:
                self.sync_status.setText(str(result))
        elif name == "notification-status":
            self.notification_status.setText(str(result))
        elif name == "background-settings":
            self.set_background_controls(result)
        elif name in ("sending-setting", "sending-state"):
            self.sending.blockSignals(True)
            self.sending.setChecked(bool(result))
            self.sending.blockSignals(False)
            self.sending.setEnabled(self.binding is not None)
            self.status.setText(
                "Отправка включена."
                if result
                else "Отправка приостановлена. Уже начатая операция может завершиться."
            )

    def failed(self, message: str) -> None:
        if message.startswith("Ошибка операции telegram-setup-") or message.startswith(
            "Ошибка операции funpay-setup-"
        ):
            return
        self.centralWidget().setEnabled(True)
        self.status.setText(message)
        self.backup_button.setEnabled(self.initialized)
        if "sending-setting" in message:
            self.backend.submit(
                "sending-state", lambda: self.storage.get_setting("sending_enabled", False)
            )
        if "background-settings" in message:
            self.initialize()

    def set_background_controls(self, result):
        for control, key in (
            (self.sync_enabled, "sync"),
            (self.notifications_enabled, "notifications"),
        ):
            control.blockSignals(True)
            control.setChecked(bool(result[key]))
            control.blockSignals(False)
            control.setEnabled(True)
        if not result["notifications"]:
            self.notification_status.setText("Выключены")
        if not result["sync"]:
            self.sync_status.setText("Приостановлена")

    def change_background(self, _):
        sync = self.sync_enabled.isChecked() if self.sender() == self.sync_enabled else None
        notifications = (
            self.notifications_enabled.isChecked()
            if self.sender() == self.notifications_enabled
            else None
        )
        self.sync_enabled.setEnabled(False)
        self.notifications_enabled.setEnabled(False)
        self.backend.submit(
            "background-settings", lambda: self.services.set_background(sync, notifications)
        )

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

    def change_sending(self, enabled: bool) -> None:
        if (
            enabled
            and QMessageBox.question(
                self,
                "Разрешить отправку?",
                "Тексты в очереди будут отправляться покупателям FunPay.\n"
                "После перезапуска очередь продолжит работу.\n"
                "При неизвестном результате повтора нет.\n"
                "Сетевая запись проверена только на mock/fixtures; живой FunPay ещё не проверен.\n"
                "Разрешить отправку, включая уже ожидающие операции?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            self.sending.blockSignals(True)
            self.sending.setChecked(False)
            self.sending.blockSignals(False)
            return
        self.sending.setEnabled(False)

        async def persist():
            await self.services.set_sending(enabled)
            return enabled

        self.backend.submit("sending-setting", persist)

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
        if self.setup_dialog and self.setup_dialog.isVisible():
            return
        source, _ = QFileDialog.getOpenFileName(self, "Резервная копия", "", "SQLite (*.db)")
        if not source:
            return
        if (
            QMessageBox.question(
                self,
                "Восстановить базу?",
                "Текущие настройки, история и очередь будут заменены выбранной копией.\n"
                "Отправка, синхронизация и уведомления после восстановления будут выключены.\n"
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
            await self.services.stop_telegram()
            try:
                await self.storage.restore(Path(source), safety)
            finally:
                await self.storage.migrate()
                if self.network_enabled:
                    await self.services.start_telegram()
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

    def show_setup(self) -> None:
        if self.setup_dialog and self.setup_dialog.isVisible():
            self.setup_dialog.raise_()
            return
        self.setup_dialog = TelegramSetupDialog(self.storage, self.backend, self)
        self.setup_dialog.finished.connect(lambda _: self.initialize())
        self.setup_dialog.show()

    def show_funpay_setup(self) -> None:
        if self.funpay_dialog and self.funpay_dialog.isVisible():
            self.funpay_dialog.raise_()
            return
        self.funpay_dialog = FunPaySetupDialog(self.services, self.backend, self)
        self.funpay_dialog.finished.connect(lambda _: self.initialize())
        self.funpay_dialog.show()

    def reconnect_telegram(self) -> None:
        async def reconnect():
            await self.services.stop_telegram()
            await self.services.start_telegram()

        self.backend.submit("reconnect", reconnect)
