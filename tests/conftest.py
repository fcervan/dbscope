from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from dbscope.config import EnvironmentConfig, Settings


@pytest.fixture
def env() -> EnvironmentConfig:
    return EnvironmentConfig(
        host="dev-db.example.internal",
        database="app_dev",
        user="ro_dev",
        password=SecretStr("senha-super-secreta"),
        sslmode="require",
        description="Development",
    )


@pytest.fixture
def settings(tmp_path: Path, env: EnvironmentConfig) -> Settings:
    return Settings(
        environments={"dev": env},
        default_environment="dev",
        audit_log=str(tmp_path / "audit.jsonl"),
        config_dir=tmp_path,
    )


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[environments.dev]
host = "dev-db.example.internal"
port = 3306
database = "app_dev"
user = "ro_dev"
password = "senha-dev"
sslmode = "require"

[environments.qa]
host = "qa-db.example.internal"
port = 3306
database = "app_qa"
user = "ro_qa"
password = "senha-qa"
sslmode = "require"

[environments.prod]
host = "prod-replica.example.internal"
port = 3306
database = "app_prod"
user = "ro_prod"
password = "senha-prod"
sslmode = "require"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path
