import re


EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(value: str) -> bool:
    email = value.strip()
    if not email or len(email) > 254:
        return False
    if any(char.isspace() for char in email):
        return False
    if email.count("@") != 1:
        return False

    local_part, domain_part = email.split("@")
    if not local_part or not domain_part:
        return False
    if len(local_part) > 64:
        return False
    if ".." in domain_part:
        return False

    return EMAIL_RE.fullmatch(email) is not None

