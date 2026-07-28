from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from utils import timestamp
from kick.constants import logger

if TYPE_CHECKING:
    from constants import JsonType


def _as_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return timestamp(value)
    except ValueError:
        logger.debug(f"Kick: unrecognized timestamp format: {value!r}")
        return None


class KickReward:
    """
    A single reward of a campaign. 'required_units' is the watch time in minutes.
    """
    __slots__ = ("id", "name", "required_units", "progress", "claimed")

    def __init__(self, data: JsonType):
        self.id: str = str(data.get("id", ''))
        self.name: str = data.get("name") or "Unknown reward"
        self.required_units: int = int(data.get("required_units") or 0)
        # filled in from the progress endpoint
        self.progress: int = 0
        self.claimed: bool = False

    def __repr__(self) -> str:
        return f"KickReward({self.name}, {self.progress}/{self.required_units})"

    @property
    def earned(self) -> bool:
        """
        Whether the watch time requirement has been met, claimed or not.
        """
        return self.claimed or (self.required_units > 0 and self.progress >= self.required_units)

    @property
    def claimable(self) -> bool:
        return not self.claimed and self.required_units > 0 and self.progress >= self.required_units

    def update_progress(self, data: JsonType, campaign_progress: int) -> None:
        self.claimed = bool(data.get("claimed", False))
        required = data.get("required_units")
        if required is not None:
            self.required_units = int(required)
        progress = data.get("progress")
        # not every payload carries per-reward progress - fall back to the campaign-wide counter
        self.progress = int(campaign_progress if progress is None else progress)


class KickCampaign:
    """
    A drops campaign. Campaigns with an empty channel list ("general" ones) can be mined
    on any channel streaming the campaign's category, while the rest require one of their own.
    """
    __slots__ = (
        "id", "name", "game", "category_id", "status",
        "starts_at", "ends_at", "channels", "rewards", "progress_units",
    )

    def __init__(self, data: JsonType):
        self.id: str = str(data.get("id", ''))
        self.name: str = data.get("name") or "Unknown campaign"
        category: JsonType = data.get("category") or {}
        self.game: str = category.get("name") or "Unknown game"
        self.category_id: int | None = category.get("id")
        self.status: str = data.get("status") or "unknown"
        self.starts_at: datetime | None = _as_datetime(data.get("starts_at"))
        self.ends_at: datetime | None = _as_datetime(data.get("ends_at"))
        self.channels: list[str] = [
            slug for channel in (data.get("channels") or [])
            if isinstance(channel, dict) and (slug := channel.get("slug"))
        ]
        self.rewards: list[KickReward] = [
            KickReward(reward) for reward in (data.get("rewards") or [])
            if isinstance(reward, dict)
        ]
        self.progress_units: int = 0

    def __repr__(self) -> str:
        return f"KickCampaign({self.game}: {self.name}, {len(self.rewards)} rewards)"

    @property
    def is_general(self) -> bool:
        """
        `True` when any channel streaming the campaign's category counts towards it.
        """
        return not self.channels

    @property
    def active(self) -> bool:
        if self.status != "active" or self.category_id is None:
            return False
        now = datetime.now(timezone.utc)
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.ends_at is not None and now >= self.ends_at:
            return False
        return True

    @property
    def remaining_rewards(self) -> list[KickReward]:
        return [reward for reward in self.rewards if not reward.earned]

    @property
    def claimable_rewards(self) -> list[KickReward]:
        return [reward for reward in self.rewards if reward.claimable]

    @property
    def finished(self) -> bool:
        return not self.remaining_rewards

    @property
    def first_unearned(self) -> KickReward | None:
        """
        The reward we're effectively mining right now - the cheapest one still unearned.
        """
        remaining = self.remaining_rewards
        if not remaining:
            return None
        return min(remaining, key=lambda reward: reward.required_units or 0)

    @property
    def total_progress(self) -> int:
        """
        Best available "minutes watched" number for this campaign.
        """
        if (reward := self.first_unearned) is not None and reward.progress:
            return reward.progress
        return self.progress_units

    def update_progress(self, data: JsonType) -> None:
        """
        Merge in one entry of the /drops/progress payload.
        """
        self.progress_units = int(data.get("progress_units") or 0)
        by_id = {reward.id: reward for reward in self.rewards}
        for reward_data in (data.get("rewards") or []):
            if not isinstance(reward_data, dict):
                continue
            reward_id = str(reward_data.get("reward_id") or reward_data.get("id") or '')
            if (reward := by_id.get(reward_id)) is not None:
                reward.update_progress(reward_data, self.progress_units)


def parse_campaigns(campaigns_response: JsonType) -> list[KickCampaign]:
    data = campaigns_response.get("data") if isinstance(campaigns_response, dict) else None
    if not isinstance(data, list):
        return []
    return [KickCampaign(entry) for entry in data if isinstance(entry, dict)]


def merge_progress(campaigns: list[KickCampaign], progress_response: JsonType | None) -> None:
    """
    Apply a /drops/progress response onto already parsed campaigns, in place.
    """
    if isinstance(progress_response, dict):
        entries = progress_response.get("data")
    else:
        entries = progress_response
    if not isinstance(entries, list):
        return
    by_id = {campaign.id: campaign for campaign in campaigns}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        campaign_id = str(entry.get("campaign_id") or entry.get("id") or '')
        if (campaign := by_id.get(campaign_id)) is not None:
            campaign.update_progress(entry)
