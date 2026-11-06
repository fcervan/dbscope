"""MySQL/MariaDB access with server-side read-only transactions."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_value
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import Cursor, SSCursor

from .config import SYSTEM_SCHEMAS, EnvironmentConfig


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    truncated_cells: int
    duration_ms: int
    row_count: int


def _ssl_kwargs(env: EnvironmentConfig) -> dict[str, Any]:
    if env.sslmode == "disable":
        return {"ssl_disabled": True}

    if env.sslmode == "require":
        options: dict[str, Any] = {
            "check_hostname": False,
            "verify_mode": False,
        }
        if env.ssl_cert:
            options["cert"] = env.ssl_cert
        if env.ssl_key:
            options["key"] = env.ssl_key
        return {"ssl": options}

    verify_options: dict[str, Any] = {
        "ssl_verify_cert": True,
        "ssl_verify_identity": env.sslmode == "verify-identity",
    }
    if env.ssl_ca:
        verify_options["ssl_ca"] = env.ssl_ca
    if env.ssl_cert:
        verify_options["ssl_cert"] = env.ssl_cert
    if env.ssl_key:
        verify_options["ssl_key"] = env.ssl_key
    return verify_options


def _try_session_setting(cursor: Cursor, statement: str) -> None:
    """Apply an optional session setting shared by MySQL and MariaDB."""
    try:
        cursor.execute(statement)
    except pymysql.err.MySQLError:
        return


class MySqlClient:
    """Thin, synchronous MySQL client used by the MCP service."""

    @contextmanager
    def connect(self, env: EnvironmentConfig) -> Iterator[Connection]:
        timeout_s = max(env.connect_timeout_s, (env.query_timeout_ms // 1000) + 5)
        kwargs: dict[str, Any] = {
            "host": env.host,
            "port": env.port,
            "user": env.user,
            "password": env.secret(),
            "database": env.database,
            "charset": "utf8mb4",
            "connect_timeout": env.connect_timeout_s,
            "read_timeout": timeout_s,
            "write_timeout": timeout_s,
            "autocommit": False,
            "local_infile": False,
            "program_name": "dbscope",
            **_ssl_kwargs(env),
        }
        connection = pymysql.connect(**kwargs)
        try:
            with connection.cursor() as cursor:
                _try_session_setting(cursor, "SET SESSION TRANSACTION READ ONLY")
                _try_session_setting(cursor, "SET SESSION innodb_lock_wait_timeout = 5")
                _try_session_setting(
                    cursor, f"SET SESSION max_execution_time = {env.query_timeout_ms}"
                )
                _try_session_setting(
                    cursor,
                    f"SET SESSION max_statement_time = {env.query_timeout_ms / 1000:.3f}",
                )
            connection.rollback()
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _start_read_only(cursor: Cursor) -> None:
        cursor.execute("START TRANSACTION READ ONLY")

    def execute_select(
        self,
        env: EnvironmentConfig,
        sql: str,
        *,
        max_rows: int,
        max_bytes: int,
        max_cell_chars: int,
    ) -> QueryResult:
        started = time.perf_counter()
        with self.connect(env) as connection, connection.cursor(SSCursor) as cursor:
            self._start_read_only(cursor)
            try:
                cursor.execute(sql)
                description = cursor.description or ()
                columns = [str(column[0]) for column in description]
                fetched = cursor.fetchmany(max_rows + 1)
                truncated = len(fetched) > max_rows
                if truncated:
                    fetched = fetched[:max_rows]

                rows: list[list[Any]] = []
                used_bytes = 0
                truncated_cells = 0
                for raw_row in fetched:
                    converted: list[Any] = []
                    for value in raw_row:
                        converted_value, cell_truncated = _jsonable(value, max_cell_chars)
                        converted.append(converted_value)
                        if cell_truncated:
                            truncated_cells += 1
                    encoded = json.dumps(
                        converted,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    if rows and used_bytes + len(encoded) > max_bytes:
                        truncated = True
                        break
                    if not rows and len(encoded) > max_bytes:
                        truncated = True
                        break
                    rows.append(converted)
                    used_bytes += len(encoded)
            finally:
                connection.rollback()

        duration_ms = int((time.perf_counter() - started) * 1000)
        return QueryResult(
            columns=columns,
            rows=rows,
            truncated=truncated,
            truncated_cells=truncated_cells,
            duration_ms=duration_ms,
            row_count=len(rows),
        )

    def list_schemas(self, env: EnvironmentConfig) -> list[str]:
        placeholders = ", ".join(["%s"] * len(SYSTEM_SCHEMAS))
        system_schemas = tuple(sorted(SYSTEM_SCHEMAS))
        with self.connect(env) as connection, connection.cursor() as cursor:
            self._start_read_only(cursor)
            try:
                cursor.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    f"WHERE schema_name NOT IN ({placeholders}) ORDER BY schema_name",
                    system_schemas,
                )
                schemas = [str(row[0]) for row in cursor.fetchall()]
                return [schema for schema in schemas if schema.casefold() not in SYSTEM_SCHEMAS]
            finally:
                connection.rollback()

    def list_tables(self, env: EnvironmentConfig, schema: str) -> list[dict[str, str]]:
        with self.connect(env) as connection, connection.cursor() as cursor:
            self._start_read_only(cursor)
            try:
                cursor.execute(
                    "SELECT table_name, table_type FROM information_schema.tables "
                    "WHERE table_schema = %s ORDER BY table_name",
                    (schema,),
                )
                return [{"name": str(row[0]), "type": str(row[1])} for row in cursor.fetchall()]
            finally:
                connection.rollback()

    def describe_table(
        self,
        env: EnvironmentConfig,
        schema: str,
        table: str,
    ) -> dict[str, Any] | None:
        with self.connect(env) as connection, connection.cursor() as cursor:
            self._start_read_only(cursor)
            try:
                cursor.execute(
                    "SELECT table_name, table_type, table_comment "
                    "FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    (schema, table),
                )
                table_row = cursor.fetchone()
                if table_row is None:
                    return None

                cursor.execute(
                    "SELECT column_name, column_type, is_nullable, column_default, "
                    "column_key, extra, column_comment "
                    "FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (schema, table),
                )
                columns = [
                    {
                        "name": str(row[0]),
                        "type": str(row[1]),
                        "nullable": str(row[2]).upper() == "YES",
                        "default": _text(row[3]),
                        "key": str(row[4]),
                        "extra": str(row[5]),
                        "comment": _text(row[6]),
                    }
                    for row in cursor.fetchall()
                ]

                cursor.execute(
                    "SELECT index_name, non_unique, column_name, seq_in_index "
                    "FROM information_schema.statistics "
                    "WHERE table_schema = %s AND table_name = %s "
                    "ORDER BY index_name, seq_in_index",
                    (schema, table),
                )
                index_map: dict[str, dict[str, Any]] = {}
                for index_name, non_unique, column_name, _ in cursor.fetchall():
                    entry = index_map.setdefault(
                        str(index_name),
                        {
                            "name": str(index_name),
                            "unique": not bool(non_unique),
                            "columns": [],
                        },
                    )
                    entry["columns"].append(str(column_name))

                return {
                    "name": str(table_row[0]),
                    "type": str(table_row[1]),
                    "comment": _text(table_row[2]),
                    "columns": columns,
                    "indexes": list(index_map.values()),
                }
            finally:
                connection.rollback()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _jsonable(value: Any, max_chars: int) -> tuple[Any, bool]:
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        if math.isfinite(value):
            return value, False
        return str(value), False
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + "...", True
        return value, False
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>", False
    if isinstance(value, (datetime, date, time_value, timedelta, Decimal)):
        text = str(value)
        if len(text) > max_chars:
            return text[:max_chars] + "...", True
        return text, False

    text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...", True
    return text, False
