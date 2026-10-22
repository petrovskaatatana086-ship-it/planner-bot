# Legacy adoption и репетиция миграции: проверка

Полный регресс: **305 тестов OK**, 208.608с, exit code 0.
Лог: `full-adoption-rehearsal-tests-2026-10-22.txt`, UTF-8, объединённый stdout/stderr.
Новых проверок: 14 adoption и 8 migration/restore. git diff --check PASS.
Прежние предупреждения aiohttp о строковых app keys сохраняются.

Adoption проверен fake Google и временными SQLite, включая независимый engine:

- inspect не создаёт sync, план не содержит сырых email/названия/calendar ID;
- apply меняет только private properties с If-Match и notify=False;
- сохраняются ID, время, участники, описание, конференция и чужие properties;
- потеря ответа PATCH оставляет durable intent, повтор сверяет adoption_id
  и завершает без второго PATCH/аудита;
- неподходящее событие, чужие метки, повторяющийся event ID, удалённое событие,
  отменённая встреча, Google outbox и ETag conflict не присваиваются;
- права владельца проверяются до сети и replay; изменения данных без increment
  version и смена profile.version во время сети не применяют прежний план;
- writer-lock не удерживается во время Google GET/PATCH;
- unadopted/adopting legacy-встреча блокирует business mutation с rollback;
- worker не переписывает adopting intent и не пересоздаёт missing legacy event
  даже при старом unapplied CalendarSync;
- после принятия reschedule/cancel работают с тем же event ID, без insert.

Migration/restore использует настоящий Alembic через явно переданное соединение,
не читая .env. Проверены старые данные/предложение переноса, отказ при дубликатах,
частичная DDL-ошибка, WAL (committed/uncommitted), конкурентное изменение источника,
отказ перезаписи каталога и отсутствие неявного stamp для unversioned базы.
Все тестовые соединения явно закрываются; Windows наследует ACL рабочего каталога.

На **копии реальной локальной planner.db** отдельно выполнен CLI rehearsal:
0005_meeting_google_event_url → 0011_bot_command_inputs. `migration_ok`,
`legacy_data_preserved`, `restore_verified`, `source_unchanged` — true, exit code 0.
Отчёт: `local-copy-migration-restore-2026-10-22.json`, только количества/результаты.
Источник открывался read-only. Снимки с данными сохранены только в игнорируемом
`.deploy/meetly-rehearsal-2026-10-22/`, в git не включаются.

Граница: никаких live Google PATCH, отправок email/Telegram, подключений VPS или
изменений production/master не выполнялось. Реальное принятие серверных legacy
events, остановка старых writers, отдельный серверный прогон и API/auth впереди.
При restore старого снимка удалённые Google-метки не откатываются; без сохранённого
intent их нельзя автоматически присвоить. Инструкции — `docs/11-meetly-maintenance.md`.
