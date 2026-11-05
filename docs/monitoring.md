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
на отдельном VPS и подключать к нему каждое приложение:

1. Скопируйте на VPS `compose.yaml`, `compose.monitoring.yaml`,
   `compose.glitchtip.yaml`, `compose.phoenix.yaml`, каталог `monitoring/`
   и заполните `.env`.
2. Поставьте reverse proxy с TLS (Caddy/Traefik/nginx) перед UI:
   Grafana (3000), Prometheus (9090), Loki (3100), GlitchTip (8000),
   Phoenix (6006). Все `*_BIND` по умолчанию `127.0.0.1`: открывайте
   наружу только через proxy, а не публикацией портов.
3. Phoenix: добавьте auth на уровне proxy (basic auth, OIDC или VPN) —
   у самого Phoenix встроенного логина может не быть.
4. В каждом приложении укажите удалённые endpoints вместо локальных:
   `GRAFANA_ROOT_URL`, `PHOENIX_COLLECTOR_ENDPOINT=https://<vps>/phoenix`,
   `PHOENIX_PUBLIC_URL=https://<vps>/phoenix`, `GLITCHTIP_DSN` из UI GlitchTip.
5. Ограничьте firewall: снаружи только 443 (и 22 для себя).
