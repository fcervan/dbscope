from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymysql
import pytest
from pydantic import SecretStr

from dbscope.audit import AuditLogger
from dbscope.config import EnvironmentConfig, Settings
from dbscope.db import QueryResult
from dbscope.errors import (
    DatabaseError,
    InvalidArgumentError,
    InvalidEnvironmentError,
    SqlDeniedError,
    TableNotFoundError,
)
from dbscope.service import DbScopeService


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.schemas = ["app_dev", "other"]
        self.tables = [{"name": "users", "type": "BASE TABLE"}]
        self.description: dict[str, Any] | None = {
            "name": "users",
            "type": "BASE TABLE",
            "comment": "",
            "columns": [
                {
                    "name": "id",
                    "type": "int",
                    "nullable": False,
                    "default": None,
                    "key": "PRI",
                    "extra": "",
                    "comment": "",
                }
            ],
            "indexes": [{"name": "PRIMARY", "unique": True, "columns": ["id"]}],
        }
        self.query_result = QueryResult(
            columns=["name", "cpf"],
            rows=[["alice", "12345678900"], ["bob", "98765432100"]],
            truncated=False,
            truncated_cells=0,
            duration_ms=4,
            row_count=2,
        )
        self.error: BaseException | None = None

    def list_schemas(self, env: EnvironmentConfig) -> list[str]:
        self.calls.append({"method": "list_schemas", "env": env})
        return list(self.schemas)

    def list_tables(
        self,
        env: EnvironmentConfig,
        schema: str,
    ) -> list[dict[str, str]]:
        self.calls.append({"method": "list_tables", "env": env, "schema": schema})
        return list(self.tables)

    def describe_table(
        self,
        env: EnvironmentConfig,
        schema: str,
        table: str,
    ) -> dict[str, Any] | None:
        self.calls.append(
            {"method": "describe_table", "env": env, "schema": schema, "table": table}
        )
        return self.description

    def execute_select(
        self,
        env: EnvironmentConfig,
        sql: str,
        *,
        max_rows: int,
        max_bytes: int,
        max_cell_chars: int,
    ) -> QueryResult:
        self.calls.append(
            {
                "method": "execute_select",
                "env": env,
                "sql": sql,
                "max_rows": max_rows,
                "max_bytes": max_bytes,
                "max_cell_chars": max_cell_chars,
            }
        )
        if self.error is not None:
            raise self.error
        return self.query_result


def make_service(
    settings: Settings,
    client: FakeClient | None = None,
    audit: FakeAudit | None = None,
) -> tuple[DbScopeService, FakeClient, FakeAudit]:
    fake_client = client or FakeClient()
    fake_audit = audit or FakeAudit()
    service = DbScopeService(settings, client=fake_client, audit=fake_audit)  # type: ignore[arg-type]
    return service, fake_client, fake_audit


def test_list_environments_never_exposes_connection_data(settings: Settings) -> None:
    service, _, _ = make_service(settings)

    result = service.list_environments()

    assert result["ok"] is True
    payload = json.dumps(result)
    assert "dev-db.example.internal" not in payload
    assert "ro_dev" not in payload
    assert "app_dev" not in payload
    assert "senha-super-secreta" not in payload
    assert result["environments"][0]["name"] == "dev"


def test_query_defaults_to_dev(settings: Settings) -> None:
    service, client, audit = make_service(settings)

    result = service.query(None, "SELECT 1")

    assert result["ok"] is True
    assert client.calls[0]["env"].database == "app_dev"
    assert audit.events[-1]["environment"] == "dev"
    assert audit.events[-1]["success"] is True


def test_query_caps_max_rows_to_environment(settings: Settings) -> None:
    service, client, _ = make_service(settings)

    result = service.query("dev", "SELECT 1", max_rows=999)

    assert result["ok"] is True
    assert client.calls[0]["max_rows"] == 200


@pytest.mark.parametrize("max_rows", [0, -1, True])
def test_query_rejects_invalid_max_rows(settings: Settings, max_rows: Any) -> None:
    service, _, audit = make_service(settings)

    with pytest.raises(InvalidArgumentError):
        service.query("dev", "SELECT 1", max_rows=max_rows)

    assert audit.events[-1]["error_code"] == "invalid_argument"


def test_query_masks_configured_columns(
    settings: Settings,
    env: EnvironmentConfig,
) -> None:
    masked_env = env.model_copy(update={"mask_columns": ["cpf"]})
    masked_settings = settings.model_copy(update={"environments": {"dev": masked_env}})
    service, _, _ = make_service(masked_settings)

    result = service.query("dev", "SELECT name, cpf FROM users")

    assert result["rows"] == [
        ["alice", "***"],
        ["bob", "***"],
    ]


def test_query_denies_writes(settings: Settings) -> None:
    service, _, audit = make_service(settings)

    with pytest.raises(SqlDeniedError):
        service.query("dev", "DELETE FROM users")

    assert audit.events[-1]["error_code"] == "denied_sql"
    assert audit.events[-1]["success"] is False


def test_invalid_environment_is_audited(settings: Settings) -> None:
    service, _, audit = make_service(settings)

    with pytest.raises(InvalidEnvironmentError):
        service.query("staging", "SELECT 1")

    assert audit.events[-1]["error_code"] == "invalid_environment"


def test_driver_errors_are_sanitized(settings: Settings) -> None:
    client = FakeClient()
    client.error = pymysql.err.OperationalError(
        1045,
        "Access denied for user 'ro_dev'@'dev-db.example.internal'",
    )
    service, _, audit = make_service(settings, client=client)

    with pytest.raises(DatabaseError) as caught:
        service.query("dev", "SELECT 1")

    message = caught.value.safe_message
    assert "ro_dev" not in message
    assert "dev-db.example.internal" not in message
    assert audit.events[-1]["error_code"] == "db_error"


def test_query_audit_redacts_sql_literals(settings: Settings) -> None:
    service, _, audit = make_service(settings)

    service.query(
        "dev",
        "SELECT * FROM users WHERE cpf = '12345678900' AND age > 42",
    )

    event = audit.events[-1]
    assert event["sql_redacted"]
    assert "12345678900" not in event["sql_redacted"]
    assert "42" not in event["sql_redacted"]


def test_audit_file_never_contains_credentials(settings: Settings) -> None:
    logger = AuditLogger(settings.audit_log)
    client = FakeClient()
    client.error = pymysql.err.OperationalError(
        1045,
        "Access denied for user 'ro_dev'@'dev-db.example.internal' "
        "using password senha-super-secreta",
    )
    service = DbScopeService(settings, client=client, audit=logger)  # type: ignore[arg-type]

    with pytest.raises(DatabaseError):
        service.query("dev", "SELECT 1")

    content = Path(settings.audit_log).read_text(encoding="utf-8")
    assert "senha-super-secreta" not in content
    assert "dev-db.example.internal" not in content
    assert "ro_dev" not in content


def test_explain_prefixes_validated_select(settings: Settings) -> None:
    service, client, _ = make_service(settings)

    result = service.explain("dev", "SELECT 1")

    assert result["ok"] is True
    assert client.calls[0]["sql"] == "EXPLAIN SELECT 1"
    assert client.calls[0]["max_rows"] == 200


def test_list_schemas_filters_allowlist(
    settings: Settings,
    env: EnvironmentConfig,
) -> None:
    restricted_env = env.model_copy(update={"allowed_schemas": ["app_dev"]})
    restricted_settings = settings.model_copy(update={"environments": {"dev": restricted_env}})
    service, _, _ = make_service(restricted_settings)

    result = service.list_schemas("dev")

    assert result["schemas"] == ["app_dev"]


def test_list_tables_rejects_schema_outside_allowlist(
    settings: Settings,
    env: EnvironmentConfig,
) -> None:
    restricted_env = env.model_copy(update={"allowed_schemas": ["app_dev"]})
    restricted_settings = settings.model_copy(update={"environments": {"dev": restricted_env}})
    service, _, audit = make_service(restricted_settings)

    with pytest.raises(InvalidArgumentError):
        service.list_tables("dev", "other")

    assert audit.events[-1]["success"] is False


def test_describe_table_returns_metadata(settings: Settings) -> None:
    service, _, _ = make_service(settings)

    result = service.describe_table("dev", "app_dev", "users")

    assert result["ok"] is True
    assert result["name"] == "users"
    assert result["schema"] == "app_dev"
    assert result["columns"][0]["name"] == "id"


def test_describe_table_not_found(settings: Settings) -> None:
    client = FakeClient()
    client.description = None
    service, _, audit = make_service(settings, client=client)

    with pytest.raises(TableNotFoundError):
        service.describe_table("dev", "app_dev", "missing")

    assert audit.events[-1]["error_code"] == "table_not_found"


def test_prod_uses_stricter_limits(
    settings: Settings,
    env: EnvironmentConfig,
) -> None:
    prod_env = EnvironmentConfig(
        host="prod-replica.example.internal",
        database="app_prod",
        user="ro_prod",
        password=SecretStr("senha-prod"),
        sslmode="require",
        is_prod=True,
        max_rows=100,
        max_bytes=262_144,
        query_timeout_ms=3_000,
    )
    prod_settings = settings.model_copy(
        update={"environments": {**settings.environments, "prod": prod_env}}
    )
    service, client, _ = make_service(prod_settings)

    service.query("prod", "SELECT 1", max_rows=999)

    assert client.calls[0]["env"].is_prod is True
    assert client.calls[0]["max_rows"] == 100
    assert client.calls[0]["max_bytes"] == 262_144

    service.query(None, "SELECT 1")

    assert client.calls[1]["env"].database == "app_dev"
