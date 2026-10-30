# Зависимости и лицензии

Проверка 30.10.2026: первичные документы перечислены в ARCHITECTURE.md. Реально
установленные версии фиксирует requirements-dev.lock; pip check проходит.

| Компонент | Версия проверенного окружения | Лицензия |
| --- | --- | --- |
| Python | 3.12.14 x64 | PSF |
| PySide6 / shiboken6 | 6.12.0 | LGPL-3.0 / GPL alternatives, см. metadata Qt |
| aiosqlite | 0.22.1 | MIT, проверен установленный LICENSE |
| pydantic-settings | 2.16.2 | MIT |
| keyring | 25.7.0 | MIT, проверен установленный LICENSE |
| aiogram | 3.31.0 | MIT; мастер и рабочий бот |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT; Telegram и read-only FunPay |
| beautifulsoup4 / soupsieve | 4.15.0 / 3.0.4 | MIT; разбор HTML FunPay |
| PyInstaller | 6.22.3 | GPL с исключением для собранных приложений |
| pytest / pytest-asyncio / ruff | 9.2.0 / 1.4.1 / 0.17.3 | MIT / Apache-2.0 / MIT |
| FunPayAPI (не установлен) | upstream docs 1.1.0 | GPLv3, проверен upstream LICENSE |

Собственный код GPL-3.0-only. Это выбор лицензии проекта, а не заявление о полном
аудите будущего EXE. Полный список транзитивных пакетов доступен в lock; например,
certifi MPL-2.0, aiohttp Apache-2.0 AND MIT, python-dotenv BSD-3-Clause.
Не все установленные dev-пакеты входят в EXE.

До публичной дистрибуции нужно сформировать notices фактически упакованных файлов,
приложить применимые лицензии Qt/зависимостей, обеспечить corresponding source
и инструкции пересборки/замены библиотек. Учесть плагины Qt, DLL и bundled Python.
Коммерческая лицензия Qt не приобретается; используются open-source условия.
Preview не публикуется автоматически. Сторонние лицензии не заменяются GPL проекта.
