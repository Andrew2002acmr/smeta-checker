# Работа с проектом

Монорепозиторий: `backend/` (Python 3.14, uv) + `frontend/` (Nuxt 4, pnpm).
Общее: `compose.yaml`, `.env` / `.env.example`, `docs/`.

- Изменяйте только согласованный worktree; не переносите правки в `dev` или другие деревья без отдельного задания.
- Backend: команды из `backend/` — `uv sync --frozen`, `uv run pytest`, `uv run ruff check .`, `uv run pyright`. Пины в `backend/uv.lock`.
- Живые обращения к модели расходуют квоту и передают фрагменты документов провайдеру. Не заменяйте их тестовыми ответами при заявлении о live-проверке.
- Секреты храните только в игнорируемом корневом `.env` либо окружении. Не записывайте их в команды, отчёты и коммиты.

## Frontend (Nuxt workspace)

- Каталог `frontend/` (pnpm 11.25.0, Node 24). Установка: `pnpm --dir frontend install --frozen-lockfile`.
- После изменения Python DTO (из `backend/`): `uv run python -m scripts.export_frontend_openapi`, затем `pnpm --dir frontend run api:generate`.
- Проверки: `pnpm --dir frontend run fmt:check`, `lint`, `typecheck`, `test:unit`, `api:check` (`PYTHON` — абсолютный путь к `backend/.venv/bin/python`), `build`.
- E2E: отдельная БД `smeta_test_ui` + `backend/tests/workspace_server.py` (синтетический LLM-клиент, без квоты), затем `pnpm --dir frontend run test:e2e` с `FRONTEND_URL` и `E2E_FIXTURE_ROOT`. Детали — в `frontend/README.md`.
- `app/generated/` и `openapi.json` — генерируемые, вручную не править. `public/pdfjs` собран из `pdfjs-dist@6.4.299`.
- BFF-токен (`NUXT_BACKEND_TOKEN`) только в серверном окружении; секреты — только `.env`.

## Дизайн (pen.dev)

- Эталон дизайна — pen.dev-документ `untitled.pen` в каталоге `/Users/nikgretenok/.pencil/documents/47db7130-ca63-463a-bbd5-043c77cce694/`: фреймы `01 · История проверок`, `02 · Новая проверка (загрузка)`, `03 · Результаты аудита`, `04 · Сверка позиции` + переменные темы (light/dark).
- Любое изменение UI в `frontend/` и любое изменение дизайна в pen.dev вносятся в оба места в рамках одной задачи: код ↔ канвас всегда синхронизированы.
- Порядок при правке UI: сначала обнови фрейм в pen.dev через MCP `pencil` (`get_app_state` → `execute`/`browser`), затем внеси то же изменение в код, затем сверь скриншот кода с канвасом.
- Порядок при правке дизайна в pen.dev: сразу перенеси в код теми же токенами из `frontend/app/assets/css/theme.css`, затем прогони проверки frontend.
- Не читай `.pen`-файлы напрямую (зашифрованы) — только через MCP-инструменты `pencil`.

## Браузер

Для интерактивной проверки Nuxt используйте `agent-browser` CLI или его MCP-инструменты. Справка: `agent-browser --help`.

1. Используйте именованную сессию: `agent-browser --session smeta-check open http://127.0.0.1:3000`.
2. Получайте свежие ссылки элементов: `agent-browser --session smeta-check snapshot -i`.
3. Выполняйте действия через `click @ref`, `fill @ref "текст"`, `press Enter`; после перерисовки получайте новый snapshot.
4. Сохраняйте снимки и отчёты в игнорируемую `.audit_cache/`. Проверяйте содержимое изображения, прежде чем заявлять о визуальной проверке.
5. Для существующего браузера используйте только явно выбранный собственный CDP endpoint (`--cdp <port-or-url>`). Не используйте `--auto-connect` к пользовательскому браузеру без разрешения.
6. Закрывайте только свою сессию. Не используйте `close --all`.

При отсутствии загруженного MCP используйте CLI; наличие `.mcp.json` не доказывает, что сервер подключён в текущей сессии. Новый Chrome не требуется, если доступен системный Chrome через `--executable-path` или уже запущенный собственный браузер через `--cdp`.

Документация: https://agent-browser.dev/installation и https://agent-browser.dev/cdp-mode.

## Наблюдаемость (GlitchTip + Phoenix)

- Ошибки API/воркеров/Nuxt: GlitchTip (Sentry-совместимый), DSN в `GLITCHTIP_DSN`.
- LLM-трейсы, промпты, evals: Arize Phoenix (OTLP), compose.phoenix.yaml.
- MCP:
  - Phoenix: `http://localhost:6006/mcp` (встроен с v19).
  - GlitchTip: включить `GLITCHTIP_ENABLE_MCP=true`, endpoint `/mcp`.
- CLI:
  - Phoenix: `npx @arizeai/phoenix-cli ...` (`px`), включая `trace list/get`.
  - GlitchTip: `glitchtip-cli` (beta).
- Skills: `npx skills add Arize-ai/phoenix` для Phoenix.
- При работе с проектами и отладке наблюдаемости используй CLI/MCP этих систем, а не только логи.

