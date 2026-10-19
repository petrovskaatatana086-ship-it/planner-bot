# Meetly — контракт API v1

Дата: 20.10.2026. Статус: спроектирован, не реализован. Базовый путь `/api/meetly/v1`. Архитектура и правила — [технический проект](08-meetly-technical-design.md).

## Общие соглашения

JSON UTF-8, snake_case. ID — положительные целые, Telegram ID не принимается как идентификатор actor из тела команды. Время RFC3339 с offset, ответы UTC Z и timezone=Europe/Moscow. Даты YYYY-MM-DD. Только HTTPS; Authorization: Bearer <session>, кроме POST /session. Публичный /health не раскрывает клиентов и секреты.

Успех: `{data: ..., request_id: string}`. Список: data.items, data.next_cursor (null в конце). limit=20 по умолчанию, 1–100; cursor непрозрачный и привязан к фильтрам. Стабильный порядок с ID как вторым ключом. Для календаря from/to — включительные московские даты, не более 31 дня; availability дополнительно ограничен горизонтом владельца.

Ошибки: `{error: {code, message, fields?: {field: code}, retryable: boolean}, request_id}`. Без исключений, SQL, токенов и текстов внешних ответов. 400 INVALID_REQUEST (включая cursor/JSON/неизвестные поля), 401 SESSION_EXPIRED/INVALID_AUTH, 403 REGISTRATION_REQUIRED/ACCOUNT_BLOCKED/FORBIDDEN, 404 NOT_FOUND, 409 SLOT_UNAVAILABLE/STALE_VERSION/INVALID_STATE/IDEMPOTENCY_KEY_REUSED/SETTINGS_CHANGED, 422 VALIDATION_FAILED, 428 VERSION_REQUIRED, 429 RATE_LIMITED, 503 CALENDAR_UNAVAILABLE/DATABASE_BUSY/MAINTENANCE. Retry-After у временных ограничений. Ошибки доступа проверяются до выдачи сохранённого результата команды.

Бизнес-мутации требуют Idempotency-Key: UUID. Изменение существующего ресурса требует If-Match: "<version>"; для действий встречи версия Meeting, для решения заявки — BookingRequest, для настроек — OwnerProfile. Регистрация — create-if-unregistered без If-Match. Сессии не являются бизнес-командами: POST выдаёт новый токен, DELETE повторно отозванного/неизвестного токена возвращает 204. Для всех прочих успешных операций 200, создание заявки/переноса/регистрации 201. Replay возвращает исходный код/результат и Idempotency-Replayed:true.

## Вход и профиль

| Метод/путь | Доступ | Запрос и результат |
| --- | --- | --- |
| POST /session | Telegram initData | Authorization: tma <raw>; без тела → token, expires_at; правила HMAC/возраста в техпроекте |
| DELETE /session | Обладатель токена | Отзыв токена → 204 |
| GET /me | Сессия | role client/owner, registration_required, status, profile, timezone; до регистрации только собственный минимальный профиль |
| GET /consent | Сессия | version, text; только опубликованный действующий текст |
| POST /registration | Новый клиент | full_name, email, consent:true, consent_version → профиль; outdated consent_version → 409 CONSENT_CHANGED |
| GET /operations/{key} | Сессия | Только собственная команда → resource_type/id, сохранённые status_code/response; неизвестная → 404 |

Имя: trim, 1–255 символов, запрет управляющих символов. Email: общий app/domain/email.py, максимум 254. Регистрация не подтверждает владение email и не должна так называться в UI. consent_version должен соответствовать опубликованному тексту. Для legacy consent_at без версии повторное согласие принудительно не требуется. Юридический текст не генерируется техническим этапом: использовать действующий согласованный текст бота; при отсутствии рабочего текста регистрацию не включать.

## Общая доступность

| Метод/путь | Доступ | Запрос и результат |
| --- | --- | --- |
| GET /availability/days | Зарегистрированный клиент/owner | from,to → date, available_slots_count; только общая доступность без чужих данных |
| GET /availability/slots | Зарегистрированный клиент/owner | date, опционально meeting_id для собственного/админского переноса → slots[start_at,end_at], timezone, settings_version, checked_at |
| GET /booking-rules | Зарегистрированный клиент/owner | duration, buffer, daily_limit, horizon, notice, enabled, timezone, settings_version |

meeting_id проверяется на принадлежность до обращения к календарю. Для переноса duration берётся из встречи. Отсутствие свободного времени — 200 с пустым списком; ошибка обязательного Google — 503. Результат просмотра не является резервом. Клиент отправляет start_at, не произвольный end_at или длительность.

## Клиент

| Метод/путь | Запрос/фильтры | Результат |
| --- | --- | --- |
| GET /records | view=upcoming/history, cursor,limit | Собственные исходные заявки с вложенной встречей; одна карточка на заявку |
| GET /requests/{id} | — | Request DTO и связанная встреча при наличии |
| POST /requests | start_at,title,comment?,settings_version | Request DTO; повторная проверка слота/настроек |
| GET /meetings/{id} | — | Meeting DTO |
| POST /meetings/{id}/reschedule-requests | start_at,comment?,settings_version | Reschedule DTO и Meeting DTO с увеличенной version |
| POST /meetings/{id}/cancel | reason? | Meeting DTO; при pending переносе он закрывается атомарно |

Все пути только для собственных записей; owner использует административные команды. title trim 3–100 символов, как validate_title. Новая общая граница comment/reason — 2000 символов, без управляющих символов кроме переносов строк/табуляции; ввод в боте приводится к тому же валидатору. Причина отклонения заявки/переноса обязательна, 1–2000 после trim; причина отмены необязательна. HTML не интерпретируется.

upcoming: будущие активные встречи и pending заявки, ближайшее start_at первым. history: отклонённые/withdrawn заявки, отменённые и завершённые встречи, pending с прошедшим start_at; время по убыванию. Approved не выдаётся второй самостоятельной карточкой рядом со своей Meeting. GET requests сохраняет исходное решение даже в history.

## Администратор

| Метод/путь | Запрос/фильтры | Результат |
| --- | --- | --- |
| GET /admin/calendar | from,to,cursor,limit | Карточки диапазона по текущему времени встреч; исходная approved заявка не дублируется |
| GET /admin/requests | status=all/pending/approved/rejected/withdrawn,cursor,limit | Исходные заявки, created_at DESC,id DESC |
| GET /admin/requests/{id} | — | Request DTO с ClientSummary и встречей |
| GET /admin/meetings/{id} | — | Meeting DTO с ClientSummary |
| GET /admin/inbox | kind=all/booking/reschedule,cursor,limit | pending заявки и переносы, created_at ASC,id; тип обязателен в элементе |
| GET /admin/statistics | — | total,approved,rejected,pending,additional.withdrawn,pending_reschedules; одна согласованная SQL read-транзакция |
| POST /admin/requests/{id}/approve | {} | Request DTO и созданная Meeting DTO |
| POST /admin/requests/{id}/reject | reason | Request DTO |
| POST /admin/meetings/{id}/approve-reschedule | reschedule_request_id | Meeting DTO и Reschedule DTO |
| POST /admin/meetings/{id}/reject-reschedule | reschedule_request_id,reason | Meeting DTO и Reschedule DTO |
| POST /admin/meetings/{id}/reschedule | start_at,settings_version,reason? | Meeting DTO; pending-перенос → superseded |
| POST /admin/meetings/{id}/cancel | reason? | Meeting DTO |
| GET /admin/settings | — | Все шесть настроек, timezone, version |
| PATCH /admin/settings | Непустое подмножество шести полей | Настройки с version+1; диапазоны в техпроекте |
| POST /admin/meetings/{id}/retry-sync | {} | integration_state; повтор существующего failed-задания, без новой встречи |

У PATCH настроек имена соответствуют OwnerProfile: default_duration,buffer_minutes,daily_limit,booking_horizon_days,min_booking_notice_hours,booking_enabled. Рабочее расписание и подключение Google не редактируются этим API первой версии. Административные результаты персональные; чужие имена/email не появляются в availability. retry-sync требует If-Match версии встречи и Idempotency-Key, не меняет бизнес-статус встречи.

## DTO

Request: id,version,title,comment,start_at,end_at,timezone,status,rejection_reason,reservation_active,reserved_until,created_at,meeting_id,allowed_actions. Owner дополнительно получает client; клиент — только свою карточку. allowed_actions: approve/reject только owner при pending; отзыв не возвращается.

Meeting: id,version,booking_request_id,title,start_at,end_at,timezone,status,is_past,google_meet_url,google_event_url,integration_state,pending_reschedule,last_reschedule_decision,cancellation_reason,allowed_actions. Ссылки только собственному клиенту и owner, не в списке общих слотов. Причины и решения из структурированной истории/доступных legacy данных; если старая причина не восстановима — null, не выдуманный текст.

Reschedule: id,meeting_id,proposed_start_at,proposed_end_at,comment,status,reason,created_at,decided_at. ClientSummary только администратору: id,full_name,email. Наружу не выдаются OAuth, credential ID, token_hash, outbox payload и служебные тексты Google.

## Пример записи

Запрос POST /requests с Authorization, Idempotency-Key, Content-Type:application/json:

```json
{"start_at":"2026-10-23T10:00:00+03:00","title":"Консультация","comment":null,"settings_version":3}
```

Ответ 201 содержит data Request DTO с pending и reserved_until, request_id. При занятом времени — 409 SLOT_UNAVAILABLE без создания записи. Если ответ потерян, тот же ключ/тело возвращает тот же ID. If-Match не нужен для создания; решение по заявке требует её version. Это пример формы контракта, не реально доступный слот.

## Проверка контракта при реализации

Схемы и тесты должны покрыть каждый маршрут: обязательные поля/заголовки, role/ownership, 401/403/404, валидацию, version, replay и безопасную сериализацию. Генерацию OpenAPI из реализованных схем добавить на этапе 4; этот документ не объявляется машинно проверенной спецификацией. Нет endpoint для отзыва pending, изменения Google credentials или назначения роли.
