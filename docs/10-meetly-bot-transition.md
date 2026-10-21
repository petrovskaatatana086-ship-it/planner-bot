# Переход обработчиков бота на общий слой

Состояние 21.10.2026. Это карта незавершённого подключения, не отчёт о полном переходе production.

| Путь app/bot/handlers.py | Состояние | Общая операция / оставшаяся работа |
| --- | --- | --- |
| _load_user / _load_callback_user | Переведён | ProfileCommands.ensure_telegram_user, атомарный stub, сохранение регистрационного имени |
| confirm_registration | Переведён | ProfileCommands.register, явное согласие/версия, без снятия блокировки |
| update_numeric_meeting_setting / toggle_booking | Переведён | ProfileCommands.apply_settings_message, version, ключ сообщения |
| submit_booking | Старый путь | BookingCommands.create_request; сохранить settings_version и command_id в FSM, убрать прямое уведомление |
| _approve_request_by_id, approve_request, approve_request_callback | Старый путь | BookingCommands.approve_request; стабильный ключ и версия карточки, убрать прямые Google/SMTP/Telegram вызовы |
| reject_request_with_reason / reject_request_callback | Старый путь | BookingCommands.reject_request; сохранить ID/версию при начале ввода причины |
| reschedule_meeting_by_owner | Старый путь | BookingCommands.reschedule_by_owner; передавать ресурсную/настроечную версию |
| cancel_meeting_by_owner / cancel_meeting_by_client | Старый путь | BookingCommands.cancel_meeting; убрать прежние прямые отправки, оставить подтверждение действия |
| request_reschedule_by_client | Старый путь | BookingCommands.request_reschedule; срок нового времени, сохранённая длительность |
| approve_client_reschedule / reject_client_reschedule | Старый путь | Общие approve/reject_reschedule с ID конкретного предложения и Meeting.version |
| choose_date / choose_time / start_booking | Старые выборки | Общая availability с интервалами и актуальными настройками; оформление формы не резервирует время |
| save_google_calendar_selection / OAuth callback | Старый путь | Контролируемый binding change, version профиля, согласование уже созданных CalendarSync |
| block_client / unblock_client | Старый путь | Общая операция доступа и стабильный ключ; не менять данные регистрации |
| app.main | Worker не запущен | Сборка GoogleTokens/Gateway/Availability/BookingCommands и handlers доставки, startup/shutdown/drain |

Перед включением новых booking writers нужно перевести их вместе с жизненным циклом worker. Нельзя одновременно оставить старые отправки и активировать новый outbox: это создаст дубли и обойдёт общие гарантии. MEETLY_ENABLED не должен выключать общие команды бота/worker.

Ключ текстового изменения настроек основан на public bot id + chat id + message id + action (SHA256). Текстовая команда владельца задаёт абсолютное значение и применяется к актуальному состоянию внутри writer-lock; replay не перечитывает expected_version. API PATCH использует отдельный patch_settings с обязательным expected_version. Для заявок/карточек при подключении нужны сохранённые версии, а не повторное привязывание старого ключа к новой версии после commit.

До live-перехода: сверить legacy pending с неизвестным previous_status, подготовить принятие старых Google events без меток, прогнать миграции на копии/restore, проверить отсутствие двух writers старой и новой версии. Эти шаги ещё не выполнены на сервере.
