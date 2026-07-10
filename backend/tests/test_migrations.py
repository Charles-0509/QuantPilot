from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[2]


def alembic(db_path: Path, revision: str) -> None:
    environment = os.environ.copy()
    environment["INVESTOR_DB_PATH"] = str(db_path)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_auth_migration_handles_fresh_and_existing_sqlite(tmp_path: Path) -> None:
    fresh_path = tmp_path / "fresh.db"
    alembic(fresh_path, "head")
    fresh = create_engine(f"sqlite:///{fresh_path}")
    assert {"auth_users", "oauth_access_tokens"}.issubset(inspect(fresh).get_table_names())
    fresh.dispose()

    existing_path = tmp_path / "existing.db"
    alembic(existing_path, "0002_connection_config")
    existing = create_engine(f"sqlite:///{existing_path}")
    # 0001 uses current metadata, so remove the new tables to reproduce a real
    # pre-1.1.0 database stamped at 0002.
    with existing.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS oauth_access_tokens"))
        connection.execute(text("DROP TABLE IF EXISTS auth_users"))
        connection.execute(text("CREATE TABLE preserved_records (value TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO preserved_records (value) VALUES ('keep-me')"))
    existing.dispose()

    alembic(existing_path, "head")
    upgraded = create_engine(f"sqlite:///{existing_path}")
    assert {"auth_users", "oauth_access_tokens", "preserved_records"}.issubset(
        inspect(upgraded).get_table_names()
    )
    with upgraded.connect() as connection:
        assert connection.scalar(text("SELECT value FROM preserved_records")) == "keep-me"
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004_multi_user"
    upgraded.dispose()


def test_multi_user_migration_assigns_existing_records_to_admin(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-1.2.db"
    legacy = create_engine(f"sqlite:///{db_path}")
    with legacy.begin() as connection:
        statements = [
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
            "INSERT INTO alembic_version VALUES ('0003_auth')",
            "CREATE TABLE auth_users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL, username_normalized VARCHAR(64) NOT NULL UNIQUE, password_hash TEXT NOT NULL, is_active BOOLEAN NOT NULL, failed_login_count INTEGER NOT NULL, locked_until DATETIME, last_login_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
            "INSERT INTO auth_users VALUES (1,'admin','admin','hash',1,0,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            "CREATE TABLE strategies (id VARCHAR(36) PRIMARY KEY, is_template BOOLEAN NOT NULL)",
            "INSERT INTO strategies VALUES ('template',1),('private',0)",
            "CREATE TABLE backtest_runs (id VARCHAR(36) PRIMARY KEY)",
            "INSERT INTO backtest_runs VALUES ('backtest')",
            "CREATE TABLE signals (id VARCHAR(36) PRIMARY KEY)",
            "INSERT INTO signals VALUES ('signal')",
            "CREATE TABLE orders (id VARCHAR(64) PRIMARY KEY)",
            "INSERT INTO orders VALUES ('order')",
            "CREATE TABLE event_logs (id INTEGER PRIMARY KEY)",
            "INSERT INTO event_logs VALUES (1)",
            "CREATE TABLE risk_settings (id INTEGER PRIMARY KEY)",
            "INSERT INTO risk_settings VALUES (1)",
            "CREATE TABLE engine_state (id INTEGER PRIMARY KEY)",
            "INSERT INTO engine_state VALUES (1)",
            "CREATE TABLE connection_config (id INTEGER PRIMARY KEY)",
            "INSERT INTO connection_config VALUES (1)",
            "CREATE TABLE watchlist (symbol VARCHAR(16) PRIMARY KEY)",
            "INSERT INTO watchlist VALUES ('SPY')",
        ]
        for statement in statements:
            connection.execute(text(statement))
    legacy.dispose()

    alembic(db_path, "head")
    upgraded = create_engine(f"sqlite:///{db_path}")
    with upgraded.connect() as connection:
        assert connection.scalar(text("SELECT role FROM auth_users WHERE id=1")) == "admin"
        assert connection.scalar(text("SELECT owner_user_id FROM strategies WHERE id='private'")) == 1
        assert connection.scalar(text("SELECT owner_user_id FROM strategies WHERE id='template'")) is None
        for table in ("backtest_runs", "signals", "orders", "event_logs", "risk_settings", "engine_state", "connection_config", "watchlist"):
            assert connection.scalar(text(f"SELECT user_id FROM {table} LIMIT 1")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004_multi_user"
    upgraded.dispose()
