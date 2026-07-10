from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response, status
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuthUser, OAuthAccessToken, utcnow


SESSION_COOKIE = "quantpilot_session"
CSRF_COOKIE = "quantpilot_csrf"
CSRF_HEADER = "X-CSRF-Token"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
MAX_LOGIN_FAILURES = 5
LOCK_MINUTES = 15

password_hasher = PasswordHash.recommended()
_dummy_hash = password_hasher.hash("QuantPilot-dummy-password-value")


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def validate_username(value: str) -> str:
    username = value.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=422,
            detail="用户名需为3至64位，只能包含字母、数字、点、下划线或短横线",
        )
    return username


def validate_password(value: str, *, field_name: str = "密码") -> None:
    if len(value) < PASSWORD_MIN_LENGTH or len(value) > PASSWORD_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"{field_name}长度必须为12至128位")


def hash_password(value: str) -> str:
    return password_hasher.hash(value)


def verify_password(value: str, password_hash: str) -> bool:
    return password_hasher.verify(value, password_hash)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class IssuedSession:
    access_token: str
    csrf_token: str
    expires_at: datetime
    record: OAuthAccessToken


@dataclass(slots=True)
class AuthenticatedSession:
    user: AuthUser
    token: OAuthAccessToken
    via_cookie: bool


def issue_session(db: Session, user: AuthUser, settings: Settings) -> IssuedSession:
    access_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(hours=settings.quantpilot_session_hours)
    record = OAuthAccessToken(
        user_id=user.id,
        token_hash=digest_secret(access_token),
        csrf_token_hash=digest_secret(csrf_token),
        expires_at=expires_at,
    )
    db.add(record)
    db.flush()
    return IssuedSession(access_token, csrf_token, expires_at, record)


def set_session_cookies(response: Response, session: IssuedSession, settings: Settings) -> None:
    max_age = max(0, int((session.expires_at - utcnow()).total_seconds()))
    common = {
        "secure": settings.quantpilot_cookie_secure,
        "samesite": "strict",
        "path": "/",
        "max_age": max_age,
    }
    response.set_cookie(SESSION_COOKIE, session.access_token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, session.csrf_token, httponly=False, **common)


def clear_session_cookies(response: Response, settings: Settings) -> None:
    common = {
        "secure": settings.quantpilot_cookie_secure,
        "samesite": "strict",
        "path": "/",
    }
    response.delete_cookie(SESSION_COOKIE, httponly=True, **common)
    response.delete_cookie(CSRF_COOKIE, httponly=False, **common)


def request_token(request: Request) -> tuple[str | None, bool]:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip(), False
    return request.cookies.get(SESSION_COOKIE), True


def authenticate_raw_token(db: Session, raw_token: str | None, *, via_cookie: bool) -> AuthenticatedSession | None:
    if not raw_token:
        return None
    record = db.scalar(
        select(OAuthAccessToken).where(OAuthAccessToken.token_hash == digest_secret(raw_token))
    )
    now = utcnow()
    if record is None or record.revoked_at is not None or (as_utc(record.expires_at) or now) <= now:
        return None
    user = db.get(AuthUser, record.user_id)
    if user is None or not user.is_active:
        return None
    last_used = as_utc(record.last_used_at)
    if last_used is None or now - last_used > timedelta(minutes=5):
        record.last_used_at = now
        db.commit()
    return AuthenticatedSession(user=user, token=record, via_cookie=via_cookie)


def authenticate_request(db: Session, request: Request) -> AuthenticatedSession | None:
    raw_token, via_cookie = request_token(request)
    return authenticate_raw_token(db, raw_token, via_cookie=via_cookie)


def validate_csrf(db: Session, request: Request, session: AuthenticatedSession) -> bool:
    if not session.via_cookie:
        return True
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or not cookie or not secrets.compare_digest(header, cookie):
        return False
    return secrets.compare_digest(digest_secret(header), session.token.csrf_token_hash)


def authenticate_password(db: Session, username: str, password: str) -> AuthUser:
    normalized = normalize_username(username)
    user = db.scalar(select(AuthUser).where(AuthUser.username_normalized == normalized))
    now = utcnow()
    if user is None:
        password_hasher.verify(password, _dummy_hash)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    locked_until = as_utc(user.locked_until)
    if locked_until is not None and locked_until > now:
        raise HTTPException(status_code=429, detail="登录尝试过多，请稍后再试")
    password_valid, updated_hash = password_hasher.verify_and_update(password, user.password_hash)
    if not user.is_active or not password_valid:
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_LOGIN_FAILURES:
            user.failed_login_count = 0
            user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if updated_hash is not None:
        user.password_hash = updated_hash
    db.commit()
    db.refresh(user)
    return user


def revoke_session(db: Session, token_id: str) -> None:
    token = db.get(OAuthAccessToken, token_id)
    if token is not None and token.revoked_at is None:
        token.revoked_at = utcnow()
        db.commit()


def revoke_all_sessions(db: Session, user_id: int) -> None:
    db.execute(
        update(OAuthAccessToken)
        .where(OAuthAccessToken.user_id == user_id, OAuthAccessToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.commit()
