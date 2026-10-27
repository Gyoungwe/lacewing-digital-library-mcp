"""Background jobs that outlive the MCP stdio session.

Each job is a directory ``<LDL_MCP_HOME>/jobs/<job_id>/`` holding ``job.json``
(parameters and state), ``control.json`` (pause/cancel requests) and
``log.txt``. Workers run as detached processes (``python -m ldl_mcp.worker``),
so closing Codex or restarting the MCP server does not stop a download; the
state files let any later session report progress or resume.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any

from .paths import jobs_dir, now_iso, read_json, write_json_atomic

ACTIVE = {"starting", "running"}
RESUMABLE = {"paused", "interrupted", "auth_required", "failed", "attention_required", "cancelled"}
KINDS = {"download", "verify", "metadata"}


def job_dir(job_id: str) -> Path:
    if not job_id or "/" in job_id or ".." in job_id:
        raise ValueError("Invalid job_id")
    path = jobs_dir() / job_id
    if not path.is_dir():
        raise FileNotFoundError(f"Unknown job_id {job_id!r}")
    return path


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return False


def _is_worker(pid: Any, job_id: str) -> bool:
    """The pid is alive *and* is this job's worker (guards against pid reuse after a reboot)."""
    if not _pid_alive(pid):
        return False
    command = ""
    try:
        command = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        try:
            command = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return True  # cannot inspect; fall back to the liveness check
    return "ldl_mcp.worker" in command and job_id in command


def load(job_id: str) -> dict[str, Any]:
    path = job_dir(job_id)
    job = read_json(path / "job.json", {}) or {}
    if job.get("status") in ACTIVE and not _is_worker(job.get("pid"), job_id):
        job["status"] = "interrupted"
        job["message"] = "The worker process is no longer running; resume to continue from the last checkpoint."
    return job


def update(job_id: str, **fields: Any) -> dict[str, Any]:
    path = job_dir(job_id) / "job.json"
    job = read_json(path, {}) or {}
    job.update(fields)
    job["updated_at"] = now_iso()
    write_json_atomic(path, job)
    return job


def control_action(job_id: str) -> str | None:
    value = read_json(job_dir(job_id) / "control.json", {}) or {}
    return value.get("action")


def _set_control(job_id: str, action: str | None) -> None:
    write_json_atomic(job_dir(job_id) / "control.json", {"action": action, "at": now_iso()})


def _spawn(job_id: str) -> int:
    path = job_dir(job_id)
    log = (path / "log.txt").open("ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "ldl_mcp.worker", job_id],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return process.pid


def _active_for_root(output_parent: str, genus_key: str) -> dict[str, Any] | None:
    for job in list_jobs(limit=200):
        if job.get("status") in ACTIVE:
            params = job.get("params", {})
            if params.get("output_parent") == output_parent and job.get("genus_key") == genus_key:
                return job
    return None


def start(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"Unknown job kind {kind!r}")
    genus_key = f"{kind}:" + str(params.get("scope_id") or params.get("genus") or params.get("comb_obj_id") or params.get("monograph_url") or "").casefold()
    busy = _active_for_root(str(params.get("output_parent")), genus_key)
    if busy:
        raise RuntimeError(f"Job {busy['job_id']} is already running for this genus and folder.")
    job_id = f"{kind}-{now_iso()[:10].replace('-', '')}-{secrets.token_hex(3)}"
    path = jobs_dir() / job_id
    path.mkdir(parents=True)
    write_json_atomic(
        path / "job.json",
        {
            "job_id": job_id,
            "kind": kind,
            "genus_key": genus_key,
            "params": params,
            "status": "starting",
            "created_at": now_iso(),
            "runs": 0,
        },
    )
    _set_control(job_id, None)
    pid = _spawn(job_id)
    return update(job_id, pid=pid)


def resume(job_id: str, mode: str | None = None) -> dict[str, Any]:
    job = load(job_id)
    if job.get("status") in ACTIVE:
        return job
    if job.get("status") not in RESUMABLE and job.get("status") != "completed":
        raise RuntimeError(f"Job {job_id} cannot be resumed from status {job.get('status')!r}")
    params = dict(job.get("params", {}))
    params["refresh"] = False
    if mode:
        params["download_mode"] = mode
    _set_control(job_id, None)
    update(job_id, status="starting", params=params, message="")
    pid = _spawn(job_id)
    return update(job_id, pid=pid)


def request_stop(job_id: str, action: str = "pause") -> dict[str, Any]:
    if action not in {"pause", "cancel"}:
        raise ValueError("action must be pause or cancel")
    job = load(job_id)
    if job.get("status") in ACTIVE:
        _set_control(job_id, action)
        return {**job, "requested": action, "note": "The worker stops at the next request boundary and keeps its checkpoint."}
    return {**job, "requested": None, "note": "Job is not running."}


def tail(job_id: str, lines: int = 40) -> list[str]:
    path = job_dir(job_id) / "log.txt"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in deque(handle, maxlen=max(1, min(lines, 400)))]


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    items = []
    for path in sorted(jobs_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, limit)]:
        if (path / "job.json").exists():
            try:
                items.append(load(path.name))
            except Exception:
                continue
    return items
