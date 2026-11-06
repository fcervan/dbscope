"""MCP tools exposed over stdio."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from .errors import DbScopeError
from .service import DbScopeService

INSTRUCTIONS = (
    "dbscope provides read-only SQL access to configured MySQL/MariaDB "
    "environments. Credentials are never exposed. The default environment comes "
    "from configuration (dev unless overridden); prod is never used implicitly. "
    "Only SELECT statements are accepted."
)


def _safe_call[T](operation: Callable[[], T]) -> T | dict[str, Any]:
    try:
        return operation()
    except DbScopeError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": "Erro interno ao executar a ferramenta.",
            },
        }


def create_server(service: DbScopeService) -> FastMCP:
    server = FastMCP("dbscope", instructions=INSTRUCTIONS, log_level="WARNING")

    @server.tool()
    def list_environments() -> dict[str, Any]:
        """List configured environment names and non-sensitive metadata."""
        return _safe_call(service.list_environments)

    @server.tool()
    def list_schemas(environment: str | None = None) -> dict[str, Any]:
        """List schemas available in an environment. Defaults to the configured one."""
        return _safe_call(lambda: service.list_schemas(environment))

    @server.tool()
    def list_tables(*, environment: str | None = None, schema: str) -> dict[str, Any]:
        """List tables and views in a schema. Defaults to the configured environment."""
        return _safe_call(lambda: service.list_tables(environment, schema))

    @server.tool()
    def describe_table(
        *,
        environment: str | None = None,
        schema: str,
        table: str,
    ) -> dict[str, Any]:
        """Describe columns and indexes of a table or view."""
        return _safe_call(lambda: service.describe_table(environment, schema, table))

    @server.tool()
    def query(
        *,
        environment: str | None = None,
        sql: str,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """Run one read-only SELECT. Defaults to config; prod is never implicit."""
        return _safe_call(lambda: service.query(environment, sql, max_rows))

    @server.tool()
    def explain(*, environment: str | None = None, sql: str) -> dict[str, Any]:
        """Return EXPLAIN output for a read-only SELECT."""
        return _safe_call(lambda: service.explain(environment, sql))

    return server
