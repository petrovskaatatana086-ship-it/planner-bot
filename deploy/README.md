# Размещение calendar-bot

Сервер: `85.198.65.229`, SSH-пользователь `tataynka_pe`.
Поддомен: `https://calendar.tatyana-petrovskaya.ru`.
Бот: `https://t.me/my_meeting_helper_23_bot`.

## Изоляция

- Приложение: `/opt/calendar-bot`; отдельный Compose-проект `calendar-bot`.
- База SQLite: `/opt/calendar-bot/data/planner.db`, импортирована согласованная копия локальной базы.
- Секреты: `/opt/calendar-bot/.env`, права `600`, не входят в образ и Git.
- Контейнер работает от UID/GID `10001`, корневая файловая система доступна только для чтения.
- HTTP контейнера опубликован только на `127.0.0.1:8012`; лимит RAM — 256 МБ, RAM со swap — 384 МБ.
- PostgreSQL из локального `docker-compose.yml` на этом сервере не запускается.
- Nginx: отдельный файл `/etc/nginx/sites-available/calendar.tatyana-petrovskaya.ru` и одноимённая ссылка в `sites-enabled`.
- Отдельный формат журналирования `/etc/nginx/conf.d/calendar-bot-logging.conf` исключает параметры OAuth из access log. Access log aiohttp отключён по той же причине.
- Сертификат calendar выпускается через Certbot webroot; его renewal-конфигурация содержит отдельный deploy hook проверки и reload nginx.

Существующие проекты `planner`, `counterparty-check` и сайты основного домена, `checking`, `currency`, `planner` используют прежние файлы и сервисы. Их контрольные суммы и резервная копия nginx до размещения находятся в `/var/backups/calendar-bot`.

## Эксплуатация

Команды на сервере:

```sh
sudo docker compose -f /opt/calendar-bot/compose.production.yml ps
sudo docker compose -f /opt/calendar-bot/compose.production.yml logs --tail 50 bot
sudo docker stats --no-stream
curl -fsS https://calendar.tatyana-petrovskaya.ru/health
```

Для обновления сначала сохранить резервную копию базы, затем передать только исходники и файлы сборки. Не перезаписывать серверные `.env` и `data` локальными копиями.

```sh
sudo systemctl start calendar-bot-backup.service
sudo docker compose -f /opt/calendar-bot/compose.production.yml build
sudo docker compose -f /opt/calendar-bot/compose.production.yml up -d --wait
```

Миграции Alembic выполняются перед запуском процесса бота. Автозапуск контейнера после перезагрузки обеспечивает `restart: unless-stopped` и включённый Docker.

## Резервные копии

`calendar-bot-backup.timer` запускается ежедневно около 07:15–07:25 по Москве (04:15 UTC с небольшим случайным сдвигом). SQLite Backup API создаёт согласованный снимок и проверяет `PRAGMA integrity_check`. Хранятся последние 14 снимков в `/var/backups/calendar-bot`, права каталога `700`, файлов `600`.

Проверка:

```sh
sudo systemctl status calendar-bot-backup.timer --no-pager
sudo journalctl -u calendar-bot-backup.service -n 10 --no-pager
```

При восстановлении остановить только сервис `bot` этого Compose-проекта; сохранить текущую БД и её journal/WAL-файлы в отдельный каталог; восстановить снимок в `data/planner.db`, установить владельца `10001:10001` и права `600`; запустить этот же Compose-проект. Не восстанавливать старый снимок поверх работающей базы.

Это копии на том же сервере; внешнее резервное хранилище пока не подключено. Для расшифровки Google-токенов необходим сохранённый `OAUTH_ENCRYPTION_KEY` из `.env`.

## Google Calendar и почта

В Google Cloud для текущего OAuth Client ID добавить в **Authorized redirect URIs**:

```text
https://calendar.tatyana-petrovskaya.ru/google/oauth/callback
```

После этого владелец открывает раздел Google Calendar в Telegram-боте, проходит авторизацию и выбирает календарь. Само наличие клиентских ключей в `.env` не означает подключение аккаунта. В перенесённой базе на момент размещения профиль имеет `not_connected`, выбранного календаря нет.

SMTP-настройки не заполнены. Письма не отправляются, пока не настроены SMTP и адрес отправителя. Telegram-уведомления от SMTP не зависят.

## Последующий переход на webhook

Сейчас используется **polling**, Telegram webhook не установлен. Не запускать одновременно локальный polling того же бота.

Поддомен, HTTPS и reverse proxy уже существуют. Для переключения следующим отдельным изменением:

1. Добавить webhook-режим в приложение (например, `SimpleRequestHandler` aiogram), отдельный маршрут `/telegram/webhook` и проверку `X-Telegram-Bot-Api-Secret-Token`. Токен бота не использовать в URL.
2. Добавить только этому virtual host nginx точный location для POST `/telegram/webhook` с проксированием на `127.0.0.1:8012`.
3. Остановить polling, запустить webhook-обработчик и вызвать `setWebhook` с HTTPS URL и отдельным секретом. Не использовать `drop_pending_updates=true` при обычном переходе.
4. Проверить `getWebhookInfo` и доставку реального пользовательского сообщения.
5. Для возврата вызвать `deleteWebhook` без удаления ожидающих обновлений, остановить webhook-режим и запустить один polling-процесс.

Источники: [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook), [установка Docker на Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

## Отключение только нового проекта

```sh
sudo docker compose -f /opt/calendar-bot/compose.production.yml stop bot
sudo systemctl disable --now calendar-bot-backup.timer
```

При необходимости отключить только ссылку `sites-enabled/calendar.tatyana-petrovskaya.ru`, выполнить `nginx -t` и плавный reload. Не восстанавливать весь старый nginx поверх действующих сайтов и не применять глобальные Docker prune/down к чужим проектам.
