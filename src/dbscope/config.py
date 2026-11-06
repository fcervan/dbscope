"""Configuration loading and secret handling.

Secrets are loaded from a TOML file outside the repository. Relative paths in
the TOML are resolved against the directory that contains the config file, so
the same file works locally and inside a container mounted at /config.
"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .errors import ConfigError, InvalidEnvironmentError

TLSMode = Literal["disable", "require", "verify-ca", "verify-identity"]

SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
PROD_MAX_ROWS = 100
PROD_MAX_BYTES = 262_144
PROD_QUERY_TIMEOUT_MS = 3_000


class EnvironmentConfig(BaseModel):
    """One database environment. Never serialize this object to the LLM."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=64)
    user: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    password_file: str | None = None
    use_keyring: bool = False
    sslmode: TLSMode = "verify-ca"
    ssl_ca: str | None = None
    ssl_cert: str | None = None
    ssl_key: str | None = None
    connect_timeout_s: int = Field(default=5, ge=1, le=60)
    query_timeout_ms: int = Field(default=10_000, ge=100, le=300_000)
    max_rows: int = Field(default=200, ge=1, le=10_000)
    max_bytes: int = Field(default=1_048_576, ge=1024, le=50_000_000)
    max_cell_chars: int = Field(default=4_096, ge=16, le=1_000_000)
    allowed_schemas: list[str] = Field(default_factory=list)
    mask_columns: list[str] = Field(default_factory=list)
    description: str = ""
    is_prod: bool = False

    def secret(self) -> str:
        if self.password is None:
            return ""
        return self.password.get_secret_value()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environments: dict[str, EnvironmentConfig]
    default_environment: str = "dev"
    audit_log: str
    config_dir: Path
    keyring_service: str = "dbscope"

    @property
    def environment_names(self) -> list[str]:
        return sorted(self.environments)

    @property
    def sensitive_values(self) -> list[str]:
        values: set[str] = set()
        for env in self.environments.values():
            values.add(env.host)
            values.add(env.user)
            values.add(env.database)
            password = env.secret()
            if password:
                values.add(password)
        return sorted((value for value in values if value), key=len, reverse=True)

    def get_environment(self, name: str | None) -> tuple[str, EnvironmentConfig]:
        selected = name or self.default_environment
        if selected not in self.environments:
            available = ", ".join(self.environment_names)
            raise InvalidEnvironmentError(f"Ambiente inválido. Use um destes: {available}.")
        return selected, self.environments[selected]


def _check_mode(path: Path, *, kind: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise ConfigError(f"Não foi possível verificar o arquivo de {kind}.") from None
    if mode & 0o077:
        raise ConfigError(f"O arquivo de {kind} deve ter permissão 600.")


def _resolve_path(base: Path, value: str | None) -> str | None:
    if value is None:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return str(candidate.resolve())


def _resolve_runtime_paths(raw: dict[str, Any], base: Path) -> dict[str, Any]:
    resolved = dict(raw)
    for field in ("password_file", "ssl_ca", "ssl_cert", "ssl_key"):
        value = resolved.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ConfigError(f"O campo {field} deve ser um caminho textual.")
            resolved[field] = _resolve_path(base, value)
    return resolved


def _build_environment(
    name: str,
    raw: Any,
    *,
    base: Path,
    settings_keyring_service: str,
) -> EnvironmentConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"Ambiente '{name}' inválido.")

    data = dict(raw)
    if name.casefold() == "prod":
        data["is_prod"] = True
        data.setdefault("max_rows", PROD_MAX_ROWS)
        data.setdefault("max_bytes", PROD_MAX_BYTES)
        data.setdefault("query_timeout_ms", PROD_QUERY_TIMEOUT_MS)

    data = _resolve_runtime_paths(data, base)

    try:
        env = EnvironmentConfig.model_validate(data)
    except ValidationError as exc:
        fields = sorted({str(detail["loc"][0]) for detail in exc.errors() if detail.get("loc")})
        field_text = ", ".join(fields) if fields else "desconhecidos"
        raise ConfigError(
            f"Configuração inválida para o ambiente '{name}' (campos: {field_text})."
        ) from None

    sources = sum(
        [
            env.password is not None,
            env.password_file is not None,
            env.use_keyring,
        ]
    )
    if sources != 1:
        raise ConfigError(
            f"O ambiente '{name}' deve definir exatamente uma fonte de senha: "
            "password, password_file ou use_keyring."
        )
    if env.password is not None and not env.password.get_secret_value():
        raise ConfigError(f"A senha do ambiente '{name}' não pode ser vazia.")
    if env.database.casefold() in SYSTEM_SCHEMAS:
        raise ConfigError(f"O ambiente '{name}' não pode usar um schema de sistema.")

    if env.is_prod:
        if env.sslmode == "disable":
            raise ConfigError("O ambiente de produção não pode desabilitar TLS.")
        if env.max_rows > PROD_MAX_ROWS:
            raise ConfigError(f"Produção permite no máximo {PROD_MAX_ROWS} linhas.")
        if env.max_bytes > PROD_MAX_BYTES:
            raise ConfigError(f"Produção permite no máximo {PROD_MAX_BYTES} bytes.")
        if env.query_timeout_ms > PROD_QUERY_TIMEOUT_MS:
            raise ConfigError(f"Produção permite timeout de no máximo {PROD_QUERY_TIMEOUT_MS} ms.")

    if env.sslmode == "verify-identity" and not env.ssl_ca:
        raise ConfigError("O modo verify-identity exige ssl_ca.")
    if env.ssl_cert and not env.ssl_key:
        raise ConfigError("ssl_cert exige ssl_key.")
    if env.ssl_key and not env.ssl_cert:
        raise ConfigError("ssl_key exige ssl_cert.")
    for field_name, field_value in (
        ("ssl_ca", env.ssl_ca),
        ("ssl_cert", env.ssl_cert),
    ):
        if field_value and not Path(field_value).is_file():
            raise ConfigError(f"O arquivo {field_name} do ambiente '{name}' não existe.")

    if env.password_file:
        password_path = Path(env.password_file)
        _check_mode(password_path, kind="senha")
        try:
            value = password_path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise ConfigError(f"Não foi possível ler a senha do ambiente '{name}'.") from None
        if not value:
            raise ConfigError(f"O arquivo de senha do ambiente '{name}' está vazio.")
        env = env.model_copy(update={"password": SecretStr(value), "password_file": None})

    if env.use_keyring:
        try:
            import keyring
        except ImportError:
            raise ConfigError(
                "use_keyring foi definido, mas o pacote keyring não está instalado."
            ) from None
        try:
            value = keyring.get_password(settings_keyring_service, name)
        except Exception:
            raise ConfigError(f"Não foi possível obter a senha do keyring para '{name}'.") from None
        if not value:
            raise ConfigError(f"Nenhuma senha encontrada no keyring para '{name}'.")
        env = env.model_copy(update={"password": SecretStr(value), "use_keyring": False})

    if env.ssl_key:
        _check_mode(Path(env.ssl_key), kind="chave TLS")

    return env


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError("Arquivo de configuração não encontrado.")
    if not path.is_file():
        raise ConfigError("O caminho de configuração não é um arquivo.")
    _check_mode(path, kind="configuração")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("Não foi possível ler o arquivo de configuração.") from None
    if not isinstance(data, dict):
        raise ConfigError("O arquivo de configuração deve ser uma tabela TOML.")
    return data


def load_settings(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env_vars = dict(os.environ if environ is None else environ)
    raw_path = (
        str(config_path)
        if config_path is not None
        else env_vars.get("DBSCOPE_CONFIG", "~/.config/dbscope/config.toml")
    )
    path = Path(raw_path).expanduser().resolve()
    base = path.parent

    data = _load_toml(path)
    allowed_keys = {"environments", "default_environment", "audit_log", "keyring_service"}
    unknown = sorted(set(data) - allowed_keys)
    if unknown:
        raise ConfigError("A configuração contém chaves não reconhecidas.")

    raw_environments = data.get("environments")
    if not isinstance(raw_environments, dict) or not raw_environments:
        raise ConfigError("A configuração deve definir pelo menos um ambiente.")

    keyring_service = data.get("keyring_service", "dbscope")
    if not isinstance(keyring_service, str) or not keyring_service:
        raise ConfigError("keyring_service deve ser um texto não vazio.")

    environments: dict[str, EnvironmentConfig] = {}
    for name, raw_env in raw_environments.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("Nome de ambiente inválido.")
        environments[name] = _build_environment(
            name,
            raw_env,
            base=base,
            settings_keyring_service=keyring_service,
        )

    default_environment = data.get("default_environment", "dev")
    if not isinstance(default_environment, str):
        raise ConfigError("default_environment deve ser um texto.")
    if default_environment not in environments:
        raise ConfigError("O ambiente padrão não está configurado.")
    if environments[default_environment].is_prod:
        raise ConfigError("O ambiente padrão nunca pode ser produção.")

    raw_audit = env_vars.get("DBSCOPE_AUDIT_LOG") or data.get(
        "audit_log", "~/.local/state/dbscope/audit.jsonl"
    )
    if not isinstance(raw_audit, str) or not raw_audit:
        raise ConfigError("audit_log deve ser um caminho textual.")
    audit_log = _resolve_path(base, raw_audit)
    assert audit_log is not None

    return Settings(
        environments=environments,
        default_environment=default_environment,
        audit_log=audit_log,
        config_dir=base,
        keyring_service=keyring_service,
    )
