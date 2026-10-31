from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

LABELS = {
    "idle": "Ожидает",
    "sending": "Выполняется",
    "raised": "Поднято",
    "cooldown": "Лимит FunPay",
    "unknown": "Результат неизвестен",
    "rejected": "Отказ",
    "auth": "Сессия недействительна",
    "paused": "Пауза",
}


class RaisingSetupDialog(QDialog):
    def __init__(self, services, backend, parent=None):
        super().__init__(parent)
        self.services, self.backend = services, backend
        self.prefix = "raise-setup-" + uuid4().hex
        self.account_id = None
        self.last_enabled = False
        self.busy = False
        self.closed = False
        self.setWindowTitle("Автоподнятие лотов")
        self.resize(740, 560)
        layout = QVBoxLayout(self)
        help_text = QLabel(
            "Выберите категории своих лотов и интервал попыток. "
            "Автоподнятие по умолчанию выключено.\n"
            "Ограничение FunPay имеет приоритет над вашим интервалом. После таймаута повтор "
            "останавливается до вашего решения. Поддерживаются обычные лоты, не игровая валюта."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Категория", "Состояние", "Следующая (UTC)", "Последний успех (UTC)"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.interval = QSpinBox()
        self.interval.setRange(15, 1440)
        self.interval.setValue(60)
        self.interval.setSuffix(" мин · интервал попыток")
        layout.addWidget(self.interval)
        self.enabled = QCheckBox("Включить автоподнятие выбранных категорий")
        layout.addWidget(self.enabled)
        self.save = QPushButton("Сохранить настройки")
        self.save.setEnabled(False)
        self.save.clicked.connect(self.persist)
        layout.addWidget(self.save)
        refresh = QPushButton("Загрузить категории FunPay (без поднятия)")
        refresh.clicked.connect(self.load)
        layout.addWidget(refresh)
        reset = QPushButton("Разрешить новые попытки остановленных категорий…")
        reset.clicked.connect(self.reset)
        layout.addWidget(reset)
        self.status = QLabel("Загрузите категории для настройки.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        backend.completed.connect(self.completed)
        backend.failed.connect(self.failed)
        self.finished.connect(self.cleanup)
        self.load()

    def load(self):
        if self.busy:
            return
        self.busy = True
        self.status.setText("Чтение своих категорий FunPay…")
        self.backend.submit(self.prefix + "-load", self.services.load_raise_catalog)

    def poll(self):
        if self.closed or self.busy or not self.account_id:
            return
        self.busy = True
        self.backend.submit(
            self.prefix + "-poll", lambda: self.services.raiser.repository.rows(self.account_id)
        )

    def persist(self):
        if not self.account_id or self.busy:
            return
        enabled = self.enabled.isChecked()
        if (
            enabled
            and not self.last_enabled
            and QMessageBox.question(
                self,
                "Включить автоподнятие?",
                "Программа начнёт поднимать выбранные категории на FunPay, "
                "включая после перезапуска.\n"
                "Первые попытки — не раньше чем через 30 секунд. Включить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        selected = [
            self.table.item(i, 0).data(Qt.ItemDataRole.UserRole)
            for i in range(self.table.rowCount())
            if self.table.item(i, 0).checkState() == Qt.CheckState.Checked
        ]
        account = self.account_id
        interval = self.interval.value() * 60

        async def save():
            await self.services.raiser.repository.configure(account, selected, interval, enabled)
            self.services.raiser.wake.set()
            return enabled

        self.busy = True
        self.backend.submit(self.prefix + "-save", save)

    def reset(self):
        if self.busy or not self.account_id:
            return
        if (
            QMessageBox.question(
                self,
                "Разрешить новую попытку?",
                "При неизвестном результате лоты уже могли подняться.\n"
                "Разрешить новые попытки категорий с неизвестным результатом или отказом?\n"
                "Ограничения времени сохранятся. "
                "При включённом режиме попытки продолжатся автоматически.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.backend.submit(
            self.prefix + "-reset",
            lambda: self.services.raiser.repository.reset_unknown(self.account_id),
        )

    @staticmethod
    def stamp(value):
        return datetime.fromtimestamp(value, UTC).strftime("%d.%m %H:%M") if value else "—"

    def update_states(self, rows):
        by_id = {r["game_id"]: r for r in rows}
        for i in range(self.table.rowCount()):
            row = by_id.get(self.table.item(i, 0).data(Qt.ItemDataRole.UserRole))
            if row:
                for col, text in enumerate(
                    (
                        LABELS.get(row["status"], "Остановлено"),
                        self.stamp(row["next_attempt"]),
                        self.stamp(row["last_success"]),
                    ),
                    1,
                ):
                    item = QTableWidgetItem(text)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.table.setItem(i, col, item)

    def completed(self, name, result):
        if self.closed or not name.startswith(self.prefix):
            return
        self.busy = False
        if name.endswith("-load"):
            self.account_id = result["account_id"]
            self.last_enabled = result["enabled"]
            self.enabled.setChecked(result["enabled"])
            self.table.setRowCount(len(result["rows"]))
            for i, row in enumerate(result["rows"]):
                item = QTableWidgetItem(row["name"])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(Qt.ItemDataRole.UserRole, row["game_id"])
                item.setCheckState(
                    Qt.CheckState.Checked if row["enabled"] else Qt.CheckState.Unchecked
                )
                self.table.setItem(i, 0, item)
            if result["rows"]:
                self.interval.setValue(result["rows"][0]["interval_seconds"] // 60)
            self.update_states(result["rows"])
            self.table.resizeColumnsToContents()
            self.save.setEnabled(bool(result["rows"]))
            self.status.setText(
                "Категории загружены."
                if result["rows"]
                else "Поддерживаемые опубликованные лоты не найдены."
            )
            self.timer.start(3000)
        elif name.endswith("-poll"):
            self.update_states(result)
        elif name.endswith("-save"):
            self.last_enabled = result
            self.status.setText("Настройки сохранены. Результаты попыток появятся в таблице.")
        elif name.endswith("-reset"):
            self.status.setText("Новые попытки разрешены с сохранением ограничений времени.")

    def failed(self, message):
        if self.prefix in message and not self.closed:
            self.busy = False
            self.status.setText(
                "Не удалось выполнить действие. "
                "Проверьте выбранные категории, аккаунт и доступ к FunPay."
            )

    def cleanup(self, _):
        self.closed = True
        self.timer.stop()
        self.backend.completed.disconnect(self.completed)
        self.backend.failed.disconnect(self.failed)
