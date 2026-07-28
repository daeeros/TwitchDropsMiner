from __future__ import annotations

import sys
import html
import asyncio
import logging
from collections import deque
from typing import Any, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    pass

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
# room taken by the <pre></pre> wrapper
PRE_WRAPPER_LENGTH = len("<pre></pre>")
# a single runaway line (a traceback, a dumped payload) must not eat the whole window
MAX_LINE_LENGTH = 512


class TelegramHandler(logging.Handler):
    """
    Async logging handler that keeps a single "live" Telegram message and edits it in place,
    so the chat shows a rolling tail of the last `tail_lines` log lines instead of a wall
    of message fragments.

    A new message is only posted when the current one can no longer be edited.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        update_interval: float = 3.0,
        tail_lines: int = 50,
        level: int = logging.NOTSET,
    ):
        super().__init__(level)
        self._chat_id = chat_id
        self._update_interval = update_interval
        # NOTE: emit() can be called from any thread - deque.append is atomic, unlike
        # putting onto an asyncio.Queue, which is what this used to do
        self._lines: deque[str] = deque(maxlen=tail_lines)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._message_id: int | None = None
        self._sent_text: str | None = None
        self._api_url = f"https://api.telegram.org/bot{bot_token}"

    def start(self) -> None:
        """Start the background update loop. Must be called from an async context."""
        if self._task is None:
            self._task = asyncio.create_task(self._update_loop())

    def emit(self, record: logging.LogRecord) -> None:
        """Add a formatted log record to the tail."""
        try:
            self._lines.append(self.format(record))
        except Exception:
            self.handleError(record)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def _update_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._update_interval)
                await self._update()
            except asyncio.CancelledError:
                # one final update, so the last lines before the shutdown are visible
                await self._update()
                break
            except Exception as exc:
                # print to stderr to avoid recursing back into this handler
                print(f"[TelegramHandler] Error in update loop: {exc}", file=sys.stderr)

    def _render(self) -> str:
        """
        Build the message body out of the buffered lines.

        This is a tail view, so when it doesn't fit, the oldest lines go first.
        """
        lines = [
            line if len(line) <= MAX_LINE_LENGTH else f"{line[:MAX_LINE_LENGTH]}..."
            for line in self._lines
        ]
        # NOTE: escaping can grow the text (& -> &amp;), so the limit is checked on the
        # escaped result, not on the raw one
        limit = TELEGRAM_MAX_MESSAGE_LENGTH - PRE_WRAPPER_LENGTH
        escaped = html.escape("\n".join(lines))
        while lines and len(escaped) > limit:
            del lines[0]
            escaped = html.escape("\n".join(lines))
        return escaped

    async def _update(self) -> None:
        """Push the current tail into the live message, posting one if there isn't any."""
        content = self._render()
        if not content or content == self._sent_text:
            # nothing new to show: no request at all. This is also what keeps Telegram's
            # "message is not modified" error from ever happening.
            return
        text = f"<pre>{content}</pre>"

        if self._message_id is not None:
            outcome = await self._api_call(
                "editMessageText",
                {
                    "chat_id": self._chat_id,
                    "message_id": self._message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if outcome is not None:
                self._sent_text = content
                return
            if self._message_id is not None:
                # throttled or offline: keep the message and try again on the next tick
                return
            # the message turned out to be uneditable - fall through and post a new one

        outcome = await self._api_call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if outcome is not None:
            self._message_id = outcome.get("message_id")
            self._sent_text = content

    async def _api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """
        Call the Telegram API. Returns the result on success, `None` on any failure.

        Failures are not all equal, and the difference decides whether we keep editing the
        current message or start a new one:
          • 429 - we're being throttled. Wait it out, the message is still fine.
          • network trouble - nothing to conclude about the message, retry later.
          • anything else (400, 403...) - the message can't be edited any more, so `_message_id`
            is cleared and the caller posts a fresh one.
        """
        try:
            session = await self._get_session()
            async with session.post(f"{self._api_url}/{method}", json=payload) as response:
                body: dict[str, Any] = await response.json()
                if response.status == 200:
                    # editMessageText can answer with a plain `true` instead of a Message
                    result = body.get("result")
                    return result if isinstance(result, dict) else {}
                if response.status == 429:
                    retry_after = float(body.get("parameters", {}).get("retry_after", 1))
                    print(
                        f"[TelegramHandler] rate limited, waiting {retry_after}s",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(retry_after)
                    return None
                print(
                    f"[TelegramHandler] {method} failed with {response.status}: "
                    f"{body.get('description', body)}",
                    file=sys.stderr,
                )
                # the live message is unusable - the caller will post a new one
                self._message_id = None
                return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[TelegramHandler] {method} failed: {exc}", file=sys.stderr)
            return None

    async def async_close(self) -> None:
        """Gracefully stop the update loop and close the HTTP session."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def close(self) -> None:
        """Override logging.Handler.close - schedule async cleanup if possible."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.async_close())
        except RuntimeError:
            pass
        super().close()
