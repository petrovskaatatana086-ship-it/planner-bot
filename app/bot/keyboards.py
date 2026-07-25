from datetime import date, datetime

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.domain.booking import format_moscow_date


def client_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Записаться на встречу")],
            [KeyboardButton(text="Мои заявки"), KeyboardButton(text="Мои встречи")],
            [KeyboardButton(text="Мой профиль"), KeyboardButton(text="Поддержка")],
        ],
        resize_keyboard=True,
    )


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


def booking_dates_keyboard(dates: list[date]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for item in dates:
        row.append(KeyboardButton(text=format_moscow_date(item)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
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
