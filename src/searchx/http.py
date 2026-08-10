from __future__ import annotations

import gzip
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class HttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(slots=True)
class HttpResult:
    status: int
    data: Any
    elapsed_ms: float
    headers: dict[str, str]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so provider credentials never leave their origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class HttpClient:
    def __init__(self, timeout: float = 12.0, retries: int = 1, user_agent: str = "searchx-native/0.2") -> None:
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        # urllib forwards request headers during redirects, including Bearer and
        # API-key credentials.  Provider APIs should not require a redirect;
        # surfacing one as an HTTP error is safer than forwarding credentials to
        # an unverified URL.
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResult:
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            start = time.perf_counter()
            req = urllib.request.Request(url, data=payload, headers=request_headers, method=method.upper())
            try:
                with self._opener.open(req, timeout=timeout or self.timeout) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                    data = json.loads(text) if text.strip() else {}
                    return HttpResult(
                        status=response.status,
                        data=data,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                        headers={k.lower(): v for k, v in response.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                raw = exc.read() if exc.fp else b""
                text = raw.decode("utf-8", errors="replace")
                last_error = HttpError(
                    f"HTTP {exc.code}: {self._safe_error_text(text)}",
                    status=exc.code,
                    body=text,
                )
                if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise last_error
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                self._sleep(attempt, retry_after)
            except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise HttpError(f"network/decode error: {exc}") from exc
                self._sleep(attempt, None)
        raise HttpError(str(last_error or "request failed"))

    @staticmethod
    def _safe_error_text(text: str) -> str:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in ("error", "message", "detail"):
                    if key in obj:
                        value = obj[key]
                        return str(value)[:500]
        except json.JSONDecodeError:
            pass
        return " ".join(text.split())[:500]

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 4.0))
                return
            except ValueError:
                pass
        time.sleep(min(0.35 * (2**attempt) + random.random() * 0.15, 2.5))
