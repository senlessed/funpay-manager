# FunPay Telegram Manager — архитектура

Статус на 30.10.2026: начальная Windows-основа, не публичный релиз.
Документ различает реализованное ядро и целевую архитектуру. Чек-лист — в PLAN.md.

## 1. Windows и жизненный цикл

Python 3.12+, PySide6 Widgets, QSystemTrayIcon. DesktopApp владеет главным окном,
настройками и BackendThread. Закрытие окна скрывает его только при доступном трее;
«Полностью выйти» останавливает backend, ожидает finished, освобождает блокировку.
QLockFile в постоянном каталоге данных не допускает второй экземпляр. Сетевых
портов, сервера и инфраструктуры разработчика нет. Данные — в
%LOCALAPPDATA%/FunPayTelegramManager/{data,config,logs,backups}, вне onefile extraction.
Сам EXE portable, пользовательские данные следуют за профилем Windows.

## 2. Qt и asyncio

Qt работает в главном потоке, отдельный QThread запускает asyncio.run(). Передача
операций через run_coroutine_threadsafe, результатов через queued Qt signals.
Виджеты никогда не изменяются из backend. Не используем дополнительный мост qasync:
так меньше зависимостей и нет зависимости от экспериментального QtAsyncio.
aiogram, aiosqlite и будущие планировщики живут в одном backend loop.
Отдельные задачи имеют собственный retry/backoff; общая TaskGroup с fail-fast для
независимых служб не подходит. При выходе — stop intake, отмена задач, закрытие
HTTP/SQLite/адаптера, ожидание потока. Принудительный terminate() не используется.

## 3. Исследование FunPay и решение

Проверены первичные источники:

- [LIMBODS/FunPayAPI](https://github.com/LIMBODS/FunPayAPI),
  [документация Account](https://funpayapi.readthedocs.io/ru/latest/account.html),
  [Runner](https://funpayapi.readthedocs.io/ru/latest/updater.html),
  [PyPI](https://pypi.org/project/FunPayAPI/).
- [aioFunPayAPI](https://github.com/ImInsane-1337/aioFunPayAPI) — асинхронный форк.
- [AlexFed1104/FunPay-API](https://github.com/AlexFed1104/FunPay-API) — отдельная
  реализация с собственным контрактом; не взаимозаменяема по названию пакета.

У исходного FunPayAPI документированы Account(golden_key).get(), get_chats(),
get_chat_history(), send_message(), get_sells(), get_order(), get_user(),
get_lot_fields(), get_raise_modal(), raise_lots(category_id, subcategories, exclude).
Runner.listen(requests_delay=...) — синхронный генератор. Метод поднятия действует
на категорию/подкатегории, а не на произвольный единственный lot ID. Полный список
своих предложений следует получать через подтверждённые возможности UserProfile,
не через выдуманный Account.get_my_lots(). Это не официальный API FunPay.

Базовый кандидат — исходный FunPayAPI за нашим FunPayAdapter: лучше описан контракт
и проверена GPLv3. PyPI 1.1.0 выпущен в 2023 году; это НЕ доказательство актуальной
работоспособности или поддержки. Асинхронный форк может устранить блокировки, но
само описание репозитория не заменяет аудит кода/зависимостей и контрактные тесты.
Окончательный production pin требует проверки read-only вызовов на текущем FunPay
и конкретного upstream commit. Сейчас FunPayAPI намеренно не установлен и не
включён в runtime, реализованы Protocol и явный тестовый mock.

Для синхронного кандидата — один выделенный executor с max_workers=1, владение
Account/requests.Session только там, ограниченные HTTP timeouts, конечные poll
шаги. Не переносить бесконечный Runner.listen в to_thread: отмена await не остановит
блокирующий поток. Перед включением событий проверить get_updates()/parse_updates()
на выбранном commit и ограничения их состояния. Не совмещать запросы одной Session
из нескольких потоков. Таймаут записи означает unknown, а не безопасный retry.
CAPTCHA/401/блокировка приостанавливают автоматические действия до вмешательства.

## 4. Telegram

[aiogram long polling](https://docs.aiogram.dev/en/latest/dispatcher/long_polling.html),
[Telegram Bot API](https://core.telegram.org/bots/api),
[privacy mode](https://core.telegram.org/bots/features#privacy-mode).
Один Bot/Dispatcher, один polling consumer на токен. Вызовы getMe при настройке,
закрытие session в finally. Без webhook и входящих портов. Pending updates не
удалять без решения владельца. Хендлер фиксирует операцию в SQLite ДО завершения
обработки update. Для очереди исходящих нужна уникальность (bot_id, update_id),
также (telegram_chat_id, telegram_message_id), поскольку message ID локален чату.
Тексты отображаются без parse_mode либо с обязательным HTML escaping. Длинные
сообщения разбивать в пределах Telegram; медиа не имитировать текстовой доставкой.

## 5. Владелец, личный бот и Topics

Реализована первая часть: getMe + getWebhookInfo, скрытый ввод токена, private
/start с криптографическим кодом, monotonic TTL 300 секунд, 100 попыток и запрет
повторного назначения владельца. Сессии закрываются в finally. Webhook не удаляется.
INSERT владельца открывает транзакцию, затем токен записывается в Credential Manager
в namespace хеша пути профиля, после чего фиксируется SQLite. При ошибке vault
INSERT откатывается. Два хранилища не могут заменить владельца/токен конкурентно.
Vault и SQLite не имеют общей транзакции: авария после vault и до commit может
оставить токен без владельца; повторная локальная настройка завершает привязку.
Отмена до принятия кода прекращает привязку; уже начатая фиксация может завершиться.
После привязки polling останавливается: рабочее меню BotService будет отдельным этапом.
Группа и FunPay ещё не реализованы. API сверены с документацией aiogram 3.31.0
и Telegram; настоящие ключи в этих проверках не использовались.

Будущий мастер: приветствие → getMe/защищённое сохранение токена → случайный код
владельца с TTL и лимитом попыток → FunPay read-only auth → необязательная группа
→ функции → проверка. Коды генерируются secrets, хранятся только в памяти, истекают
по monotonic, сравниваются compare_digest. /start без кода никому не даёт права.
Владелец фиксируется однократно в транзакции; повторная привязка — локальное действие.

Для группы отдельный одноразовый код принимает только уже привязанный owner ID.
Проверяются supergroup, is_forum, отсутствие публичного username, getChatMember
владельца (creator/administrator) и бота (administrator, can_manage_topics).
Отсутствие username — необходимая проверка, но не доказательство отсутствия других
участников. Все участники закрытой группы видят переписки, Topics не ACL.
Операции разрешены только owner ID, точному group ID и известному thread ID.
Боты, anonymous sender_chat, системные события и команды не отправляются в FunPay.

TopicManager сериализует создание по (account_id, chat_id); UNIQUE в SQLite служит
последней защитой. Маппинг включает group_id, thread_id, status и timestamps.
Создание темы в Telegram и commit не атомарны: падение между ними может оставить
сиротскую тему. Не обещаем exactly-once; нужна ручная перепривязка/восстановление.
Удалённая тема помечается invalid после конкретной ошибки API, новая создаётся по
политике восстановления; не создавать новые темы на любой сетевой таймаут.

## 6. Маршрутизация сообщений

Вход: FunPay → SyncWorker → MessageService → SQLite → notification outbox → Telegram.
Выход: личный бот/тема → авторизация → MessageService → outgoing queue → Adapter.
Ключ истории (account_id, chat_id, remote_message_id); nickname никогда не ключ.
Источники: funpay, private_bot, topic. Направление определяется по author_id ==
account.id, не по совпадению текста. Исходящие с сайта также добавляются в историю.
Подтверждённый remote ID объединяет локальную отправку с echo FunPay.
Каждое назначение Telegram имеет отдельный delivery record. Повтор notification
при неопределённом результате тоже может дублироваться: это отображаемая граница API.
SyncWorker сохраняет cursor/ID в транзакции с событиями; bounded history catch-up
после рестарта, без обещания полной истории за период выключенного ПК.

## 7. SQLite и миграции

aiosqlite, WAL, foreign_keys=ON, busy_timeout=5000. user_version — версия схемы;
BEGIN IMMEDIATE, все DDL и bump версии в одной транзакции. Новая неизвестная версия
базы запрещает запуск старого приложения. Короткие соединения/транзакции не
позволяют корутинам смешивать commit. Миграции добавляются с функциями, пустых
фиктивных доменных таблиц в первой версии нет. Реализованы settings, sync_state,
audit_logs и согласованный backup через SQLite backup API.

Целевая модель (каждая ссылка account_id → accounts.id):

| Таблицы | Ключи, связи и индексы |
| --- | --- |
| accounts | внутренний PK, UNIQUE remote_id; секретов нет |
| chats, customers | UNIQUE(account_id, remote_id), chat → customer |
| messages | UNIQUE(account_id,chat_id,remote_id), INDEX(chat_id,created_at) |
| orders | UNIQUE(account_id,remote_id), customer/chat FK, decimal amount + currency |
| lots, lot_categories | account/category FK, UNIQUE(account_id,remote_id) |
| lot_raise_settings, lot_raise_schedules | category FK, enabled, next_attempt_at |
| lot_raise_attempts | category FK, result, INDEX(category_id,created_at) |
| telegram_topic_mappings | UNIQUE(account_id,chat_id), UNIQUE(group_id,thread_id) |
| telegram_message_mappings | message FK, UNIQUE(destination_chat_id,message_id) |
| outgoing_messages | operation UUID PK, dedupe keys, INDEX(status,created_at) |
| message_templates | title/body/category, локальные шаблоны |
| notifications | UNIQUE(event_id,destination), delivery state |
| settings, sync_state, audit_logs | настройка/компонент PK, audit timestamp index |

Время UTC; деньги Decimal/строка, валюты не складывать вместе. История не очищается
автоматически. Restore реализован через SQLite backup API без ручной замены
DB/WAL файлов: read-only источник проверяется на integrity, version и обязательные
колонки, затем под общим storage lock создаётся safety backup и восстанавливается
рабочая база. GUI блокирует операции обслуживания до завершения. Все потребители
обязаны использовать один LocalStorage; будущие сетевые workers нужно останавливать
до восстановления, чтобы исключить устаревшее состояние в памяти.

Автозапуск — opt-in HKCU Run для frozen EXE, аргумент --minimized, quoting путей с
пробелами; текущая запись сравнивается с актуальным путём, исправление только по
кнопке пользователя. Запись не обновляется автоматически. См.
[документацию Microsoft](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys).

## 8. Очередь исходящих

Поля из ТЗ: operation_id, account/chat, source, telegram identifiers, text, status,
attempts, created/updated timestamps, last_error (редактированная причина).
queued → sending → sent | failed | unknown. Claim под BEGIN IMMEDIATE. В одном чате
строгий FIFO; unknown блокирует дальнейшую отправку до сверки/решения владельца.
После аварии sending → unknown. До сети разрешён retry с backoff; после возможного
принятия сервером слепой retry запрещён. History reconciliation по remote ID;
одно совпадение текста не доказывает доставку. Кнопка повторной отправки сообщает
о риске дубля и создаёт новую явную операцию. Реализация запланирована на этап 6.

## 9. Лоты и изоляция служб

LotManager — отдельная задача. Автоподнятие выключено. Ручное поднятие требует
выбора категории и подтверждения. Сохраняются выбор/исключения/попытки/расписание.
Расписание следует ответам FunPay, точное время показывается только при наличии
достоверного cooldown. Ошибки → jitter/backoff; auth/CAPTCHA → pause. После рестарта
одна актуальная попытка, без воспроизведения пропущенных действий. Ошибка лотов
не завершает Telegram; недоступность Telegram не останавливает лоты.

OrderService/Analytics/Customers/Templates — следующие модули, не пустые кнопки.
Статистика показывает покрытие наблюдения и валюты; заметки никогда не отправляются
покупателям. Никаких refund/withdraw в первой реализации.

## 10. Безопасность

CredentialStore явно использует keyring.backends.Windows.WinVaultKeyring, без
plaintext fallback. Токен и golden_key не входят в Settings/SQLite, логи/экспорт.
Логи сейчас содержат только имя внутренней операции и тип исключения, без сетевых
тел/URL/stack traces. Ротация ограничивает размер. Windows профиль/backup БД содержит
персональные сообщения, это не шифрование БД. Ни код, ни документация не требуют
ввода секретов в чат Codex. Подключения к аккаунтам на этом этапе отсутствуют.

## 11. Сборка и лицензия

[PyInstaller license](https://pyinstaller.org/en/stable/license.html),
[Qt licenses](https://doc.qt.io/qtforpython-6/licenses.html),
[Qt commercial/open-source](https://doc.qt.io/qtforpython-6/commercial/index.html),
[FunPayAPI GPLv3](https://github.com/LIMBODS/FunPayAPI/blob/master/LICENSE).
Для собственного кода выбран GPL-3.0-only с учётом FunPayAPI GPLv3. Это не заключение
о готовности всего binary distribution: нужны notices транзитивных зависимостей,
тексты Qt/third-party лицензий, соответствующие исходники и выполнение условий
замены/пересборки Qt при применении LGPL. Используем QtCore/Gui/Widgets/Network,
без ненужных Qt-модулей. PyInstaller exception не отменяет лицензии зависимостей.

scripts/build.ps1: onefile/windowed, FunPayManager.exe; отдельный --smoke-test
без ключей создаёт временную БД, окно offscreen и завершает backend. Локальный smoke
не заменяет проверку на чистых Windows 10/11, трее, Defender и DPI. Публичный выпуск
остаётся закрыт до полного аудита и функциональных критериев.

## 12. GitHub Actions

Windows runner/Python 3.12, установка зафиксированных зависимостей, ruff/pytest,
build, smoke с timeout, SHA256, upload artifacts. Никаких реальных ключей. Обычный
push не публикует release. Первый workflow только для CI-артефактов; публикацию по
тегам v* добавляем на этапе 9 после release gate. permissions contents:read.

## 13. Риски и границы доказательств

Документация FunPayAPI 1.1.0 может отставать от сайта. Исследование репозиториев
не доказывает поддержку CAPTCHA/антибота/новых форм. Не обещаем обход ограничений
или отсутствие санкций площадки. Telegram имеет лимиты и retry_after; перенос
текста не обеспечивает exactly-once. Работа только при включённом ПК и интернете.
Тестовый адаптер не доказывает реальную доставку. Нет публичного GitHub URL и
аккаунтов для integration tests; публикация и реальные действия не выполнялись.
Первый этап полезен для проверки desktop lifecycle, а не для ведения продаж.
