from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    apca_api_key_id: str = ""
    apca_api_secret_key: str = ""
    alpaca_data_feed: str = "iex"
    alpaca_connect_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    alpaca_trading_read_timeout_seconds: float = Field(default=6.0, ge=2.0, le=60.0)
    alpaca_data_read_timeout_seconds: float = Field(default=45.0, ge=5.0, le=300.0)
    alpaca_retry_attempts: int = Field(default=3, ge=1, le=6)
    alpaca_retry_base_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    alpaca_retry_max_seconds: float = Field(default=4.0, ge=0.1, le=30.0)
    alpaca_circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    alpaca_circuit_recovery_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    alpaca_read_cache_seconds: float = Field(default=5.0, ge=0.0, le=30.0)
    alpaca_asset_cache_seconds: float = Field(default=300.0, ge=5.0, le=3600.0)
    alpaca_recent_bars_cache_seconds: float = Field(default=10.0, ge=0.0, le=60.0)
    alpaca_daily_bars_cache_seconds: float = Field(default=900.0, ge=900.0, le=86400.0)
    alpaca_stream_retry_base_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    alpaca_stream_retry_max_seconds: float = Field(default=300.0, ge=5.0, le=1800.0)
    investor_db_path: str = "data/investor.db"
    quantpilot_host: str = "0.0.0.0"
    quantpilot_port: int = 10000
    quantpilot_cookie_secure: bool = False
    quantpilot_session_hours: int = Field(default=12, ge=1, le=168)
    quote_interval_seconds: int = Field(default=5, ge=5, le=300)
    default_symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["SPY", "QQQ"])
    log_level: str = "INFO"

    @field_validator("default_symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",") if item.strip()]
        return list(value or [])

    @property
    def database_url(self) -> str:
        path = Path(self.investor_db_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"

    @property
    def alpaca_configured(self) -> bool:
        key = self.apca_api_key_id.strip()
        secret = self.apca_api_secret_key.strip()
        return bool(
            key
            and secret
            and not key.lower().startswith("your_")
            and not secret.lower().startswith("your_")
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
