from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout

from funpay_manager.telegram.setup import TelegramSetup


class TelegramSetupDialog(QDialog):
    """First part of setup: bot validation and owner pairing, no customer actions."""

    def __init__(self, storage, backend, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройка Telegram")
        self.setMinimumWidth(530)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.backend = backend
        self.service = TelegramSetup(storage)
        self.prefix = "telegram-setup-" + uuid4().hex
        self.busy = False
        self.closed = False
        self.binding = None
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Программа бесплатная и работает локально, без VPS. Для автоматизаций\n"
            "компьютер и приложение должны быть включены.\n\n"
            "1. Создайте отдельного бота у @BotFather командой /newbot.\n"
            "2. Введите токен здесь. Не отправляйте его в чат или исходники.\n"
            "3. Отправьте одноразовый код вашему боту в личном чате.\n\n"
            "Сейчас настраивается только Telegram. Подключение FunPay и группы\n"
            "появится на следующем этапе; управление продажами ещё недоступно."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Bot Token от BotFather")
        layout.addWidget(self.token)
        self.check = QPushButton("Проверить токен и начать привязку")
        self.check.clicked.connect(self.validate)
        layout.addWidget(self.check)
        self.status = QLabel("Токен сохраняется защищённо после подтверждения владельца.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.close_button = QPushButton("Отмена")
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(self.close_button)
        backend.completed.connect(self.completed)
        backend.failed.connect(self.failed)
        self.finished.connect(self.cleanup)

    def validate(self):
        token = self.token.text().strip()
        if not token or self.busy:
            return
        self.busy = True
        self.token.clear()
        self.token.setEnabled(False)
        self.check.setEnabled(False)
        self.status.setText("Проверка getMe и настроек webhook…")
        self.backend.submit(self.prefix + "-check", lambda: self.service.validate(token))

    def completed(self, name, result):
        if self.closed:
            return
        if name == self.prefix + "-check":
            self.status.setText(
                f"Бот @{result['username']} проверен.\n"
                "Откройте его личный чат и отправьте в течение 5 минут:\n\n"
                f"/start {result['code']}\n\n"
                "Код одноразовый. Не передавайте его другим людям. Ожидаю подтверждение…"
            )
            self.backend.submit(self.prefix + "-bind", self.service.bind)
        elif name == self.prefix + "-bind":
            self.binding = result
            self.status.setText(
                f"Владелец {result['owner_id']} привязан к @{result['username']}.\n"
                "Токен сохранён в Windows Credential Manager.\n"
                "Рабочее меню бота и FunPay ещё не подключены."
            )
            self.close_button.setText("Готово")
            self.close_button.clicked.disconnect()
            self.close_button.clicked.connect(self.accept)

    def failed(self, message):
        if self.closed:
            return
        if self.prefix not in message:
            return
        self.service.cancel()
        self.service = TelegramSetup(self.service.storage)
        self.busy = False
        self.token.setEnabled(True)
        self.check.setEnabled(True)
        self.status.setText(
            "Не удалось завершить настройку. Проверьте токен, интернет и Credential Manager.\n"
            "У бота не должно быть webhook или другого запущенного polling-процесса.\n"
            "Код действует 5 минут. Для новой попытки снова введите токен."
        )

    def cleanup(self, _):
        self.closed = True
        self.service.cancel()
        self.token.clear()
        self.status.clear()
        self.backend.completed.disconnect(self.completed)
        self.backend.failed.disconnect(self.failed)
