# Мониторинг smeta-checker через Grafana

Опциональный стек: Grafana + Prometheus + Loki + exporters. Основной
`compose.yaml` его не поднимает, чтобы не нагружать CPU-only сервер.

## Запуск

```bash
docker compose -f compose.yaml -f compose.monitoring.yaml up -d
```

Grafana: `http://127.0.0.1:3001` (логин/пароль из `.env`:
`GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`). По умолчанию используется
`admin` / `admin`: задайте надёжный пароль и ограничьте доступ к порту.

Остановить только мониторинг:

```bash
docker compose -f compose.yaml -f compose.monitoring.yaml stop grafana prometheus loki promtail
```

## Что отслеживать

### Инфраструктура
- CPU, RAM, диск, сеть хоста (node-exporter).
- Контейнеры: CPU/RAM/рестарты (cAdvisor).
- PostgreSQL: подключения, транзакции, размер БД, медленные запросы (postgres-exporter; в БД уже включён `log_min_duration_statement=1000`).
- Redis: память, ключи, hit/miss, подключённые клиенты (redis-exporter).
- Доступность API и Nuxt: HTTP 2xx/пробы (blackbox-exporter на `/healthz` и `/`).

### Приложение
- Логи всех контейнеров через Loki/Promtail (фильтр по `container`).
- Ошибки 4xx/5xx в логах API/Nuxt.
- Время ответа healthcheck (blackbox probe duration).
- LLM-трейсинг — через Phoenix (OTLP, compose.phoenix.yaml), Grafana его не дублирует.

### Очереди и воркеры
- Живость `worker-pdf` / `worker-llm` (pidfile healthcheck в compose).
- Длина очередей Celery в Redis (метрика `redis_db_keys`/`redis_key_size` или ручной `LLEN`).
- Повторы/сбои задач — в логах воркеров.

### Аудиты (бизнес-метрики)
- Количество запусков и их статусы (`running`/`done`/`failed`) — SQL по таблице `runs`.
- Длительность подготовки PDF и позиций — логи `audit_tasks`.
- Ошибки LLM/OCR — логи воркеров, трейсы Phoenix, события GlitchTip.

### Безопасность и вход (AD)
- Неудачные попытки входа (401/403) в логах Nuxt.
- Блокировки по rate limit (3 попытки на учётную запись / 20 с IP за 15 минут).
- Отзывы сессий при недоступности AD.
- TLS/STARTTLS ошибки LDAP.
- Доступ к Grafana: рекомендуется включить LDAP-аутентификацию Grafana (см. ниже).

## Grafana и Active Directory

Grafana поддерживает LDAP. Пример конфигурации — `monitoring/grafana/ldap.toml.example`.
Чтобы включить:
1. Скопируйте пример в `monitoring/grafana/ldap.toml`.
2. Заполните параметры AD (сервер, base DN, bind DN).
3. Добавьте в `compose.monitoring.yaml` для сервиса `grafana`:
   - env `GF_AUTH_LDAP_ENABLED=true`
   - env `GF_AUTH_LDAP_CONFIG_FILE=/etc/grafana/ldap.toml`
   - volume `./monitoring/grafana/ldap.toml:/etc/grafana/ldap.toml:ro`
4. Перезапустите Grafana.

Пока AD для Grafana не настроен, используйте локального admin и ограничьте доступ к порту (bind на `127.0.0.1`).

## Рекомендуемые дашборды

- Node Exporter Full (id 1860) — CPU/RAM/диск.
- PostgreSQL (id 9628) — БД.
- Redis (id 11835) — кэш/брокер.
- cAdvisor (id 14282) — контейнеры.
- Blackbox (id 7587) — доступность.

## GlitchTip + Phoenix

Ошибки API/воркеров/Nuxt уходят в GlitchTip (Sentry-совместимый DSN),
LLM-трейсы, промпты и evals — в Arize Phoenix (OTLP). Оба стека опциональны
и поднимаются отдельными compose-файлами поверх основного:

```bash
docker compose -f compose.yaml -f compose.monitoring.yaml \
  -f compose.glitchtip.yaml -f compose.phoenix.yaml up -d
# или: make observability
```

GlitchTip: после первого запуска откройте его UI (по умолчанию
`http://127.0.0.1:8001`), создайте организацию и проект, скопируйте DSN
в `GLITCHTIP_DSN` в `.env` и перезапустите `api`/`workspace`. Frontend
использует тот же DSN через `NUXT_PUBLIC_SENTRY_DSN`.

Phoenix: задайте `PHOENIX_COLLECTOR_ENDPOINT` (по умолчанию
`http://phoenix:6006` внутри Docker-сети), `PHOENIX_PROJECT_NAME` и
`PHOENIX_ENVIRONMENT`. При пустом endpoint трейсинг отключён.

## Телеметрия и конфиденциальность

Во внешнюю телеметрию могут попадать сведения ВОР, фрагменты ПД, промпты,
ответы и цитаты. Используйте только разрешённые для передачи документы;
отключение или маскирование телеметрии не отключает передачу данных выбранному
LLM-провайдеру. Код поддерживает `AUDIT_TELEMETRY_REDACT_PDF=1` для скрытия
текста фрагментов ПД и `AUDIT_TELEMETRY_MASK_PROMPTS=1` для скрытия сообщений
`generate-audit`; метаданные остаются. Эти настройки не гарантируют удаление
всех чувствительных данных из наблюдений.

**В текущем `compose.yaml` эти две переменные не передаются в контейнеры**:
одного добавления в `.env` недостаточно. Если маскирование обязательно,
организуйте явную передачу в нужные контейнеры и проверьте фактическую
конфигурацию до использования чувствительных документов; иначе отключите
внешнюю телеметрию. Доставка асинхронная, поэтому вызов `flush()` не является
подтверждением появления трейса в Phoenix. Ошибка телеметрии не
доказывает ошибку аудита, а наличие цитаты — корректность вывода модели.

## Развёртывание observability на VPS

Полный стек (Grafana/Prometheus/Loki + GlitchTip + Phoenix) можно поднять
на отдельном VPS и подключать к нему каждое приложение.
Автономный бандл — каталог `observability/` в корне репозитория:
собственные postgres/redis, мониторинг, GlitchTip, Phoenix (SQLite на volume),
Traefik-маршруты и скрипт provision GlitchTip.

Развёрнуто на `new_vps` (193.233.85.147, док `observability/`, `/root/smeta-observability`):

| Сервис | URL | Доступ |
|---|---|---|
| Grafana | https://grafana.nikgretenok.online | логин `admin`, пароль в `.env` на сервере |
| GlitchTip | https://glitchtip.nikgretenok.online | `admin@localhost`, пароль в `.env` на сервере; регистрация закрыта |
| Phoenix UI | https://phoenix.nikgretenok.online | Traefik basic-auth `obs`, пароль в `.env` на сервере |
| Phoenix OTLP | https://phoenix.nikgretenok.online/v1/traces | открыт (бэкенды не умеют basic-auth) |
| Prometheus / Loki | только через Grafana proxy или `ssh -L` | порты наружу не выведены |

Подключение приложения (значения в `.env` приложения):

```bash
GLITCHTIP_DSN=https://66d6ba5f42804c22b17296c0f2faf811@glitchtip.nikgretenok.online/1
NUXT_PUBLIC_SENTRY_DSN=$GLITCHTIP_DSN
PHOENIX_COLLECTOR_ENDPOINT=https://phoenix.nikgretenok.online
PHOENIX_PROJECT_NAME=smeta-checker
PHOENIX_PUBLIC_URL=https://phoenix.nikgretenok.online
```

CLI: `PHOENIX_BASE_URL=https://phoenix.nikgretenok.online npx @arizeai/phoenix-cli trace list`.
MCP: у Phoenix v12 отдельного `/mcp` нет (GET возвращает SPA), у GlitchTip `/mcp`
требует токен (`invalid_token` без него).

### Доступ при блокировке публичного IP (RKN)

Публичный IP VPS может быть недоступен из отдельных сетей. Всё, что собирается
внутри VPS (Prometheus скрейпы, Loki, blackbox, Postgres/Redis exporters),
от блока не зависит и продолжает работать. А вот ingest снаружи
(OTLP из приложений, Sentry-envelopes, просмотр UI) по публичным доменам
из забаненной сети не дойдёт — для этого есть tailnet-путь.

На Traefik заведены HTTP-алиасы (роутеры `*-ts` в
`observability/traefik/observability-tailnet.yml`, HTTP достаточно —
шифрование даёт сам WireGuard):

| Сервис | Tailnet URL |
|---|---|
| Grafana | http://grafana.1987817.tail3de041.ts.net |
| GlitchTip UI + ingest | http://glitchtip.1987817.tail3de041.ts.net |
| Phoenix UI | http://phoenix.1987817.tail3de041.ts.net (basic-auth `obs`) |
| Phoenix OTLP | http://phoenix.1987817.tail3de041.ts.net/v1/traces (открыт) |

Условия: устройство в tailnet (`tailscale up`), резолв алиасов —
MagicDNS поддомены не отдаёт, поэтому на каждом клиенте нужна строка
в `/etc/hosts` (или split-horizon в tailnet DNS):

```text
100.98.72.30 grafana.1987817.tail3de041.ts.net glitchtip.1987817.tail3de041.ts.net phoenix.1987817.tail3de041.ts.net
```

DSN/OTLP для приложений в забаненной сети — tailnet-варианты:

```bash
GLITCHTIP_DSN=http://66d6ba5f42804c22b17296c0f2faf811@glitchtip.1987817.tail3de041.ts.net/1
PHOENIX_COLLECTOR_ENDPOINT=http://phoenix.1987817.tail3de041.ts.net
PHOENIX_PUBLIC_URL=http://phoenix.1987817.tail3de041.ts.net
```

Проверено: OTLP-спан через tailnet-алиас долетел и виден в Phoenix
(`smoke-tailnet-otlp`), все три UI отвечают (Grafana 302→login,
GlitchTip 200, Phoenix 401 без auth). Tailscale node cert
(`1987817.tail3de041.ts.net`, LE) лежит в
`/etc/dokploy/traefik/dynamic/tscerts/` и подключён как defaultCertificate —
понадобится, если решишь терминировать HTTPS на bare node name.

Доступ к серверу: SSH только через Tailscale exit-node
(`tailscale up`, `tailscale set --exit-node=100.98.72.30`),
прямой SSH с обычной сети висит на баннере.

Хост `new_vps` управляется Dokploy+Traefik: маршруты лежат в
`/etc/dokploy/traefik/dynamic/observability.yml` (источник — `observability/traefik/`).
Свободного места на диске мало (~6 ГБ): следить за ростом `phoenix.db`
и retention Prometheus (15d) / Loki.

Для нового хоста без Dokploy: скопируйте `observability/`, заполните `.env`,
поставьте reverse proxy с TLS (Caddy/Traefik/nginx) перед Grafana (3000),
GlitchTip (8000), Phoenix (6006). Phoenix: auth на уровне proxy (basic auth,
OIDC или VPN) — у самого Phoenix встроенного логина нет. Ограничьте firewall:
снаружи только 443 (и 22 для себя).
