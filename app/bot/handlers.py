from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import async_sessionmaker
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from types import SimpleNamespace
import hashlib
import uuid

from app.bot.keyboards import (
    booking_comment_keyboard,
    booking_confirmation_keyboard,
    booking_dates_keyboard,
    booking_times_keyboard,
    client_main_menu,
    confirm_registration_keyboard,
    consent_keyboard,
    owner_request_actions_keyboard,
    owner_main_menu,
    meeting_actions_keyboard, meeting_cancel_keyboard, meeting_move_keyboard, owner_move_keyboard,
)
from app.bot.states import Booking, GoogleCalendarSelection, Registration, Rejection, MeetingMove, MoveRejection
from app.config import Settings
from app.db.models import User, Meeting, RescheduleRequest
from app.db.repositories import (
    get_booking_request,
    get_google_oauth_credential,
    get_owner_profile,
    list_client_meetings,
    list_client_requests,
    list_clients,
    list_pending_requests,
)
from app.domain.booking import (
    MOSCOW_TZ,
    format_moscow_date,
    format_moscow_dt,
    parse_moscow_date,
    parse_moscow_time,
    to_utc,
    validate_title,
)
from app.domain.email import is_valid_email
from app.domain.intervals import utc
from app.domain.roles import OWNER_ROLE
from app.domain.registration import CONSENT_TEXT, CONSENT_VERSION, valid_name
from app.services.profiles import ProfileCommands, SETTING_RANGES
from app.services.booking import BookingError
from app.db.transactions import DatabaseBusy
from app.integrations.google_calendar import list_google_calendars
from app.integrations.google_oauth import build_google_oauth_link, google_oauth_is_configured
from app.services.google_binding import GoogleBindingCommands
from app.services.google_tokens import GoogleTokens

router = Router()


def _profile_commands(settings, session_factory):
    return ProfileCommands(session_factory, owner_telegram_id=settings.owner_telegram_id)


def _message_command_key(message, settings, action):
    # Store/hash only the public bot id, never the secret token portion.
    identity = f"telegram:{settings.bot_token.split(':', 1)[0]}:{message.chat.id}:{message.message_id}:{action}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _command_error(error):
    return {
        "ACCOUNT_BLOCKED": "Ваш доступ сейчас ограничен. Напишите специалисту для уточнения.",
        "FORBIDDEN": "У вас нет доступа к этому действию.",
        "CONSENT_CHANGED": "Текст согласия обновился. Начните регистрацию заново через /start.",
        "INVALID_STATE": "Действие уже выполнено или данные изменились. Обновите раздел.",
        "STALE_VERSION": "Данные изменились. Обновите раздел и повторите действие.",
        "VALIDATION_FAILED": "Проверьте введённые данные и повторите действие.",
        "SLOT_UNAVAILABLE": "Это время уже занято. Выберите другой слот.",
        "CALENDAR_UNAVAILABLE": "Не удалось проверить Google Calendar. Повторите позже.",
        "CALENDAR_BINDING_IN_USE": "Календарь связан с существующими встречами. Для смены подключения сначала нужен перенос событий специалистом.",
        "CALENDAR_ADOPTION_REQUIRED": "Сначала нужно проверить подключение этой встречи к Google Calendar.",
        "RESOURCE_NOT_FOUND": "Заявка или встреча не найдена.",
        "BOOKING_DISABLED": "Запись временно выключена специалистом.",
        "NOTICE_REQUIRED": "До встречи осталось слишком мало времени для этого действия.",
        "LEGACY_RESCHEDULE_REVIEW_REQUIRED": "Для старого запроса нужен разбор специалистом: назначьте время явно или отмените встречу.",
    }.get(getattr(error, "code", "DATABASE_BUSY"), "Не удалось выполнить действие. Повторите позже.")


async def _booking_command(message, booking_runtime, action, *, actor, command_id, **payload):
    if booking_runtime is None:
        await message.answer("Запись и изменение встреч временно недоступны.")
        return None
    try:
        return await booking_runtime.bot_commands.execute(action, actor_id=actor.id,
            command_id=command_id, **payload)
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return None


async def _booking_availability(message, runtime, action, **payload):
    if runtime is None:
        await message.answer("Запись временно недоступна.")
        return None
    try:
        return await getattr(runtime.availability_queries, action)(**payload)
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return None


def _booking_dates_markup(data):
    return booking_dates_keyboard(data.get("allowed_dates", []),
        previous_page=data.get("date_previous", False), next_page=data.get("date_next", False))


async def _show_booking_dates(message, state, view):
    dates = [day.date for day in view.days if day.available_slots_count]
    await state.update_data(allowed_dates=dates, page_from=view.from_date, page_to=view.to_date,
        date_previous=view.from_date > view.checked_at.astimezone(MOSCOW_TZ).date(),
        date_next=view.to_date < view.horizon_end, settings_version=view.settings_version,
        booking_command_id=uuid.uuid4().hex)
    await state.set_state(Booking.waiting_for_date)
    data = await state.get_data()
    text = "Выберите дату кнопкой ниже." if dates else "В этом периоде свободных дат нет."
    await message.answer(text + _invitation_email_line(data.get("invitation_email")),
                         reply_markup=_booking_dates_markup(data))


async def _cancel_booking_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Запись отменена. Главное меню.", reply_markup=client_main_menu())


def _display_name(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "Пользователь"
    return user.full_name or user.username or str(user.id)


def _booking_summary(data: dict) -> str:
    return (
        "Проверьте заявку:\n"
        f"Дата и время: {format_moscow_dt(data['selected_start'])} МСК\n"
        f"Название: {data['title']}\n"
        f"Комментарий: {data.get('comment') or 'нет'}"
    )


def _invitation_email_line(email: str | None) -> str:
    if not email:
        return ""
    return f"\n\nПриглашение на встречу будет отправлено на: {email}"


def _request_card(request, client=None) -> str:
    client_line = ""
    if client is not None:
        client_line = f"\nКлиент: {client.full_name}, {client.email or 'email не указан'}"
    return (
        f"Заявка #{request.id}{client_line}\n"
        f"Дата и время: {format_moscow_dt(request.start_at)} МСК\n"
        f"Название: {request.title}\n"
        f"Комментарий: {request.comment or 'нет'}\n"
        f"Статус: {request.status}"
    )


def _profile_text(profile) -> str:
    booking_status = "включена" if profile.booking_enabled else "выключена"
    google_line = profile.google_status
    if profile.google_email:
        google_line = f"{google_line} ({profile.google_email})"
    return (
        "Настройки встреч:\n"
        f"Запись: {booking_status}\n"
        f"Длительность: {profile.default_duration} мин.\n"
        f"Перерыв: {profile.buffer_minutes} мин.\n"
        f"Дневной лимит: {profile.daily_limit}\n"
        f"Горизонт записи: {profile.booking_horizon_days} дн.\n"
        f"Минимум до встречи: {profile.min_booking_notice_hours} ч.\n"
        f"Google: {google_line}\n\n"
        "Команды:\n"
        "Длительность 60\n"
        "Перерыв 15\n"
        "Лимит 8\n"
        "Горизонт 14\n"
        "Минимум 24\n"
        "Запись вкл\n"
        "Запись выкл"
    )


async def _load_user(message: Message, settings: Settings, session_factory: async_sessionmaker):
    telegram_user = message.from_user
    if telegram_user is None:
        return None

    return await _profile_commands(settings, session_factory).ensure_telegram_user(
            telegram_id=telegram_user.id,
            full_name=_display_name(message),
            username=telegram_user.username,
        )


async def _load_callback_user(callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker):
    callback_user = callback.from_user
    if callback_user is None:
        return None

    return await _profile_commands(settings, session_factory).ensure_telegram_user(
            telegram_id=callback_user.id,
            full_name=callback_user.full_name or callback_user.username or str(callback_user.id),
            username=callback_user.username,
        )


async def _require_registered_client(message: Message, settings: Settings, session_factory):
    user = await _load_user(message, settings, session_factory)
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram. Попробуйте еще раз.")
        return None
    if user.role == OWNER_ROLE:
        await message.answer("Этот сценарий доступен клиентам. Для управления используйте /admin.")
        return None
    if not user.email or not user.consent_at:
        await message.answer("Сначала завершите регистрацию через /start.")
        return None
    if user.status != "active":
        await message.answer("Ваш доступ к записи сейчас ограничен. Напишите специалисту для уточнения.")
        return None
    return user


def _client_status_label(status: str) -> str:
    if status == "active":
        return "активен"
    if status == "blocked":
        return "заблокирован"
    return status


def _client_card(client: User) -> str:
    return (
        f"#{client.id} {client.full_name}\n"
        f"Email: {client.email or 'не указан'}\n"
        f"Telegram ID: {client.telegram_id}\n"
        f"Статус: {_client_status_label(client.status)}"
    )


async def _approve_request_by_id(request_id, *, owner, bot, settings, session_factory,
                                 booking_runtime=None, command_id, expected_version=None):
    if booking_runtime is None:
        return False, "Изменение встреч временно недоступно."
    payload = dict(request_id=request_id)
    if expected_version is not None:
        payload["expected_version"] = expected_version
    try:
        await booking_runtime.bot_commands.execute("approve_request", actor_id=owner.id,
            command_id=command_id, **payload)
    except (BookingError, DatabaseBusy) as error:
        return False, _command_error(error)
    return True, f"Решение по заявке #{request_id} сохранено. Клиент получит отдельное уведомление."


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, settings: Settings, session_factory):
    await state.clear()
    user = await _load_user(message, settings, session_factory)
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram. Попробуйте еще раз.")
        return

    if user.role == OWNER_ROLE:
        await message.answer(
            "Вы вошли как специалист-администратор. Время указано по Москве, МСК.",
            reply_markup=owner_main_menu(),
        )
        return

    if user.email and user.consent_at:
        if user.status != "active":
            await message.answer("Ваш доступ сейчас ограничен. Напишите специалисту для уточнения.")
            return
        await message.answer(
            "Главное меню. Время указано по Москве, МСК.",
            reply_markup=client_main_menu(),
        )
        return

    await state.set_state(Registration.waiting_for_consent)
    await message.answer(
        CONSENT_TEXT,
        reply_markup=consent_keyboard(),
    )


@router.message(Command("admin"))
async def admin(message: Message, settings: Settings, session_factory):
    user = await _load_user(message, settings, session_factory)
    if user and user.role == OWNER_ROLE:
        await message.answer("Управление системой.", reply_markup=owner_main_menu())
        return
    await message.answer("У вас нет доступа к управлению системой")


@router.message(Registration.waiting_for_consent, F.text.casefold() == "принимаю")
async def accept_consent(message: Message, state: FSMContext):
    await state.update_data(consent=True, consent_version=CONSENT_VERSION)
    await state.set_state(Registration.waiting_for_name)
    await message.answer("Введите ваше имя.")


@router.message(Registration.waiting_for_consent)
async def reject_consent(message: Message):
    await message.answer("Чтобы продолжить регистрацию, нажмите «Принимаю».")


@router.message(Registration.waiting_for_name)
async def enter_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not valid_name(name):
        await message.answer("Имя должно содержать от 1 до 255 символов без управляющих знаков. Введите имя ещё раз.")
        return

    await state.update_data(full_name=name)
    await state.set_state(Registration.waiting_for_email)
    await message.answer("Введите email. На него будет отправляться приглашение в календарь.")


@router.message(Registration.waiting_for_email)
async def enter_email(message: Message, state: FSMContext):
    email = (message.text or "").strip()
    if not is_valid_email(email):
        await message.answer("Email выглядит некорректно. Проверьте адрес и введите еще раз.")
        return

    await state.update_data(email=email)
    data = await state.get_data()
    await state.set_state(Registration.waiting_for_confirmation)
    await message.answer(
        "Проверьте данные:\n"
        f"Имя: {data['full_name']}\n"
        f"Email: {email}\n\n"
        "Проверьте адрес электронной почты. На него будет отправлено приглашение в календарь.",
        reply_markup=confirm_registration_keyboard(),
    )


@router.message(Registration.waiting_for_confirmation, F.text == "Изменить имя")
async def change_name(message: Message, state: FSMContext):
    await state.set_state(Registration.waiting_for_name)
    await message.answer("Введите имя заново.")


@router.message(Registration.waiting_for_confirmation, F.text == "Изменить email")
async def change_email(message: Message, state: FSMContext):
    await state.set_state(Registration.waiting_for_email)
    await message.answer("Введите email заново.")


@router.message(Registration.waiting_for_confirmation, F.text == "Подтвердить регистрацию")
async def confirm_registration(message: Message, state: FSMContext, settings: Settings, session_factory):
    telegram_user = message.from_user
    if telegram_user is None:
        await message.answer("Не удалось определить пользователя Telegram. Попробуйте еще раз.")
        return

    data = await state.get_data()
    commands = _profile_commands(settings, session_factory)
    user = await commands.ensure_telegram_user(telegram_id=telegram_user.id,
        full_name=_display_name(message), username=telegram_user.username)
    try:
        await commands.register(actor_id=user.id,
            command_id=_message_command_key(message, settings, "register"),
            full_name=data.get("full_name"), email=data.get("email"),
            consent=data.get("consent"), consent_version=data.get("consent_version"))
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return

    await state.clear()
    await message.answer("Регистрация завершена. Главное меню.", reply_markup=client_main_menu())


@router.message(Registration.waiting_for_confirmation)
async def confirm_registration_fallback(message: Message):
    await message.answer("Подтвердите регистрацию или выберите, что нужно изменить.")


@router.message(F.text == "Записаться на встречу")
async def start_booking(message: Message, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return
    view = await _booking_availability(message, booking_runtime, "days", actor_id=user.id)
    if view is None:
        return
    await state.clear()
    await state.update_data(invitation_email=user.email)
    await _show_booking_dates(message, state, view)


@router.message(Booking.waiting_for_time, F.text == "Изменить дату")
@router.message(
    Booking.waiting_for_date,
    Booking.waiting_for_time,
    Booking.waiting_for_title,
    Booking.waiting_for_comment,
    Booking.waiting_for_confirmation,
    F.text == "Отмена",
)
async def cancel_booking_flow(message: Message, state: FSMContext, settings: Settings = None, session_factory=None,
                              booking_runtime=None):
    if (message.text or "").strip() == "Изменить дату":
        await start_booking(message, state, settings, session_factory, booking_runtime)
        return
    await _cancel_booking_flow(message, state)


@router.message(Booking.waiting_for_date)
async def choose_date(message: Message, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return
    data = await state.get_data()
    if "settings_version" not in data or "page_from" not in data:
        await message.answer("Форма устарела. Начните запись заново.")
        return
    text = (message.text or "").strip()
    if text in {"Следующие даты", "Предыдущие даты"}:
        forward = text == "Следующие даты"
        if not data.get("date_next" if forward else "date_previous"):
            await message.answer("Выберите дату из списка.", reply_markup=_booking_dates_markup(data))
            return
        first = data["page_to"]+timedelta(days=1) if forward else data["page_from"]-timedelta(days=31)
        view = await _booking_availability(message, booking_runtime, "days", actor_id=user.id,
            from_date=first, settings_version=data["settings_version"])
        if view is not None:
            await _show_booking_dates(message, state, view)
        return
    selected_date = parse_moscow_date(text)
    if selected_date not in data.get("allowed_dates", []):
        await message.answer("Выберите дату кнопкой из списка.", reply_markup=_booking_dates_markup(data))
        return
    view = await _booking_availability(message, booking_runtime, "slots", actor_id=user.id,
        date=selected_date, settings_version=data["settings_version"])
    if view is None:
        return
    if not view.slots:
        await message.answer("На эту дату свободного времени уже нет. Выберите другую дату.",
                             reply_markup=_booking_dates_markup(data))
        return
    slots = [interval.start.astimezone(MOSCOW_TZ) for interval in view.slots]
    await state.update_data(selected_date=selected_date, slots=slots, booking_command_id=uuid.uuid4().hex)
    await state.set_state(Booking.waiting_for_time)
    await message.answer("Выберите время кнопкой ниже." + _invitation_email_line(data.get("invitation_email")),
                         reply_markup=booking_times_keyboard(slots))


@router.message(Booking.waiting_for_time)
async def choose_time(message: Message, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return
    data = await state.get_data()
    if not all(key in data for key in ("slots", "selected_date", "settings_version")):
        await message.answer("Форма устарела. Начните запись заново.")
        return
    selected_time = parse_moscow_time(message.text or "")
    selected_start = next((slot for slot in data["slots"] if slot.time() == selected_time), None)
    if selected_start is None:
        await message.answer("Выберите время кнопкой из списка.", reply_markup=booking_times_keyboard(data["slots"]))
        return
    view = await _booking_availability(message, booking_runtime, "slots", actor_id=user.id,
        date=data["selected_date"], settings_version=data["settings_version"])
    if view is None:
        return
    if selected_start not in [interval.start for interval in view.slots]:
        await message.answer("Это время уже недоступно. Выберите дату и время заново.")
        await start_booking(message, state, settings, session_factory, booking_runtime)
        return
    await state.update_data(selected_start=selected_start, booking_command_id=uuid.uuid4().hex)
    await state.set_state(Booking.waiting_for_title)
    await message.answer("Введите название встречи. От 3 до 100 символов.", reply_markup=ReplyKeyboardRemove())


@router.message(Booking.waiting_for_title)
async def enter_booking_title(message: Message, state: FSMContext):
    title = validate_title(message.text or "")
    if title is None:
        await message.answer("Название должно быть от 3 до 100 символов. Введите еще раз.")
        return

    await state.update_data(title=title)
    await state.set_state(Booking.waiting_for_comment)
    await message.answer(
        "Добавьте комментарий или нажмите «Без комментария».",
        reply_markup=booking_comment_keyboard(),
    )


@router.message(Booking.waiting_for_comment)
async def enter_booking_comment(message: Message, state: FSMContext):
    raw_comment = (message.text or "").strip()
    comment = None if raw_comment.casefold() == "без комментария" else raw_comment[:1000]
    await state.update_data(comment=comment, booking_command_id=uuid.uuid4().hex)
    data = await state.get_data()
    await state.set_state(Booking.waiting_for_confirmation)
    await message.answer(
        _booking_summary(data)
        + _invitation_email_line(data.get("invitation_email"))
        + "\n\nОтправьте «Отправить заявку», «Изменить дату», «Изменить время», "
        "«Изменить название» или «Изменить комментарий».",
        reply_markup=booking_confirmation_keyboard(),
    )


@router.message(Booking.waiting_for_confirmation, F.text == "Изменить дату")
async def booking_change_date(message: Message, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    await start_booking(message, state, settings, session_factory, booking_runtime)


@router.message(Booking.waiting_for_confirmation, F.text == "Изменить время")
async def booking_change_time(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(Booking.waiting_for_time)
    await message.answer(
        "Выберите время кнопкой ниже." + _invitation_email_line(data.get("invitation_email")),
        reply_markup=booking_times_keyboard(data["slots"]),
    )


@router.message(Booking.waiting_for_confirmation, F.text == "Изменить название")
async def booking_change_title(message: Message, state: FSMContext):
    await state.set_state(Booking.waiting_for_title)
    await message.answer("Введите название встречи заново.", reply_markup=ReplyKeyboardRemove())


@router.message(Booking.waiting_for_confirmation, F.text == "Изменить комментарий")
async def booking_change_comment(message: Message, state: FSMContext):
    await state.set_state(Booking.waiting_for_comment)
    await message.answer(
        "Введите комментарий заново или нажмите «Без комментария».",
        reply_markup=booking_comment_keyboard(),
    )


@router.message(Booking.waiting_for_confirmation, F.text == "Отправить заявку")
async def submit_booking(message: Message, state: FSMContext, settings: Settings, session_factory,
                         booking_runtime=None):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return
    data = await state.get_data()
    if not all(key in data for key in ("selected_start", "title", "settings_version", "booking_command_id")):
        await message.answer("Форма устарела. Начните запись заново.")
        return
    result = await _booking_command(message, booking_runtime, "create_request", actor=user,
        command_id=data["booking_command_id"], start_at=data["selected_start"], title=data["title"],
        comment=data.get("comment"), settings_version=data["settings_version"])
    if result is None:
        return
    await state.clear()
    await message.answer(f"Заявка #{result['id']} сохранена. После решения вы получите уведомление в Telegram.",
                         reply_markup=client_main_menu())


@router.message(Booking.waiting_for_confirmation)
async def booking_confirmation_fallback(message: Message):
    await message.answer("Отправьте «Отправить заявку» или выберите, что нужно изменить.")


@router.message(F.text == "Мои заявки")
async def my_requests(message: Message, settings: Settings, session_factory):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return

    async with session_factory() as session:
        requests = await list_client_requests(session, user.id)

    if not requests:
        await message.answer("У вас пока нет заявок.")
        return
    await message.answer("\n\n".join(_request_card(item) for item in requests))


@router.message(F.text == "Мои встречи")
async def my_meetings(message: Message, settings: Settings, session_factory, booking_runtime=None):
    user = await _load_user(message, settings, session_factory)
    if user is None:
        return
    if user.role == OWNER_ROLE:
        await message.answer("Список встреч владельца появится после подключения календаря.")
        return

    async with session_factory() as session:
        meetings = await list_client_meetings(session, user.id)

    if not meetings:
        await message.answer("У вас пока нет подтвержденных встреч.")
        return
    now = booking_runtime.commands.clock() if booking_runtime else datetime.now(timezone.utc)
    labels = {"confirmed": "Подтверждена", "rescheduled": "Перенесена", "reschedule_pending": "Ожидает решения по переносу",
              "cancelled_by_owner": "Отменена специалистом", "cancelled_by_client": "Отменена клиентом"}
    for item in meetings:
        active = item.status in {"confirmed", "rescheduled", "reschedule_pending"} and utc(item.start_at) > now
        markup = meeting_actions_keyboard(item.id, item.version, moving=item.status == "reschedule_pending") if active and booking_runtime else None
        await message.answer(f"Встреча #{item.id}\nДата и время: {format_moscow_dt(item.start_at)} МСК\n"
            f"Название: {item.title}\nСтатус: {labels.get(item.status, item.status)}", reply_markup=markup)


def _callback_command_key(callback, settings):
    return hashlib.sha256(f"telegram:{settings.bot_token.split(':', 1)[0]}:callback:{callback.id}".encode()).hexdigest()


async def _client_meeting_action(callback, settings, session_factory, booking_runtime, *, moving=False):
    if booking_runtime is None:
        raise BookingError("BOOKING_UNAVAILABLE")
    actor = await _load_callback_user(callback, settings, session_factory)
    if actor is None or actor.status != "active":
        raise BookingError("ACCOUNT_BLOCKED")
    if actor.telegram_id == settings.owner_telegram_id:
        raise BookingError("FORBIDDEN")
    if not actor.email or not actor.consent_at:
        raise BookingError("REGISTRATION_REQUIRED")
    _, identifier, version = callback.data.split(":")
    async with session_factory() as session:
        meeting = await session.get(Meeting, int(identifier))
        if meeting is None or meeting.client_id != actor.id:
            raise BookingError("RESOURCE_NOT_FOUND")
        if meeting.version != int(version):
            raise BookingError("STALE_VERSION")
        allowed = {"confirmed", "rescheduled"} if moving else {"confirmed", "rescheduled", "reschedule_pending"}
        if meeting.status not in allowed or utc(meeting.start_at) <= booking_runtime.commands.clock():
            raise BookingError("INVALID_STATE")
    return actor, meeting


@router.callback_query(F.data.regexp(r"^meeting_move:\d+:\d+$"))
async def meeting_move_callback(callback: CallbackQuery, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    try:
        actor, meeting = await _client_meeting_action(callback, settings, session_factory, booking_runtime, moving=True)
    except (BookingError, DatabaseBusy) as error:
        await callback.answer(_command_error(error), show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer("Откройте встречу в личном чате с ботом.", show_alert=True)
        return
    await state.clear()
    await state.update_data(move_meeting_id=meeting.id, move_version=meeting.version, move_actor_id=actor.id)
    await state.set_state(MeetingMove.waiting_for_datetime)
    example = utc(meeting.start_at) + timedelta(days=1)
    while example.weekday() >= 5:
        example += timedelta(days=1)
    await callback.message.answer(f"Перенос встречи #{meeting.id}. Введите новую дату и время по Москве: ДД.ММ.ГГГГ ЧЧ:ММ.\n"
        f"Например: {format_moscow_dt(example)}. Бот проверит доступность времени.\n"
        "До согласования специалистом прежнее время встречи остаётся в силе.", reply_markup=meeting_move_keyboard())
    await callback.answer()


@router.message(MeetingMove.waiting_for_datetime)
async def submit_meeting_move(message: Message, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    if message.text in {"Отмена", "Мои встречи"}:
        await state.clear()
        await message.answer("Выбор нового времени отменён. Встреча сохранена.", reply_markup=client_main_menu())
        return
    client = await _require_registered_client(message, settings, session_factory)
    if client is None:
        return
    parts = (message.text or "").split()
    day = parse_moscow_date(parts[0]) if len(parts) == 2 else None
    time = parse_moscow_time(parts[1]) if len(parts) == 2 else None
    if day is None or time is None:
        await message.answer("Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    data = await state.get_data()
    if data.get("move_actor_id") != client.id or not data.get("move_meeting_id") or not data.get("move_version"):
        await state.clear()
        await message.answer("Форма устарела. Откройте «Мои встречи» заново.", reply_markup=client_main_menu())
        return
    result = await _booking_command(message, booking_runtime, "request_reschedule", actor=client,
        command_id=_message_command_key(message, settings, "request_reschedule_form"),
        meeting_id=data["move_meeting_id"], expected_version=data["move_version"],
        start_at=to_utc(datetime.combine(day, time, tzinfo=MOSCOW_TZ)))
    if result is not None:
        await state.clear()
        await message.answer("Запрос переноса сохранён. До решения специалиста действует прежнее время встречи.", reply_markup=client_main_menu())


@router.callback_query(F.data.regexp(r"^meeting_cancel:\d+:\d+$"))
async def meeting_cancel_callback(callback: CallbackQuery, settings: Settings, session_factory, booking_runtime=None):
    try:
        _, meeting = await _client_meeting_action(callback, settings, session_factory, booking_runtime)
    except (BookingError, DatabaseBusy) as error:
        await callback.answer(_command_error(error), show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.answer(f"Отменить встречу #{meeting.id} на {format_moscow_dt(meeting.start_at)} МСК?",
            reply_markup=meeting_cancel_keyboard(meeting.id, meeting.version))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^meeting_cancel_yes:\d+:\d+$"))
async def confirm_meeting_cancel_callback(callback: CallbackQuery, settings: Settings, session_factory, booking_runtime=None):
    actor = await _load_callback_user(callback, settings, session_factory)
    if actor is None or actor.telegram_id == settings.owner_telegram_id or not isinstance(callback.message, Message):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, identifier, version = callback.data.split(":")
    result = await _booking_command(callback.message, booking_runtime, "cancel_meeting", actor=actor,
        command_id=_callback_command_key(callback, settings), meeting_id=int(identifier), expected_version=int(version), reason="Отменено клиентом")
    if result is not None:
        await callback.message.answer(f"Встреча #{identifier} отменена. Обновите «Мои встречи».")
    await callback.answer("Отмена сохранена" if result is not None else "Не удалось отменить встречу")


@router.callback_query(F.data == "meeting_cancel_no")
async def keep_meeting_callback(callback: CallbackQuery):
    await callback.answer("Встреча сохранена")


@router.message(F.text.in_({"Новые заявки", "Ожидают решения"}))
async def pending_requests(message: Message, settings: Settings, session_factory):
    user = await _load_user(message, settings, session_factory)
    if not user or user.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    async with session_factory() as session:
        requests = await list_pending_requests(session)
        moves = (await session.execute(select(Meeting, RescheduleRequest).join(
            RescheduleRequest, RescheduleRequest.meeting_id == Meeting.id).where(
            Meeting.status == "reschedule_pending", RescheduleRequest.status == "pending").order_by(RescheduleRequest.id))).all()

    if not requests and not moves:
        await message.answer("Новых заявок нет.")
        return

    for request, client in requests:
        await message.answer(
            _request_card(request, client)
            + f"\nПодтвердить: Подтвердить {request.id}"
            + f"\nСогласовать: Согласовать {request.id}"
            + f"\nОтклонить: Отклонить {request.id}"
            + f"\nНе согласовывать: Не согласовывать {request.id}",
            reply_markup=owner_request_actions_keyboard(request.id, request.version),
        )
    for meeting, proposal in moves:
        await message.answer(f"Запрос переноса встречи #{meeting.id}\nНазвание: {meeting.title}\n"
            f"Сейчас: {format_moscow_dt(meeting.start_at)} МСК\n"
            f"Предложено: {format_moscow_dt(proposal.proposed_start_at)} МСК",
            reply_markup=owner_move_keyboard(meeting.id, meeting.version, proposal.id))


@router.callback_query(F.data.regexp(r"^(ma|mr):\d+:\d+:\d+$"))
async def owner_move_callback(callback: CallbackQuery, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_callback_user(callback, settings, session_factory)
    if owner is None or owner.status != "active" or owner.telegram_id != settings.owner_telegram_id:
        await callback.answer("Нет доступа", show_alert=True)
        return
    if booking_runtime is None or not isinstance(callback.message, Message):
        await callback.answer("Действие временно недоступно", show_alert=True)
        return
    action, identifier, version, proposal_id = callback.data.split(":")
    payload = dict(meeting_id=int(identifier), expected_version=int(version), reschedule_request_id=int(proposal_id))
    if action == "mr":
        async with session_factory() as session:
            meeting = await session.get(Meeting, int(identifier))
            proposal = await session.get(RescheduleRequest, int(proposal_id))
            if (meeting is None or meeting.version != int(version) or meeting.status != "reschedule_pending"
                    or proposal is None or proposal.meeting_id != meeting.id or proposal.status != "pending"):
                await callback.answer("Данные изменились. Обновите «Новые заявки».", show_alert=True)
                return
        await state.clear()
        await state.update_data(move_rejection=payload)
        await state.set_state(MoveRejection.waiting_for_reason)
        await callback.message.answer("Введите причину отклонения переноса или «Отмена».")
        await callback.answer()
        return
    result = await _booking_command(callback.message, booking_runtime, "approve_reschedule", actor=owner,
        command_id=_callback_command_key(callback, settings), **payload)
    if result is not None:
        await callback.message.answer(f"Перенос встречи #{identifier} согласован. Клиент получит уведомление.")
    await callback.answer("Решение сохранено" if result is not None else "Не удалось сохранить решение")


@router.message(MoveRejection.waiting_for_reason)
async def reject_move_form(message: Message, state: FSMContext, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if owner is None or owner.status != "active" or owner.telegram_id != settings.owner_telegram_id:
        await message.answer("Нет доступа")
        return
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Решение не изменено.", reply_markup=owner_main_menu())
        return
    data = await state.get_data()
    payload = data.get("move_rejection")
    if not payload:
        await state.clear()
        await message.answer("Форма устарела. Откройте «Новые заявки» заново.")
        return
    result = await _booking_command(message, booking_runtime, "reject_reschedule", actor=owner,
        command_id=_message_command_key(message, settings, "reject_move_form"), reason=message.text or "", **payload)
    if result is not None:
        await state.clear()
        await message.answer("Запрос переноса отклонён. Прежнее время встречи сохранено.", reply_markup=owner_main_menu())


@router.callback_query(F.data.regexp(r"^approve_request:\d+(?::\d+)?$"))
async def approve_request_callback(callback: CallbackQuery, settings: Settings, session_factory,
                                   booking_runtime=None):
    owner = await _load_callback_user(callback, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Обновите список заявок: эта кнопка устарела.", show_alert=True)
        return
    request_id, version = map(int, parts[1:])
    key = hashlib.sha256(f"telegram:{settings.bot_token.split(':', 1)[0]}:callback:{callback.id}".encode()).hexdigest()
    ok, text = await _approve_request_by_id(request_id, owner=owner, bot=callback.bot,
        settings=settings, session_factory=session_factory, booking_runtime=booking_runtime,
        command_id=key, expected_version=version)
    if isinstance(callback.message, Message):
        await callback.message.answer(text)
    await callback.answer("Решение сохранено" if ok else text, show_alert=not ok)


@router.callback_query(F.data.regexp(r"^reject_request:\d+(?::\d+)?$"))
async def reject_request_callback(callback: CallbackQuery, state: FSMContext, settings: Settings, session_factory):
    owner = await _load_callback_user(callback, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Обновите список заявок: эта кнопка устарела.", show_alert=True)
        return
    request_id, version = map(int, parts[1:])
    await state.update_data(reject_request_id=request_id, reject_request_version=version)
    await state.set_state(Rejection.waiting_for_reason)
    if isinstance(callback.message, Message):
        await callback.message.answer(f"Укажите причину отказа для заявки #{request_id}.")
    await callback.answer("Жду причину отказа")


@router.message(F.text == "Настройки встреч")
async def meeting_settings(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    async with session_factory() as session:
        profile = await get_owner_profile(session, owner.id)
        if profile is None:
            await message.answer("Профиль владельца еще не создан. Отправьте /start.")
            return
        await message.answer(_profile_text(profile))


@router.message(F.text.regexp(r"^(Длительность|Перерыв|Лимит|Горизонт|Минимум)\s+\d+$"))
async def update_numeric_meeting_setting(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    command, raw_value = (message.text or "").split(maxsplit=1)
    value = int(raw_value)
    fields = {
        "Длительность": "default_duration", "Перерыв": "buffer_minutes", "Лимит": "daily_limit",
        "Горизонт": "booking_horizon_days", "Минимум": "min_booking_notice_hours",
    }
    field = fields[command]
    min_value, max_value = SETTING_RANGES[field]
    if value < min_value or value > max_value:
        await message.answer(f"Значение должно быть от {min_value} до {max_value}.")
        return

    try:
        data = await _profile_commands(settings, session_factory).apply_settings_message(
            actor_id=owner.id, command_id=_message_command_key(message, settings, "settings"),
            changes={field: value})
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return
    await message.answer("Настройка обновлена.\n\n" + _profile_text(SimpleNamespace(**data)))


@router.message(F.text.in_({"Запись вкл", "Запись выкл"}))
async def toggle_booking(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    enabled = message.text == "Запись вкл"
    try:
        data = await _profile_commands(settings, session_factory).apply_settings_message(
            actor_id=owner.id, command_id=_message_command_key(message, settings, "settings"),
            changes={"booking_enabled": enabled})
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return
    await message.answer("Настройка обновлена.\n\n" + _profile_text(SimpleNamespace(**data)))


@router.message(F.text == "Google Calendar")
async def google_calendar(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    if not google_oauth_is_configured(settings):
        await message.answer(
            "Google Calendar еще не настроен.\n\n"
            "Заполните переменные в .env:\n"
            "GOOGLE_CLIENT_ID\n"
            "GOOGLE_CLIENT_SECRET\n"
            "GOOGLE_REDIRECT_URI\n\n"
            "После этого перезапустите бота и снова нажмите «Google Calendar».",
        )
        return

    async with session_factory() as session:
        profile = await get_owner_profile(session, owner.id)
        credential = await get_google_oauth_credential(session, owner.id)

    if credential is not None and credential.status == "active":
        calendar_id = profile.calendar_id if profile is not None and profile.calendar_id else "не выбран"
        google_email_line = f"Google-аккаунт: {profile.google_email}\n" if profile and profile.google_email else ""
        await message.answer(
            "Google Calendar подключен.\n\n"
            f"{google_email_line}"
            f"Основной календарь: {calendar_id}\n\n"
            "Чтобы выбрать календарь, отправьте «Выбрать календарь Google».",
        )
        return

    link = build_google_oauth_link(settings, owner.id)
    try:
        await GoogleBindingCommands(session_factory, owner_telegram_id=settings.owner_telegram_id).begin_oauth(
            actor_id=owner.id, oauth_state=link.state)
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return
    status = "pending"
    await message.answer(
        "Подключение Google Calendar.\n\n"
        f"Текущий статус: {status}\n"
        "Откройте ссылку, выберите Google-аккаунт владельца и разрешите доступ к календарю:\n\n"
        f"{link.url}\n\n"
        "После разрешения Google вернет вас на GOOGLE_REDIRECT_URI, "
        "а бот сохранит подключение и пришлет уведомление.",
    )


@router.message(F.text == "Выбрать календарь Google")
async def choose_google_calendar(message: Message, state: FSMContext, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    try:
        version = await GoogleBindingCommands(session_factory,
            owner_telegram_id=settings.owner_telegram_id).selection_version(actor_id=owner.id)
        access_token = await GoogleTokens(session_factory, settings=settings, owner_user_id=owner.id)()
        calendars = await list_google_calendars(access_token)
    except Exception:
        await message.answer("Не удалось получить календари Google. Повторите позже.")
        return

    if not calendars:
        await message.answer("В Google-аккаунте не найдено календарей с правами владельца.")
        return

    await state.set_state(GoogleCalendarSelection.waiting_for_calendar_number)
    await state.update_data(
        google_binding_version=version,
        google_calendars=[
            {
                "id": calendar.id,
                "summary": calendar.summary,
                "primary": calendar.primary,
                "access_role": calendar.access_role,
            }
            for calendar in calendars
        ]
    )
    lines = []
    for index, calendar in enumerate(calendars, start=1):
        primary = " основной" if calendar.primary else ""
        lines.append(f"{index}. {calendar.summary}{primary}\nID: {calendar.id}")
    await message.answer(
        "Выберите основной календарь для встреч.\n\n"
        + "\n\n".join(lines)
        + "\n\nОтправьте номер календаря.",
    )


@router.message(GoogleCalendarSelection.waiting_for_calendar_number)
async def save_google_calendar_selection(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory,
):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        await state.clear()
        return

    try:
        selected_number = int((message.text or "").strip())
    except ValueError:
        await message.answer("Отправьте номер календаря из списка.")
        return

    data = await state.get_data()
    calendars = data.get("google_calendars", [])
    if selected_number < 1 or selected_number > len(calendars):
        await message.answer("Такого номера нет в списке. Отправьте номер календаря.")
        return

    selected = calendars[selected_number - 1]
    try:
        await GoogleBindingCommands(session_factory, owner_telegram_id=settings.owner_telegram_id).select_calendar(
            actor_id=owner.id, command_id=_message_command_key(message, settings, "google_calendar"),
            calendar_id=selected["id"], expected_version=data.get("google_binding_version"))
    except (BookingError, DatabaseBusy) as error:
        await message.answer(_command_error(error))
        return

    await state.clear()
    await message.answer(
        "Основной календарь выбран.\n\n"
        f"Название: {selected['summary']}\n"
        f"ID: {selected['id']}",
    )


@router.message(F.text.regexp(r"^(Подтвердить|Согласовать)\s+\d+$"))
async def approve_request(message: Message, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    request_id = int((message.text or "").split()[-1])
    _ok, text = await _approve_request_by_id(request_id, owner=owner, bot=message.bot,
        settings=settings, session_factory=session_factory, booking_runtime=booking_runtime,
        command_id=_message_command_key(message, settings, "approve_request"))
    await message.answer(text)


@router.message(F.text.regexp(r"^(Отклонить|Не согласовывать)\s+\d+$"))
async def start_reject_request(message: Message, state: FSMContext, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    request_id = int((message.text or "").split()[-1])
    async with session_factory() as session:
        request = await get_booking_request(session, request_id)
        if request is None or request.status != "pending":
            await message.answer("Заявка не найдена или уже обработана.")
            return
        version = request.version
    await state.update_data(reject_request_id=request_id, reject_request_version=version)
    await state.set_state(Rejection.waiting_for_reason)
    await message.answer(f"Укажите причину отказа для заявки #{request_id}.")


@router.message(Rejection.waiting_for_reason)
async def reject_request_with_reason(message: Message, state: FSMContext, settings: Settings,
                                     session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    data = await state.get_data()
    if "reject_request_id" not in data or "reject_request_version" not in data:
        await message.answer("Форма устарела. Обновите список заявок.")
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина должна быть не короче 3 символов.")
        return
    result = await _booking_command(message, booking_runtime, "reject_request", actor=owner,
        command_id=_message_command_key(message, settings, "reject_request"),
        request_id=data["reject_request_id"], expected_version=data["reject_request_version"], reason=reason)
    if result is not None:
        await state.clear()
        await message.answer(f"Заявка #{result['id']} отклонена. Клиент получит уведомление.")


@router.message(F.text.regexp(r"^Перенести\s+\d+\s+\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}$"))
async def reschedule_meeting_by_owner(message: Message, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    parts = (message.text or "").split()
    selected_date, selected_time = parse_moscow_date(parts[2]), parse_moscow_time(parts[3])
    if selected_date is None or selected_time is None:
        await message.answer("Укажите дату ДД.ММ.ГГГГ и время ЧЧ:ММ.")
        return
    result = await _booking_command(message, booking_runtime, "reschedule_by_owner", actor=owner,
        command_id=_message_command_key(message, settings, "reschedule_by_owner"), meeting_id=int(parts[1]),
        start_at=to_utc(datetime.combine(selected_date, selected_time, tzinfo=MOSCOW_TZ)))
    if result is not None:
        await message.answer(f"Новое время встречи #{result['id']} сохранено. Клиент получит уведомление после обновления календаря.")


@router.message(F.text.regexp(r"^Отменить встречу\s+\d+(\s+.+)?$"))
async def cancel_meeting_by_owner(message: Message, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    parts = (message.text or "").split(maxsplit=3)
    result = await _booking_command(message, booking_runtime, "cancel_meeting", actor=owner,
        command_id=_message_command_key(message, settings, "cancel_meeting"), meeting_id=int(parts[2]),
        reason=parts[3] if len(parts) > 3 else "Отменено специалистом")
    if result is not None:
        await message.answer(f"Встреча #{result['id']} отменена. Удаление события из календаря может занять некоторое время.")


@router.message(F.text.regexp(r"^Запросить перенос\s+\d+\s+\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(\s+.+)?$"))
async def request_reschedule_by_client(message: Message, settings: Settings, session_factory, booking_runtime=None):
    client = await _require_registered_client(message, settings, session_factory)
    if client is None:
        return
    parts = (message.text or "").split(maxsplit=5)
    selected_date, selected_time = parse_moscow_date(parts[3]), parse_moscow_time(parts[4])
    if selected_date is None or selected_time is None:
        await message.answer("Укажите дату ДД.ММ.ГГГГ и время ЧЧ:ММ.")
        return
    result = await _booking_command(message, booking_runtime, "request_reschedule", actor=client,
        command_id=_message_command_key(message, settings, "request_reschedule"), meeting_id=int(parts[2]),
        start_at=to_utc(datetime.combine(selected_date, selected_time, tzinfo=MOSCOW_TZ)),
        comment=parts[5] if len(parts) > 5 else None)
    if result is not None:
        await message.answer("Запрос переноса сохранён. Специалист получит уведомление.")


@router.message(F.text.regexp(r"^Согласовать перенос\s+\d+$"))
async def approve_client_reschedule(message: Message, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    parts = (message.text or "").split(maxsplit=3)
    result = await _booking_command(message, booking_runtime, "approve_reschedule", actor=owner,
        command_id=_message_command_key(message, settings, "approve_reschedule"), meeting_id=int(parts[2]))
    if result is not None:
        await message.answer(f"Перенос встречи #{result['id']} согласован. Клиент получит уведомление после обновления календаря.")


@router.message(F.text.regexp(r"^Отклонить перенос\s+\d+(\s+.+)?$"))
async def reject_client_reschedule(message: Message, settings: Settings, session_factory, booking_runtime=None):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return
    parts = (message.text or "").split(maxsplit=3)
    result = await _booking_command(message, booking_runtime, "reject_reschedule", actor=owner,
        command_id=_message_command_key(message, settings, "reject_reschedule"), meeting_id=int(parts[2]), reason=parts[3] if len(parts) > 3 else "Специалист отклонил перенос")
    if result is not None:
        await message.answer(f"Перенос встречи #{result['id']} отклонён. Клиент получит уведомление.")


@router.message(F.text.regexp(r"^Отменить мою встречу\s+\d+(\s+.+)?$"))
async def cancel_meeting_by_client(message: Message, settings: Settings, session_factory, booking_runtime=None):
    client = await _require_registered_client(message, settings, session_factory)
    if client is None:
        return
    parts = (message.text or "").split(maxsplit=4)
    result = await _booking_command(message, booking_runtime, "cancel_meeting", actor=client,
        command_id=_message_command_key(message, settings, "cancel_meeting"), meeting_id=int(parts[3]),
        reason=parts[4] if len(parts) > 4 else "Отменено клиентом")
    if result is not None:
        await message.answer(f"Встреча #{result['id']} отменена. Удаление события из календаря может занять некоторое время.")


@router.message(F.text == "Управление системой")
async def system_menu(message: Message, settings: Settings, session_factory):
    user = await _load_user(message, settings, session_factory)
    if user and user.role == OWNER_ROLE:
        await message.answer(
            "Состояние системы: этап 2 запущен.\n"
            "База данных: доступна.\n"
            "Заявки: доступны.\n"
            "Google Calendar: будет подключен на следующем этапе.",
        )
        return
    await message.answer("У вас нет доступа к управлению системой")


@router.message(F.text == "Клиенты")
async def clients_menu(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    async with session_factory() as session:
        clients = await list_clients(session)

    if not clients:
        await message.answer("Клиентов пока нет.")
        return

    cards = [
        _client_card(client)
        for client in clients
    ]
    await message.answer(
        "Клиенты:\n\n"
        + "\n\n".join(cards)
        + "\n\nКоманды:\n"
        "Заблокировать клиента 12\n"
        "Разблокировать клиента 12"
    )


async def _set_client_access(message, settings, session_factory, *, blocked):
    owner = await _load_user(message, settings, session_factory)
    if not owner:
        return
    client_id = int((message.text or "").split()[-1])
    try:
        result = await _profile_commands(settings, session_factory).set_client_access(
            actor_id=owner.id,
            command_id=_message_command_key(message, settings, "client_access"),
            client_id=client_id, blocked=blocked)
    except (BookingError, DatabaseBusy) as error:
        await message.answer("Клиент не найден." if getattr(error, "code", None) == "RESOURCE_NOT_FOUND"
                             else _command_error(error))
        return
    label = "заблокирован" if result["status"] == "blocked" else "разблокирован"
    await message.answer(f"Клиент #{client_id} {label}.")


@router.message(F.text.regexp(r"^Заблокировать клиента\s+\d+$"))
async def block_client(message: Message, settings: Settings, session_factory):
    await _set_client_access(message, settings, session_factory, blocked=True)


@router.message(F.text.regexp(r"^Разблокировать клиента\s+\d+$"))
async def unblock_client(message: Message, settings: Settings, session_factory):
    await _set_client_access(message, settings, session_factory, blocked=False)


@router.message(
    F.text.in_(
        {
            "Отправить заявку",
            "Изменить дату",
            "Изменить время",
            "Изменить название",
            "Изменить комментарий",
            "Отмена",
            "Без комментария",
        }
    )
)
async def stale_booking_buttons(message: Message):
    if (message.text or "").strip() == "Отмена":
        await message.answer("Действие отменено. Главное меню.", reply_markup=client_main_menu())
        return
    await message.answer(
        "Бот был перезапущен, поэтому текущий шаг записи сбросился. "
        "Нажмите «Записаться на встречу» и начните запись заново.",
        reply_markup=client_main_menu(),
    )


@router.message()
async def fallback(message: Message):
    await message.answer("Этот раздел будет реализован на следующих этапах. Используйте /start.")
