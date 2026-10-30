# План разработки

P0 — безопасность и целостность; P1 — функции первого релиза; P2 — удобство.
Отмечается только реализованное; проверки фиксируются отдельно в docs/VALIDATION.md.

## Этап 1 — исследование и архитектура (P0, без зависимостей)

- [x] Изучить пустую рабочую папку и окружение; найти Python 3.12.
- [x] Проверить первичные документы FunPayAPI, альтернатив, Telegram, Qt/PyInstaller.
- [x] Описать границы API, данные, очередь, безопасность, лицензии, риски.
- [x] Создать pyproject.toml и модульную структуру.
- [ ] Выбрать production upstream commit по результатам read-only smoke на FunPay.
- [ ] Завершить аудит всех транзитивных лицензий binary distribution.

Критерий: архитектура объясняет каждый поток и отказ; непроверенные предположения явны.
Основная архитектура готова; выбор production-интеграции предварительный.

## Этап 2 — Windows-основа (P0, зависит от 1)

- [x] Главное окно PySide6, статус без фиктивных подключений.
- [x] Системный трей, скрытие/возврат, полный выход, проверка доступности трея.
- [x] QLockFile для единственного экземпляра в каталоге профиля.
- [x] asyncio backend в QThread, передача результата signals, отмена при выходе.
- [x] SQLite WAL/foreign keys/busy timeout, атомарная миграция, settings.
- [x] Backup SQLite API; настройки сохраняются после перезапуска.
- [x] Ограниченные логи без произвольных текстов исключений.
- [x] Восстановление проверенной копии через SQLite backup API и страховочная копия.
- [ ] Retention backups, выбор размера/срока логов.
- [x] Добровольный HKCU автозапуск EXE, --minimized, диагностика перемещения EXE.
- [ ] Ручные проверки трея, DPI, второй экземпляр на Windows 10/11.

Критерий: окно не блокируется при background I/O, close-to-tray/exit сохраняют данные,
ошибка задачи не завершает backend. Автозапуск/restore до завершения этапа.

## Этап 3 — безопасный мастер (P0, 2 + production adapter)

- [x] Обёртка только над Windows Credential Manager (не подключена к UI).
- [ ] 7 шагов мастера, русские инструкции BotFather/golden_key, getMe.
- [ ] Криптографический одноразовый код, TTL, лимит попыток, атомарный owner claim.
- [ ] Валидация FunPay без побочных действий, закрытие HTTP session.
- [ ] Одноразовая привязка группы, getChat/getChatMember, права и privacy mode.
- [ ] Feature flags, автоподнятие по умолчанию off; итоговая диагностика.

Критерий: чужой /start/callback/код после TTL не дают доступ, секреты отсутствуют
в config/DB/логах, отмена мастера безопасна. Никаких секретов через Codex/Telegram.

## Этап 4 — переписка (P0/P1, 3)

- [x] FunPayAdapter Protocol и явный MockFunPayAdapter для тестов.
- [ ] Сериализованный production адаптер, HTTP timeouts, contract tests.
- [ ] accounts/chats/messages/customers migration + FK/индексы.
- [ ] SyncWorker, bounded history, durable cursor, backoff, pause/reconnect.
- [ ] MessageService, remote ID дедупликация и направление по author ID.
- [ ] aiogram polling, owner middleware, inline меню/пагинация/поиск/ответ/назад.
- [ ] Уведомления и delivery outbox; HTML escaping/длинный текст.

Критерий: подтверждённая доставка, история перезапускается, UI не пишет в адаптер
напрямую. Реальные тестовые сообщения только после явного разрешения.

## Этап 5 — Topics (P0/P1, 4)

- [ ] Миграции topic/message mappings, UNIQUE + lock от гонок.
- [ ] Создание/перепривязка/удалённая тема; восстановление после падения.
- [ ] Маршрутизация owner + group + thread; фильтр bot/system/command/sender_chat.
- [ ] Синхронизация private/topic/site, отсутствие echo-циклов.

Критерий: два покупателя и одинаковые nickname не смешиваются; foreign user,
foreign group, unknown thread не вызывают отправку; повтор события не создаёт тему.

## Этап 6 — надёжность (P0, перед включением реальных ответов этапа 4)

- [ ] Durable outgoing queue, дедупликация updates и source message.
- [ ] FIFO по chat, атомарный claim, sending после аварии → unknown.
- [ ] Разделение failed/unknown, сверка history, ручное решение при неопределённости.
- [ ] Изоляция workers, jitter/retry_after, graceful shutdown сетевых ресурсов.
- [ ] Тесты рестарта/crash/потери сети/конкуренции, длительный mock-run.

Критерий: при timeout-after-send нет слепого повтора, текст сохранён, неизвестная
доставка явно видна. SQLite commit предшествует подтверждению Telegram update.

## Этап 7 — лоты (P1, 4 + 6)

- [ ] Подтвердить list/profile/raise API на выбранном commit.
- [ ] Категории/лоты, ручное поднятие с подтверждением, ошибки/лимиты.
- [ ] settings/schedules/attempts migrations, исключения, pause/resume.
- [ ] Автоподнятие off по умолчанию, cooldown только из достоверного ответа.
- [ ] Восстановление расписания без catch-up всплеска; независимость Telegram.

Критерий: mock тестирует rate limit/auth/рестарт; реальные поднятия только с разрешения.

## Этап 8 — дополнительные функции (P1/P2, 4 + 6)

- [ ] Заказы: список, поиск, фильтр, переход в чат, Decimal/currency.
- [ ] Статистика с покрытием данных, периоды, валюты отдельно, first response time.
- [ ] Покупатели: локальные заметки/метки, история без автоотправки заметок.
- [ ] Шаблоны CRUD, безопасные переменные, preview перед отправкой.
- [ ] Настройки уведомлений, опциональные Windows уведомления.
- [ ] Проверка GitHub Releases по заданному repo URL, без автозагрузки/запуска.

Критерий: нет фиктивных чисел и неподдерживаемых кнопок.

## Этап 9 — EXE и CI (P1; release зависит от 3–8)

- [x] PyInstaller onefile/windowed, FunPayManager.exe — локальная сборка основы, smoke exit 0.
- [x] Подготовить CI Windows: lint/test/build/smoke/SHA256/upload artifacts.
- [ ] Выполнить подготовленный workflow на GitHub runner.
- [x] Зафиксировать проверенные версии зависимостей (requirements-dev.lock).
- [ ] Подготовить полный набор binary license notices.
- [ ] Проверить чистые Windows 10/11 без Python, трей, перенос EXE, Defender.
- [ ] Release по тегам v*, contents:write только для release job, EXE + SHA256.
- [ ] Публичный репозиторий и публикация только по разрешению владельца.

Критерий: тесты блокируют выпуск, smoke без токенов, документация соответствует EXE.

## Этап 10 — финализация (P1, 9)

- [ ] README со скриншотами, скачиванием, полным мастером, FAQ, обновлением.
- [ ] CONTRIBUTING/SECURITY/CHANGELOG, лицензии и complete corresponding source.
- [ ] Измерения idle/памяти/soak без выдуманных показателей.
- [ ] Проверить все критерии раздела 25 исходного ТЗ с evidence.

Сейчас: исследование выполнено, этап 2 начат; основа собрана в EXE. 20 тестов прошли,
локальный frozen smoke exit 0; evidence в docs/VALIDATION.md. Production Telegram/FunPay, мастер,
Topics, очередь и лоты ещё не реализованы. Не считать эту сборку готовой для продаж.
