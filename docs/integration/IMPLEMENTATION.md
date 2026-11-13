# Подключение Nuxt к smeta-checker (порт PR-42)

20 октября 2026. Ветка `nikgritenok/Принять-PR-аккуратно`, база — `dev` на `adafdf2`
(PR-43 уже влит). PR-42 собран на `7d60864`, поэтому принят портированием
по частям под новый движок, а не прямым мерджем.

## Монорепозиторий (21.10.2026)

Корень прибран: `backend/` — весь Python (API, Celery, тесты,
Dockerfile, `uv.lock`), `frontend/` — Nuxt. В корне только `compose.yaml`,
`.env.example`, `docs/` и метафайлы. История файлов сохранена через `git mv`.
Нативный запуск без Docker не поддерживается (`start-workspace.ps1`,
`scripts/run_workspace.py` и `.devcontainer/` удалены).

- Единый `compose.yaml`: `api`, `workspace` (Nuxt),
  `db`, `redis`, `worker-pdf`, `worker-llm`, `migrate`.
  Отдельный `compose.frontend.yaml` удалён.
- Порт Nuxt публикуется только на host loopback (`127.0.0.1:3000`).
  BFF-режим `NUXT_TRUST_PROXY=1` доверяет Docker-шлюзу, сохраняя Host
  allowlist, same-origin и сессию. По умолчанию выключен.
- Backend-образ копирует `scripts/` (нужен `scripts.audit_worker` для
  fallback-процесса).
- `.env` — в корне монорепо; backend читает его через
  `audit_settings._project_env_path`.
- Инструкция с нуля: [docs/docker-quickstart.md](../docker-quickstart.md).

## Удаление Streamlit (21.10.2026)

Nuxt — единственный UI. Удалены `backend/app.py`, `backend/audit_inspector.py`,
зависимость `streamlit` (79 пакетов вместо 95), сервис `ui` из compose,
`UI_PORT` из `.env.example`, AppTest-скрипты (`test_app_flow.py`,
`compare_live_branches.py`, `compare_real_documents.py`, `run_live_yandex.py`).
`baseline_audit.run_baseline` переписан на прямой `run_audit` без AppTest —
те же входы и то же ядро, 5/5 позиций сверяются как раньше.

## Что работает

Интерфейс КДФП в `frontend/` обращается к настоящему API через Nitro BFF.
Импорт использует UUID проекта и явное назначение документа:
`POST /api/v1/documents` — multipart `project_id`, `kind`, `file`,
и либо `new_document=true`, либо `document_id` для новой редакции.
Сопоставления по имени файла и AI-именования нет.

- Постоянные проекты с ручным названием, архивом и восстановлением.
- Документы имеют отдельные UUID; исходное имя, метаданные и уточнения хранятся
  на редакции. Те же байты дедуплицируются только внутри выбранного документа.
- `GET /api/v1/projects/{id}/catalog` и `GET /api/v1/documents/{id}/detail` —
  каталог и карточка с редакциями; `PATCH` меняет название/архив/действующую редакцию.
  Для ВОР `last_run` относится к последнему запуску любой её редакции;
  `vor_updated` и `pd_updated` показывают подтверждённое отличие выбранной
  редакции ВОР и закреплённых редакций ПД от действующих. `run_freshness`:
  `current` — совпадают, `stale` — есть отличие, `unavailable` — сравнение
  или сведения о запуске недоступны, `null` — запуска нет. Архивная ПД
  недоступна; пустой сохранённый набор ПД допустим.
- `GET /api/v1/projects/{id}/audits/count` возвращает `{"count": N}` —
  точное число всех запусков проекта, независимо от пагинации истории.
- Проверка идёт по действующим редакциям всех проектных PDF без ручного
  выбора. Обозначения из ВОР сверяются с текстом ПД; отсутствующее
  обозначение — «Ошибка» позиции без вызова модели. Обновление ПД не
  переписывает старые запуски.
- Предварительная проверка ссылок ВОР на PDF до аудита.
- Отдельный audit run для каждой ВОР, стадии подготовки/проверки/финализации,
  реальные счётчики position jobs. Без выдуманных процентов.
- История сохранённых аудитов, фильтры/поиск/пагинация строк, Word-отчёт,
  восстановление выбранной позиции по URL после reload.
- Исходный лист и ячейки XLSX (из `PositionSource` движка), окно таблицы,
  точная версия PDF, PDF.js, физическая/печатная страницы, дерево разделов,
  поиск по PDF.
- Цитаты с привязкой к версии PDF. Подсветка только при однозначном совпадении
  в нативном текстовом слое; иначе честное сообщение и просмотр страницы.
- Проверка оформления выбранного листа XLSX через `vor_rules.py`, без LLM.

## Отличия от PR-42

- Provenance позиций берётся из `PositionSource` движка (лист/строка/ячейки),
  а не из PR-полей `sheet/source_row/source_cells`: движок PR-43 уже хранит
  координаты, дублировать их запрещено.
- `ResultDTO.cells` — `{адрес: значение}` (как в `PositionSource.cells`),
  а не `{поле: адрес}`. UI показывает стабильный адрес исходной строки.
- Мультилист XLSX: `_read_vor_sheet` + `_read_vor_frame`-комбайнер с per-sheet
  provenance (формулы, offsets, секции). Однолистовые книги идут старым путём
  без изменения индексов.
- Даты контракта нормализованы к UTC (`Z`): Zod `z.iso.datetime()` отклоняет
  `+07:00`, а хранилище отдаёт локальную зону PostgreSQL.
- Колонки отчёта: `problem` ← `Что не так`, `action` ← файл + `Где проверить`
  (в новом `build_user_report` нет колонки `Что сделать`).
- `backend/scripts/audit_worker.py` конвертирует `VersionRef` → `DocumentVersionRow`
  для `run_tasks_job`.
- `frontend/.nuxtrc` не переносился (артефакт `nuxt prepare`, не конфиг).
- `frontend/public/pdfjs` собран локально из `pdfjs-dist@6.4.299` (тот же
  набор файлов и байты, что в PR; сверено по `diff`/`cmp`).
- `frontend/openapi.json` и `app/generated/` перегенерированы из текущего API.

## Изменения на сервере

`workspace_models.py`, `workspace_api.py`, `workspace_service.py`: контракты,
маршруты, представление сохранённых результатов. Статусы/действия строятся
существующим `audit_reports.build_user_report`; правила не копируются в TS.

Новый run сохраняет снимок `position_inputs` в config. `load_run` возвращает
`indexed_results` + `created_at/finished_at`. Старым запускам вход
восстанавливается из их неизменяемой версии ВОР.

При preflight/старте проверяется принадлежность версий проекту; дубли имён PDF
отклоняются; загрузка читает не более лимита + 1 байт. Выдача оригиналов:
Range/206/416 + ETag. Исправлен Windows `file://C:\...`.

Production fallback — отдельный процесс `scripts.audit_worker` (настройки через
stdin, супервизор по отсутствию прогресса). История/progress — один лёгкий SQL
без JSON payloads. PDF: `use_layout(False)`, `hdr_info=False`, гард сложных
векторов (>5000 путей → ручная проверка), прогресс подготовки по страницам.

## Проверка (локально, 20.10.2026)

| Проверка | Результат |
|---|---|
| Python `uv run pytest` (из `backend/`) | **456 passed, 1 skipped, 76 subtests** |
| Ruff check / format новых файлов / Pyright | чисто |
| Nuxt typecheck, Oxlint, Oxfmt | чисто |
| Vitest | **8 passed** (контракты + маппинг статусов/чисел/страниц) |
| OpenAPI / SDK+Zod drift (`api:check`) | чисто |
| Nuxt production build | собран |
| Playwright на built Nuxt + реальном API/PG | **6 passed** |

E2E стенд: API 127.0.0.1:8116 + БД `smeta_test_ui`, UI 127.0.0.1:3016,
фикстура `backend/tests/workspace_server.py` (синтетический клиент LLM, без квоты).
После прогона серверы остановлены, БД оставлена для повтора.

## Границы версии

- ONLYOFFICE, редакторские ревизии, PDF-заметки не переносились; кнопок,
  выдающих их за подключённые, в интерфейсе нет.
- BFF — локальный (`assertLocal` + HttpOnly-сессия); публичный сервер требует
  отдельной авторизации.
- Живые обращения к LLM не выполнялись; E2E доказывает интеграцию, а не
  качество модели.
