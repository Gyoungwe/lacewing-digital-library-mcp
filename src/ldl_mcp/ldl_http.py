"""A ``requests`` adapter exposing the subset of Playwright's APIRequestContext the downloader uses.

The original genus downloader passed ``context.request`` (cookies shared with the
logged-in browser) into its download functions. Keeping the same interface lets
the MCP reuse that tested logic unchanged while sending the saved LDL cookies
from a background worker with no browser attached.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import requests


class JobPaused(KeyboardInterrupt):
    """Raised at a request boundary when the job owner asked to pause or cancel.

    It subclasses KeyboardInterrupt on purpose: the downloader already treats
    Ctrl+C as "save progress and stop", and per-document ``except Exception``
    handlers must not record it as a download failure.
    """

    def __init__(self, action: str = "pause") -> None:
        super().__init__(action)
        self.action = action


class APIResponse:
    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self.status = response.status_code
        self.status_text = response.reason or ""
        self.ok = 200 <= response.status_code < 300
        self.url = response.url
        self.headers = {str(k).lower(): str(v) for k, v in response.headers.items()}

    def body(self) -> bytes:
        return self._response.content

    def text(self) -> str:
        return self._response.text

    def json(self) -> Any:
        return json.loads(self._response.content)


class RequestsAPIContext:
    def __init__(
        self,
        session: requests.Session,
        min_interval: float = 0.35,
        control: Callable[[], str | None] | None = None,
    ) -> None:
        self.session = session
        self.min_interval = min_interval
        self.control = control
        self._last = 0.0
        self.request_count = 0

    def _before(self) -> None:
        if self.control:
            action = self.control()
            if action in {"pause", "cancel"}:
                raise JobPaused(action)
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _after(self) -> None:
        self._last = time.time()
        self.request_count += 1

    @staticmethod
    def _seconds(timeout: float | None) -> float:
        return max(5.0, float(timeout) / 1000.0) if timeout else 120.0

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        fail_on_status_code: bool = False,
    ) -> APIResponse:
        self._before()
        try:
            response = self.session.get(url, headers=headers or {}, timeout=self._seconds(timeout), allow_redirects=True)
        finally:
            self._after()
        if fail_on_status_code:
            response.raise_for_status()
        return APIResponse(response)

    def post(
        self,
        url: str,
        data: Any = None,
        timeout: float | None = None,
        fail_on_status_code: bool = False,
        headers: dict[str, str] | None = None,
    ) -> APIResponse:
        self._before()
        try:
            response = self.session.post(
                url,
                json=data if data is not None else {},
                headers={"Accept": "application/json", **(headers or {})},
                timeout=self._seconds(timeout),
            )
        finally:
            self._after()
        if fail_on_status_code:
            response.raise_for_status()
        return APIResponse(response)
