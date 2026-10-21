# Общие профили и частичное подключение бота — 21.10.2026

Результат: ProfileCommands, domain/registration, consent_version (migration 0010), перевод identity/registration/settings функций handlers.py на общий слой. Booking/Google/access handlers пока не переведены; полный список в docs/10-meetly-bot-transition.md.

Проверки:
- общий регресс 201 тест OK, full-profile-tests-2026-10-21.txt;
- после уточнения Unicode категорий имени отдельный окончательный набор профиля 20 тестов OK, profile-final-tests-2026-10-21.txt (первоначальный отдельный набор 18 — profile-tests-2026-10-21.txt);
- повтор и две параллельные регистрации по Telegram ID не перезаписывают профиль;
- concurrent ensure одного Telegram ID создаёт один stub;
- explicit consent/version обязательны; старый consent_version отклоняется, legacy consent_at не сбрасывается;
- blocked stub не регистрируется, blocked registered не получает replay и не разблокируется;
- новый Telegram display_name сохраняет введённое имя/email, обновляет username;
- проверены управляющие знаки/имя/email и легитимные emoji с ZWJ;
- API-style patch требует версию, concurrent patch даёт один успех; текстовые bot команды сериализуются без потери других полей;
- проверены строгие типы/диапазоны/неизвестные поля, spoof stored owner role, payload mismatch при повторе ключа;
- реальные функции confirm_registration, accept_consent, _load_user, update_numeric_meeting_setting и toggle_booking вызываются с fake Telegram/FSM, реальные transports не используются;
- реальная цепочка Alembic Operations 0001–0010 сохраняет legacy имя/email/consent_at, consent_version остаётся NULL; схема users сверена с метаданными.

Новые тесты не читают рабочий .env, используют временные SQLite-файлы и явные синтетические настройки. git diff --check PASS. Live API/Telegram/Google/SMTP/VPS не затрагивались.

Решения:
- используется текущий текст приглашения существующего бота, помеченный bot-consent-v1; новый юридический текст/политика этим техническим блоком не создаются;
- абсолютные текстовые настройки владельца принимают актуальную версию под SQLite lock; HTTP-карточки обязаны использовать patch_settings с expected_version. Это отдельные публичные методы, а не неявное отключение проверки HTTP;
- имя регистрации больше не заменяется при каждом Telegram-сообщении;
- вне общего слоя пока остаются booking writers, выбор Google-календаря/OAuth и управление доступом клиентов;
- worker нельзя включать рядом со старыми прямыми отправками; перед live-переходом требуется контролируемый legacy adoption/restore и миграция dev/тестовой БД.
