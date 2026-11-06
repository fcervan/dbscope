from __future__ import annotations

import asyncio
from typing import Any

from dbscope.errors import SqlDeniedError
from dbscope.server import _safe_call, create_server


class FakeService:
    def list_environments(self) -> dict[str, Any]:
        return {"ok": True, "environments": []}

    def list_schemas(self, environment: str | None = None) -> dict[str, Any]:
        return {"ok": True, "environment": environment or "dev", "schemas": []}

    def list_tables(self, environment: str | None, schema: str) -> dict[str, Any]:
        return {"ok": True, "environment": environment or "dev", "schema": schema}

    def describe_table(
        self,
        environment: str | None,
        schema: str,
        table: str,
    ) -> dict[str, Any]:
        return {"ok": True, "environment": environment or "dev", "table": table}

    def query(
        self,
        environment: str | None,
        sql: str,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        return {"ok": True, "environment": environment or "dev", "sql": sql}

    def explain(self, environment: str | None, sql: str) -> dict[str, Any]:
        return {"ok": True, "environment": environment or "dev", "sql": sql}


def test_registers_expected_tools() -> None:
    server = create_server(FakeService())  # type: ignore[arg-type]

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "list_environments",
        "list_schemas",
        "list_tables",
        "describe_table",
        "query",
        "explain",
    }


def test_safe_call_returns_structured_error() -> None:
    def operation() -> dict[str, Any]:
        raise SqlDeniedError("Consulta não permitida.")

    result = _safe_call(operation)

    assert result == {
        "ok": False,
        "error": {"code": "denied_sql", "message": "Consulta não permitida."},
    }


def test_safe_call_hides_unexpected_exceptions() -> None:
    def operation() -> dict[str, Any]:
        raise RuntimeError("secret internal detail")

    result = _safe_call(operation)

    assert result == {
        "ok": False,
        "error": {
            "code": "internal_error",
            "message": "Erro interno ao executar a ferramenta.",
        },
    }
    assert "secret internal detail" not in str(result)


def test_safe_call_returns_result() -> None:
    result = _safe_call(lambda: {"ok": True, "rows": []})

    assert result == {"ok": True, "rows": []}
