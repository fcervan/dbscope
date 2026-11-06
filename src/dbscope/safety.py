"""SQL validation, result redaction and sanitized driver errors."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .config import SYSTEM_SCHEMAS, EnvironmentConfig
from .errors import (
    DatabaseError,
    DbScopeError,
    InvalidArgumentError,
    QueryTimeoutError,
    SqlDeniedError,
    TableNotFoundError,
)

MAX_SQL_CHARS = 20_000
_ALLOWED_QUERY_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_DENIED_FUNCTIONS = {
    "BENCHMARK",
    "GET_LOCK",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "LOAD_FILE",
    "MASTER_POS_WAIT",
    "RELEASE_LOCK",
    "SLEEP",
}
_EXECUTABLE_COMMENT = re.compile(r"/\*!")
_FORBIDDEN_CLAUSE = re.compile(
    r"\b(?:for\s+update|for\s+share|lock\s+in\s+share\s+mode|procedure\s+analyse)\b",
    re.IGNORECASE,
)
_INTO_CLAUSE = re.compile(r"\binto\s+(?:outfile|dumpfile|@)", re.IGNORECASE)
_TIME_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")

_PASSWORD_PATTERN = re.compile(r"(?i)\b(password|passwd|pwd)\s*=\s*[^;\s]+")
_DSN_PATTERN = re.compile(r"(?i)\b(?:mysql|mariadb|mysql\+pymysql)://[^\s]+")
_ACCESS_DENIED_PATTERN = re.compile(r"(?i)(access denied for user\s+)'[^']*'@'[^']*'")
_HOST_PATTERN = re.compile(
    r"(?i)((?:unknown mysql server host|can't connect to mysql server on)\s*)'[^']*'"
)
_LITERAL_PATTERN = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def parse_single_statement(sql: str) -> exp.Expression:
    """Parse one statement, ignoring quoted literals in dangerous-clause checks."""
    if not isinstance(sql, str) or not sql.strip():
        raise SqlDeniedError("SQL vazio.")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlDeniedError("SQL muito longo.")
    if "\x00" in sql:
        raise SqlDeniedError("SQL contém caractere inválido.")
    if _EXECUTABLE_COMMENT.search(sql):
        raise SqlDeniedError("Comentários executáveis não são permitidos.")

    without_literals = _LITERAL_PATTERN.sub("''", sql)
    if _INTO_CLAUSE.search(without_literals):
        raise SqlDeniedError("SELECT ... INTO OUTFILE/DUMPFILE/variável não é permitido.")
    if _FORBIDDEN_CLAUSE.search(without_literals):
        raise SqlDeniedError("Cláusulas de lock/procedure não são permitidas.")

    try:
        statements = sqlglot.parse(sql, read="mysql")
    except ParseError:
        raise SqlDeniedError("SQL inválido ou não suportado.") from None

    if len(statements) != 1:
        raise SqlDeniedError("Apenas um statement por chamada é permitido.")
    statement = statements[0]
    if statement is None:
        raise SqlDeniedError("SQL vazio ou composto apenas por comentários.")
    return statement


def _cte_names(statement: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}


def _check_table_access(
    statement: exp.Expression,
    env: EnvironmentConfig,
) -> None:
    allowed = {schema for schema in env.allowed_schemas}
    ctes = _cte_names(statement)

    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if not table.db and name in ctes:
            continue

        database = table.db or env.database
        if database.lower() in SYSTEM_SCHEMAS:
            raise SqlDeniedError("Schemas de sistema não são acessíveis por esta ferramenta.")
        if allowed and database not in allowed:
            raise SqlDeniedError("Schema não permitido pela configuração.")


def _check_expression(statement: exp.Expression) -> None:
    if not isinstance(statement, _ALLOWED_QUERY_TYPES):
        raise SqlDeniedError("Apenas consultas SELECT são permitidas.")

    for node in statement.walk():
        if isinstance(node, exp.Into):
            raise SqlDeniedError("SELECT ... INTO não é permitido.")
        if isinstance(node, exp.Lock):
            raise SqlDeniedError("Cláusulas de lock não são permitidas.")
        if isinstance(node, exp.Anonymous):
            if node.name.upper() in _DENIED_FUNCTIONS:
                raise SqlDeniedError("Função não permitida em consultas.")
        elif (
            isinstance(node, exp.Func) and node.sql_name().upper() in _DENIED_FUNCTIONS  # type: ignore[no-untyped-call]
        ):
            raise SqlDeniedError("Função não permitida em consultas.")


def validate_select_sql(
    sql: str,
    env: EnvironmentConfig,
) -> str:
    """Validate one read-only SELECT and return it without the trailing semicolon."""

    statement = parse_single_statement(sql)
    _check_expression(statement)
    _check_table_access(statement, env)

    clean = sql.strip()
    if clean.endswith(";"):
        clean = clean[:-1].rstrip()
    return clean


def validate_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise InvalidArgumentError(f"{kind} inválido.")
    if len(name) > 64:
        raise InvalidArgumentError(f"{kind} inválido.")
    if any(ord(char) < 32 for char in name):
        raise InvalidArgumentError(f"{kind} inválido.")
    if kind.casefold() == "schema" and name.casefold() in SYSTEM_SCHEMAS:
        raise InvalidArgumentError("O schema informado não é acessível.")
    return name


def validate_schema_access(schema: str, env: EnvironmentConfig) -> str:
    validate_identifier(schema, "Schema")
    if env.allowed_schemas and schema not in set(env.allowed_schemas):
        raise InvalidArgumentError("Schema não permitido pela configuração.")
    return schema


def redact_text(text: str, sensitive_values: Iterable[str]) -> str:
    if not text:
        return text

    redacted = text
    for value in sorted({value for value in sensitive_values if value}, key=len, reverse=True):
        redacted = redacted.replace(value, "***")

    redacted = _ACCESS_DENIED_PATTERN.sub(r"\1***", redacted)
    redacted = _HOST_PATTERN.sub(r"\1'***'", redacted)
    redacted = _DSN_PATTERN.sub("***", redacted)
    redacted = _PASSWORD_PATTERN.sub(r"\1=***", redacted)
    redacted = _LITERAL_PATTERN.sub("'?'", redacted)
    return redacted


def redact_sql(sql: str, sensitive_values: Iterable[str]) -> str:
    redacted = redact_text(sql, sensitive_values)
    redacted = _TIME_PATTERN.sub("?", redacted)
    redacted = " ".join(redacted.split())
    if len(redacted) > 2_000:
        redacted = redacted[:2_000] + "..."
    return redacted


def _error_parts(exc: BaseException) -> tuple[int | None, str]:
    args: tuple[Any, ...] = getattr(exc, "args", ())
    errno = args[0] if args and isinstance(args[0], int) else None
    message = ""
    if len(args) > 1 and isinstance(args[1], str):
        message = args[1]
    elif args and isinstance(args[0], str):
        message = args[0]
    return errno, message


def sanitize_driver_error(
    exc: BaseException,
    sensitive_values: Iterable[str],
) -> DbScopeError:
    errno, raw_message = _error_parts(exc)
    message = redact_text(raw_message, sensitive_values)
    message = " ".join(message.split())
    if len(message) > 300:
        message = message[:300] + "..."

    if errno in {1044, 1045, 1142, 1227, 1370}:
        return DatabaseError("Permissão negada pelo banco.", errno=errno)
    if errno in {2002, 2003, 2005}:
        return DatabaseError("Não foi possível conectar ao banco.", errno=errno)
    if errno in {2006, 2013, 2055}:
        return DatabaseError("A conexão com o banco foi perdida.", errno=errno)
    if errno == 1205:
        return DatabaseError("Timeout aguardando lock no banco.", errno=errno)
    if errno in {1969, 3024}:
        return QueryTimeoutError()
    if errno == 1049:
        return DatabaseError("Database configurado não existe.", errno=errno)
    if errno == 1146:
        return TableNotFoundError()
    if errno == 1054:
        detail = f" ({message})" if message else ""
        return DatabaseError(f"Coluna não encontrada{detail}.", errno=errno)
    if errno == 1064:
        detail = f" ({message})" if message else ""
        return DatabaseError(f"Erro de sintaxe SQL{detail}.", errno=errno)

    detail = f" ({message})" if message else ""
    return DatabaseError(f"Erro ao executar a consulta no banco{detail}.", errno=errno)
