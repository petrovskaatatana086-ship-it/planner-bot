from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    waiting_for_consent = State()
    waiting_for_name = State()
    waiting_for_email = State()
    waiting_for_confirmation = State()


class Booking(StatesGroup):
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_title = State()
    waiting_for_comment = State()
    waiting_for_confirmation = State()


class Rejection(StatesGroup):
    waiting_for_reason = State()


class GoogleCalendarSelection(StatesGroup):
    waiting_for_calendar_number = State()


class MeetingMove(StatesGroup):
    waiting_for_datetime = State()


class MoveRejection(StatesGroup):
    waiting_for_reason = State()
