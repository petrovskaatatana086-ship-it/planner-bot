from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


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

