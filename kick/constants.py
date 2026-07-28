from __future__ import annotations

import logging
from pathlib import Path
from datetime import timedelta

from constants import WORKING_DIR

# Logger: a child of the main "TwitchDrops" logger, so it inherits every handler
# main.py sets up - console, log file and Telegram - without any extra wiring.
logger = logging.getLogger("TwitchDrops.kick")

# Paths
KICK_COOKIES_PATH = Path(WORKING_DIR, "kick_cookies.txt")

# Endpoints
CAMPAIGNS_URL = "https://web.kick.com/api/v1/drops/campaigns"
PROGRESS_URL = "https://web.kick.com/api/v1/drops/progress"
CLAIM_URL = "https://web.kick.com/api/v1/drops/claim"
CHANNEL_URL = "https://kick.com/api/v2/channels/{slug}"
LIVESTREAMS_URL = (
    "https://web.kick.com/api/v1/livestreams"
    "?limit={limit}&sort=viewer_count_desc&category_id={category_id}"
)
WS_TOKEN_URL = "https://websockets.kick.com/viewer/v1/token"
WS_CONNECT_URL = "wss://websockets.kick.com/viewer/v1/connect?token={token}"

# NOTE: Kick's WAF is picky about what a request looks like. This exact header set is known
# to pass, while a more "complete" browser-like set (Sec-Fetch-*, a full Chrome UA) does not.
# Do not extend this without testing against the live API first.
KICK_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://kick.com",
    "Referer": "https://kick.com/",
}
# The websockets.kick.com host is gated on this header - without it, even an otherwise valid
# request is rejected by the WAF. It's a public constant of Kick's web client, not a secret.
KICK_CLIENT_TOKEN = "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823"

# Websocket message types
WS_TYPE_PING = "ping"
WS_TYPE_HANDSHAKE = "channel_handshake"
WS_TYPE_USER_EVENT = "user_event"
WS_WATCH_EVENT = "tracking.user.watch.livestream"

# Intervals and delays
WATCH_EVENT_INTERVAL = timedelta(seconds=60)  # how often the watch event is sent
WS_KEEPALIVE_INTERVAL = timedelta(seconds=10)  # ping/handshake cadence in between
PROGRESS_POLL_INTERVAL = timedelta(seconds=60)
LIVE_CHECK_INTERVAL = timedelta(seconds=60)
NO_CAMPAIGNS_DELAY = timedelta(minutes=5)
NO_CHANNELS_DELAY = timedelta(minutes=1)
ERROR_DELAY = timedelta(minutes=1)
AUTH_RETRY_DELAY = timedelta(minutes=15)

# How many consecutive progress polls may show no movement before we give up on the channel.
# Kick credits a watched minute a little late, so this needs slack over the poll interval.
MAX_STALLED_POLLS = 5
# How many live streamers to consider when a campaign accepts any channel of its category
LIVESTREAMS_LIMIT = 24
