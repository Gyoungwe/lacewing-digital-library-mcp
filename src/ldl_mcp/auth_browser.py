"""Login helper process: ``python -m ldl_mcp.auth_browser``.

Started detached by ``auth.start_login`` so the MCP call returns immediately.
It opens an isolated persistent browser profile, waits for the user to log in
and press the in-page save button, then writes the LDL cookies and exits.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from . import auth
from .paths import now_iso, read_json, write_json_atomic

LOGIN_URL = "https://lacewing.tamu.edu/homepage/contributors"
FALLBACK_URL = "https://lacewing.tamu.edu/"

SAVE_BUTTON_SCRIPT = """
(() => {
  if (window.top !== window) return;
  const install = () => {
    const id = 'ldl-mcp-save-session';
    if (document.getElementById(id) || !document.body) return;
    const box = document.createElement('div');
    box.id = id;
    Object.assign(box.style, {
      position: 'fixed', right: '12px', bottom: '12px', zIndex: '2147483647',
      background: '#6a1b9a', color: 'white', padding: '8px 12px', borderRadius: '6px',
      font: '600 14px -apple-system, "Segoe UI", sans-serif', boxShadow: '0 2px 8px #0008'
    });
    const label = document.createElement('div');
    label.textContent = 'LDL MCP 控制的登录窗口';
    label.style.marginBottom = '6px';
    const button = document.createElement('button');
    button.textContent = '我已登录，保存会话 / Save LDL session';
    Object.assign(button.style, {cursor: 'pointer', font: 'inherit', padding: '4px 8px'});
    button.onclick = () => { if (window.ldlMcpSave) window.ldlMcpSave(); button.textContent = '正在保存…'; };
    const note = document.createElement('div');
    note.id = 'ldl-mcp-save-note';
    note.style.marginTop = '6px';
    note.style.fontWeight = '400';
    box.append(label, button, note);
    document.body.appendChild(box);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install, {once: true});
})()
"""


def _update(**fields: Any) -> None:
    state = read_json(auth.state_path(), {}) or {}
    state.update(fields)
    state["updated_at"] = now_iso()
    write_json_atomic(auth.state_path(), state)


def _note(context: Any, text: str) -> None:
    for page in list(context.pages):
        try:
            page.evaluate(
                "t => { const n = document.getElementById('ldl-mcp-save-note'); if (n) n.textContent = t; "
                "const b = document.querySelector('#ldl-mcp-save-session button'); if (b) b.textContent = '我已登录，保存会话 / Save LDL session'; }",
                text,
            )
        except Exception:
            pass


def _launch(playwright: Any) -> tuple[Any, str]:
    errors: list[str] = []
    for channel in ("chrome", "msedge", None):
        try:
            kwargs: dict[str, Any] = {
                "user_data_dir": str(auth.profile_dir()),
                "headless": False,
                "viewport": None,
                "accept_downloads": False,
            }
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch_persistent_context(**kwargs), channel or "chromium"
        except Exception as exc:
            errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0]}")
    raise RuntimeError("Could not start Chrome, Edge or Playwright Chromium: " + " | ".join(errors))


def _verify(context: Any, edoc_id: int) -> tuple[bool, str]:
    response = context.request.get(
        f"https://lacewing.tamu.edu/edoc/{edoc_id}",
        headers={"Range": "bytes=0-1023"},
        timeout=60_000,
        fail_on_status_code=False,
    )
    body = response.body()[:2048]
    if b"LDL Authenticate - Log In" in body or b"Enter Username and Password" in body or "/homepage/contributors" in response.url.lower():
        return False, "LDL 仍显示登录页；请先完成登录再保存。"
    if response.status not in (200, 206):
        return False, f"验证 EDoc {edoc_id} 返回 HTTP {response.status}。"
    return True, f"已验证可以打开 EDoc {edoc_id}。"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-minutes", type=int, default=20)
    parser.add_argument("--verify-edoc", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _update(status="failed", message="Playwright is not installed in the MCP environment (pip install playwright).")
        return 2

    save_requested = {"value": False}
    deadline = time.time() + max(1, args.timeout_minutes) * 60
    with sync_playwright() as playwright:
        try:
            context, channel = _launch(playwright)
        except Exception as exc:
            _update(status="failed", message=str(exc))
            return 3
        _update(status="waiting_for_login", browser_channel=channel, message="Waiting for the user to log in and click save.")
        try:
            context.expose_binding("ldlMcpSave", lambda source: save_requested.__setitem__("value", True))
            context.add_init_script(SAVE_BUTTON_SCRIPT)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                try:
                    page.goto(FALLBACK_URL, wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass
            page.bring_to_front()
            last_cookies: list[dict[str, Any]] = []
            while time.time() < deadline:
                if not context.pages:
                    break
                try:
                    last_cookies = context.cookies()
                except Exception:
                    break
                if save_requested["value"]:
                    save_requested["value"] = False
                    if args.verify_edoc:
                        _update(status="verifying")
                        ok, message = _verify(context, args.verify_edoc)
                        if not ok:
                            _update(status="waiting_for_login", message=message)
                            _note(context, message)
                            continue
                    count = auth.save_cookies(last_cookies, source=f"browser:{channel}")
                    if count == 0:
                        _update(status="waiting_for_login", message="No LDL cookies yet; log in first.")
                        _note(context, "还没有 LDL 登录 Cookie，请先登录。")
                        continue
                    _update(
                        status="saved",
                        cookie_count=count,
                        verified=bool(args.verify_edoc),
                        message="LDL session saved. The browser will close.",
                    )
                    _note(context, "会话已保存，窗口即将关闭。")
                    time.sleep(1.5)
                    return 0
                time.sleep(0.5)
            if time.time() >= deadline:
                _update(status="timeout", message="Login window timed out before the session was saved.")
            else:
                _update(status="browser_closed", message="The login window was closed before the session was saved.")
            return 1
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
