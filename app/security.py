from cryptography.fernet import Fernet, InvalidToken


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


def encrypt_secret(value: str, key: str) -> str:
    return Fernet(key.encode("ascii")).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str, key: str) -> str:
    try:
        return Fernet(key.encode("ascii")).decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt secret with provided key") from exc

