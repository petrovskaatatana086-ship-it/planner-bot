from datetime import date, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.domain.booking import format_moscow_date


def client_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Записаться на встречу")],
            [KeyboardButton(text="Мои заявки"), KeyboardButton(text="Мои встречи")],
        ],
        resize_keyboard=True,
    )


def meeting_actions_keyboard(meeting_id, version, *, moving=False, owner=False):
    prefix = "owner_meeting" if owner else "meeting"
    rows = []
    if not moving or owner:
        rows.append([InlineKeyboardButton(text="Перенести", callback_data=f"{prefix}_move:{meeting_id}:{version}")])
    rows.append([InlineKeyboardButton(text="Отменить встречу", callback_data=f"{prefix}_cancel:{meeting_id}:{version}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def meeting_cancel_keyboard(meeting_id, version, *, owner=False):
    prefix = "owner_meeting" if owner else "meeting"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, отменить", callback_data=f"{prefix}_cancel_yes:{meeting_id}:{version}"),
        InlineKeyboardButton(text="Оставить встречу", callback_data="meeting_cancel_no"),
    ]])


def meeting_move_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)


def owner_move_keyboard(meeting_id, version, proposal_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Согласовать перенос", callback_data=f"ma:{meeting_id}:{version}:{proposal_id}"),
        InlineKeyboardButton(text="Отклонить перенос", callback_data=f"mr:{meeting_id}:{version}:{proposal_id}"),
    ]])


def owner_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Новые заявки"), KeyboardButton(text="Мои встречи")],
            [KeyboardButton(text="Расписание"), KeyboardButton(text="Настройки встреч")],
            [KeyboardButton(text="Google Calendar"), KeyboardButton(text="Клиенты")],
            [KeyboardButton(text="Управление системой"), KeyboardButton(text="Статистика")],
        ],
        resize_keyboard=True,
    )


def consent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Принимаю")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirm_registration_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Подтвердить регистрацию")],
            [KeyboardButton(text="Изменить имя"), KeyboardButton(text="Изменить email")],
        ],
        resize_keyboard=True,
    )


def booking_dates_keyboard(dates: list[date], *, previous_page=False, next_page=False) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for item in dates:
        row.append(KeyboardButton(text=format_moscow_date(item)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    navigation = []
    if previous_page:
        navigation.append(KeyboardButton(text="Предыдущие даты"))
    if next_page:
        navigation.append(KeyboardButton(text="Следующие даты"))
    if navigation:
        rows.append(navigation)
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


def booking_times_keyboard(slots: list[datetime]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for slot in slots:
        row.append(KeyboardButton(text=slot.strftime("%H:%M")))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text="Изменить дату"), KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


def booking_comment_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Без комментария")],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def booking_confirmation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отправить заявку")],
            [KeyboardButton(text="Изменить дату"), KeyboardButton(text="Изменить время")],
            [KeyboardButton(text="Изменить название"), KeyboardButton(text="Изменить комментарий")],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
    )


def owner_request_actions_keyboard(request_id: int, version: int = 1) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Согласовать", callback_data=f"approve_request:{request_id}:{version}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"reject_request:{request_id}:{version}"),
            ]
        ]
    )
