"""LDL login session: a separate browser for the user to log in, and a private cookie store.

The MCP never accepts or stores a password. ``auth_start`` opens an isolated
Chrome/Edge profile (never the user's everyday profile); after the user logs in
and clicks the in-page "save session" button, only the ``lacewing.tamu.edu``
cookies are written to ``<LDL_MCP_HOME>/auth/ldl_cookies.json`` with mode 0600.
Download jobs then reuse those cookies through ``requests`` without a browser.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from .paths import auth_dir, now_iso, read_json, write_json_atomic

LDL_DOMAIN = "lacewing.tamu.edu"
USER_AGENT = "lacewing-digital-library-mcp/0.3 (research use; low-rate)"


class NoSessionError(RuntimeError):
    """No saved LDL login cookies are available."""


def cookie_path() -> Path:
    return auth_dir() / "ldl_cookies.json"


def state_path() -> Path:
    return auth_dir() / "login_state.json"


def profile_dir() -> Path:
    path = auth_dir() / "browser_profile"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def ldl_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cookies if LDL_DOMAIN in str(c.get("domain", "")).lstrip(".")]


def save_cookies(cookies: list[dict[str, Any]], source: str) -> int:
    kept = ldl_cookies(cookies)
    write_json_atomic(
        cookie_path(),
        {"saved_at": now_iso(), "source": source, "domain": LDL_DOMAIN, "cookies": kept},
        mode=0o600,
    )
    return len(kept)


def load_cookies() -> list[dict[str, Any]]:
    value = read_json(cookie_path(), {}) or {}
    cookies = value.get("cookies", []) if isinstance(value, dict) else []
    return [c for c in cookies if isinstance(c, dict) and c.get("name")]


def clear_cookies() -> bool:
    try:
        cookie_path().unlink()
        return True
    except FileNotFoundError:
        return False


def build_session(require_cookies: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    cookies = load_cookies()
    if require_cookies and not cookies:
        raise NoSessionError("No saved LDL login session. Call auth_start and log in first.")
    for cookie in cookies:
        session.cookies.set(
            str(cookie["name"]),
            str(cookie.get("value", "")),
            domain=str(cookie.get("domain") or LDL_DOMAIN),
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure", True)),
        )
    return session


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cookie_summary() -> dict[str, Any]:
    value = read_json(cookie_path(), {}) or {}
    cookies = load_cookies()
    now = time.time()
    expiries = [float(c["expires"]) for c in cookies if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
    session_only = sum(1 for c in cookies if not (isinstance(c.get("expires"), (int, float)) and c["expires"] > 0))
    return {
        "saved": bool(cookies),
        "saved_at": value.get("saved_at") if isinstance(value, dict) else None,
        "source": value.get("source") if isinstance(value, dict) else None,
        "cookie_count": len(cookies),
        "session_cookie_count": session_only,
        "earliest_expiry_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(expiries))) if expiries else None,
        "expired_cookie_count": sum(1 for e in expiries if e < now),
        "cookie_names": sorted({str(c["name"]) for c in cookies}),
    }


def probe_edoc(edoc_id: int, session: requests.Session | None = None) -> dict[str, Any]:
    """Request the first KiB of an EDoc to see whether the saved session can open it."""
    session = session or build_session()
    response = session.get(
        f"https://{LDL_DOMAIN}/edoc/{int(edoc_id)}",
        headers={"Range": "bytes=0-1023"},
        timeout=60,
        allow_redirects=True,
    )
    body = response.content[:2048]
    login_page = (
        b"LDL Authenticate - Log In" in body
        or b"Enter Username and Password" in body
        or "/homepage/contributors" in response.url.lower()
    )
    return {
        "EDocID": int(edoc_id),
        "http_status": response.status_code,
        "returned_document": body.startswith(b"%PDF") or (response.status_code in (200, 206) and not body.lstrip().startswith(b"<")),
        "login_required": login_page,
        "note": "A public EDoc can open without login, so a successful probe proves access to this EDoc only.",
    }


def login_state() -> dict[str, Any]:
    state = read_json(state_path(), {}) or {}
    if state.get("status") in {"starting", "waiting_for_login", "verifying"} and not _pid_alive(state.get("pid")):
        state["status"] = "browser_closed"
        state["message"] = "The login browser exited before a session was saved."
    return state


def start_login(verify_edoc_id: int | None = None, timeout_minutes: int = 20) -> dict[str, Any]:
    current = login_state()
    if current.get("status") in {"starting", "waiting_for_login", "verifying"} and _pid_alive(current.get("pid")):
        return {"started": False, "reason": "A login browser is already open.", **current}
    write_json_atomic(state_path(), {"status": "starting", "started_at": now_iso(), "pid": None})
    log = (auth_dir() / "login_browser.log").open("ab")
    args = [sys.executable, "-m", "ldl_mcp.auth_browser", "--timeout-minutes", str(int(timeout_minutes))]
    if verify_edoc_id:
        args += ["--verify-edoc", str(int(verify_edoc_id))]
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    state = read_json(state_path(), {}) or {}
    state.update({"pid": process.pid})
    write_json_atomic(state_path(), state)
    return {
        "started": True,
        "pid": process.pid,
        "instructions": (
            "A separate Chrome/Edge window is opening. Log in to LDL there, then click the purple "
            "'我已登录，保存会话 / Save LDL session' button at the bottom right. Poll auth_status until status is 'saved'."
        ),
    }


def stop_login() -> bool:
    state = login_state()
    pid = state.get("pid")
    if _pid_alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                return False
        return True
    return False


def status(probe_edoc_id: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"login_browser": login_state(), "cookies": cookie_summary()}
    if probe_edoc_id:
        try:
            result["probe"] = probe_edoc(probe_edoc_id)
        except requests.RequestException as exc:
            result["probe"] = {"EDocID": probe_edoc_id, "error": type(exc).__name__}
    result["storage"] = {"cookie_file_mode": "0600", "location": str(auth_dir())}
    return result
