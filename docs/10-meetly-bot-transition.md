# Переход обработчиков бота на общий слой

Состояние 21.10.2026. Это карта незавершённого подключения, не отчёт о полном переходе production.

| Путь app/bot/handlers.py | Состояние | Общая операция / оставшаяся работа |
| --- | --- | --- |
| _load_user / _load_callback_user | Переведён | ProfileCommands.ensure_telegram_user, атомарный stub, сохранение регистрационного имени |
| confirm_registration | Переведён | ProfileCommands.register, явное согласие/версия, без снятия блокировки |
| update_numeric_meeting_setting / toggle_booking | Переведён | ProfileCommands.apply_settings_message, version, ключ сообщения |
| submit_booking | Переведён, opt-in runtime | create_request; settings_version и ключ формы в FSM, прямое уведомление удалено |
| _approve_request_by_id, approve_request, approve_request_callback | Переведён, opt-in runtime | approve_request; версия inline-карточки, стабильный ключ; без прямых Google/SMTP/Telegram |
| reject_request_with_reason / reject_request_callback | Переведён, opt-in runtime | reject_request; ID/версия сохраняются при начале ввода причины |
| reschedule_meeting_by_owner | Переведён, opt-in runtime | reschedule_by_owner; версии ресурса и настроек фиксируются в BotCommandInput |
| cancel_meeting_by_owner / cancel_meeting_by_client | Переведён, opt-in runtime | cancel_meeting; без прежних прямых отправок, только подтверждение действия |
| request_reschedule_by_client | Переведён, opt-in runtime | request_reschedule; срок нового времени и длительность проверяет общий сервис |
| approve_client_reschedule / reject_client_reschedule | Переведён, opt-in runtime | approve/reject_reschedule с сохранёнными ID предложения и Meeting.version |
| choose_date / choose_time / start_booking | Частично | start сохраняет версию/ключ формы без BookingDraft; превью слотов ещё старое, итоговая команда проверяет полные интервалы |
| save_google_calendar_selection / OAuth callback | Старый путь | Контролируемый binding change, version профиля, согласование уже созданных CalendarSync |
| block_client / unblock_client | Старый путь | Общая операция доступа и стабильный ключ; не менять данные регистрации |
| app.main | Подключён, opt-in | Проверка схемы, injection runtime, startup, наблюдение за ошибками, HTTP drain → runtime close → Bot close |

Перед включением новых booking writers нужно перевести их вместе с жизненным циклом worker. Нельзя одновременно оставить старые отправки и активировать новый outbox: это создаст дубли и обойдёт общие гарантии. MEETLY_ENABLED не должен выключать общие команды бота/worker.

Ключ текстового изменения настроек основан на public bot id + chat id + message id + action (SHA256). Текстовая команда владельца задаёт абсолютное значение и применяется к актуальному состоянию внутри writer-lock; replay не перечитывает expected_version. API PATCH использует отдельный patch_settings с обязательным expected_version. Для заявок/карточек при подключении нужны сохранённые версии, а не повторное привязывание старого ключа к новой версии после commit.

До live-перехода: сверить legacy pending с неизвестным previous_status, подготовить принятие старых Google events без меток, прогнать миграции на копии/restore, проверить отсутствие двух writers старой и новой версии. Эти шаги ещё не выполнены на сервере.

## Подготовленная сборка runtime (21.10.2026)

`app/services/booking_runtime.py` собирает BookingCommands, GoogleAvailability,
GoogleEventGateway, обе группы адаптеров доставки и OutboxWorker. Один gateway
используется для проверки занятости и синхронизации; один обязательный environment
передаётся командам и доставкам. Создание объекта не запускает worker и не вызывает
Google/Telegram. Runtime подключён к app.main и booking handlers, включение явное.

OwnerGoogleTokens при обращении находит активного пользователя по настроенному
Telegram ID и передаёт его внутренний ID в GoogleTokens. Владелец может впервые
зарегистрироваться после старта процесса. Проверка выполняется заново на каждом
обращении, read-сессия закрывается до сетевого refresh. Это не отменяет необходимости
перевода изменений доступа и Google binding на общий слой.

`start()` допускает повторный/конкурентный вызов без второго worker. `close()`
останавливает получение новых заданий, ждёт текущую доставку (по умолчанию до 35с),
затем отменяет и дожидается задачи. Отмена доставки сохраняет lease для восстановления.
Ошибки worker не скрываются при повторном start/close. app.main наблюдает за
задачей через wait/supervise_transport и останавливает транспорт при её ошибке.
После close можно запустить новый цикл.

Порядок shutdown: прекратить приём/дождаться обработчиков, закрыть runtime,
затем закрыть общий Bot и DB. Runtime не владеет Bot или engine и не закрывает их.
Отмена сетевой операции не гарантирует отмену уже принятого удалённого эффекта;
SMTP в to_thread также может продолжиться после отмены asyncio-задачи.

## Включение и повтор команды

По умолчанию `BOOKING_RUNTIME_ENABLED=false`: новые записи и изменения встреч
недоступны, старого writer fallback больше нет. Это отдельный флаг технического
перехода, не MEETLY_ENABLED. Регистрация/настройки продолжают использовать общий
слой. Для изолированного тестового запуска нужны актуальная схема (head 0011),
`BOOKING_RUNTIME_ENABLED=true` и уникальный стабильный `CALENDAR_ENVIRONMENT`.
Рабочие .env, сервер и master этим коммитом не изменяются. Флаг нельзя включать на
рабочих данных до проверки legacy events, переноса Google binding и восстановления.

`bot_command_inputs` (0011) фиксирует вход/версии перед командой под коротким SQLite
lock. Текстовый ключ = bot id + chat id + message id + action; callback key содержит
Telegram callback id, а карточка — явную версию. Неизвестные старые кнопки без версии
просят обновить список. Причина отказа вводится с сохранённой в FSM версией заявки.
Форма создания хранит версию настроек/UUID; новое редактирование комментария
обновляет ключ формы. FSM остаётся в памяти: после перезапуска незавершённую форму
нужно заполнить заново, но уже принятые команды сохраняют replay в БД.

При retry используются первоначальные версии и ID конкретного предложения переноса,
даже если операция завершилась/ресурс изменился. Неуспешная операция также сохраняет
снимок: для нового состояния требуется новое сообщение/форма. BotCommandInput и
OperationResult должны сохраняться вместе; автоматическая очистка пока отсутствует.

Превью слотов ещё использует старые выборки и может показать время, которое итоговая
общая команда отклонит с учётом буферов, полного пересечения или изменения настроек.
Следующая работа: общая выдача доступности, binding/access writers, legacy adoption,
изолированный серверный прогон; затем API/auth. Полного live-перехода пока нет.
