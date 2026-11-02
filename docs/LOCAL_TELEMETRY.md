# Локальная телеметрия

Отдельный Compose-проект `smeta-telemetry`, сеть `smeta-checker-local_default`.
Приложение должно быть запущено. Команды из корня репозитория:

```powershell
.\scripts\telemetry-local.ps1 start
.\scripts\telemetry-local.ps1 status
.\scripts\telemetry-local.ps1 stop
```

Остановка этой группы не останавливает приложение или проверки. Для подключения
новых ключей Langfuse нужно пересоздать API и обработчики приложения, когда нет
активной проверки. Обычный `docker compose restart` окружение не обновляет.

## Где смотреть

- Grafana: http://localhost:3001/d/smeta-local — состояние API, БД, Redis,
  очереди позиций и логи. Логин `admin`, пароль `GRAFANA_ADMIN_PASSWORD` в `.env.local`.
- Langfuse: http://localhost:3002 — запросы к модели, входной контекст, ответы,
  длительность, токены, этапы и ошибки. Логин `TELEMETRY_ADMIN_EMAIL`, пароль
  `TELEMETRY_ADMIN_PASSWORD` в `.env.local`. Проект `VOR checks`.
- Prometheus: http://localhost:9090/targets — проверка сбора метрик.
- Предупреждения: http://localhost:9090/alerts — недоступность API/БД/Redis,
  повторяющиеся 5xx и позиции активных проверок без обновления. Отправка
  уведомлений в почту или мессенджер не подключена.
- Loki: http://localhost:3100/ready — готовность хранилища логов.

В Langfuse один запуск связан с сессией `audit-<run_id>`, одна позиция — отдельная
трассировка. В Grafana Explore выбрать Loki и искать, например:

```logql
{project="smeta-checker-local"} |= "<run_id>"
{project="smeta-checker-local",service="worker-normative"}
```

Логи собираются через Docker API с фильтром только двух проектов. Другие проекты
компьютера не собираются. OCR-изображения и исправления по-прежнему находятся в
хранилище приложения; телеметрия не заменяет их резервную копию.

## Данные и ограничения

- Постоянные каталоги привязаны к `TELEMETRY_DATA_ROOT`, на этом ПК
  `D:/VOR/telemetry`. Исключение — ClickHouse: Linux-том
  `smeta-telemetry-clickhouse-data` внутри виртуального диска Docker Desktop
  `D:/VOR/docker-desktop/DockerDesktopWSL`. MergeTree требует атомарного
  переименования каталогов, которое не работает в обычной папке Windows.
  При переносе нужно отдельно выгрузить этот том; копии `D:/VOR/telemetry`
  недостаточно. Docker Desktop должен хранить свой виртуальный диск на D.
- Prometheus: 15 дней, максимум 2 ГБ TSDB; Loki: 7 дней; события Langfuse: 30 дней.
  Встроенная политика Langfuse требует Enterprise, поэтому здесь используются
  обычные TTL таблиц ClickHouse `events_full` / `events_core` и lifecycle S3.
  Эта локальная политика распространяется на все объекты S3-бакета телеметрии,
  включая будущие вложения. Не используйте этот бакет для датасетов, которые
  нужно хранить дольше. ПД, OCR и пользовательская разметка приложения хранятся отдельно.
  Сроки хранения не являются жёстким общим дисковым лимитом; очистка асинхронная.
- Docker-логи каждого сервиса: 3 файла по 10 МБ. ClickHouse-файлы диагностики:
  2 по 20 МБ. Остальные БД и S3 требуют контроля свободного места.
- UI и служебные порты опубликованы только на `127.0.0.1`. Базы Langfuse
  отдельные от рабочей БД приложения. Внешняя продуктовая аналитика выключена.
- Метрики БД читаются ролью `smeta_monitor`: `pg_monitor` и SELECT только на
  `runs` и `position_jobs`, транзакции по умолчанию read-only. Новые таблицы не
  предоставляются автоматически.
- Из PostgreSQL берутся сохранённые статусы очередей, поэтому они доступны
  независимо от перезапуска API. Десять минут без обновления — повод проверить
  задачу, а не доказательство зависания.
- Трассировки содержат контекст ПД/ВОР и ответы модели и остаются в локальном
  Langfuse. Включено для будущих запросов; старые запросы автоматически не
  восстанавливаются. Сам сбор не вызывает модель и не расходует её квоту.
- Стоимость не следует считать нулевой, если отсутствуют тарифы модели.
  Достоверны записанные токены и число запросов; денежные оценки требуют тарифов.
- Метрики HTTP относятся к API; счётчики, записанные только в процессах Celery,
  через `/metrics` API не видны. Для состояния задач используются запросы к БД,
  для LLM — Langfuse. Метрики Linux node-exporter не выдаются за состояние Windows.

Секреты находятся только в игнорируемом `.env.local`; его нельзя коммитить.
S3-совместимое хранилище RustFS используется для событий Langfuse. При переносе
нужно сохранить все каталоги телеметрии и секреты, включая ключ шифрования.
Образы после установки закреплены по digest в Compose.

После обновления схемы Langfuse нужно проверить, что TTL сохранился:

```powershell
docker exec smeta-telemetry-langfuse-clickhouse-1 clickhouse-client --multiquery --query 'SET enable_full_text_index=1; ALTER TABLE events_full MODIFY TTL toDateTime(start_time) + INTERVAL 30 DAY DELETE; ALTER TABLE events_core MODIFY TTL toDateTime(start_time) + INTERVAL 30 DAY DELETE;'
.\backend\.venv\Scripts\python.exe .\scripts\telemetry-bootstrap-s3.py
```

Для API-чтения в Langfuse v4 используется `/api/public/v2/observations` с
ограничением времени и `traceId`. Старый `/api/public/traces` возвращает 404
в режиме `events_only`; это не означает потерю трассировки.

Основа настройки: [официальный Docker Compose Langfuse](https://langfuse.com/self-hosting/deployment/docker-compose),
[инициализация проекта](https://langfuse.com/self-hosting/administration/headless-initialization),
[Docker-логи через Alloy](https://grafana.com/docs/alloy/latest/monitor/monitor-docker-containers/).
