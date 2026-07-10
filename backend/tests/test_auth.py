from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from app import database, main
from app.database import Base, get_db
from app.models import AuthUser, EngineState, OAuthAccessToken, utcnow
from app.config import Settings
from app.services.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    digest_secret,
    hash_password,
    issue_session,
    set_session_cookies,
    verify_password,
)
from app.templates import TEMPLATES


@pytest.fixture()
def auth_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database_engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(database_engine)
    testing_session = sessionmaker(bind=database_engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    class FakeRuntimeManager:
        def __init__(self, settings):
            self.settings = settings

        async def start(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def ensure(self, _user_id: int):
            return None

        async def disable(self, _user_id: int) -> None:
            return None

    monkeypatch.setattr(main, "SessionLocal", testing_session)
    monkeypatch.setattr(database, "SessionLocal", testing_session)
    monkeypatch.setattr(main, "UserRuntimeManager", FakeRuntimeManager)
    main.app.dependency_overrides[get_db] = override_db
    main.app.openapi_schema = None
    yield main.app, testing_session
    main.app.dependency_overrides.clear()
    main.app.openapi_schema = None
    database_engine.dispose()


def setup(client: TestClient, username: str = "charles", password: str = "correct-horse-battery"):
    response = client.post("/api/auth/setup", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return response


def csrf_header(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE)}


def test_argon2id_hashes_are_salted_and_never_store_plaintext(auth_app) -> None:
    app, sessions = auth_app
    first = hash_password("same-password-value")
    second = hash_password("same-password-value")
    assert first != second
    assert first.startswith("$argon2id$")
    assert verify_password("same-password-value", first)

    with TestClient(app) as client:
        response = setup(client)
        raw_token = response.json()["access_token"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
    with sessions() as db:
        user = db.get(AuthUser, 1)
        token = db.scalar(select(OAuthAccessToken))
        assert user is not None and "correct-horse-battery" not in user.password_hash
        assert token is not None and token.token_hash == digest_secret(raw_token)
        assert raw_token not in token.token_hash
        assert raw_token.count(".") != 2


def test_secure_cookie_setting_is_available_for_https(auth_app, tmp_path) -> None:
    _app, sessions = auth_app
    with sessions() as db:
        user = AuthUser(
            id=1,
            username="charles",
            username_normalized="charles",
            password_hash=hash_password("correct-horse-battery"),
        )
        db.add(user)
        db.flush()
        issued = issue_session(
            db,
            user,
            Settings(
                _env_file=None,
                investor_db_path=str(tmp_path / "secure-cookie.db"),
                quantpilot_cookie_secure=True,
            ),
        )
        response = Response()
        set_session_cookies(
            response,
            issued,
            Settings(
                _env_file=None,
                investor_db_path=str(tmp_path / "secure-cookie.db"),
                quantpilot_cookie_secure=True,
            ),
        )
        assert all("Secure" in value for value in response.headers.getlist("set-cookie"))
        db.rollback()


def test_engine_stays_paused_until_first_admin_exists(auth_app) -> None:
    app, sessions = auth_app
    with TestClient(app) as client:
        assert client.get("/api/auth/status").json()["setup_required"] is True
    with sessions() as db:
        assert db.get(EngineState, 1) is None


def test_setup_validation_duplicate_and_concurrent_initialization(auth_app) -> None:
    app, _sessions = auth_app
    with TestClient(app) as client:
        weak = client.post("/api/auth/setup", json={"username": "charles", "password": "too-short"})
        assert weak.status_code == 422

    def attempt(username: str) -> int:
        with TestClient(app) as thread_client:
            return thread_client.post(
                "/api/auth/setup",
                json={"username": username, "password": "strong-password-value"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(attempt, ["charles", "quantadmin"]))
    assert statuses == [201, 409]

    with TestClient(app) as client:
        duplicate = client.post(
            "/api/auth/setup",
            json={"username": "another", "password": "another-strong-password"},
        )
        assert duplicate.status_code == 409
        assert client.get("/api/auth/status").json()["setup_required"] is False
    with _sessions() as db:
        state = db.get(EngineState, 1)
        assert state is not None
        assert state.reason == "等待用户开启交易引擎"


def test_login_lockout_after_five_failures(auth_app) -> None:
    app, sessions = auth_app
    with TestClient(app) as client:
        setup(client)
        client.cookies.clear()
        for _ in range(5):
            response = client.post(
                "/api/auth/token",
                data={"username": "charles", "password": "wrong-password-value"},
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
        locked = client.post(
            "/api/auth/token",
            data={"username": "charles", "password": "correct-horse-battery"},
        )
        assert locked.status_code == 429
    with sessions() as db:
        user = db.get(AuthUser, 1)
        assert user is not None and user.locked_until is not None


def test_cookie_csrf_bearer_expiry_and_logout(auth_app) -> None:
    app, sessions = auth_app
    with TestClient(app) as client:
        response = setup(client)
        token = response.json()["access_token"]
        set_cookies = response.headers.get_list("set-cookie")
        session_cookie = next(value for value in set_cookies if value.startswith(f"{SESSION_COOKIE}="))
        csrf_cookie = next(value for value in set_cookies if value.startswith(f"{CSRF_COOKIE}="))
        assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
        assert "HttpOnly" not in csrf_cookie and "SameSite=strict" in csrf_cookie

        assert client.get("/api/auth/me").status_code == 200
        assert client.post("/api/auth/logout").status_code == 403
        assert client.post("/api/auth/logout", headers=csrf_header(client)).status_code == 204
        assert client.get("/api/auth/me").status_code == 401

        logged_in = client.post(
            "/api/auth/token",
            data={"username": "charles", "password": "correct-horse-battery"},
        )
        bearer = logged_in.json()["access_token"]
        client.cookies.clear()
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {bearer}"}
        ).status_code == 200
        assert client.post(
            "/api/auth/logout", headers={"Authorization": f"Bearer {bearer}"}
        ).status_code == 204

        with sessions() as db:
            record = db.scalar(
                select(OAuthAccessToken).where(OAuthAccessToken.token_hash == digest_secret(token))
            )
            record.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 401


def test_change_password_revokes_every_session(auth_app) -> None:
    app, sessions = auth_app
    with TestClient(app) as client:
        setup_response = setup(client)
        first_token = setup_response.json()["access_token"]
        second_login = client.post(
            "/api/auth/token",
            data={"username": "charles", "password": "correct-horse-battery"},
        )
        second_token = second_login.json()["access_token"]
        changed = client.post(
            "/api/auth/change-password",
            headers=csrf_header(client),
            json={
                "current_password": "correct-horse-battery",
                "new_password": "new-correct-horse-battery",
            },
        )
        assert changed.status_code == 204
        for token in (first_token, second_token):
            assert client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code == 401
        old_login = client.post(
            "/api/auth/token",
            data={"username": "charles", "password": "correct-horse-battery"},
        )
        assert old_login.status_code == 401
        assert client.post(
            "/api/auth/token",
            data={"username": "charles", "password": "new-correct-horse-battery"},
        ).status_code == 200
    with sessions() as db:
        assert db.scalar(
            select(OAuthAccessToken).where(OAuthAccessToken.revoked_at.is_(None))
        ) is not None


def test_protected_api_docs_openapi_and_websocket(auth_app) -> None:
    app, _sessions = auth_app
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/metadata").status_code == 401
        assert client.get("/docs").status_code == 401
        assert client.get("/openapi.json").status_code == 401
        with pytest.raises(WebSocketDisconnect) as unauthenticated:
            with client.websocket_connect(
                "/ws/events", headers={"Origin": "http://testserver"}
            ):
                pass
        assert unauthenticated.value.code == 4401

        setup(client)
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        body = schema.json()
        assert body["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/api/auth/token"
        assert body["paths"]["/api/metadata"]["get"]["security"] == [
            {"OAuth2PasswordBearer": ["user"]}
        ]
        assert "security" not in body["paths"]["/api/health"]["get"]

        with client.websocket_connect(
            "/ws/events", headers={"Origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json() == {"event": "connected", "data": {"paper": True}}
        with pytest.raises(WebSocketDisconnect) as cross_origin:
            with client.websocket_connect(
                "/ws/events", headers={"Origin": "https://evil.example"}
            ):
                pass
        assert cross_origin.value.code == 4403


def test_admin_creates_users_and_business_data_is_isolated(auth_app) -> None:
    app, sessions = auth_app
    with TestClient(app) as admin_client:
        setup(admin_client)
        created = admin_client.post(
            "/api/users",
            headers=csrf_header(admin_client),
            json={
                "username": "trader-two",
                "password": "second-user-password",
                "role": "user",
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        assert created.json()["alpaca_configured"] is False

        assert admin_client.put(
            "/api/watchlist",
            headers=csrf_header(admin_client),
            json={"symbols": ["AAPL"]},
        ).status_code == 200
        strategy_definition = dict(TEMPLATES["sma_cross"])
        strategy_definition["name"] = "管理员私有策略"
        admin_strategy = admin_client.post(
            "/api/strategies",
            headers=csrf_header(admin_client),
            json={"definition": strategy_definition},
        )
        assert admin_strategy.status_code == 201, admin_strategy.text

        with TestClient(app) as user_client:
            login = user_client.post(
                "/api/auth/token",
                data={"username": "trader-two", "password": "second-user-password"},
            )
            assert login.status_code == 200
            assert user_client.get("/api/users").status_code == 403
            assert user_client.get("/api/watchlist").json() != ["AAPL"]
            names = [item["name"] for item in user_client.get("/api/strategies").json()]
            assert "管理员私有策略" not in names

            user_definition = dict(TEMPLATES["sma_cross"])
            user_definition["name"] = "用户私有策略"
            response = user_client.post(
                "/api/strategies",
                headers=csrf_header(user_client),
                json={"definition": user_definition},
            )
            assert response.status_code == 201, response.text

        admin_names = [item["name"] for item in admin_client.get("/api/strategies").json()]
        assert "用户私有策略" not in admin_names

    with sessions() as db:
        user = db.get(AuthUser, user_id)
        assert user is not None and user.role == "user"
        assert db.scalar(select(EngineState).where(EngineState.user_id == user_id)) is not None
