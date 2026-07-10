from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AuthUser, ConnectionConfig, EngineState
from .schemas import (
    AuthPasswordChange,
    AuthSetupRequest,
    AuthStatusRead,
    AuthUserRead,
    OAuthTokenRead,
)
from .services.auth import (
    AuthenticatedSession,
    authenticate_password,
    authenticate_request,
    clear_session_cookies,
    hash_password,
    issue_session,
    normalize_username,
    revoke_all_sessions,
    revoke_session,
    set_session_cookies,
    validate_password,
    validate_username,
    verify_password,
)
from .templates import seed_user_defaults


router = APIRouter(prefix="/api/auth", tags=["authentication"])
settings = get_settings()


def prevent_token_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def user_read(user: AuthUser, db: Session | None = None) -> AuthUserRead:
    return AuthUserRead(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        alpaca_configured=(
            (
                db.scalar(
                    select(ConnectionConfig.id).where(ConnectionConfig.user_id == user.id)
                )
                is not None
            )
            if db is not None
            else False
        )
        or (
            user.id == 1 and settings.alpaca_configured
        ),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def require_session(request: Request) -> AuthenticatedSession:
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return session


@router.get("/status", response_model=AuthStatusRead)
def auth_status(request: Request, db: Session = Depends(get_db)) -> AuthStatusRead:
    user = db.get(AuthUser, 1)
    session = authenticate_request(db, request) if user is not None else None
    return AuthStatusRead(
        setup_required=user is None,
        authenticated=session is not None,
        user=user_read(session.user, db) if session else None,
    )


@router.post("/setup", response_model=OAuthTokenRead, status_code=201)
def setup_admin(
    payload: AuthSetupRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> OAuthTokenRead:
    if db.get(AuthUser, 1) is not None:
        raise HTTPException(status_code=409, detail="管理员已经初始化")
    username = validate_username(payload.username)
    validate_password(payload.password)
    user = AuthUser(
        id=1,
        username=username,
        username_normalized=normalize_username(username),
        password_hash=hash_password(payload.password),
        role="admin",
        is_active=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="管理员已经初始化") from exc
    session = issue_session(db, user, settings)
    seed_user_defaults(db, user.id)
    engine_state = db.scalar(select(EngineState).where(EngineState.user_id == user.id))
    if engine_state is not None and engine_state.reason == "等待首次创建管理员":
        engine_state.reason = "管理员已创建，等待用户开启交易引擎"
    db.commit()
    set_session_cookies(response, session, settings)
    prevent_token_caching(response)
    return OAuthTokenRead(
        access_token=session.access_token,
        expires_in=settings.quantpilot_session_hours * 3600,
        scope=user.role,
    )


@router.post("/token", response_model=OAuthTokenRead)
def oauth_token(
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> OAuthTokenRead:
    if db.get(AuthUser, 1) is None:
        raise HTTPException(status_code=409, detail="请先创建管理员")
    user = authenticate_password(db, form.username, form.password)
    session = issue_session(db, user, settings)
    db.commit()
    set_session_cookies(response, session, settings)
    prevent_token_caching(response)
    return OAuthTokenRead(
        access_token=session.access_token,
        expires_in=settings.quantpilot_session_hours * 3600,
        scope=user.role,
    )


@router.get("/me", response_model=AuthUserRead)
def current_user(
    session: AuthenticatedSession = Depends(require_session),
    db: Session = Depends(get_db),
) -> AuthUserRead:
    return user_read(session.user, db)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session: AuthenticatedSession = Depends(require_session),
    db: Session = Depends(get_db),
) -> None:
    revoke_session(db, session.token.id)
    clear_session_cookies(response, settings)


@router.post("/logout-all", status_code=204)
def logout_all(
    response: Response,
    session: AuthenticatedSession = Depends(require_session),
    db: Session = Depends(get_db),
) -> None:
    revoke_all_sessions(db, session.user.id)
    clear_session_cookies(response, settings)


@router.post("/change-password", status_code=204)
def change_password(
    payload: AuthPasswordChange,
    response: Response,
    session: AuthenticatedSession = Depends(require_session),
    db: Session = Depends(get_db),
) -> None:
    if not verify_password(payload.current_password, session.user.password_hash):
        raise HTTPException(status_code=422, detail="当前密码不正确")
    validate_password(payload.new_password, field_name="新密码")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=422, detail="新密码不能与当前密码相同")
    user = db.get(AuthUser, session.user.id)
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    revoke_all_sessions(db, user.id)
    clear_session_cookies(response, settings)
