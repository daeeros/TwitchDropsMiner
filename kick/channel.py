from __future__ import annotations

from typing import TYPE_CHECKING

from kick.http import KickHTTP, KickRequestError
from kick.constants import CHANNEL_URL, LIVESTREAMS_URL, LIVESTREAMS_LIMIT, logger

if TYPE_CHECKING:
    from constants import JsonType


class KickChannel:
    """
    A Kick channel, along with whatever we know about the stream it's currently running.
    """
    __slots__ = ("slug", "channel_id", "livestream_id", "category_id", "online", "title")

    def __init__(self, slug: str):
        self.slug: str = slug
        self.channel_id: int | None = None
        self.livestream_id: int | None = None
        self.category_id: int | None = None
        self.online: bool = False
        self.title: str = ''

    def __repr__(self) -> str:
        return f"KickChannel({self.slug}, {'ONLINE' if self.online else 'OFFLINE'})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, KickChannel):
            return self.slug == other.slug
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.slug)

    def _update(self, data: JsonType) -> None:
        self.channel_id = data.get("id")
        livestream: JsonType | None = data.get("livestream")
        if not livestream or not livestream.get("is_live"):
            self.online = False
            self.livestream_id = None
            self.category_id = None
            return
        self.online = True
        self.livestream_id = livestream.get("id")
        self.title = livestream.get("session_title") or ''
        categories = livestream.get("categories") or []
        self.category_id = categories[0].get("id") if categories else None

    async def refresh(self, http: KickHTTP) -> bool:
        """
        Re-read the channel's state. Returns whether it's live right now.
        """
        try:
            data = await http.get_json(CHANNEL_URL.format(slug=self.slug))
        except KickRequestError as exc:
            logger.debug(f"Kick: couldn't check {self.slug}: {exc}")
            # unknown state: treat as offline, the caller will simply pick someone else
            self.online = False
            return False
        if not isinstance(data, dict):
            self.online = False
            return False
        self._update(data)
        return self.online

    @property
    def watchable(self) -> bool:
        return self.online and self.channel_id is not None and self.livestream_id is not None

    def matches_category(self, category_id: int | None) -> bool:
        if category_id is None or self.category_id is None:
            return True
        return self.category_id == category_id


async def find_live_channels(http: KickHTTP, category_id: int) -> list[str]:
    """
    Channel slugs currently streaming a given category, most viewers first.
    """
    try:
        data = await http.get_json(
            LIVESTREAMS_URL.format(limit=LIVESTREAMS_LIMIT, category_id=category_id)
        )
    except KickRequestError as exc:
        logger.warning(f"Kick: couldn't list live channels for category {category_id}: {exc}")
        return []
    if not isinstance(data, dict):
        return []
    payload = data.get("data")
    if isinstance(payload, dict):
        livestreams = payload.get("livestreams") or []
    elif isinstance(payload, list):
        livestreams = payload
    else:
        livestreams = []
    slugs: list[str] = []
    for stream in livestreams:
        if not isinstance(stream, dict):
            continue
        channel = stream.get("channel") or {}
        if (slug := channel.get("slug")):
            slugs.append(slug)
    return slugs
