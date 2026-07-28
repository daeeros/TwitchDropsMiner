from __future__ import annotations

from typing import Any, TypedDict, TYPE_CHECKING

from yarl import URL

from utils import json_load, json_save
from constants import SETTINGS_PATH, DEFAULT_LANG, PriorityMode

if TYPE_CHECKING:
    from main import ParsedArgs


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    watch_url: str
    exclude: set[str]
    priority: list[str]
    connection_quality: int
    priority_mode: PriorityMode
    telegram_bot_token: str
    telegram_chat_id: str
    kick_enabled: bool


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "watch_url": "",
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "priority_mode": PriorityMode.PRIORITY_ONLY,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "kick_enabled": False,
}


class Settings:
    # from args
    log: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    # empty: auto-detect the watch event URL and fall back between the known hosts
    # non-empty: always use this exact URL, without any fallbacks
    watch_url: str
    exclude: set[str]
    priority: list[str]
    connection_quality: int
    priority_mode: PriorityMode
    # Kick: mining runs alongside Twitch when enabled. Kick has no device-code login,
    # so the session token is read from an exported kick_cookies.txt.
    # NOTE: Kick only ever mines the games listed in 'priority' - see KickMiner._wanted_campaigns
    kick_enabled: bool

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True