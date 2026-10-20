# Google Calendar sync — проверка 21.10.2026

Полный прогон: 133 теста, 0 errors/failures, лог full-calendar-tests-2026-10-21.txt. Новый набор test_calendar_sync.py: 22 теста (17 первоначальных, ещё 5 вошли в полный прогон). Создаются уникальные синтетические SQLite-файлы в workspace с очисткой; .env и рабочая БД не используются новыми тестами.

Проверены:
- сохранение стабильного event ID до сети; timeout после remote INSERT → GET вместо второго создания;
- 409 → чтение существующего события; проверка environment/meeting private properties;
- timeout после PATCH → сверка remote версии перед повтором, без второго PATCH;
- ручное изменение времени и HTTP 412 → конфликт, без перезаписи;
- отмена до создания, удаление после создания, gateway DELETE 410;
- новая версия во время сети: applied старой версии сохраняется, desired новая не объявляется synced;
- expired lease_token не применяет CalendarSync hook; новый worker восстанавливает результат;
- асинхронное появление Meet URL без второго события;
- смена calendar binding/environment не допускает операций в чужом календаре;
- 401 refresh ровно один раз, 429/Retry-After, quota 403 vs permission 403;
- refresh выполняется без write-lock, новый access token используется повторно;
- повтор enqueue той же synced версии не сбрасывает состояние;
- delay notice без ложной ссылки, последующий follow-up, гонка между remote send notice и DB acknowledgement.

Реальный Calendar/Telegram/SMTP не вызывается. FakeCalendar моделирует состояние и потерянные ответы; Gateway использует поддельный HTTP requester. Фактические разрешения Google-аккаунта, реальная сеть, фоновые конфликты сторонних событий и интеграция в Telegram пока не проверены.

Ограничения:
- worker не запускается из app.main, handlers ещё на старом пути; новые booking commands требуют local-only profile до реализации снимка занятости;
- legacy события используют сохранённый ID, но при отсутствии маркировки требуют отдельного контролируемого принятия, а не blind PATCH;
- восстановление OAuth работает с существующей схемой credentials, автоматическая смена Google аккаунта/календаря не выполняется;
- сверка относится к событию обрабатываемой встречи; плановый скан календаря за 14 дней остаётся впереди;
- fallback уведомления дают информацию о локальном решении, не подтверждают готовность Google;
- сетевой приём уведомления и SQLite commit остаются разными системами: абсолютная exactly-once доставка SMTP/Telegram не обещается.

Официальные источники сверены 21.10.2026:
- https://developers.google.com/workspace/calendar/api/v3/reference/events/insert — заданный ID события и отдельный conference request;
- https://developers.google.com/workspace/calendar/api/v3/reference/events/get — восстановление по event ID;
- https://developers.google.com/workspace/calendar/api/v3/reference/events/update — согласованное изменение с ETag.

Миграция 0008 добавляет только calendar_sync. Её результат и предыдущие миграции проверены настоящими Alembic Operations на синтетической старой схеме. В production миграции не запускались.
