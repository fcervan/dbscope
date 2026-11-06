from __future__ import annotations

import pymysql
import pytest

from dbscope.config import EnvironmentConfig
from dbscope.errors import (
    DatabaseError,
    InvalidArgumentError,
    QueryTimeoutError,
    SqlDeniedError,
)
from dbscope.safety import (
    redact_sql,
    redact_text,
    sanitize_driver_error,
    validate_identifier,
    validate_schema_access,
    validate_select_sql,
)

ALLOWED = [
    "SELECT 1",
    "SELECT 1;",
    "SELECT * FROM t WHERE id = 1",
    "SELECT * FROM a JOIN b ON a.id = b.id",
    "SELECT * FROM (SELECT 1 AS x) AS sub",
    "WITH x AS (SELECT 1 AS n) SELECT * FROM x",
    "SELECT 1 UNION SELECT 2",
    "SELECT 1 INTERSECT SELECT 2",
    "SELECT 1 EXCEPT SELECT 2",
    "SELECT (SELECT 1) AS value",
    "SELECT * FROM t /* comentário */",
    "SELECT 'for update' AS note",
    'SELECT "into @x" AS note',
    "SELECT 'into outfile' AS note",
]

DENIED = [
    "",
    "   ",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "INSERT INTO t VALUES (1)",
    "REPLACE INTO t VALUES (1)",
    "DROP TABLE t",
    "ALTER TABLE t ADD c INT",
    "TRUNCATE TABLE t",
    "CREATE TABLE t (id INT)",
    "CALL procedure_name()",
    "SET @x = 1",
    "SHOW TABLES",
    "DESC t",
    "EXPLAIN SELECT 1",
    "SELECT 1; SELECT 2",
    "SELECT 1 INTO OUTFILE '/tmp/out'",
    "SELECT 1 INTO DUMPFILE '/tmp/out'",
    "SELECT 1 INTO @variable",
    "SELECT * FROM t FOR UPDATE",
    "SELECT * FROM t FOR SHARE",
    "SELECT * FROM t LOCK IN SHARE MODE",
    "SELECT * FROM t PROCEDURE ANALYSE()",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(1000000, MD5('x'))",
    "SELECT GET_LOCK('name', 10)",
    "SELECT 1 /*!40101 SET NAMES utf8 */",
    "SELECT * FROM mysql.user",
    "SELECT * FROM information_schema.tables",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_selects(env: EnvironmentConfig, sql: str) -> None:
    assert validate_select_sql(sql, env)


@pytest.mark.parametrize("sql", DENIED)
def test_denied_statements(env: EnvironmentConfig, sql: str) -> None:
    with pytest.raises(SqlDeniedError):
        validate_select_sql(sql, env)


def test_semicolon_is_removed(env: EnvironmentConfig) -> None:
    assert validate_select_sql("SELECT 1;", env) == "SELECT 1"


def test_cte_is_not_treated_as_schema(env: EnvironmentConfig) -> None:
    restricted = env.model_copy(update={"allowed_schemas": ["app_dev"]})
    assert validate_select_sql("WITH x AS (SELECT 1) SELECT * FROM x", restricted)


def test_allowed_schema_is_enforced(env: EnvironmentConfig) -> None:
    restricted = env.model_copy(update={"allowed_schemas": ["app_dev"]})

    assert validate_select_sql("SELECT * FROM app_dev.t", restricted)
    with pytest.raises(SqlDeniedError, match="Schema"):
        validate_select_sql("SELECT * FROM other.t", restricted)


def test_identifier_validation() -> None:
    assert validate_identifier("users", "Tabela") == "users"

    with pytest.raises(InvalidArgumentError):
        validate_identifier("", "Tabela")
    with pytest.raises(InvalidArgumentError):
        validate_identifier("x" * 65, "Tabela")
    assert validate_identifier("mysql", "Tabela") == "mysql"
    with pytest.raises(InvalidArgumentError):
        validate_identifier("mysql", "Schema")
    with pytest.raises(InvalidArgumentError):
        validate_identifier("MYSQL", "Schema")
    with pytest.raises(InvalidArgumentError):
        validate_identifier("bad\nname", "Tabela")


def test_schema_access_validation(env: EnvironmentConfig) -> None:
    restricted = env.model_copy(update={"allowed_schemas": ["app_dev"]})

    assert validate_schema_access("app_dev", restricted) == "app_dev"
    with pytest.raises(InvalidArgumentError, match="Schema"):
        validate_schema_access("other", restricted)
    with pytest.raises(InvalidArgumentError):
        validate_schema_access("sys", restricted)


def test_redact_text_removes_known_values() -> None:
    values = ["ro_dev", "dev-db.example.internal", "senha-super-secreta", "app_dev"]
    text = (
        "Access denied for user 'ro_dev'@'dev-db.example.internal' "
        "password=senha-super-secreta database=app_dev"
    )

    redacted = redact_text(text, values)

    for value in values:
        assert value not in redacted
    assert "***" in redacted


def test_redact_text_removes_string_literals() -> None:
    redacted = redact_text("SELECT 'a@b.com' AS email", [])

    assert "a@b.com" not in redacted
    assert "'?'" in redacted


def test_redact_sql_removes_literals_and_numbers() -> None:
    sql = "SELECT * FROM users WHERE email = 'a@b.com' AND id = 12345"

    redacted = redact_sql(sql, ["users"])

    assert "a@b.com" not in redacted
    assert "12345" not in redacted
    assert "'?'" in redacted


def test_driver_permission_error_is_sanitized() -> None:
    exc = pymysql.err.OperationalError(
        1045,
        "Access denied for user 'ro_dev'@'dev-db.example.internal' (using password: YES)",
    )

    safe = sanitize_driver_error(exc, ["ro_dev", "dev-db.example.internal"])

    assert isinstance(safe, DatabaseError)
    assert "ro_dev" not in safe.safe_message
    assert "dev-db.example.internal" not in safe.safe_message
    assert "permiss" in safe.safe_message.lower()


def test_driver_timeout_error_is_mapped() -> None:
    safe = sanitize_driver_error(
        pymysql.err.OperationalError(3024, "Maximum execution time exceeded"),
        [],
    )

    assert isinstance(safe, QueryTimeoutError)


def test_driver_syntax_error_keeps_useful_redacted_message() -> None:
    safe = sanitize_driver_error(
        pymysql.err.OperationalError(
            1064,
            "You have an error near 'senha-super-secreta' at line 1",
        ),
        ["senha-super-secreta"],
    )

    assert "sintaxe" in safe.safe_message.lower()
    assert "senha-super-secreta" not in safe.safe_message


def test_unknown_driver_error_is_sanitized() -> None:
    safe = sanitize_driver_error(
        RuntimeError("connection to dev-db.example.internal failed for ro_dev"),
        ["dev-db.example.internal", "ro_dev"],
    )

    assert "dev-db.example.internal" not in safe.safe_message
    assert "ro_dev" not in safe.safe_message
