from __future__ import annotations

from pathlib import Path

import pytest

from dbscope.config import load_settings
from dbscope.errors import ConfigError, InvalidEnvironmentError


def _write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_text(content.strip() + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_loads_environments_and_defaults(config_file: Path) -> None:
    settings = load_settings(config_file, environ={})

    assert settings.environment_names == ["dev", "prod", "qa"]
    assert settings.default_environment == "dev"
    assert settings.environments["prod"].is_prod is True
    assert settings.environments["prod"].max_rows == 100
    assert settings.environments["prod"].max_bytes == 262_144
    assert settings.environments["prod"].query_timeout_ms == 3_000
    assert settings.environments["dev"].max_rows == 200


def test_secret_is_not_in_repr(config_file: Path) -> None:
    settings = load_settings(config_file, environ={})

    assert "senha-dev" not in repr(settings)
    assert "senha-dev" not in str(settings)


def test_default_environment_is_dev(config_file: Path) -> None:
    settings = load_settings(config_file, environ={})
    name, env = settings.get_environment(None)

    assert name == "dev"
    assert env.database == "app_dev"


def test_invalid_environment_is_rejected(config_file: Path) -> None:
    settings = load_settings(config_file, environ={})

    with pytest.raises(InvalidEnvironmentError):
        settings.get_environment("staging")


def test_config_file_must_be_private(config_file: Path) -> None:
    config_file.chmod(0o644)

    with pytest.raises(ConfigError, match="600"):
        load_settings(config_file, environ={})


def test_password_file_is_resolved_relative_to_config(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secret = secrets / "dev.pass"
    _write(secret, "senha-do-arquivo")
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password_file = "secrets/dev.pass"
sslmode = "require"
""",
    )

    settings = load_settings(config, environ={})

    assert settings.environments["dev"].secret() == "senha-do-arquivo"


def test_password_file_must_be_private(tmp_path: Path) -> None:
    secret = tmp_path / "dev.pass"
    _write(secret, "senha", mode=0o644)
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password_file = "dev.pass"
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="600"):
        load_settings(config, environ={})


def test_exactly_one_password_source_is_required(tmp_path: Path) -> None:
    secret = tmp_path / "dev.pass"
    _write(secret, "senha")
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "inline"
password_file = "dev.pass"
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="exatamente uma fonte"):
        load_settings(config, environ={})


def test_prod_limits_cannot_be_increased(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "require"

[environments.prod]
host = "prod-replica.example.internal"
database = "app_prod"
user = "ro_prod"
password = "senha"
sslmode = "require"
max_rows = 500
""",
    )

    with pytest.raises(ConfigError, match="100"):
        load_settings(config, environ={})


def test_prod_cannot_disable_tls(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "require"

[environments.prod]
host = "prod-replica.example.internal"
database = "app_prod"
user = "ro_prod"
password = "senha"
sslmode = "disable"
""",
    )

    with pytest.raises(ConfigError, match="TLS"):
        load_settings(config, environ={})


def test_verify_identity_requires_ca(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "verify-identity"
""",
    )

    with pytest.raises(ConfigError, match="verify-identity"):
        load_settings(config, environ={})


def test_default_environment_cannot_be_prod(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
default_environment = "prod"

[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "require"

[environments.prod]
host = "prod-replica.example.internal"
database = "app_prod"
user = "ro_prod"
password = "senha"
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="produção"):
        load_settings(config, environ={})


def test_dev_is_required_by_default(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.qa]
host = "qa-db.example.internal"
database = "app_qa"
user = "ro_qa"
password = "senha"
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="padrão"):
        load_settings(config, environ={})


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
unexpected = true

[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="não reconhecidas"):
        load_settings(config, environ={})


def test_audit_log_env_override(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
audit_log = "logs/audit.jsonl"

[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "require"
""",
    )
    override = tmp_path / "custom" / "audit.jsonl"

    settings = load_settings(config, environ={"DBSCOPE_AUDIT_LOG": str(override)})

    assert settings.audit_log == str(override)


def test_prod_name_is_case_insensitive(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = "senha"
sslmode = "require"

[environments.PROD]
host = "prod-replica.example.internal"
database = "app_prod"
user = "ro_prod"
password = "senha"
sslmode = "require"
""",
    )

    settings = load_settings(config, environ={})

    assert settings.environments["PROD"].is_prod is True
    assert settings.environments["PROD"].max_rows == 100


def test_empty_inline_password_is_rejected(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "app_dev"
user = "ro_dev"
password = ""
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="vazia"):
        load_settings(config, environ={})


def test_system_database_is_rejected(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.toml",
        """
[environments.dev]
host = "dev-db.example.internal"
database = "information_schema"
user = "ro_dev"
password = "senha"
sslmode = "require"
""",
    )

    with pytest.raises(ConfigError, match="sistema"):
        load_settings(config, environ={})
