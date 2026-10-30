from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout


class TopicsSetupDialog(QDialog):
    def __init__(self, services, backend, parent=None):
        super().__init__(parent)
        self.services, self.backend = services, backend
        self.prefix = "topics-setup-" + uuid4().hex
        self.closed = False
        self.polling = False
        self.setWindowTitle("Telegram Topics · закрытая группа")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(580, 520)
        self.resize(620, 560)
        layout = QVBoxLayout(self)
        help_text = QLabel(
            "Создайте отдельную закрытую супергруппу, включите темы и добавьте вашего бота.\n"
            "Назначьте бота администратором с правом управления темами;\n"
            "привязанный владелец Telegram тоже должен быть администратором группы.\n"
            "У группы не должно быть публичного username и связанного канала.\n\n"
            "Все участники группы смогут читать переписки всех покупателей.\n"
            "Темы не изолируют доступ. Отвечать в FunPay сможет только привязанный владелец.\n"
            "После привязки новые сообщения будут зеркалироваться в темы этого бота.\n"
            "Отправка в FunPay всё равно требует отдельного разрешения в Windows.\n\n"
            "После её включения обычный текст владельца в привязанной теме\n"
            "ставится в очередь покупателю. Не используйте такие темы для внутренних заметок.\n\n"
            "Бот-администратор получает обычные сообщения. Если они не поступают,\n"
            "проверьте права и /setprivacy в BotFather. Второй бот не нужен."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.begin = QPushButton("Я понимаю доступ участников · сгенерировать код")
        self.begin.clicked.connect(self.start)
        layout.addWidget(self.begin)
        self.status = QLabel("Программа должна быть запущена, а личный Telegram-бот — подключён.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        backend.completed.connect(self.completed)
        backend.failed.connect(self.failed)
        self.finished.connect(self.cleanup)

    def start(self):
        self.begin.setEnabled(False)
        self.backend.submit(self.prefix + "-begin", self.services.group_pairing.begin)

    def poll(self):
        if self.polling or self.closed:
            return
        self.polling = True

        async def status():
            pairing = self.services.group_pairing
            return {
                "group": await self.services.storage.get_setting("telegram_group"),
                "active": bool(pairing.challenge and pairing.challenge.active),
                "error": pairing.error,
            }

        self.backend.submit(self.prefix + "-status", status)

    def completed(self, name, result):
        if self.closed:
            return
        if name == self.prefix + "-begin":
            self.status.setText(
                "Отправьте в течение 5 минут от привязанного владельца\n"
                "обычным сообщением в подготовленной закрытой группе:\n\n"
                f"/bindgroup {result}\n\nКод одноразовый. Не пересылайте его другим людям."
            )
            self.timer.start(1000)
        elif name == self.prefix + "-status":
            self.polling = False
            if result["group"]:
                self.timer.stop()
                self.status.setText(
                    f"Группа «{result['group']['title']}» привязана.\n"
                    "Новые сообщения создадут темы при появлении активности.\n"
                    "Для существующего чата: личный бот → История → Тема."
                )
            elif not result["active"]:
                self.timer.stop()
                self.begin.setEnabled(True)
                self.status.setText("Срок кода истёк или привязка отменена. Создайте новый код.")
            elif result["error"]:
                self.status.setText(result["error"] + "\nКоманда с кодом остаётся в вашей группе.")

    def failed(self, message):
        if self.prefix not in message or self.closed:
            return
        self.polling = False
        self.timer.stop()
        self.begin.setEnabled(True)
        self.status.setText(
            "Не удалось начать/проверить привязку. Убедитесь, что Telegram и FunPay "
            "подключены, а другая группа не привязана."
        )

    def cleanup(self, _):
        self.closed = True
        self.timer.stop()
        self.services.group_pairing.cancel()
        self.status.clear()
        self.backend.completed.disconnect(self.completed)
        self.backend.failed.disconnect(self.failed)
