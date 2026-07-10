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
