# FBE 0.85

<p align="center">
  <img src="static/branding/fbe-wordmark.png" alt="FBE" width="360">
</p>

**FBE** - локальная операционная платформа для Wildberries FBS, маркировки «Честный Знак», печати, каталога и экономики.

Проект работает локально на Windows и объединяет ежедневные операции продавца в одном интерфейсе: заказы и поставки, КИЗ, True API / СУЗ, печать этикеток, товарный каталог и экономику. Интерфейс FBE 0.85 рассчитан на desktop-работу при 100% масштабе браузера и использует единый application shell с REAL / DEMO режимами.

> FBE не является официальным продуктом Wildberries или ЦРПТ. Для production-операций пользователь самостоятельно отвечает за корректность учетных данных, настроек товарных групп и выполняемых действий.

## Возможности

- **Wildberries FBS** - новые задания, поставки, статусы, QR грузомест и операционная синхронизация.
- **Честный Знак** - заказ и хранение КИЗ, печать, отчеты о нанесении, ввод в оборот и контроль статусов через True API / СУЗ.
- **Смешанные поставки** - независимый lifecycle для разных товарных групп, включая парфюмерию и дезодоранты.
- **Печать** - WB-стикеры, внутренние этикетки, DataMatrix, PDF/native-пути и интеграция с локальными BarTender-шаблонами.
- **Каталог** - SQLite-каталог, маркировочные профили, GTIN, ТН ВЭД, документы соответствия и unit-cost данные.
- **Экономика** - локальные снимки, WB Finance/Advertising API, себестоимость и операционные показатели.
- **Профили продавцов** - отдельный runtime/workspace для каждого WB seller SID.
- **DEMO MODE** - синтетические данные и жесткая блокировка production-сети/печати для безопасной демонстрации.

## Стек

- Python 3.11
- FastAPI + Uvicorn
- SQLite
- Jinja2 + vanilla JavaScript + CSS
- Requests
- openpyxl
- ReportLab / pypdf / svglib
- Windows printing via pywin32

FBE остается локальным single-process приложением. Redis, Celery и отдельный сервер БД не требуются.

## Установка

### Требования

- Windows 10/11 x64
- Python **3.11** x64
- Java 8+ для встроенного DataMatrix renderer
- BarTender - только если используются соответствующие локальные шаблоны печати

### Первый запуск

1. Распакуйте архив FBE в отдельную папку.
2. Запустите `install_fbe.bat`.
3. После установки запустите ярлык FBE или `start_fbe.bat`.
4. Откройте `http://127.0.0.1:8000`, если браузер не открылся автоматически.
5. В профиле слева выберите **«Подключить Wildberries»** и введите API token.

Установщик создает локальную `.venv` и устанавливает зависимости из `requirements.txt`. Виртуальное окружение не входит в репозиторий.

При обновлении из предыдущей локальной установки установщик также ищет поддерживаемые `.btw`-шаблоны BarTender. С вашего подтверждения они копируются только во временное `data/local/`-хранилище. После подключения подтвержденного WB-профиля FBE предлагает перенести их в seller workspace; существующие шаблоны не перезаписываются. `.btw` по-прежнему не входят в Git/release source.

Подробности по профилям, переносу старой БД, Честному Знаку и DEMO: [docs/INSTALLATION.md](docs/INSTALLATION.md).

## Подключение Wildberries

Токен вводится через профиль FBE и валидируется read-only запросом профиля продавца. FBE сохраняет локально подтвержденные `name`, `sid`, `tin` и `tradeMark`.

Секреты хранятся только в runtime-файле:

```text
data/local/connections.json
```

Этот файл исключен из Git. Токен не возвращается обратно в UI/API и не должен храниться в `config.example.json` или исходном коде.

### Изоляция seller SID

Для каждого продавца используется отдельное рабочее пространство:

```text
data/workspaces/<SHA-256 seller SID>/
```

Внутри находятся его SQLite, кеши, экономика, выгрузки, печатные артефакты и локальные шаблоны. FBE не объединяет данные разных seller SID.

## Честный Знак

Настройки True API / СУЗ задаются в локальном профиле и привязаны к seller SID. В исходный код и Git они не попадают.

Основные локальные параметры подключения:

- ИНН участника;
- OMS ID;
- Connection ID;
- thumbprint сертификата;
- параметры товарных групп и документов соответствия.

Production-мутации не выполняются автоматически при установке или тестировании.

## DEMO MODE

DEMO предназначен для демонстрации интерфейса и основных сценариев без реального кабинета.

В DEMO:

- создается отдельная синтетическая SQLite;
- используются фиктивные товары, заказы, поставки, КИЗ и экономика;
- WB работает через mock;
- production-запросы True API / СУЗ блокируются;
- внешние процессы и реальная печать блокируются;
- печать всегда остается dry-run;
- реальные credentials и рабочие seller workspaces не используются.

В интерфейсе постоянно отображается `DEMO MODE`.

## Runtime и данные

Репозиторий содержит только source, safe examples и необходимые assets. Рабочие данные создаются после запуска и не должны коммититься.

Основные runtime-пути:

```text
data/local/            # локальные подключения и credentials
data/workspaces/       # данные seller SID
data/demo/             # сгенерированный DEMO workspace
data/unconnected/      # временный workspace до подключения
logs/                  # runtime logs
```

`.env`, `.venv`, SQLite, WAL/SHM, сертификаты, PDF, diagnostics, raw API responses, backups и generated print files исключены через `.gitignore` и проверяются release guard.

## Проверки

Установка dev-зависимостей:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Основные проверки:

```bash
python -m pytest -q
python -m compileall -q .
python tools/check_workspace.py
python tools/git_safety_check.py .
```

JavaScript можно дополнительно проверить через Node.js:

```bash
node --check static/app.js
node --check static/catalog.js
node --check static/connections.js
node --check static/economy.js
```

`git_safety_check.py` - release guard, а не замена ручной проверки Git index и истории репозитория.

## Обновление с legacy-версий

FBE 0.85 **не угадывает**, какому продавцу принадлежит старая `data/fbe.db`, и не присваивает ее seller SID автоматически. Для безопасного переноса предусмотрен `tools/adopt_legacy_workspace.py`.

Перед переносом сохраните старую установку целиком и следуйте [docs/INSTALLATION.md](docs/INSTALLATION.md).

Старая SQLite не переносится автоматически. BarTender-шаблоны обрабатываются отдельно и безопаснее: installer может сохранить их в local staging, а окончательная привязка выполняется только после выбора проверенного seller SID в UI.

## Структура проекта

```text
app.py                 FastAPI application / UI endpoints
settings.py            effective configuration
connections.py         local connection/profile store
local_templates.py     safe local BarTender template transfer
workspaces.py          seller workspace resolution
demo_mode.py           synthetic DEMO database
wb_api.py              Wildberries API client
suz_client.py           True API / СУЗ client
marking*.py            marking lifecycle/storage
catalog/                catalog repository and routes
economy/                economy repository and routes
templates/              Jinja views
static/                 UI styles/scripts/assets
tests/                  regression tests
tools/                  release/migration/runtime tools
```

## Безопасность публикации

Перед `git add` / публикацией:

```bash
python tools/git_safety_check.py .
git status
git ls-files
```

Никогда не коммитьте рабочие credentials или runtime data. Если секрет уже когда-либо был закоммичен, одного `.gitignore` недостаточно - его необходимо удалить из Git history и заменить/отозвать у провайдера.

## Сторонние компоненты

FBE включает DataMatrix Java runtime с Barcode4J. Его лицензия и NOTICE сохранены отдельно в [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) и каталоге [`third_party/`](third_party/).

## Лицензирование

Исходный код FBE опубликован для просмотра и портфолио. На текущем этапе права на собственный код FBE сохраняются за автором; см. [`LICENSE`](LICENSE).

История публичных релизов: [`CHANGELOG.md`](CHANGELOG.md).
