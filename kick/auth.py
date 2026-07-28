from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from kick.constants import KICK_COOKIES_PATH, logger


COOKIE_NAME = "session_token"


def parse_netscape_cookies(path: Path) -> dict[str, str]:
    """
    Read a Netscape-format cookies file - the kind browser extensions like "Get cookies.txt"
    export - and return the Kick cookies found in it.
    """
    cookies: dict[str, str] = {}
    try:
        contents = path.read_text(encoding="utf8")
    except OSError as exc:
        logger.warning(f"Kick: couldn't read {path.name}: {exc}")
        return cookies
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # domain, include_subdomains, path, secure, expires, name, value
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name, value = parts[0], parts[5], parts[6]
        if "kick.com" not in domain:
            continue
        cookies[name] = unquote(value)
    return cookies


def resolve_session_token() -> str | None:
    """
    Figure out the Kick session token to use.

    Kick has no device-code login like Twitch does, so the token has to come from the user:
    they export their browser cookies for kick.com into KICK_COOKIES_PATH.
    """
    if KICK_COOKIES_PATH.exists():
        cookies = parse_netscape_cookies(KICK_COOKIES_PATH)
        if (token := cookies.get(COOKIE_NAME, '')):
            logger.debug(f"Kick: using the session token from {KICK_COOKIES_PATH.name}")
            return token
        logger.warning(
            f"Kick: no '{COOKIE_NAME}' cookie found in {KICK_COOKIES_PATH.name}"
        )
    return None
