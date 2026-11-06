"""Sanitized error types used by every public dbscope surface."""

from __future__ import annotations

from typing import Any


class DbScopeError(Exception):
    """Base error whose message is safe to return to the LLM.

    Raw driver exceptions must never be attached to this error. Callers are
    responsible for redacting secrets before constructing a safe message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.safe_message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigError(DbScopeError):
    def __init__(self, message: str) -> None:
        super().__init__("config_error", message)


class InvalidEnvironmentError(DbScopeError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_environment", message)


class InvalidArgumentError(DbScopeError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_argument", message)


class SqlDeniedError(DbScopeError):
    def __init__(self, message: str) -> None:
        super().__init__("denied_sql", message)


class DatabaseError(DbScopeError):
    def __init__(self, message: str, *, errno: int | None = None) -> None:
        details = {"errno": errno} if errno is not None else None
        super().__init__("db_error", message, details=details)


class QueryTimeoutError(DbScopeError):
    def __init__(self, message: str = "A consulta excedeu o tempo limite.") -> None:
        super().__init__("query_timeout", message)


class TableNotFoundError(DbScopeError):
    def __init__(self, message: str = "Tabela ou view não encontrada.") -> None:
        super().__init__("table_not_found", message)


class AuditError(DbScopeError):
    def __init__(self) -> None:
        super().__init__(
            "audit_error",
            "Não foi possível gravar a auditoria; a operação foi interrompida.",
        )
