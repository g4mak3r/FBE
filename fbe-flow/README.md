# FBE Flow

Локальная операционная платформа для продавцов маркетплейсов. Версия 0.1: Foundation.

Python 3.11+, FastAPI, SQLite, Jinja2, vanilla JavaScript. Независимый проект в `fbe-flow/`.

## Запуск на Windows

Установить Python 3.11 или новее. Открыть каталог `fbe-flow`, запустить
`install_flow.bat`, затем `start_flow.bat`. Открыть <http://127.0.0.1:8765>.
Остановка: Ctrl+C в окне запуска.

Или из PowerShell:

```powershell
cd fbe-flow
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements-lock.txt .
.\.venv\Scripts\python.exe -m fbe_flow
```

Сначала создать продавца в интерфейсе. Можно создать несколько рабочих пространств
и переключать их в sidebar. Подключений и внешних адаптеров в этой версии нет.
Для проверки контрактов используется адаптер только в тестах.

## Разработка и проверки

```powershell
.\.venv\Scripts\python.exe -m pip install -c requirements-lock.txt -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

На Linux/macOS: `python3 -m venv .venv`, затем те же команды через `.venv/bin/python`.
Зависимости проекта закреплены в `pyproject.toml`, версии транзитивных зависимостей —
в `requirements-lock.txt`. CI проверяет Foundation отдельно на Ubuntu и Windows.

## Данные и API

Windows: `%LOCALAPPDATA%\FBE Flow\flow.sqlite3`. Linux/macOS:
`$XDG_DATA_HOME/fbe-flow/flow.sqlite3` или `~/.local/share/fbe-flow/flow.sqlite3`.
Можно указать `FBE_FLOW_DATA_DIR` или запустить
`python -m fbe_flow --data-dir C:\FBEFlowData --port 8765`.
Данные не зависят от каталога установки и не попадают в Git.
Для копирования/резервирования всей папки данных сначала остановить приложение.
При обновлении повторить `install_flow.bat`: миграции применятся при следующем запуске.

Один процесс на папку данных; запуск с несколькими workers не поддерживается.
Shell работает без CDN. OpenAPI: `/openapi.json`; необязательные `/docs` и `/redoc`
используют CDN FastAPI и могут требовать интернет.

API для seller-scoped подключений, настроек, операций и чтения нормализованных
записей находится в `/api/sellers/{seller_id}/...`.
Изменяющие запросы требуют JSON и заголовок `X-FBE-Flow: 1`.
Ошибки: `404` неизвестный/чужой ID, `409` конфликт, `422` некорректные данные.
Настройки имеют произвольные JSON-значения. Credentials в JSON не сохранять.

## Структура

| Путь | Назначение |
| --- | --- |
| `src/fbe_flow/core/` | БД, миграции, нормализованные модели и контракт integrations |
| `src/fbe_flow/modules/` | Sellers, connections, records, operations, settings |
| `src/fbe_flow/integrations/` | Точка регистрации будущих адаптеров |
| `src/fbe_flow/web/` | Routes, Jinja template, CSS и vanilla JS |
| `src/fbe_flow/app.py`, `__main__.py` | Сборка приложения и локальный CLI |
| `tests/` | Isolation, FK, транзакции, очередь, HTTP и lifespan |
| `ARCHITECTURE.md` | Архитектурная основа следующих этапов |

Операционные контуры добавляются по [ARCHITECTURE.md](ARCHITECTURE.md).
