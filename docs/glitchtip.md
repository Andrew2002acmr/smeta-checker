# Self-hosted GlitchTip (Sentry-совместимый error tracking)

Опциональный стек. Основной `compose.yaml` его не поднимает.

## Запуск

```bash
docker compose -f compose.yaml -f compose.glitchtip.yaml up -d
```

GlitchTip UI: `http://127.0.0.1:8001` (порт из `GLITCHTIP_PORT`).

## Настройка

1. В `.env` задайте `GLITCHTIP_SECRET_KEY` (любая длинная случайная строка) и при необходимости `GLITCHTIP_DOMAIN`.
2. Запустите стек, откройте UI, создайте организацию и проект.
3. Скопируйте DSN проекта в `.env`: `GLITCHTIP_DSN=<dsn>` (и/или `NUXT_PUBLIC_SENTRY_DSN` для фронтенда).
4. Перезапустите `api` и `workspace`: `docker compose ... restart api workspace`.

GlitchTip использует существующие `db` (отдельная БД `glitchtip`) и `redis` (db 2). БД создаётся автоматически контейнером `glitchtip-db-init`.

## Остановка

```bash
docker compose -f compose.yaml -f compose.glitchtip.yaml stop glitchtip
```

## Примечания

- Для отправки почты задайте `GLITCHTIP_EMAIL_URL` (SMTP). По умолчанию `consolemail://` (письма в логах).
- Внешний Sentry/GlitchTip по-прежнему поддерживается через `GLITCHTIP_DSN`/`SENTRY_DSN`.