OWNER_ROLE = "owner"
CLIENT_ROLE = "client"


def resolve_role(telegram_id: int, owner_telegram_id: int) -> str:
    if telegram_id == owner_telegram_id:
        return OWNER_ROLE
    return CLIENT_ROLE

