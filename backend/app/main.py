from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import update

from .api import router, websocket_endpoint
from .auth_api import router as auth_router
from .config import get_settings
from .database import SessionLocal, init_db
from .models import AuthUser, BacktestRun, ConnectionConfig, EngineState, utcnow
from .services.auth import SESSION_COOKIE, authenticate_raw_token, authenticate_request, validate_csrf
from .services.alpaca_service import AlpacaService
from .services.credentials import CredentialDecryptionError, decrypt_credential
from .services.engine import TradingEngine
from .templates import seed_defaults

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed_defaults(db)
        db.execute(
            update(BacktestRun)
            .where(BacktestRun.status.in_(["queued", "running"]))
            .values(
                status="failed",
                error="服务重启导致回测中断，请重新运行",
                completed_at=utcnow(),
            )
        )
        if db.get(AuthUser, 1) is None:
            engine_state = db.get(EngineState, 1)
            engine_state.status = "paused"
            engine_state.reason = "等待首次创建管理员"
        db.commit()
    alpaca = AlpacaService(settings)
    with SessionLocal() as db:
        saved_connection = db.get(ConnectionConfig, 1)
        if saved_connection is not None:
            try:
                alpaca.configure(
                    decrypt_credential(saved_connection.api_key_cipher, settings),
                    decrypt_credential(saved_connection.api_secret_cipher, settings),
                    feed=saved_connection.data_feed,
                    source="web",
                    updated_at=saved_connection.updated_at,
                )
            except (CredentialDecryptionError, ValueError):
                logging.getLogger(__name__).warning("Stored web Alpaca credentials could not be decrypted")
    trading_engine = TradingEngine(settings, alpaca)
    app.state.alpaca = alpaca
    app.state.trading_engine = trading_engine
    await trading_engine.start()
    yield
    await trading_engine.shutdown()


app = FastAPI(
    title="QuantPilot",
    description="只连接 Alpaca Paper Trading 的自托管量化交易平台",
    version="1.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/token",
}
PROTECTED_DOC_PATHS = {"/docs", "/docs/", "/redoc", "/redoc/", "/openapi.json"}


@app.middleware("http")
async def oauth_session_auth(request: Request, call_next):
    path = request.url.path
    protected = (path.startswith("/api/") and path not in PUBLIC_API_PATHS) or path in PROTECTED_DOC_PATHS
    if protected:
        with SessionLocal() as db:
            auth_session = authenticate_request(db, request)
            if auth_session is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "请先登录"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not validate_csrf(
                db, request, auth_session
            ):
                return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败，请刷新页面后重试"})
            request.state.auth_session = auth_session
    return await call_next(request)


app.include_router(auth_router)
app.include_router(router)


def protected_openapi() -> dict:
    """Describe the OAuth2 password flow already enforced by middleware."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    security_schemes["OAuth2PasswordBearer"] = {
        "type": "oauth2",
        "flows": {
            "password": {
                "tokenUrl": "/api/auth/token",
                "scopes": {"admin": "QuantPilot 单管理员访问权限"},
            }
        },
    }
    for path, operations in schema.get("paths", {}).items():
        if path in PUBLIC_API_PATHS:
            continue
        for method, operation in operations.items():
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}:
                operation["security"] = [{"OAuth2PasswordBearer": ["admin"]}]
    app.openapi_schema = schema
    return schema


app.openapi = protected_openapi


@app.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host or origin.split("://", 1)[-1].rstrip("/") != host:
        await websocket.close(code=4403)
        return
    with SessionLocal() as db:
        auth_session = authenticate_raw_token(
            db,
            websocket.cookies.get(SESSION_COOKIE),
            via_cookie=True,
        )
    if auth_session is None:
        await websocket.close(code=4401)
        return
    await websocket_endpoint(websocket)


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
assets_dir = frontend_dist / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    index = frontend_dist / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(
        {
            "name": "QuantPilot API",
            "paper": True,
            "message": "前端尚未构建；开发模式请运行 Vite，或执行 Docker 构建。",
        }
    )
