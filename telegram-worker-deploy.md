# Развёртывание Telegram worker

Инструкция для отдельного сервера, где уже установлены Python, venv и git. Worker забирает Telegram-уведомления из ЛК через HTTPS API и отправляет их через Telegram Bot API. Backend ЛК Telegram напрямую не вызывает.

## 1. Подготовить файлы

На сервере worker-а клонируйте репозиторий в `/opt`. В результате должен получиться каталог `/opt/Lk_telegram_worker`:

```bash
cd /opt
git clone <URL_РЕПОЗИТОРИЯ_WORKER> Lk_telegram_worker
cd /opt/Lk_telegram_worker
```

В каталоге должны быть:

- `telegram_worker.py`
- `env.exampletg`

Создайте рабочий `.env`:

```bash
cp env.exampletg .env
nano .env
```

Пример `.env`:

```env
LK_API_BASE=https://leadrecordwh.ru
TELEGRAM_WORKER_API_TOKEN=тот_же_секрет_что_в_backend_env
TELEGRAM_BOT_TOKEN=токен_telegram_бота
WORKER_ID=nl-worker-1
POLL_SECONDS=10
BATCH_SIZE=50
REQUEST_TIMEOUT_SECONDS=20
```

Важно:

- `TELEGRAM_WORKER_API_TOKEN` должен совпадать с `.env` backend-а ЛК.
- `TELEGRAM_BOT_TOKEN` хранится только на worker-сервере.
- На backend должны быть заданы `TELEGRAM_CHAT_ID`, `TELEGRAM_WORKER_API_TOKEN`, `NOTIFICATIONS_TELEGRAM_ENABLED=True`.

## 2. Установить зависимости

```bash
cd /opt/Lk_telegram_worker
python3 -m venv venv
./venv/bin/pip install requests python-dotenv
```

Проверка ручного запуска:

```bash
./venv/bin/python telegram_worker.py
```

Если всё настроено правильно, в логах будет:

```text
Telegram worker started
```

Остановить ручной запуск: `Ctrl+C`.

## 3. Создать systemd service

Создайте файл:

```bash
sudo nano /etc/systemd/system/lk-telegram-worker.service
```

Содержимое:

```ini
[Unit]
Description=LK Telegram Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/Lk_telegram_worker
ExecStart=/opt/Lk_telegram_worker/venv/bin/python /opt/Lk_telegram_worker/telegram_worker.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/Lk_telegram_worker/.env

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable lk-telegram-worker
sudo systemctl start lk-telegram-worker
```

Проверка:

```bash
sudo systemctl status lk-telegram-worker
sudo journalctl -u lk-telegram-worker -f
```

## 4. Проверить API ЛК

С worker-сервера:

```bash
TOKEN='тот_же_TELEGRAM_WORKER_API_TOKEN'

curl -i -X POST https://leadrecordwh.ru/internal/telegram-notifications/claim \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  --data '{"workerId":"manual-check","limit":1}'
```

Нормальный ответ:

```json
{"items":[]}
```

Если токен неверный, будет:

```text
401 Unauthorized
```

Если приходит `405 Not Allowed`, значит nginx на сервере ЛК не прокидывает `/internal/` в backend.

## 5. Nginx на сервере ЛК

В HTTPS `server` блоке ЛК должен быть location:

```nginx
location /internal/ {
    proxy_pass http://127.0.0.1:8000/internal/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

После изменения:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 6. Обновление worker-а

```bash
cd /opt/Lk_telegram_worker
git pull
sudo systemctl restart lk-telegram-worker
sudo journalctl -u lk-telegram-worker -n 50
```
