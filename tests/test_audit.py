from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from dbscope.audit import AuditLogger
from dbscope.errors import AuditError


def test_writes_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    logger = AuditLogger(path)

    logger.log({"tool": "query", "environment": "dev", "success": True})
    logger.log({"tool": "explain", "environment": "qa", "success": False})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tool"] == "query"
    assert first["environment"] == "dev"
    assert "timestamp" in first


def test_creates_private_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    AuditLogger(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_rotates_by_size(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path, max_bytes=250, backups=2)

    for index in range(20):
        logger.log({"tool": "query", "index": index, "success": True})

    assert path.exists()
    assert path.with_name("audit.jsonl.1").exists()
    assert path.with_name("audit.jsonl.2").exists()


def test_refuses_to_start_when_audit_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(AuditError):
        AuditLogger(blocker / "audit.jsonl")
