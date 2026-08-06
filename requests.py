"""Minimal requests-compatible shim used only by the isolated SPC4 branch.

It implements the small subset needed by the research workflows: get(),
context-manager responses, raise_for_status(), and the raw response stream.
"""
from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import Request, urlopen


class Response:
    def __init__(self, url: str, *, headers: dict[str, str] | None = None,
                 timeout: float | None = None) -> None:
        request = Request(url, headers=headers or {})
        self._response = urlopen(request, timeout=timeout)
        self.raw = self._response
        self.status_code = getattr(self._response, "status", 200)
        self.url = getattr(self._response, "url", url)

    def raise_for_status(self) -> None:
        if not 200 <= int(self.status_code) < 400:
            raise HTTPError(self.url, int(self.status_code),
                            f"HTTP status {self.status_code}",
                            getattr(self._response, "headers", None), None)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._response.close()


def get(url: str, *, stream: bool = False, timeout: float | None = None,
        headers: dict[str, str] | None = None, **_: object) -> Response:
    del stream  # urllib already exposes a streaming file-like response.
    return Response(url, headers=headers, timeout=timeout)
