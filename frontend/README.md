# КДФП · Nuxt workspace для smeta-checker

Nuxt 4 / Vue 3 / TypeScript / Nuxt UI. Этот интерфейс работает с `api.py` в этом же репозитории: `/api/v1`, постоянные проекты/версии/audit runs, Celery либо существующий fallback.

## Локальный запуск на Windows

Из корня проекта:

```powershell
uv sync --frozen
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend run build
./start-workspace.ps1
```

Требуются Python 3.12, Node 24, pnpm 11.25.0 и PostgreSQL. В игнорируемом корневом `.env` задаются `DATABASE_URL`, `BLOB_STORE_URL`, `AUDIT_API_TOKEN`. Для настоящего аудита нужны `YANDEX_CLOUD_API_KEY` и `YANDEX_CLOUD_FOLDER`; без них загрузка/история/просмотр/правила оформления доступны, запуск аудита отвечает понятной ошибкой 503. После изменения серверных настроек перезапустите launcher.

Launcher запускает API на `127.0.0.1:8100` и интерфейс на **http://127.0.0.1:3010/**, передаёт токен только процессу Nitro. Ctrl+C завершает оба дочерних процесса; журналы находятся в `.audit_cache/runtime/`. Другие порты: `python -m scripts.run_workspace --api-port 8101 --ui-port 3011`.

Подготовленная в этой рабочей копии PostgreSQL находится в контейнере `smeta-workspace-db`, данные — в отдельном Docker volume `smeta-workspace-data`, порт — loopback 55433. Случайные пароль БД и API token записаны только в игнорируемый `.env`. Для возобновления после перезапуска Docker: `docker start smeta-workspace-db`, затем `./start-workspace.ps1`.

Development: задайте серверные `NUXT_BACKEND_URL` и `NUXT_BACKEND_TOKEN`, затем `pnpm --dir frontend dev`. Токен нельзя переносить в `runtimeConfig.public`, браузерное хранилище или `NUXT_PUBLIC_*`.

## Пользовательский цикл

1. На странице загрузки сохраните XLSX/CSV и PDF в выбранном проекте. Повторная загрузка тех же байтов использует существующую версию.
2. Выберите одну версию каждого документа, проверьте привязки. При проблемах приложение требует явного решения выполнить только доступные проверки.
3. Каждая выбранная ВОР создаёт свой аудит. Страница показывает стадию и число завершённых позиций; её можно закрыть и открыть через историю.
4. После завершения доступны очереди, поиск/пагинация, Excel/Word и окно сверки. Результат привязан к `run_id`, а не к текущему состоянию проекта.
5. В окне сверки — точный лист/ячейки ВОР, PDF, физическая/печатная страницы, цитаты, дерево разделов и поиск. Переход на другую страницу не подтверждает позицию. Выделение появляется только при однозначной локализации цитаты в текстовом слое.

Проверка оформления вызывается отдельно для выбранного листа XLSX, без LLM. CSV не выдаётся за редактируемую книгу Excel. ONLYOFFICE, сохранение редакторских ревизий и PDF-заметки в этой версии интеграции ещё не подключены.

## Контракты и проверки

```powershell
python -m scripts.export_frontend_openapi
pnpm --dir frontend run api:generate
pnpm --dir frontend run api:check
pnpm --dir frontend run fmt:check
pnpm --dir frontend run lint
pnpm --dir frontend run typecheck
pnpm --dir frontend run test:unit
pnpm --dir frontend run build
```

Источник схем — реальные Pydantic DTO в `workspace_models.py`, `audit_models.py` и `api.py`. Сгенерированный SDK/TS/Zod находится в `app/generated/`; вручную его не редактируют. Экспорт OpenAPI не открывает БД и не запускает OCR/LLM.

Для E2E используется отдельная PostgreSQL `smeta_test_ui`, `tests/workspace_server.py`, синтетические файлы и явно подменный тестовый LLM-клиент. Это испытание интеграции, не живой прогон модели. Начальный запуск требует пустую БД и новый `--root`; `--resume` использует уже созданный тестовый комплект без удаления данных. Обычный production entry point тестовый клиент не подключает.

После запуска fixture API на 8116 и built Nuxt на 3016 задайте `FRONTEND_URL=http://127.0.0.1:3016` и `E2E_FIXTURE_ROOT=<полный путь fixture>`, затем `pnpm --dir frontend run test:e2e`. GitHub Actions содержит эти шаги с отдельной тестовой БД.

## Контейнерный вариант

`docker compose -f compose.frontend.yaml --profile frontend up --build` добавляет локальный Nuxt к API из основного Compose на Linux с host networking. `AUDIT_API_TOKEN` берётся из серверного `.env`.

BFF рассчитан на локальную работу: loopback, допустимый Host, same-origin запись и подписанная HttpOnly-сессия. Этот профиль не является публичным многопользовательским входом. Windows использует нативный launcher.
