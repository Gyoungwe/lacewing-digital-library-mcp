"""Local data locations for the LDL MCP research workflow.

Everything the MCP writes lives under one root so it can be backed up, moved or
deleted as a unit. Override it with ``LDL_MCP_HOME``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def data_home() -> Path:
    configured = os.getenv("LDL_MCP_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "share" / "ldl-mcp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def subdir(name: str, private: bool = False) -> Path:
    path = data_home() / name
    path.mkdir(parents=True, exist_ok=True)
    if private:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def auth_dir() -> Path:
    return subdir("auth", private=True)


def jobs_dir() -> Path:
    return subdir("jobs")


def libraries_dir() -> Path:
    return subdir("libraries")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path: Path, value: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def stable_id(prefix: str, payload: Any, length: int = 12) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", prefix).strip("-").lower()[:40] or "item"
    return f"{slug}-{digest[:length]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
