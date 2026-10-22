# Изолированный backend Meetly test

Первый серверный прогон: отдельный Telegram-бот, новая SQLite, локальные правила
записи, durable Telegram outbox. Google и SMTP принудительно отключены в Compose.
Это ещё не размещение Mini App: статические/API маршруты и кнопка не включаются.

Подготовка из корня проекта:

1. Создать отдельного бота через BotFather `/newbot`. Записать его токен в
   `BOT_TOKEN` файла `.env.local`; `OWNER_TELEGRAM_ID` должен принадлежать оператору
   тестового бота. Не использовать рабочий токен. Открыть нового бота и нажать Start.
2. Выполнить `python -m deploy.meetly_test_setup --image meetly-test:COMMIT_SHA`.
   Вместо COMMIT_SHA использовать полный SHA проверенного коммита. Результат:
   `.deploy/meetly-test/compose.yml`, `compose.env`, `app.env` и пустой `data/`.
   Токен/данные в stdout не выводятся. Если каталог уже существует, он не изменяется.
   Для проверки отличия bot ID должен присутствовать рабочий BOT_TOKEN в основной
   `.env`; оттуда ничего не копируется, значение используется только для сравнения.
3. Перед запуском повторно проверить на VPS память, диск и порт 8013. Проверка
   22.10.2026 показала: 673 МиБ available RAM, 3 ГБ диска, порт 8013 не слушал;
   это наблюдение, не резервирование порта. Production слушает 8012.
4. Собрать именно проверенный исходный коммит в `meetly-test:COMMIT_SHA` через
   Dockerfile.production. Образ не содержит .env/БД; они исключены .dockerignore.
   Maintenance-команды adoption/rehearsal включены в образ, но не выполняются при
   обычном старте. Сборка образа и запуск не выполняются командой prepare.
   Передать образ и подготовленный каталог только в `/opt/meetly-test`.
   Каталог data на Linux должен принадлежать 10001:10001. `app.env` — права 600,
   каталог с конфигурацией — 700. Не копировать рабочую БД или OAuth credentials.
5. В `/opt/meetly-test` выполнить команды ниже. Всегда указывать project-name,
   compose и env-file явно. Не выводить развёрнутый `docker compose config`
   в журнал: использовать `--quiet`.

```sh
sudo docker compose --project-name meetly-test --env-file /opt/meetly-test/compose.env -f /opt/meetly-test/compose.yml config --quiet
sudo docker compose --project-name meetly-test --env-file /opt/meetly-test/compose.env -f /opt/meetly-test/compose.yml up -d --wait
sudo docker compose --project-name meetly-test --env-file /opt/meetly-test/compose.env -f /opt/meetly-test/compose.yml ps
curl -fsS http://127.0.0.1:8013/health
```

Startup образа выполняет Alembic upgrade head на новой тестовой БД. Polling не
удаляет webhook автоматически: если у тестового токена уже есть webhook, запуск
остановится до его явного отдельного переключения. Основной бот остаётся webhook.

Проверка вручную:

- `/start` владельцем: ответ, меню администратора и настройки записи;
- `/start` вторым тестовым аккаунтом: согласие, регистрация с вымышленным email;
- клиент создаёт заявку, владелец согласует; проверить одну встречу и уведомление;
- клиент заново открывает «Мои встречи», нажимает «Перенести» и вводит новую дату/
  время по Москве (ДД.ММ.ГГГГ ЧЧ:ММ); владелец открывает «Новые заявки» и согласует
  или отклоняет перенос кнопкой (при отказе вводит причину); затем клиент проверяет
  отмену через «Отменить встречу» и отдельное подтверждение;
- отдельная новая заявка отклоняется с причиной;
- перезапуск только meetly-test сохраняет заявки/встречи/результаты команд;
- фиксировать только ID/результаты, без настоящих имён, email, токенов и сырых логов.

Письма не отправляются, Meet-ссылки не создаются. Кнопка Mini App пока не проверяется.
Google OAuth/legacy adoption, SMTP-перехватчик и публичные тестовые маршруты будут
следующим дополнением после этого прогона. При подключении API использовать тот же
согласованный origin и отдельные пути `/meetly-test/` и `/api/meetly-test/v1/`;
production `/meetly/` и `/api/meetly/v1/` не перенаправлять в тестовый процесс.

Остановка только теста:

```sh
sudo docker compose --project-name meetly-test --env-file /opt/meetly-test/compose.env -f /opt/meetly-test/compose.yml stop bot
```

Никаких глобальных stop/prune, внешних общих сетей или перезапуска production.
Остановка не удаляет тестовую БД. Перед обновлением теста — backup/restore rehearsal
из `docs/11-meetly-maintenance.md`; откат образа и восстановление БД выполняются
только с остановленными test writers. После принятых новых команд старую копию
не восстанавливать с потерей этих команд.
