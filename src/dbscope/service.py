"""Tool logic, independent from the MCP transport and easy to unit test."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from .audit import AuditLogger
from .config import EnvironmentConfig, Settings
from .db import MySqlClient
from .errors import (
    DbScopeError,
    InvalidArgumentError,
    TableNotFoundError,
)
from .safety import (
    redact_sql,
    sanitize_driver_error,
    validate_identifier,
    validate_schema_access,
    validate_select_sql,
)

T = TypeVar("T")


class DbScopeService:
    def __init__(
        self,
        settings: Settings,
        *,
        client: MySqlClient | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or MySqlClient()
        self.audit = audit or AuditLogger(settings.audit_log)

    def _resolve_for_tool(
        self,
        tool: str,
        environment: str | None,
    ) -> tuple[str, EnvironmentConfig]:
        started = time.perf_counter()
        try:
            return self.settings.get_environment(environment)
        except DbScopeError as exc:
            self._write_audit(
                tool=tool,
                environment=environment,
                sql=None,
                started=started,
                success=False,
                error_code=exc.code,
            )
            raise

    def _write_audit(
        self,
        *,
        tool: str,
        environment: str | None,
        sql: str | None,
        started: float,
        success: bool,
        row_count: int | None = None,
        truncated: bool | None = None,
        error_code: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "tool": tool,
            "environment": environment,
            "success": success,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        if sql is not None:
            event["sql_redacted"] = redact_sql(sql, self.settings.sensitive_values)
        if row_count is not None:
            event["row_count"] = row_count
        if truncated is not None:
            event["truncated"] = truncated
        if error_code is not None:
            event["error_code"] = error_code
        self.audit.log(event)

    def _run_audited(
        self,
        *,
        tool: str,
        environment: str | None,
        sql: str | None,
        operation: Callable[[], T],
    ) -> T:
        started = time.perf_counter()
        try:
            result = operation()
        except DbScopeError as exc:
            self._write_audit(
                tool=tool,
                environment=environment,
                sql=sql,
                started=started,
                success=False,
                error_code=exc.code,
            )
            raise
        except Exception as exc:
            safe_error = sanitize_driver_error(exc, self.settings.sensitive_values)
            self._write_audit(
                tool=tool,
                environment=environment,
                sql=sql,
                started=started,
                success=False,
                error_code=safe_error.code,
            )
            raise safe_error from None

        row_count: int | None = None
        truncated: bool | None = None
        if isinstance(result, dict):
            raw_row_count = result.get("row_count")
            if isinstance(raw_row_count, int):
                row_count = raw_row_count
            raw_truncated = result.get("truncated")
            if isinstance(raw_truncated, bool):
                truncated = raw_truncated

        self._write_audit(
            tool=tool,
            environment=environment,
            sql=sql,
            started=started,
            success=True,
            row_count=row_count,
            truncated=truncated,
        )
        return result

    def list_environments(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            environments = [
                {
                    "name": name,
                    "description": env.description,
                    "dialect": "mysql",
                    "is_prod": env.is_prod,
                    "read_only": True,
                    "max_rows": env.max_rows,
                    "max_bytes": env.max_bytes,
                }
                for name, env in sorted(self.settings.environments.items())
            ]
            return {"ok": True, "environments": environments}

        return self._run_audited(
            tool="list_environments",
            environment=None,
            sql=None,
            operation=operation,
        )

    def list_schemas(self, environment: str | None = None) -> dict[str, Any]:
        name, env = self._resolve_for_tool("list_schemas", environment)

        def operation() -> dict[str, Any]:
            schemas = self.client.list_schemas(env)
            if env.allowed_schemas:
                allowed = set(env.allowed_schemas)
                schemas = [schema for schema in schemas if schema in allowed]
            return {"ok": True, "environment": name, "schemas": schemas}

        return self._run_audited(
            tool="list_schemas",
            environment=name,
            sql=None,
            operation=operation,
        )

    def list_tables(
        self,
        environment: str | None,
        schema: str,
    ) -> dict[str, Any]:
        name, env = self._resolve_for_tool("list_tables", environment)

        def operation() -> dict[str, Any]:
            safe_schema = validate_schema_access(schema, env)
            tables = self.client.list_tables(env, safe_schema)
            return {
                "ok": True,
                "environment": name,
                "schema": safe_schema,
                "tables": tables,
            }

        return self._run_audited(
            tool="list_tables",
            environment=name,
            sql=None,
            operation=operation,
        )

    def describe_table(
        self,
        environment: str | None,
        schema: str,
        table: str,
    ) -> dict[str, Any]:
        name, env = self._resolve_for_tool("describe_table", environment)

        def operation() -> dict[str, Any]:
            safe_schema = validate_schema_access(schema, env)
            safe_table = validate_identifier(table, "Tabela")
            description = self.client.describe_table(env, safe_schema, safe_table)
            if description is None:
                raise TableNotFoundError()
            return {
                "ok": True,
                "environment": name,
                "schema": safe_schema,
                **description,
            }

        return self._run_audited(
            tool="describe_table",
            environment=name,
            sql=None,
            operation=operation,
        )

    def query(
        self,
        environment: str | None,
        sql: str,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        name, env = self._resolve_for_tool("query", environment)

        def operation() -> dict[str, Any]:
            clean_sql = validate_select_sql(sql, env)
            effective_rows = _effective_max_rows(max_rows, env)
            result = self.client.execute_select(
                env,
                clean_sql,
                max_rows=effective_rows,
                max_bytes=env.max_bytes,
                max_cell_chars=env.max_cell_chars,
            )
            rows = _mask_rows(result.columns, result.rows, env)
            return {
                "ok": True,
                "environment": name,
                "columns": result.columns,
                "rows": rows,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "truncated_cells": result.truncated_cells,
                "duration_ms": result.duration_ms,
            }

        return self._run_audited(
            tool="query",
            environment=name,
            sql=sql,
            operation=operation,
        )

    def explain(
        self,
        environment: str | None,
        sql: str,
    ) -> dict[str, Any]:
        name, env = self._resolve_for_tool("explain", environment)

        def operation() -> dict[str, Any]:
            clean_sql = validate_select_sql(sql, env)
            result = self.client.execute_select(
                env,
                f"EXPLAIN {clean_sql}",
                max_rows=env.max_rows,
                max_bytes=env.max_bytes,
                max_cell_chars=env.max_cell_chars,
            )
            return {
                "ok": True,
                "environment": name,
                "columns": result.columns,
                "rows": result.rows,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "truncated_cells": result.truncated_cells,
                "duration_ms": result.duration_ms,
            }

        return self._run_audited(
            tool="explain",
            environment=name,
            sql=sql,
            operation=operation,
        )


def _effective_max_rows(requested: int | None, env: EnvironmentConfig) -> int:
    if requested is None:
        return env.max_rows
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise InvalidArgumentError("max_rows deve ser um inteiro positivo.")
    if requested < 1:
        raise InvalidArgumentError("max_rows deve ser um inteiro positivo.")
    return min(requested, env.max_rows)


def _mask_rows(
    columns: list[str],
    rows: list[list[Any]],
    env: EnvironmentConfig,
) -> list[list[Any]]:
    if not env.mask_columns:
        return rows
    masked_names = {name.casefold() for name in env.mask_columns}
    indexes = {index for index, column in enumerate(columns) if column.casefold() in masked_names}
    if not indexes:
        return rows
    return [
        ["***" if index in indexes else value for index, value in enumerate(row)] for row in rows
    ]
