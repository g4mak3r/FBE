# Changelog

## 0.85 - 2026-09-07

Первый публичный релиз FBE.

### Runtime и надежность

- Централизованные Settings и локальные подключения.
- Профиль Wildberries с read-only проверкой seller profile.
- Изоляция рабочих данных по seller SID.
- REAL / DEMO с отдельными SQLite и runtime paths.
- DEMO network/process/printing guards и принудительный dry-run.
- Безопасный явный перенос legacy SQLite без автоматического присвоения seller SID.

### Wildberries FBS

- Authoritative operational sync и checkpoint свежести.
- Точный reconcile состава поставок.
- Исправления cold start, empty supply и `done/complete` lifecycle.
- Разделение фоновых cache jobs и пользовательских операций.

### Честный Знак

- Независимые товарные группы в mixed supply.
- Идемпотентная работа с report/document IDs.
- Partial-success сохранение статусов.
- Ограниченный polling terminal-aware lifecycle.
- Background status watchers вместо долгих блокирующих UI-запросов.

### UI

- Единый desktop sidebar/application shell.
- Комфортный масштаб текста, controls и таблиц при 100% browser zoom.
- Единые design tokens, статусы и формы.
- Уменьшены тяжелые blur/effects и исторические CSS overrides.
- Профиль WB и постоянный REAL / DEMO indicator.

### Git safety

- Runtime DB, credentials, logs, diagnostics, backups и generated artifacts исключены из source-кандидата.
- Добавлен `tools/git_safety_check.py`.
- Example XLSX содержат только synthetic data.
- Рабочие `.btw` не распространяются вместе с публичным source.

История внутренних версий 0.6x-0.83.x намеренно не перенесена в публичный changelog: 0.85 является первой публичной точкой проекта.
