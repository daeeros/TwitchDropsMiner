from __future__ import annotations

import json
import asyncio
from time import time
from typing import TYPE_CHECKING

import aiohttp

from kick.http import KickHTTP, KickRequestError
from kick.constants import (
    KICK_HEADERS,
    KICK_CLIENT_TOKEN,
    WS_TOKEN_URL,
    WS_CONNECT_URL,
    WS_TYPE_PING,
    WS_TYPE_HANDSHAKE,
    WS_TYPE_USER_EVENT,
    WS_WATCH_EVENT,
    WATCH_EVENT_INTERVAL,
    WS_KEEPALIVE_INTERVAL,
    logger,
)

if TYPE_CHECKING:
    from settings import Settings
    from kick.channel import KickChannel


class KickWatcher:
    """
    Keeps a viewer websocket open for one channel and emits the watch event Kick counts
    drop progress from.

    This is Kick's equivalent of Twitch's "minute-watched" payload: connect, introduce
    yourself to the channel, then report you're watching once a minute. No video involved.

    NOTE: unlike Kick's REST API - which the WAF refuses to serve to aiohttp - the websocket
    endpoint is happy with our regular aiohttp stack, so this reuses it.
    """

    def __init__(self, settings: Settings, http: KickHTTP):
        self.settings: Settings = settings
        self._http: KickHTTP = http
        self._session: aiohttp.ClientSession | None = None
        self.events_sent: int = 0

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": KICK_HEADERS["User-Agent"], "Origin": "https://kick.com"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_viewer_token(self) -> str:
        """
        Short-lived token authorizing one websocket connection.

        Requesting it with our session token is what ties the connection - and thus the
        watch time - to the user's account.
        """
        response = await self._http.get_json(
            WS_TOKEN_URL,
            auth=True,
            extra_headers={"X-Client-Token": KICK_CLIENT_TOKEN, "Sec-Fetch-Site": "same-site"},
        )
        token = (response or {}).get("data", {}).get("token")
        if not token:
            raise KickRequestError("Kick didn't return a viewer websocket token")
        return token

    @staticmethod
    def _handshake_payload(channel: KickChannel) -> str:
        return json.dumps({
            "type": WS_TYPE_HANDSHAKE,
            "data": {"message": {"channelId": channel.channel_id}},
        })

    @staticmethod
    def _watch_payload(channel: KickChannel) -> str:
        return json.dumps({
            "type": WS_TYPE_USER_EVENT,
            "data": {
                "message": {
                    "name": WS_WATCH_EVENT,
                    "channel_id": channel.channel_id,
                    "livestream_id": int(channel.livestream_id or 0),
                }
            },
        })

    async def run(self, channel: KickChannel, stop_event: asyncio.Event) -> None:
        """
        Watch `channel` until `stop_event` is set or the connection drops.

        Never raises for ordinary network trouble - the miner decides what to do next based on
        drop progress, not on this returning.
        """
        session = await self.get_session()
        proxy = str(self.settings.proxy) if self.settings.proxy else None
        try:
            token = await self.get_viewer_token()
        except KickRequestError as exc:
            logger.warning(f"Kick: couldn't get a viewer token: {exc}")
            return

        keepalive: float = WS_KEEPALIVE_INTERVAL.total_seconds()
        watch_interval: float = WATCH_EVENT_INTERVAL.total_seconds()
        try:
            async with session.ws_connect(
                WS_CONNECT_URL.format(token=token), proxy=proxy
            ) as websocket:
                logger.debug(f"Kick: websocket connected for {channel.slug}")
                await websocket.send_str(self._handshake_payload(channel))
                await websocket.send_str(self._watch_payload(channel))
                self.events_sent += 1
                last_watch_event: float = time()
                counter: int = 0
                while not stop_event.is_set():
                    counter += 1
                    # keep the connection warm the same way the web client does
                    if counter % 2:
                        await websocket.send_str(self._handshake_payload(channel))
                    else:
                        await websocket.send_str(json.dumps({"type": WS_TYPE_PING}))
                    # this doubles as our wait between keepalives
                    try:
                        message = await asyncio.wait_for(websocket.receive(), timeout=keepalive)
                    except asyncio.TimeoutError:
                        pass
                    else:
                        if message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            logger.debug(f"Kick: websocket closed for {channel.slug}")
                            break
                    now = time()
                    if now - last_watch_event >= watch_interval:
                        await websocket.send_str(self._watch_payload(channel))
                        self.events_sent += 1
                        last_watch_event = now
                        logger.debug(f"Kick: sent watch event for {channel.slug}")
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning(f"Kick: websocket problem while watching {channel.slug}: {exc}")
