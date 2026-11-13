# Автодеплой smeta-checker на Ubuntu

## Схема

Сервер сам проверяет `origin/dev` каждые 5 минут через systemd timer.
Если commit изменился, скрипт делает backup БД и blobs, собирает образы,
перезапускает контейнеры и проверяет health. При ошибке откатывает код и запускает старые образы.

## Установка на сервере

1. Установить deploy-ключ (read-only) для GitHub:
   ```
   ssh-keygen -t ed25519 -C "smeta-deploy" -f /root/.ssh/smeta_deploy -N ""
   cat /root/.ssh/smeta_deploy.pub
   ```
   Добавить публичный ключ в GitHub → Settings → Deploy keys (read-only).

2. Настроить git remote на SSH:
   ```
   cd /root/smeta-checker-dev
   git remote set-url origin git@github.com:koden585/smeta-checker.git
   git config core.sshCommand "ssh -i /root/.ssh/smeta_deploy -o IdentitiesOnly=yes"
   ```

3. Скопировать скрипт:
   ```
   cp scripts/deploy.sh /usr/local/bin/smeta-deploy.sh
   chmod 750 /usr/local/bin/smeta-deploy.sh
   ```

4. Создать systemd unit:
   ```
   cat > /etc/systemd/system/smeta-deploy.service <<'EOF'
   [Unit]
   Description=Smeta checker auto-deploy
   After=docker.service
   Requires=docker.service

   [Service]
   Type=oneshot
   ExecStart=/usr/local/bin/smeta-deploy.sh
   StandardOutput=journal
   StandardError=journal
   EOF

   cat > /etc/systemd/system/smeta-deploy.timer <<'EOF'
   [Unit]
   Description=Run smeta checker auto-deploy every 5 minutes

   [Timer]
   OnBootSec=2min
   OnUnitActiveSec=5min
   AccuracySec=30s
   Persistent=true

   [Install]
   WantedBy=timers.target
   EOF

   systemctl daemon-reload
   systemctl enable --now smeta-deploy.timer
   ```

5. Проверить:
   ```
   systemctl status smeta-deploy.timer
   journalctl -u smeta-deploy.service -n 50
   ```

## Ручной запуск

```
/usr/local/bin/smeta-deploy.sh
```

## Откат

При неудаче скрипт сам откатывает. Вручную:
```
cd /root/smeta-checker-dev
git reset --hard <previous_commit>
docker compose up -d --no-build
```

## Опциональные стеки на production

По умолчанию автодеплой поднимает также `compose.monitoring.yaml` и
`compose.glitchtip.yaml` (Grafana/Prometheus/Loki и self-hosted GlitchTip).
Отключить: `SMETA_COMPOSE_FILES=""` в окружении systemd-юнита.

GlitchTip при первом деплое автоматически создаёт пользователя/организацию/проект
и записывает `GLITCHTIP_DSN` и `NUXT_PUBLIC_SENTRY_DSN` в `.env`, затем
перезапускает `api` и `workspace`. Логин/пароль bootstrap-пользователя:
`admin@localhost` / `change-me` (смените в БД при необходимости).
