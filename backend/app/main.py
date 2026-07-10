from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router, websocket_endpoint
from .config import get_settings
from .database import SessionLocal, init_db
from .models import ConnectionConfig
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
    description="只连接 Alpaca Paper Trading 的本地量化交易平台",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    configured = settings.investor_basic_auth
    if configured and (request.url.path.startswith("/api") or request.url.path.startswith("/docs")):
        authorization = request.headers.get("Authorization", "")
        valid = False
        if authorization.startswith("Basic "):
            try:
                supplied = base64.b64decode(authorization[6:]).decode("utf-8")
                valid = secrets.compare_digest(supplied, configured)
            except Exception:
                valid = False
        if not valid:
            return JSONResponse(
                status_code=401,
                content={"detail": "需要本地登录认证"},
                headers={"WWW-Authenticate": 'Basic realm="QuantPilot"'},
            )
    return await call_next(request)


app.include_router(router)


@app.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
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
