"""Shared registration wording reused from the existing Telegram flow."""
import unicodedata

CONSENT_VERSION = "bot-consent-v1"
CONSENT_TEXT = (
    "Добро пожаловать в Планировщик встреч.\n\n"
    "Для записи нужно принять согласие на обработку данных."
)


def valid_name(value):
    if not isinstance(value, str):
        return False
    value = value.strip()
    # Unicode format characters (e.g. emoji ZWJ) are not control characters.
    return 1 <= len(value) <= 255 and not any(unicodedata.category(c) in ("Cc", "Cs") for c in value)
