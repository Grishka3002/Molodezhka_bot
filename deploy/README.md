# Deploy

## Вариант 1: обычный VPS с systemd

1. Установите Python 3.11+.
2. Скопируйте проект на сервер, например в `/opt/moodezhka-bot`.
3. Создайте `.env` рядом с `run_bots.py`.
4. Проверьте доступ к Telegram API:

```bash
python3 check_bots.py
```

5. Проверьте ручной запуск:

```bash
python3 run_bots.py
```

6. Скопируйте сервис:

```bash
sudo cp deploy/systemd/moodezhka-bot.service.example /etc/systemd/system/moodezhka-bot.service
sudo systemctl daemon-reload
sudo systemctl enable moodezhka-bot
sudo systemctl start moodezhka-bot
```

7. Логи:

```bash
journalctl -u moodezhka-bot -f
```

## Вариант 2: Docker

```bash
docker build -t moodezhka-bot .
docker run -d --name moodezhka-bot --env-file .env -v "$(pwd)/data:/app/data" moodezhka-bot
```

## Важно

- Не запускайте одновременно `run_bots.py`, `main.py` и `admin_main.py`.
- Для стабильной реакции лучше хостить на VPS с нормальным доступом к `api.telegram.org`.
- Если в логах есть `slow Telegram request`, задержка вызвана сетью или Telegram API.
