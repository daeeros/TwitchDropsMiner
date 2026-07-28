from __future__ import annotations

import json
import asyncio
import urllib.error
import urllib.request
from typing import Any, TYPE_CHECKING

from exceptions import MinerException
from utils import ExponentialBackoff
from kick.constants import KICK_HEADERS, logger

if TYPE_CHECKING:
    from settings import Settings
    from constants import JsonType


class KickRequestError(MinerException):
    """
    Raised when a request to Kick doesn't return what we wanted it to.
    """


class KickAuthError(KickRequestError):
    """
    Raised when Kick rejects our session token. Usually means the token has expired
    and the user has to grab a fresh one out of their browser.
    """


class KickHTTP:
    """
    Minimal JSON client for Kick's web API.

    NOTE: This deliberately does NOT use the aiohttp session the rest of the application runs on.
    Kick sits behind a WAF that rejects aiohttp with "Request blocked by security policy",
    no matter the headers, HTTP version or encoding, while plain urllib passes just fine.
    The requests are infrequent (a couple per minute at most), so running them in a thread
    costs us nothing and saves a dependency.
    """

    MAX_ATTEMPTS = 3

    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        self._session_token: str | None = None

    @property
    def session_token(self) -> str | None:
        return self._session_token

    @session_token.setter
    def session_token(self, value: str | None) -> None:
        self._session_token = value

    def _build_opener(self) -> urllib.request.OpenerDirector:
        proxy = self.settings.proxy
        if proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": str(proxy), "https": str(proxy)})
            )
        return urllib.request.build_opener()

    def _headers(self, *, auth: bool, extra: JsonType | None = None) -> dict[str, str]:
        headers = dict(KICK_HEADERS)
        if auth:
            if not self._session_token:
                raise KickAuthError("No Kick session token available")
            headers["Authorization"] = f"Bearer {self._session_token}"
        if extra:
            headers.update(extra)
        return headers

    def _request_sync(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = self._build_opener()
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            # an HTTP error response is still a response - let the caller decide what it means
            return exc.code, exc.read(), dict(exc.headers or {})

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        auth: bool = False,
        extra_headers: JsonType | None = None,
        payload: JsonType | None = None,
    ) -> Any:
        headers = self._headers(auth=auth, extra=extra_headers)
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf8")
            headers["Content-Type"] = "application/json"

        backoff = ExponentialBackoff(maximum=60)
        for attempt, delay in enumerate(backoff, start=1):
            try:
                status, raw, response_headers = await asyncio.to_thread(
                    self._request_sync, method, url, headers, body
                )
            except OSError as exc:
                # connection-level problem: DNS, TLS, refused, timed out...
                if attempt >= self.MAX_ATTEMPTS:
                    raise KickRequestError(f"Cannot reach {url}: {exc}") from exc
                logger.debug(f"Kick: request to {url} failed ({exc}), retrying")
                await asyncio.sleep(delay)
                continue

            if status == 200:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise KickRequestError(f"Invalid JSON from {url}") from exc
            if status in (401, 403) and auth:
                raise KickAuthError(
                    f"Kick rejected the session token ({status}) - it has most likely expired"
                )
            if status in (429, 500, 502, 503, 504) and attempt < self.MAX_ATTEMPTS:
                retry_after: float = delay
                with_header = response_headers.get("Retry-After")
                if with_header and with_header.isdigit():
                    retry_after = max(retry_after, float(with_header))
                logger.warning(
                    f"Kick: {url} returned {status}, retrying in {round(retry_after)}s"
                )
                await asyncio.sleep(retry_after)
                continue
            raise KickRequestError(f"{url} returned {status}: {raw[:200]!r}")
        # ExponentialBackoff is an infinite iterator, so this is unreachable
        raise KickRequestError(f"Request to {url} failed")

    async def get_json(
        self, url: str, *, auth: bool = False, extra_headers: JsonType | None = None
    ) -> Any:
        return await self.request_json("GET", url, auth=auth, extra_headers=extra_headers)

    async def post_json(
        self,
        url: str,
        payload: JsonType,
        *,
        auth: bool = False,
        extra_headers: JsonType | None = None,
    ) -> Any:
        return await self.request_json(
            "POST", url, auth=auth, extra_headers=extra_headers, payload=payload
        )
