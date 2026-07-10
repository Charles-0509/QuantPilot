from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings


class CredentialDecryptionError(RuntimeError):
    """Stored credentials cannot be read with this machine's local key."""


def credential_key_path(settings: Settings) -> Path:
    database_path = Path(settings.investor_db_path)
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    return database_path.parent / ".credentials.key"


def _fernet(settings: Settings) -> Fernet:
    path = credential_key_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes()
        else:
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(key)
    try:
        os.chmod(path, 0o600)
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CredentialDecryptionError("本机凭据加密密钥无效") from exc


def encrypt_credential(value: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_credential(value: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise CredentialDecryptionError("无法解密本机保存的 Alpaca 凭据") from exc


def mask_api_key(value: str) -> str:
    suffix = value.strip()[-4:]
    return f"...{suffix}" if suffix else ""
