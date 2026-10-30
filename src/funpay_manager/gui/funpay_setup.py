import threading
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout

from funpay_manager.utils.diagnostics import funpay_failure_text


class FunPaySetupDialog(QDialog):
    def __init__(self, services, backend, parent=None):
        super().__init__(parent)
        self.services, self.backend = services, backend
        self.operation = "funpay-setup-" + uuid4().hex
        self.cancelled = threading.Event()
        self.setWindowTitle("Подключение FunPay · проверка профиля")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(580, 520)
        layout = QVBoxLayout(self)
        description = QLabel(
            "Подключайте только свой аккаунт FunPay.\n"
            "В браузере откройте funpay.com и войдите в аккаунт. Затем откройте\n"
            "инструменты разработчика (F12) → Application/Хранилище → Cookies →\n"
            "https://funpay.com и скопируйте значение cookie golden_key из колонки Value.\n"
            "Нужно значение, а не название cookie и не вся строка Cookies.\n\n"
            "Этот ключ даёт доступ к аккаунту: не пересылайте его в Telegram\n"
            "или Issues. Здесь он проверяется и сохраняется в Credential Manager.\n"
            "Проверка читает профиль. Отправка включается отдельно в главном окне.\n"
            "Фоновая синхронизация и личные уведомления включены по умолчанию;\n"
            "их можно выключить флажками в главном окне.\n"
            "При CAPTCHA/блокировке остановитесь и войдите на сайт вручную."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("golden_key")
        layout.addWidget(self.key)
        self.connect_button = QPushButton("Проверить и сохранить")
        self.connect_button.clicked.connect(self.connect_account)
        layout.addWidget(self.connect_button)
        self.status = QLabel(
            "Сетевой контракт неофициальный; работа с текущим сайтом требует проверки."
        )
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        backend.completed.connect(self.completed)
        backend.operation_failed.connect(self.failed)
        self.finished.connect(self.cleanup)

    def connect_account(self):
        key = self.key.text().strip()
        if not key:
            return
        self.key.clear()
        self.key.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status.setText("Проверка профиля FunPay…")
        self.backend.submit(
            self.operation, lambda: self.services.connect_funpay(key, self.cancelled)
        )

    def completed(self, name, result):
        if name != self.operation or self.cancelled.is_set():
            return
        self.status.setText(
            f"Подключён {result['username']} · ID {result['id']}.\n"
            "Ключ сохранён защищённо. В Telegram доступен список чатов."
        )

    def failed(self, name, code):
        if name != self.operation or self.cancelled.is_set():
            return
        self.key.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.status.setText(funpay_failure_text(code))
        self.adjustSize()

    def cleanup(self, _):
        self.cancelled.set()
        self.key.clear()
        self.backend.completed.disconnect(self.completed)
        self.backend.operation_failed.disconnect(self.failed)
