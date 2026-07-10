from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth_api import require_session, user_read
from .database import get_db
from .models import AuthUser
from .schemas import AuthUserRead, UserCreateRequest, UserPasswordResetRequest, UserUpdateRequest
from .services.auth import (
    AuthenticatedSession,
    hash_password,
    normalize_username,
    revoke_all_sessions,
    validate_password,
    validate_username,
)
from .services.runtime import UserRuntimeManager
from .templates import seed_user_defaults


router = APIRouter(prefix="/api/users", tags=["user management"])


def require_admin(
    session: AuthenticatedSession = Depends(require_session),
) -> AuthenticatedSession:
    if session.user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可管理用户")
    return session


def active_admin_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(AuthUser)
            .where(AuthUser.role == "admin", AuthUser.is_active.is_(True))
        )
        or 0
    )


@router.get("", response_model=list[AuthUserRead])
def list_users(
    db: Session = Depends(get_db),
    _admin: AuthenticatedSession = Depends(require_admin),
) -> list[AuthUserRead]:
    return [user_read(user, db) for user in db.scalars(select(AuthUser).order_by(AuthUser.id)).all()]


@router.post("", response_model=AuthUserRead, status_code=201)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    _admin: AuthenticatedSession = Depends(require_admin),
) -> AuthUserRead:
    username = validate_username(payload.username)
    validate_password(payload.password)
    user = AuthUser(
        username=username,
        username_normalized=normalize_username(username),
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名已经存在") from exc
    seed_user_defaults(db, user.id)
    manager: UserRuntimeManager = request.app.state.runtime_manager
    await manager.ensure(user.id)
    return user_read(user, db)


@router.patch("/{user_id}", response_model=AuthUserRead)
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: AuthenticatedSession = Depends(require_admin),
) -> AuthUserRead:
    user = db.get(AuthUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.user.id and (
        payload.is_active is False or (payload.role is not None and payload.role != "admin")
    ):
        raise HTTPException(status_code=409, detail="不能停用或降级当前登录的管理员")
    removes_active_admin = user.role == "admin" and user.is_active and (
        payload.is_active is False or payload.role == "user"
    )
    if removes_active_admin and active_admin_count(db) <= 1:
        raise HTTPException(status_code=409, detail="系统必须保留至少一个启用的管理员")

    was_active = user.is_active
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    db.commit()
    db.refresh(user)

    manager: UserRuntimeManager = request.app.state.runtime_manager
    if was_active and not user.is_active:
        revoke_all_sessions(db, user.id)
        await manager.disable(user.id)
    elif not was_active and user.is_active:
        await manager.ensure(user.id)
    return user_read(user, db)


@router.post("/{user_id}/reset-password", status_code=204)
def reset_user_password(
    user_id: int,
    payload: UserPasswordResetRequest,
    db: Session = Depends(get_db),
    admin: AuthenticatedSession = Depends(require_admin),
) -> None:
    user = db.get(AuthUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.user.id:
        raise HTTPException(status_code=409, detail="请在设置页修改当前管理员密码")
    validate_password(payload.password, field_name="新密码")
    user.password_hash = hash_password(payload.password)
    user.failed_login_count = 0
    user.locked_until = None
    db.commit()
    revoke_all_sessions(db, user.id)
