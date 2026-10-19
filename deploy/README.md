# Размещение calendar-bot

Проверка изоляции и правила добавления следующих проектов: [отдельные проекты на VPS](../docs/05-project-isolation.md). Универсальный шаблон нового HTTP-приложения: `deploy/project-template/`.

Автоматические обновления из GitHub: [пошаговая настройка](../docs/04-autodeploy.md).

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

После этого владелец открывает раздел Google Calendar в Telegram-боте, проходит авторизацию и выбирает календарь. Само наличие клиентских ключей в `.env` не означает подключение аккаунта. Подключение и выбор календаря уже выполнены на production; при обычном обновлении образа повторять их не нужно.

SMTP-настройки не заполнены. Письма не отправляются, пока не настроены SMTP и адрес отправителя. Telegram-уведомления от SMTP не зависят.

## Webhook и polling

На production webhook включён 17 октября 2026 года по Москве. Адрес: `https://calendar.tatyana-petrovskaya.ru/telegram/webhook`. Google-аккаунт подключён, основной календарь выбран. Серверный `.env` хранит режим и отдельный секрет; автообновления из GitHub сохраняют эти настройки.

Приложение поддерживает два режима. По умолчанию `BOT_MODE=polling`; на сервере режим задаётся в `/opt/calendar-bot/.env`:

```env
BOT_MODE=webhook
TELEGRAM_WEBHOOK_URL=https://calendar.tatyana-petrovskaya.ru/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=<отдельный случайный секрет из 32–256 символов A-Z a-z 0-9 _ ->
```

Секрет создаётся, например, через `secrets.token_urlsafe(32)` и остаётся только в серверном `.env`. Его не нужно добавлять в URL, nginx или GitHub. Nginx использует точный маршрут POST `/telegram/webhook` из `deploy/nginx.conf` и передаёт заголовок `X-Telegram-Bot-Api-Secret-Token` без записи его значения в журнал.

Переключение выполняется после доставки новой версии приложения: сохранить серверные `.env` и конфиг calendar, установить только новый конфиг calendar, выполнить `nginx -t` и reload; добавить режим/URL/секрет в `.env`; пересоздать только сервис `bot` с текущим образом из `/var/lib/calendar-bot-deploy/current.json` и `/etc/calendar-bot/compose.yml`. Не добавлять новые настройки старому образу, который их ещё не поддерживает. Использовать общую блокировку `/var/lib/calendar-bot-deploy/deploy.lock`.

На старте webhook-режим регистрирует адрес в Telegram с `max_connections=1`, типами обновлений из Dispatcher и `drop_pending_updates=False`. Polling в этом режиме не запускается. На обычном завершении процесса webhook не удаляется: Telegram повторяет доставку после перезапуска. `/health` возвращает 503 до успешной настройки Telegram и 200 с `transport=webhook` после неё.

Обработчик проверяет секрет до чтения тела, ждёт завершения обработки перед 200 и возвращает ошибку при сбое. Небольшой кэш и блокировка подавляют повторную обработку одного `update_id` в пределах текущего процесса. После рестарта этот кэш и незавершённые FSM-диалоги теряются; гарантия exactly-once не заявляется. Сохранённые встречи и заявки остаются в SQLite.

Статус (на сервере):

```sh
sudo docker exec calendar-bot-bot-1 python -m app.telegram_admin status
```

Ожидаются корректный URL, `max_connections=1`, отсутствие новых ошибок доставки и пустая очередь после обработки сообщений. Написать боту `/start` с реального пользовательского аккаунта — итоговая ручная проверка ответа.

Для намеренного возврата на polling администратор сначала останавливает только бота, вызывает `python -m app.telegram_admin delete-webhook` в одноразовом контейнере текущего образа, затем устанавливает `BOT_MODE=polling` и запускает один экземпляр. Команда удаления сохраняет ожидающие обновления. Случайный локальный запуск polling при активном webhook остановится с понятной ошибкой и не удалит серверный webhook.

Источники: [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook), [установка Docker на Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

## Отключение только нового проекта

```sh
sudo docker compose -f /opt/calendar-bot/compose.production.yml stop bot
sudo systemctl disable --now calendar-bot-backup.timer
```

При необходимости отключить только ссылку `sites-enabled/calendar.tatyana-petrovskaya.ru`, выполнить `nginx -t` и плавный reload. Не восстанавливать весь старый nginx поверх действующих сайтов и не применять глобальные Docker prune/down к чужим проектам.
