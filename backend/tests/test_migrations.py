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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0003_auth"
    upgraded.dispose()
