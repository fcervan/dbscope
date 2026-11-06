"""Optional integration test against a real MySQL/MariaDB server.

Run manually with:

    DBSCOPE_INTEGRATION_CONFIG=/path/to/config.toml \
        uv run pytest -m integration

The config must point at a read-only role. No real database is used in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dbscope.config import load_settings
from dbscope.errors import SqlDeniedError
from dbscope.service import DbScopeService

pytestmark = pytest.mark.integration


def test_select_works_and_writes_are_denied(tmp_path: Path) -> None:
    config = os.environ.get("DBSCOPE_INTEGRATION_CONFIG")
    if not config:
        pytest.skip("DBSCOPE_INTEGRATION_CONFIG is not set")

    settings = load_settings(
        config,
        environ={"DBSCOPE_AUDIT_LOG": str(tmp_path / "audit.jsonl")},
    )
    service = DbScopeService(settings)

    result = service.query(environment=None, sql="SELECT 1 AS ok")

    assert result["ok"] is True
    assert result["rows"][0][0] == 1

    with pytest.raises(SqlDeniedError):
        service.query(environment=None, sql="DELETE FROM definitely_missing_table")
