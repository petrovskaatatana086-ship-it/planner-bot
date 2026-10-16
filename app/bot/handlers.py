from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import async_sessionmaker
from datetime import datetime, timedelta, timezone

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
)
from app.bot.states import Booking, GoogleCalendarSelection, Registration, Rejection
from app.config import Settings
from app.db.models import User
from app.db.repositories import (
    approve_booking_request,
    approve_meeting_reschedule,
    cancel_meeting,
    complete_client_registration,
    create_booking_draft,
    create_booking_request_from_draft,
    ensure_user_stub,
    get_active_owner_profile,
    get_booking_request,
    get_busy_starts,
    get_google_oauth_credential,
    get_meeting,
    get_owner_profile,
    list_client_meetings,
    list_client_requests,
    list_clients,
    list_pending_requests,
    mark_google_oauth_pending,
    reject_booking_request,
    reject_meeting_reschedule,
    request_meeting_reschedule,
    reschedule_meeting,
    set_client_status,
    set_owner_google_calendar,
    update_owner_profile_field,
)
from app.domain.booking import (
    MEETING_DURATION_MINUTES,
    MOSCOW_TZ,
    available_dates,
    available_slots_for_date,
    format_moscow_date,
    format_moscow_dt,
    parse_moscow_date,
    parse_moscow_time,
    to_utc,
    validate_title,
)
from app.domain.email import is_valid_email
from app.domain.roles import OWNER_ROLE
from app.integrations.google_calendar import list_google_calendars
from app.integrations.google_oauth import build_google_oauth_link, google_oauth_is_configured
from app.services.google import (
    cancel_google_event_for_meeting,
    create_google_event_for_meeting,
    get_google_busy_starts_for_date,
    get_valid_google_access_token,
    update_google_event_for_reschedule,
)
from app.services.email import (
    send_booking_approved_email,
    send_booking_rejected_email,
    send_meeting_cancelled_email,
    send_meeting_reschedule_rejected_email,
    send_meeting_rescheduled_email,
)

router = Router()


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

    async with session_factory() as session:
        user = await ensure_user_stub(
            session,
            telegram_id=telegram_user.id,
            owner_telegram_id=settings.owner_telegram_id,
            full_name=_display_name(message),
            username=telegram_user.username,
        )
        await session.commit()
        await session.refresh(user)
        return user


async def _load_callback_user(callback: CallbackQuery, settings: Settings, session_factory: async_sessionmaker):
    callback_user = callback.from_user
    if callback_user is None:
        return None

    async with session_factory() as session:
        user = await ensure_user_stub(
            session,
            telegram_id=callback_user.id,
            owner_telegram_id=settings.owner_telegram_id,
            full_name=callback_user.full_name or callback_user.username or str(callback_user.id),
            username=callback_user.username,
        )
        await session.commit()
        await session.refresh(user)
        return user


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


async def _approve_request_by_id(
    request_id: int,
    *,
    owner: User,
    bot,
    settings: Settings,
    session_factory,
) -> tuple[bool, str]:
    async with session_factory() as session:
        request = await get_booking_request(session, request_id)
        if request is None or request.status != "pending":
            return False, "Заявка не найдена или уже обработана."
        client_user = await session.get(User, request.client_id)
        if client_user is None:
            return False, "Клиент заявки не найден."
        meeting = await approve_booking_request(session, request=request, actor_user_id=owner.id)
        try:
            await create_google_event_for_meeting(
                session,
                settings=settings,
                owner_user_id=owner.id,
                request=request,
                meeting=meeting,
                client=client_user,
            )
        except Exception as exc:
            await session.rollback()
            return False, (
                "Не удалось создать событие Google Calendar. "
                f"Заявка не подтверждена.\nОшибка: {exc}"
            )
        await session.commit()
        client_id = request.client_id
        meet_url = meeting.google_meet_url
        calendar_url = meeting.google_event_url

    async with session_factory() as session:
        client_requests = await list_client_requests(session, client_id)
        client_request = next((item for item in client_requests if item.id == request_id), None)
        client_user = None
        if client_request is not None:
            client_user = await session.get(User, client_id)

    if client_user is not None:
        calendar_line = f"\nGoogle Calendar: {calendar_url}" if calendar_url else ""
        meet_line = f"\nGoogle Meet: {meet_url}" if meet_url else ""
        await bot.send_message(
            client_user.telegram_id,
            f"Ваша заявка #{request_id} подтверждена. "
            f"Время: {format_moscow_dt(client_request.start_at)} МСК."
            f"{calendar_line}"
            f"{meet_line}",
        )
        if client_request is not None:
            await send_booking_approved_email(
                settings,
                client=client_user,
                request=client_request,
                meeting=meeting,
            )
    return True, f"Заявка #{request_id} подтверждена."


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
        "Добро пожаловать в Планировщик встреч.\n\n"
        "Для записи нужно принять согласие на обработку данных.",
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
    await state.set_state(Registration.waiting_for_name)
    await message.answer("Введите ваше имя.")


@router.message(Registration.waiting_for_consent)
async def reject_consent(message: Message):
    await message.answer("Чтобы продолжить регистрацию, нажмите «Принимаю».")


@router.message(Registration.waiting_for_name)
async def enter_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 255:
        await message.answer("Имя должно быть от 2 до 255 символов. Введите имя еще раз.")
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
    async with session_factory() as session:
        user = await ensure_user_stub(
            session,
            telegram_id=telegram_user.id,
            owner_telegram_id=settings.owner_telegram_id,
            full_name=data["full_name"],
            username=telegram_user.username,
        )
        await complete_client_registration(
            session,
            user=user,
            full_name=data["full_name"],
            email=data["email"],
        )
        await session.commit()

    await state.clear()
    await message.answer("Регистрация завершена. Главное меню.", reply_markup=client_main_menu())


@router.message(Registration.waiting_for_confirmation)
async def confirm_registration_fallback(message: Message):
    await message.answer("Подтвердите регистрацию или выберите, что нужно изменить.")


@router.message(F.text == "Записаться на встречу")
async def start_booking(message: Message, state: FSMContext, settings: Settings, session_factory):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return

    async with session_factory() as session:
        profile = await get_active_owner_profile(session)
        if profile is not None and not profile.booking_enabled:
            await message.answer("Запись временно выключена специалистом.")
            return
        await create_booking_draft(session, user.id)
        await session.commit()

    duration = profile.default_duration if profile is not None else 60
    horizon = profile.booking_horizon_days if profile is not None else 14
    notice = profile.min_booking_notice_hours if profile is not None else 24
    dates = available_dates(
        horizon_days=horizon,
        duration_minutes=duration,
        min_notice_hours=notice,
    )
    if not dates:
        await message.answer("Сейчас нет доступных дат для записи. Попробуйте позже.")
        return

    await state.set_state(Booking.waiting_for_date)
    await state.update_data(duration=duration, horizon=horizon, notice=notice, invitation_email=user.email)
    await message.answer(
        "Выберите дату кнопкой ниже." + _invitation_email_line(user.email),
        reply_markup=booking_dates_keyboard(dates),
    )


@router.message(Booking.waiting_for_time, F.text == "Изменить дату")
@router.message(
    Booking.waiting_for_date,
    Booking.waiting_for_time,
    Booking.waiting_for_title,
    Booking.waiting_for_comment,
    Booking.waiting_for_confirmation,
    F.text == "Отмена",
)
async def cancel_booking_flow(message: Message, state: FSMContext, settings: Settings = None, session_factory=None):
    if (message.text or "").strip() == "Изменить дату":
        await start_booking(message, state, settings, session_factory)
        return
    await _cancel_booking_flow(message, state)


@router.message(Booking.waiting_for_date)
async def choose_date(message: Message, state: FSMContext, settings: Settings, session_factory):
    selected_date = parse_moscow_date(message.text or "")
    if selected_date is None:
        data = await state.get_data()
        allowed_dates = available_dates(
            horizon_days=data.get("horizon", 14),
            duration_minutes=data.get("duration", 60),
            min_notice_hours=data.get("notice", 24),
        )
        await message.answer(
            "Выберите дату кнопкой ниже." + _invitation_email_line(data.get("invitation_email")),
            reply_markup=booking_dates_keyboard(allowed_dates),
        )
        return

    data = await state.get_data()
    allowed_dates = available_dates(
        horizon_days=data.get("horizon", 14),
        duration_minutes=data.get("duration", 60),
        min_notice_hours=data.get("notice", 24),
    )
    if selected_date not in allowed_dates:
        await message.answer(
            "Эта дата недоступна. Выберите дату кнопкой из списка." + _invitation_email_line(data.get("invitation_email")),
            reply_markup=booking_dates_keyboard(allowed_dates),
        )
        return

    async with session_factory() as session:
        busy_starts = await get_busy_starts(session)
        profile = await get_active_owner_profile(session)
        if profile is not None:
            try:
                google_busy_starts = await get_google_busy_starts_for_date(
                    session,
                    settings=settings,
                    owner_user_id=profile.user_id,
                    selected_date=selected_date,
                    duration_minutes=data.get("duration", 60),
                    min_notice_hours=data.get("notice", 24),
                )
            except Exception as exc:
                await message.answer(f"Не удалось проверить занятость Google Calendar: {exc}")
                return
            busy_starts |= google_busy_starts

    slots = available_slots_for_date(
        selected_date,
        busy_starts=busy_starts,
        duration_minutes=data.get("duration", 60),
        min_notice_hours=data.get("notice", 24),
    )
    if not slots:
        await message.answer(
            "На эту дату свободных слотов уже нет. Выберите другую дату." + _invitation_email_line(data.get("invitation_email")),
            reply_markup=booking_dates_keyboard(allowed_dates),
        )
        return

    await state.update_data(selected_date=selected_date, slots=slots)
    await state.set_state(Booking.waiting_for_time)
    await message.answer(
        "Выберите время кнопкой ниже." + _invitation_email_line(data.get("invitation_email")),
        reply_markup=booking_times_keyboard(slots),
    )


@router.message(Booking.waiting_for_time)
async def choose_time(message: Message, state: FSMContext, settings: Settings, session_factory):
    selected_time = parse_moscow_time(message.text or "")
    if selected_time is None:
        data = await state.get_data()
        await message.answer(
            "Выберите время кнопкой ниже." + _invitation_email_line(data.get("invitation_email")),
            reply_markup=booking_times_keyboard(data.get("slots", [])),
        )
        return

    data = await state.get_data()
    selected_start = None
    for slot in data["slots"]:
        if slot.time() == selected_time:
            selected_start = slot
            break

    if selected_start is None:
        await message.answer(
            "Это время недоступно. Выберите время кнопкой из списка." + _invitation_email_line(data.get("invitation_email")),
            reply_markup=booking_times_keyboard(data["slots"]),
        )
        return

    async with session_factory() as session:
        busy_starts = await get_busy_starts(session)
        profile = await get_active_owner_profile(session)
        if profile is not None:
            try:
                google_busy_starts = await get_google_busy_starts_for_date(
                    session,
                    settings=settings,
                    owner_user_id=profile.user_id,
                    selected_date=data["selected_date"],
                    duration_minutes=data.get("duration", 60),
                    min_notice_hours=data.get("notice", 24),
                )
            except Exception as exc:
                await message.answer(f"Не удалось проверить занятость Google Calendar: {exc}")
                return
            busy_starts |= google_busy_starts
    if selected_start not in available_slots_for_date(
        data["selected_date"],
        busy_starts=busy_starts,
        duration_minutes=data.get("duration", 60),
        min_notice_hours=data.get("notice", 24),
    ):
        await message.answer("Слот уже заняли. Давайте выберем дату и время заново.")
        await start_booking(message, state, settings, session_factory)
        return

    await state.update_data(selected_start=selected_start)
    await state.set_state(Booking.waiting_for_title)
    await message.answer(
        "Введите название встречи. От 3 до 100 символов.",
        reply_markup=ReplyKeyboardRemove(),
    )


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
    await state.update_data(comment=comment)
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
async def booking_change_date(message: Message, state: FSMContext, settings: Settings, session_factory):
    await start_booking(message, state, settings, session_factory)


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
async def submit_booking(message: Message, state: FSMContext, settings: Settings, session_factory):
    user = await _require_registered_client(message, settings, session_factory)
    if user is None:
        return

    data = await state.get_data()
    async with session_factory() as session:
        busy_starts = await get_busy_starts(session)
        profile = await get_active_owner_profile(session)
        if profile is not None:
            try:
                google_busy_starts = await get_google_busy_starts_for_date(
                    session,
                    settings=settings,
                    owner_user_id=profile.user_id,
                    selected_date=data["selected_date"],
                    duration_minutes=data.get("duration", 60),
                    min_notice_hours=data.get("notice", 24),
                )
            except Exception as exc:
                await message.answer(f"Не удалось проверить занятость Google Calendar: {exc}")
                return
            busy_starts |= google_busy_starts
        available_slots = available_slots_for_date(
            data["selected_date"],
            busy_starts=busy_starts,
            duration_minutes=data.get("duration", 60),
            min_notice_hours=data.get("notice", 24),
        )
        if data["selected_start"] not in available_slots:
            await message.answer("Этот слот уже недоступен. Создайте заявку заново.")
            await state.clear()
            return
        request = await create_booking_request_from_draft(
            session,
            client_id=user.id,
            selected_start_local=data["selected_start"],
            title=data["title"],
            comment=data.get("comment"),
            duration_minutes=data.get("duration", 60),
        )
        await session.commit()
        request_id = request.id

    await state.clear()
    await message.answer(
        f"Заявка #{request_id} отправлена специалисту. "
        "После решения вы получите уведомление в Telegram.",
        reply_markup=client_main_menu(),
    )

    try:
        await message.bot.send_message(
            settings.owner_telegram_id,
            "Новая заявка на встречу.\n"
            f"Заявка #{request_id}\n"
            f"Клиент: {user.full_name}, {user.email}\n"
            f"Дата и время: {format_moscow_dt(data['selected_start'])} МСК\n"
            f"Название: {data['title']}\n\n"
            f"Согласовать: Подтвердить {request_id}\n"
            f"Согласовать: Согласовать {request_id}\n"
            f"Не согласовывать: Отклонить {request_id}\n"
            f"Не согласовывать: Не согласовывать {request_id}",
            reply_markup=owner_request_actions_keyboard(request_id),
        )
    except Exception:
        await message.answer("Заявка сохранена, но уведомление владельцу не отправилось.")


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
async def my_meetings(message: Message, settings: Settings, session_factory):
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
    await message.answer(
        "\n\n".join(
            f"Встреча #{item.id}\n"
            f"Дата и время: {format_moscow_dt(item.start_at)} МСК\n"
            f"Название: {item.title}\n"
            f"Статус: {item.status}"
            for item in meetings
        )
    )


@router.message(F.text.in_({"Новые заявки", "Ожидают решения"}))
async def pending_requests(message: Message, settings: Settings, session_factory):
    user = await _load_user(message, settings, session_factory)
    if not user or user.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    async with session_factory() as session:
        requests = await list_pending_requests(session)

    if not requests:
        await message.answer("Новых заявок нет.")
        return

    for request, client in requests:
        await message.answer(
            _request_card(request, client)
            + f"\nПодтвердить: Подтвердить {request.id}"
            + f"\nСогласовать: Согласовать {request.id}"
            + f"\nОтклонить: Отклонить {request.id}"
            + f"\nНе согласовывать: Не согласовывать {request.id}",
            reply_markup=owner_request_actions_keyboard(request.id),
        )


@router.callback_query(F.data.regexp(r"^approve_request:\d+$"))
async def approve_request_callback(callback: CallbackQuery, settings: Settings, session_factory):
    owner = await _load_callback_user(callback, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await callback.answer("Нет доступа", show_alert=True)
        return

    request_id = int((callback.data or "").split(":")[-1])
    ok, result_text = await _approve_request_by_id(
        request_id,
        owner=owner,
        bot=callback.bot,
        settings=settings,
        session_factory=session_factory,
    )
    callback_message = callback.message
    if isinstance(callback_message, Message):
        await callback_message.answer(result_text)
    await callback.answer("Заявка согласована" if ok else "Не удалось обработать заявку", show_alert=not ok)


@router.callback_query(F.data.regexp(r"^reject_request:\d+$"))
async def reject_request_callback(callback: CallbackQuery, state: FSMContext, settings: Settings, session_factory):
    owner = await _load_callback_user(callback, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await callback.answer("Нет доступа", show_alert=True)
        return

    request_id = int((callback.data or "").split(":")[-1])
    await state.update_data(reject_request_id=request_id)
    await state.set_state(Rejection.waiting_for_reason)
    callback_message = callback.message
    if isinstance(callback_message, Message):
        await callback_message.answer(f"Укажите причину отказа для заявки #{request_id}.")
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
        "Длительность": ("default_duration", 15, 240),
        "Перерыв": ("buffer_minutes", 0, 120),
        "Лимит": ("daily_limit", 1, 50),
        "Горизонт": ("booking_horizon_days", 1, 180),
        "Минимум": ("min_booking_notice_hours", 1, 720),
    }
    field, min_value, max_value = fields[command]
    if value < min_value or value > max_value:
        await message.answer(f"Значение должно быть от {min_value} до {max_value}.")
        return

    async with session_factory() as session:
        profile = await update_owner_profile_field(
            session,
            owner=owner,
            field=field,
            value=value,
        )
        await session.commit()
        await message.answer("Настройка обновлена.\n\n" + _profile_text(profile))


@router.message(F.text.in_({"Запись вкл", "Запись выкл"}))
async def toggle_booking(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    enabled = message.text == "Запись вкл"
    async with session_factory() as session:
        profile = await update_owner_profile_field(
            session,
            owner=owner,
            field="booking_enabled",
            value=enabled,
        )
        await session.commit()
        await message.answer("Настройка обновлена.\n\n" + _profile_text(profile))


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
    async with session_factory() as session:
        profile = await get_owner_profile(session, owner.id)
        credential = None
        if profile is not None:
            credential = await mark_google_oauth_pending(
                session,
                owner,
                oauth_state=link.state,
            )
        await session.commit()

    status = credential.status if credential is not None else "pending"
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
        async with session_factory() as session:
            access_token = await get_valid_google_access_token(
                session,
                settings=settings,
                owner_user_id=owner.id,
            )
            await session.commit()
        calendars = await list_google_calendars(access_token)
    except Exception as exc:
        await message.answer(f"Не удалось получить календари Google: {exc}")
        return

    if not calendars:
        await message.answer("В Google-аккаунте не найдено календарей с правами владельца.")
        return

    await state.set_state(GoogleCalendarSelection.waiting_for_calendar_number)
    await state.update_data(
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
    async with session_factory() as session:
        await set_owner_google_calendar(
            session,
            owner_user_id=owner.id,
            calendar_id=selected["id"],
        )
        await session.commit()

    await state.clear()
    await message.answer(
        "Основной календарь выбран.\n\n"
        f"Название: {selected['summary']}\n"
        f"ID: {selected['id']}",
    )


@router.message(F.text.regexp(r"^(Подтвердить|Согласовать)\s+\d+$"))
async def approve_request(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    request_id = int((message.text or "").split()[-1])
    _ok, result_text = await _approve_request_by_id(
        request_id,
        owner=owner,
        bot=message.bot,
        settings=settings,
        session_factory=session_factory,
    )
    await message.answer(result_text)


@router.message(F.text.regexp(r"^(Отклонить|Не согласовывать)\s+\d+$"))
async def start_reject_request(message: Message, state: FSMContext, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    request_id = int((message.text or "").split()[-1])
    await state.update_data(reject_request_id=request_id)
    await state.set_state(Rejection.waiting_for_reason)
    await message.answer(f"Укажите причину отказа для заявки #{request_id}.")


@router.message(Rejection.waiting_for_reason)
async def reject_request_with_reason(
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

    data = await state.get_data()
    request_id = data["reject_request_id"]
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина должна быть не короче 3 символов.")
        return

    async with session_factory() as session:
        request = await get_booking_request(session, request_id)
        if request is None or request.status != "pending":
            await message.answer("Заявка не найдена или уже обработана.")
            await state.clear()
            return
        await reject_booking_request(
            session,
            request=request,
            actor_user_id=owner.id,
            reason=reason,
        )
        await session.commit()
        client_id = request.client_id

    await state.clear()
    await message.answer(f"Заявка #{request_id} отклонена.")

    async with session_factory() as session:
        client_user = await session.get(User, client_id)
    if client_user is not None:
        await message.bot.send_message(
            client_user.telegram_id,
            f"Ваша заявка #{request_id} отклонена.\nПричина: {reason}",
        )
        await send_booking_rejected_email(
            settings,
            client=client_user,
            request=request,
            reason=reason,
        )


@router.message(F.text.regexp(r"^Перенести\s+\d+\s+\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}$"))
async def reschedule_meeting_by_owner(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    parts = (message.text or "").split()
    meeting_id = int(parts[1])
    selected_date = parse_moscow_date(parts[2])
    selected_time = parse_moscow_time(parts[3])
    if selected_date is None or selected_time is None:
        await message.answer("Формат: Перенести 1 27.07.2026 10:00")
        return

    new_start_local = datetime.combine(selected_date, selected_time, tzinfo=MOSCOW_TZ)
    new_start_utc = to_utc(new_start_local)

    async with session_factory() as session:
        meeting = await get_meeting(session, meeting_id)
        if meeting is None or meeting.status.startswith("cancelled"):
            await message.answer("Встреча не найдена или уже отменена.")
            return
        client_user = await session.get(User, meeting.client_id)
        if client_user is None:
            await message.answer("Клиент встречи не найден.")
            return

        profile = await get_owner_profile(session, owner.id)
        duration = profile.default_duration if profile is not None else MEETING_DURATION_MINUTES
        notice = profile.min_booking_notice_hours if profile is not None else 24
        busy_starts = await get_busy_starts(session)
        current_start = (
            meeting.start_at
            if meeting.start_at.tzinfo is not None
            else meeting.start_at.replace(tzinfo=timezone.utc)
        )
        busy_starts.discard(current_start)
        try:
            google_busy = await get_google_busy_starts_for_date(
                session,
                settings=settings,
                owner_user_id=owner.id,
                selected_date=selected_date,
                duration_minutes=duration,
                min_notice_hours=notice,
            )
            google_busy.discard(current_start)
            busy_starts |= google_busy
        except Exception as exc:
            await message.answer(f"Не удалось проверить занятость Google Calendar: {exc}")
            return

        available_slots = available_slots_for_date(
            selected_date,
            busy_starts=busy_starts,
            duration_minutes=duration,
            min_notice_hours=notice,
        )
        if new_start_local not in available_slots:
            await message.answer("Новый слот недоступен. Выберите другое время.")
            return

        new_end_utc = new_start_utc + timedelta(minutes=duration)
        await reschedule_meeting(
            session,
            meeting=meeting,
            start_at=new_start_utc,
            end_at=new_end_utc,
            actor_user_id=owner.id,
        )
        try:
            await update_google_event_for_reschedule(
                session,
                settings=settings,
                owner_user_id=owner.id,
                meeting=meeting,
            )
        except Exception as exc:
            await session.rollback()
            await message.answer(f"Не удалось обновить событие Google Calendar: {exc}")
            return
        await session.commit()

    await message.answer(f"Встреча #{meeting_id} перенесена на {format_moscow_dt(new_start_utc)} МСК.")
    await message.bot.send_message(
        client_user.telegram_id,
        f"Ваша встреча #{meeting_id} перенесена на {format_moscow_dt(new_start_utc)} МСК.",
    )
    await send_meeting_rescheduled_email(
        settings,
        client=client_user,
        meeting=meeting,
        reason="Новое время назначено специалистом.",
    )


@router.message(F.text.regexp(r"^Отменить встречу\s+\d+(\s+.+)?$"))
async def cancel_meeting_by_owner(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    parts = (message.text or "").split(maxsplit=2)
    meeting_id = int(parts[2].split()[0]) if len(parts) > 2 else 0
    reason = ""
    if len(parts) == 3:
        raw_tail = parts[2]
        reason_parts = raw_tail.split(maxsplit=1)
        reason = reason_parts[1] if len(reason_parts) == 2 else ""
    if not reason:
        reason = "Отменено специалистом"

    async with session_factory() as session:
        meeting = await get_meeting(session, meeting_id)
        if meeting is None or meeting.status.startswith("cancelled"):
            await message.answer("Встреча не найдена или уже отменена.")
            return
        client_user = await session.get(User, meeting.client_id)
        if client_user is None:
            await message.answer("Клиент встречи не найден.")
            return

        try:
            await cancel_google_event_for_meeting(
                session,
                settings=settings,
                owner_user_id=owner.id,
                meeting=meeting,
            )
        except Exception as exc:
            await message.answer(f"Не удалось отменить событие Google Calendar: {exc}")
            return
        await cancel_meeting(
            session,
            meeting=meeting,
            actor_user_id=owner.id,
            reason=reason,
        )
        await session.commit()

    await message.answer(f"Встреча #{meeting_id} отменена.")
    await message.bot.send_message(
        client_user.telegram_id,
        f"Ваша встреча #{meeting_id} отменена.\nПричина: {reason}",
    )
    await send_meeting_cancelled_email(
        settings,
        client=client_user,
        meeting=meeting,
        reason=reason,
    )


@router.message(F.text.regexp(r"^Запросить перенос\s+\d+\s+\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(\s+.+)?$"))
async def request_reschedule_by_client(message: Message, settings: Settings, session_factory):
    client = await _require_registered_client(message, settings, session_factory)
    if client is None:
        return

    parts = (message.text or "").split(maxsplit=4)
    meeting_id = int(parts[2])
    selected_date = parse_moscow_date(parts[3])
    selected_time = parse_moscow_time(parts[4].split()[0])
    comment = ""
    if len(parts) == 5:
        tail_parts = parts[4].split(maxsplit=1)
        comment = tail_parts[1] if len(tail_parts) == 2 else ""
    if selected_date is None or selected_time is None:
        await message.answer("Формат: Запросить перенос 1 27.07.2026 10:00 комментарий")
        return

    proposed_start_local = datetime.combine(selected_date, selected_time, tzinfo=MOSCOW_TZ)
    proposed_start_utc = to_utc(proposed_start_local)

    async with session_factory() as session:
        meeting = await get_meeting(session, meeting_id)
        if meeting is None or meeting.client_id != client.id or meeting.status.startswith("cancelled"):
            await message.answer("Встреча не найдена или недоступна для переноса.")
            return
        if meeting.status == "reschedule_pending":
            await message.answer("По этой встрече уже ожидает согласования запрос переноса.")
            return

        profile = await get_active_owner_profile(session)
        if profile is None:
            await message.answer("Профиль специалиста не найден.")
            return
        duration = profile.default_duration
        notice = profile.min_booking_notice_hours
        if proposed_start_utc < datetime.now(timezone.utc) + timedelta(hours=notice):
            await message.answer(f"Перенос возможен не менее чем за {notice} ч. до встречи.")
            return

        busy_starts = await get_busy_starts(session)
        current_start = (
            meeting.start_at
            if meeting.start_at.tzinfo is not None
            else meeting.start_at.replace(tzinfo=timezone.utc)
        )
        busy_starts.discard(current_start)
        try:
            google_busy = await get_google_busy_starts_for_date(
                session,
                settings=settings,
                owner_user_id=profile.user_id,
                selected_date=selected_date,
                duration_minutes=duration,
                min_notice_hours=notice,
            )
            google_busy.discard(current_start)
            busy_starts |= google_busy
        except Exception as exc:
            await message.answer(f"Не удалось проверить занятость Google Calendar: {exc}")
            return

        available_slots = available_slots_for_date(
            selected_date,
            busy_starts=busy_starts,
            duration_minutes=duration,
            min_notice_hours=notice,
        )
        if proposed_start_local not in available_slots:
            await message.answer("Запрошенный слот недоступен. Выберите другое время.")
            return

        await request_meeting_reschedule(
            session,
            meeting=meeting,
            proposed_start_at=proposed_start_utc,
            proposed_end_at=proposed_start_utc + timedelta(minutes=duration),
            comment=comment,
            actor_user_id=client.id,
        )
        await session.commit()

    await message.answer("Запрос переноса отправлен специалисту.")
    await message.bot.send_message(
        settings.owner_telegram_id,
        f"Клиент запросил перенос встречи #{meeting_id}.\n"
        f"Новое время: {format_moscow_dt(proposed_start_utc)} МСК\n"
        f"Комментарий: {comment or 'нет'}\n\n"
        f"Согласовать: Согласовать перенос {meeting_id}\n"
        f"Отклонить: Отклонить перенос {meeting_id} причина",
    )


@router.message(F.text.regexp(r"^Согласовать перенос\s+\d+$"))
async def approve_client_reschedule(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    meeting_id = int((message.text or "").split()[-1])
    async with session_factory() as session:
        meeting = await get_meeting(session, meeting_id)
        if meeting is None or meeting.status != "reschedule_pending":
            await message.answer("Запрос переноса не найден.")
            return
        client_user = await session.get(User, meeting.client_id)
        if client_user is None:
            await message.answer("Клиент встречи не найден.")
            return
        await approve_meeting_reschedule(session, meeting=meeting, actor_user_id=owner.id)
        try:
            await update_google_event_for_reschedule(
                session,
                settings=settings,
                owner_user_id=owner.id,
                meeting=meeting,
            )
        except Exception as exc:
            await session.rollback()
            await message.answer(f"Не удалось обновить событие Google Calendar: {exc}")
            return
        await session.commit()
        new_start = meeting.start_at

    await message.answer(f"Перенос встречи #{meeting_id} согласован.")
    await message.bot.send_message(
        client_user.telegram_id,
        f"Перенос встречи #{meeting_id} согласован.\n"
        f"Новое время: {format_moscow_dt(new_start)} МСК.",
    )
    await send_meeting_rescheduled_email(
        settings,
        client=client_user,
        meeting=meeting,
        reason="Запрос на перенос согласован.",
    )


@router.message(F.text.regexp(r"^Отклонить перенос\s+\d+(\s+.+)?$"))
async def reject_client_reschedule(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    parts = (message.text or "").split(maxsplit=2)
    meeting_id = int(parts[2].split()[0]) if len(parts) > 2 else 0
    reason = ""
    if len(parts) == 3:
        reason_parts = parts[2].split(maxsplit=1)
        reason = reason_parts[1] if len(reason_parts) == 2 else ""
    if not reason:
        reason = "Специалист отклонил перенос"

    async with session_factory() as session:
        meeting = await get_meeting(session, meeting_id)
        if meeting is None or meeting.status != "reschedule_pending":
            await message.answer("Запрос переноса не найден.")
            return
        client_user = await session.get(User, meeting.client_id)
        if client_user is None:
            await message.answer("Клиент встречи не найден.")
            return
        await reject_meeting_reschedule(
            session,
            meeting=meeting,
            actor_user_id=owner.id,
            reason=reason,
        )
        await session.commit()

    await message.answer(f"Перенос встречи #{meeting_id} отклонен.")
    await message.bot.send_message(
        client_user.telegram_id,
        f"Перенос встречи #{meeting_id} отклонен.\nПричина: {reason}",
    )
    await send_meeting_reschedule_rejected_email(
        settings,
        client=client_user,
        meeting=meeting,
        reason=reason,
    )


@router.message(F.text.regexp(r"^Отменить мою встречу\s+\d+(\s+.+)?$"))
async def cancel_meeting_by_client(message: Message, settings: Settings, session_factory):
    client = await _require_registered_client(message, settings, session_factory)
    if client is None:
        return

    parts = (message.text or "").split(maxsplit=3)
    meeting_id = int(parts[3].split()[0]) if len(parts) > 3 else 0
    reason = ""
    if len(parts) == 4:
        reason_parts = parts[3].split(maxsplit=1)
        reason = reason_parts[1] if len(reason_parts) == 2 else ""
    if not reason:
        reason = "Отменено клиентом"

    async with session_factory() as session:
        meeting = await get_meeting(session, meeting_id)
        if meeting is None or meeting.client_id != client.id or meeting.status.startswith("cancelled"):
            await message.answer("Встреча не найдена или уже отменена.")
            return
        profile = await get_active_owner_profile(session)
        notice = profile.min_booking_notice_hours if profile is not None else 24
        start_at = meeting.start_at if meeting.start_at.tzinfo else meeting.start_at.replace(tzinfo=timezone.utc)
        if start_at < datetime.now(timezone.utc) + timedelta(hours=notice):
            await message.answer(f"Отмена возможна не менее чем за {notice} ч. до встречи.")
            return
        try:
            await cancel_google_event_for_meeting(
                session,
                settings=settings,
                owner_user_id=profile.user_id if profile is not None else client.id,
                meeting=meeting,
            )
        except Exception as exc:
            await message.answer(f"Не удалось отменить событие Google Calendar: {exc}")
            return
        await cancel_meeting(
            session,
            meeting=meeting,
            actor_user_id=client.id,
            reason=reason,
            status="cancelled_by_client",
        )
        await session.commit()

    await message.answer(f"Встреча #{meeting_id} отменена.")
    await message.bot.send_message(
        settings.owner_telegram_id,
        f"Клиент отменил встречу #{meeting_id}.\nПричина: {reason}",
    )
    await send_meeting_cancelled_email(
        settings,
        client=client,
        meeting=meeting,
        reason=reason,
    )


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


@router.message(F.text.regexp(r"^Заблокировать клиента\s+\d+$"))
async def block_client(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    client_id = int((message.text or "").split()[-1])
    async with session_factory() as session:
        client = await session.get(User, client_id)
        if client is None or client.role == OWNER_ROLE:
            await message.answer("Клиент не найден.")
            return
        if client.status == "blocked":
            await message.answer(f"Клиент #{client.id} уже заблокирован.")
            return
        await set_client_status(
            session,
            client=client,
            status="blocked",
            actor_user_id=owner.id,
        )
        await session.commit()

    await message.answer(f"Клиент #{client_id} заблокирован.")


@router.message(F.text.regexp(r"^Разблокировать клиента\s+\d+$"))
async def unblock_client(message: Message, settings: Settings, session_factory):
    owner = await _load_user(message, settings, session_factory)
    if not owner or owner.role != OWNER_ROLE:
        await message.answer("У вас нет доступа к управлению системой")
        return

    client_id = int((message.text or "").split()[-1])
    async with session_factory() as session:
        client = await session.get(User, client_id)
        if client is None or client.role == OWNER_ROLE:
            await message.answer("Клиент не найден.")
            return
        if client.status == "active":
            await message.answer(f"Клиент #{client.id} уже активен.")
            return
        await set_client_status(
            session,
            client=client,
            status="active",
            actor_user_id=owner.id,
        )
        await session.commit()

    await message.answer(f"Клиент #{client_id} разблокирован.")


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
