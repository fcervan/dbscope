"""Structured audit log that never receives credentials."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import AuditError


class AuditLogger:
    """Append JSON lines to a file outside the repository.

    The caller is responsible for redacting SQL and error messages. The logger
    adds a UTC timestamp, creates the file with mode 600 and rotates it by size.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 5_000_000,
        backups: int = 3,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max_bytes
        self.backups = max(1, backups)
        self._lock = threading.Lock()
        self._prepare()

    def _prepare(self) -> None:
        parent_existed = self.path.parent.exists()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not parent_existed:
                os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise AuditError() from exc

        try:
            if not self.path.exists():
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                    0o600,
                )
                os.close(descriptor)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise AuditError() from exc

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.path.stat().st_size
        except OSError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return

        for index in range(self.backups - 1, 0, -1):
            source = self._backup_path(index)
            destination = self._backup_path(index + 1)
            if source.exists():
                os.replace(source, destination)
        if self.path.exists():
            os.replace(self.path, self._backup_path(1))

    def log(self, event: Mapping[str, Any]) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            **event,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
        with self._lock:
            try:
                self._rotate_if_needed(len(line.encode("utf-8")) + 1)
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                raise AuditError() from exc
