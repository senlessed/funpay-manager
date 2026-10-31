from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from funpay_manager.storage.templates import TemplateRepository
from funpay_manager.utils.templates import TemplateError, render_template, validate_template


class TemplatesSetupDialog(QDialog):
    def __init__(self, storage, backend, parent=None):
        super().__init__(parent)
        self.repository = TemplateRepository(storage)
        self.storage, self.backend = storage, backend
        self.prefix = "templates-setup-" + uuid4().hex
        self.account = None
        self.template_id = self.revision = None
        self.rows = []
        self.closed = False
        self.busy = False
        self.setWindowTitle("Шаблоны ответов")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(760, 590)
        layout = QVBoxLayout(self)
        help_text = QLabel(
            "Сохраните шаблоны для подключённого аккаунта. В личном боте: "
            "История → Шаблоны → предпросмотр → Отправить в FunPay. "
            "Автоматической отправки нет. Изменения нужно сохранить."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        columns = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMaximumWidth(230)
        self.list.currentRowChanged.connect(self.select)
        columns.addWidget(self.list)
        editor = QVBoxLayout()
        editor.addWidget(QLabel("Название (до 60 символов)"))
        self.title = QLineEdit()
        self.title.setMaxLength(60)
        editor.addWidget(self.title)
        editor.addWidget(QLabel("Текст ответа"))
        self.body = QPlainTextEdit()
        self.body.textChanged.connect(self.preview)
        editor.addWidget(self.body)
        variables = QHBoxLayout()
        self.variable_buttons = []
        for label, field in (
            ("Ник покупателя", "buyer"),
            ("Ник продавца", "seller"),
            ("ID чата", "chat_id"),
        ):
            control = QPushButton(label)
            control.clicked.connect(
                lambda checked=False, field=field: self.body.insertPlainText("{" + field + "}")
            )
            variables.addWidget(control)
            self.variable_buttons.append(control)
        editor.addLayout(variables)
        editor.addWidget(QLabel("Пример с условными данными"))
        self.example = QPlainTextEdit()
        self.example.setReadOnly(True)
        self.example.setMaximumHeight(130)
        editor.addWidget(self.example)
        columns.addLayout(editor, 1)
        layout.addLayout(columns, 1)
        controls = QHBoxLayout()
        self.buttons = []
        for label, action in (
            ("Новый", self.new),
            ("Сохранить", self.save),
            ("Удалить…", self.delete),
            ("Перезагрузить", self.load),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            controls.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(controls)
        self.status = QLabel("Загрузка…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        backend.completed.connect(self.completed)
        backend.failed.connect(self.failed)
        self.finished.connect(self.cleanup)
        self.load()

    def set_busy(self, value):
        self.busy = value
        self.list.setEnabled(not value)
        self.title.setEnabled(not value)
        self.body.setEnabled(not value)
        for control in self.variable_buttons:
            control.setEnabled(not value)
        for button in self.buttons:
            button.setEnabled(not value)

    def load(self):
        if self.busy:
            return
        self.set_busy(True)

        async def read():
            account = await self.storage.get_setting("funpay_account")
            if not account:
                raise TemplateError("Сначала подключите FunPay.")
            return account, await self.repository.list(account["id"]), None

        self.backend.submit(self.prefix + "-load", read)

    def new(self):
        self.list.setCurrentRow(-1)
        self.template_id = self.revision = None
        self.title.clear()
        self.body.clear()
        self.status.setText("Новый шаблон. Введите название и текст, затем сохраните.")

    def select(self, index):
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        self.template_id, self.revision = row["id"], row["revision"]
        self.title.setText(row["title"])
        self.body.setPlainText(row["body"])

    def preview(self):
        try:
            text = render_template(
                self.body.toPlainText(),
                {"buyer": "Покупатель", "seller": "Продавец", "chat_id": "12345"},
            )
        except TemplateError as error:
            text = str(error)
        self.example.setPlainText(text)

    def save(self):
        if self.busy or not self.account:
            return
        try:
            title, body = validate_template(self.title.text(), self.body.toPlainText())
        except TemplateError as error:
            self.status.setText(str(error))
            return
        account = self.account
        template_id, revision = self.template_id, self.revision

        async def persist():
            selected = await self.repository.save(account["id"], title, body, template_id, revision)
            return account, await self.repository.list(account["id"]), selected

        self.set_busy(True)
        self.backend.submit(self.prefix + "-save", persist)

    def delete(self):
        if self.busy or not self.account or not self.template_id:
            return
        if (
            QMessageBox.question(
                self,
                "Удалить шаблон?",
                "Шаблон будет удалён. Его неиспользованные предпросмотры перестанут отправляться.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        account = self.account
        template_id, revision = self.template_id, self.revision

        async def remove():
            await self.repository.delete(account["id"], template_id, revision)
            return account, await self.repository.list(account["id"]), None

        self.set_busy(True)
        self.backend.submit(self.prefix + "-delete", remove)

    def completed(self, name, result):
        if self.closed or not name.startswith(self.prefix):
            return
        self.set_busy(False)
        self.account, self.rows, selected = result
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([r["title"] for r in self.rows])
        index = next(
            (i for i, r in enumerate(self.rows) if r["id"] == selected), 0 if self.rows else -1
        )
        self.list.setCurrentRow(index)
        self.list.blockSignals(False)
        if index >= 0:
            self.select(index)
        else:
            self.new()
        self.status.setText(
            "Шаблон сохранён."
            if name.endswith("-save")
            else "Список загружен. Изменения сохраняются только по кнопке."
        )

    def failed(self, message):
        if not self.closed and self.prefix in message:
            self.set_busy(False)
            self.status.setText(
                "Не удалось сохранить или загрузить шаблоны. Проверьте текущий аккаунт; "
                "при изменении шаблона в другом окне перезагрузите список."
            )

    def cleanup(self, _):
        self.closed = True
        self.backend.completed.disconnect(self.completed)
        self.backend.failed.disconnect(self.failed)
