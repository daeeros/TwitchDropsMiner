from __future__ import annotations

import asyncio
from time import time
from contextlib import suppress
from datetime import timedelta, datetime, timezone
from typing import TYPE_CHECKING

from constants import CALL, MAX_INT, PriorityMode
from kick.auth import resolve_session_token
from kick.watcher import KickWatcher
from kick.inventory import KickCampaign, KickReward, parse_campaigns, merge_progress
from kick.channel import KickChannel, find_live_channels
from kick.http import KickHTTP, KickAuthError, KickRequestError
from kick.constants import (
    CAMPAIGNS_URL,
    PROGRESS_URL,
    CLAIM_URL,
    KICK_COOKIES_PATH,
    PROGRESS_POLL_INTERVAL,
    NO_CAMPAIGNS_DELAY,
    NO_CHANNELS_DELAY,
    ERROR_DELAY,
    AUTH_RETRY_DELAY,
    MAX_STALLED_POLLS,
    logger,
)

if TYPE_CHECKING:
    from settings import Settings


CLAIM_FAIL_COOLDOWN = timedelta(minutes=5)


class KickMiner:
    """
    Mines Kick drops next to the Twitch miner, in the same process.

    The flow mirrors the Twitch side: read the campaigns, decide what's worth mining, pick a
    live channel for it, then keep reporting watch time until there's a reason to move on.
    Everything is contained - a failure here logs and retries, it never touches Twitch mining.
    """

    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        self.http: KickHTTP = KickHTTP(settings)
        self.watcher: KickWatcher = KickWatcher(settings, self.http)
        self.campaigns: list[KickCampaign] = []
        self._closed = asyncio.Event()
        self._warned_no_token: bool = False
        self._claim_failures: dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._closed.set()

    @property
    def close_requested(self) -> bool:
        return self._closed.is_set()

    async def shutdown(self) -> None:
        self.close()
        await self.watcher.close()

    async def _sleep(self, delay: timedelta | float) -> None:
        """
        Sleep, but wake up immediately if we've been asked to close.
        """
        timeout = delay.total_seconds() if isinstance(delay, timedelta) else delay
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._closed.wait(), timeout=timeout)

    async def run(self) -> None:
        logger.info("Kick: miner started")
        try:
            while not self.close_requested:
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except KickAuthError as exc:
                    logger.error(f"Kick: {exc}")
                    self.http.session_token = None
                    self._warned_no_token = False
                    await self._sleep(AUTH_RETRY_DELAY)
                except KickRequestError as exc:
                    logger.warning(f"Kick: {exc}")
                    await self._sleep(ERROR_DELAY)
                except Exception:
                    logger.exception("Kick: unexpected error in the mining loop")
                    await self._sleep(ERROR_DELAY)
        finally:
            await self.watcher.close()
            logger.info("Kick: miner stopped")

    # ------------------------------------------------------------------ one full cycle

    async def _cycle(self) -> None:
        if not self._ensure_token():
            await self._sleep(AUTH_RETRY_DELAY)
            return
        self.campaigns = await self.fetch_campaigns()
        wanted = self._wanted_campaigns()
        if not wanted:
            logger.info("Kick: no campaigns to mine, waiting...")
            await self._sleep(NO_CAMPAIGNS_DELAY)
            return
        # anything already earned while we weren't looking gets claimed before we start
        await self._claim_ready(wanted)
        wanted = [campaign for campaign in wanted if not campaign.finished]
        if not wanted:
            return
        target = await self._pick_channel(wanted)
        if target is None:
            logger.info("Kick: no live channels for the wanted campaigns, waiting...")
            await self._sleep(NO_CHANNELS_DELAY)
            return
        channel, campaign = target
        await self._watch(channel, campaign)

    def _ensure_token(self) -> bool:
        token = resolve_session_token()
        if token is None:
            if not self._warned_no_token:
                logger.error(
                    "Kick: no session token available - mining is disabled. Export your "
                    f"kick.com cookies to {KICK_COOKIES_PATH.name}, next to the application."
                )
                self._warned_no_token = True
            return False
        self._warned_no_token = False
        self.http.session_token = token
        return True

    # ------------------------------------------------------------------ campaigns

    async def fetch_campaigns(self) -> list[KickCampaign]:
        campaigns = parse_campaigns(await self.http.get_json(CAMPAIGNS_URL))
        active = [campaign for campaign in campaigns if campaign.active]
        await self.refresh_progress(active)
        logger.info(
            f"Kick: {len(active)} active campaign(s) out of {len(campaigns)} total"
        )
        return active

    async def refresh_progress(self, campaigns: list[KickCampaign]) -> None:
        if not campaigns:
            return
        merge_progress(campaigns, await self.http.get_json(PROGRESS_URL, auth=True))

    def _wanted_campaigns(self) -> list[KickCampaign]:
        """
        Pick the campaigns worth mining.

        Unlike the Twitch side, Kick always mines strictly what's listed in 'priority' -
        the 'priority_mode' setting doesn't loosen that. Kick usually runs only a couple of
        campaigns at a time, so an explicit list is the whole selection, not a preference.
        The mode still decides the order among the listed games.
        """
        exclude: set[str] = self.settings.exclude
        priority: list[str] = self.settings.priority
        priority_mode: PriorityMode = self.settings.priority_mode
        now = datetime.now(timezone.utc)

        wanted: list[KickCampaign] = [
            campaign for campaign in self.campaigns
            if not campaign.finished
            and campaign.game in priority
            and campaign.game not in exclude
        ]
        if priority_mode is PriorityMode.ENDING_SOONEST:
            wanted.sort(
                key=lambda c: (c.ends_at - now).total_seconds() if c.ends_at else MAX_INT
            )
        elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
            # Kick has no "availability" - the closest analogue is the cheapest reward left
            wanted.sort(key=lambda c: _shortest_requirement(c))
        # the priority list always wins over the mode's ordering
        wanted.sort(key=lambda c: priority.index(c.game))
        return wanted

    # ------------------------------------------------------------------ channel selection

    async def _pick_channel(
        self, campaigns: list[KickCampaign]
    ) -> tuple[KickChannel, KickCampaign] | None:
        """
        Campaigns come in already ordered by preference. For each, try its own channels
        first, then - if it accepts any channel - whoever is streaming its category.
        """
        for campaign in campaigns:
            for slug in campaign.channels:
                channel = KickChannel(slug)
                if await channel.refresh(self.http) and channel.watchable:
                    return channel, campaign
            if campaign.is_general and campaign.category_id is not None:
                for slug in await find_live_channels(self.http, campaign.category_id):
                    channel = KickChannel(slug)
                    if await channel.refresh(self.http) and channel.watchable:
                        return channel, campaign
        return None

    # ------------------------------------------------------------------ watching

    async def _watch(self, channel: KickChannel, campaign: KickCampaign) -> None:
        reward = campaign.first_unearned
        target = f"{reward.required_units} min" if reward is not None else "?"
        logger.info(
            f"Kick: watching {channel.slug} for \"{campaign.name}\" ({campaign.game}, {target})"
        )
        stop_event = asyncio.Event()
        watch_task = asyncio.create_task(self.watcher.run(channel, stop_event))
        last_progress: int = campaign.total_progress
        stalled: int = 0
        try:
            while not self.close_requested:
                await self._sleep(PROGRESS_POLL_INTERVAL)
                if self.close_requested:
                    break
                if watch_task.done():
                    logger.info(f"Kick: watch connection to {channel.slug} ended")
                    break

                await self.refresh_progress([campaign])
                reward = campaign.first_unearned
                current = campaign.total_progress
                if current != last_progress:
                    # any movement counts, including the step back that happens when a reward
                    # is completed and the counter starts tracking the next one
                    if current > last_progress and reward is not None:
                        logger.log(
                            CALL,
                            f"Kick drop progress: {reward.name} "
                            f"({campaign.game}, {current}/{reward.required_units})"
                        )
                    stalled = 0
                    last_progress = current
                else:
                    stalled += 1
                    if stalled >= MAX_STALLED_POLLS:
                        logger.warning(
                            f"Kick: no progress from {channel.slug} for {stalled} minutes "
                            "despite sending watch events - switching channels"
                        )
                        break

                await self._claim_ready([campaign])
                if campaign.finished:
                    logger.info(f"Kick: \"{campaign.name}\" is done")
                    break

                if not await channel.refresh(self.http):
                    logger.info(f"Kick: {channel.slug} went offline")
                    break
                if not campaign.is_general and not channel.matches_category(campaign.category_id):
                    logger.info(f"Kick: {channel.slug} switched away from {campaign.game}")
                    break
        finally:
            # give the websocket a moment to close on its own before pulling the rug out
            stop_event.set()
            if not watch_task.done():
                with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(watch_task, timeout=5)

    # ------------------------------------------------------------------ claiming

    async def _claim_ready(self, campaigns: list[KickCampaign]) -> bool:
        claimed_any = False
        for campaign in campaigns:
            for reward in campaign.claimable_rewards:
                if self._on_claim_cooldown(reward):
                    continue
                if await self._claim(campaign, reward):
                    claimed_any = True
        return claimed_any

    def _on_claim_cooldown(self, reward: KickReward) -> bool:
        failed_at = self._claim_failures.get(reward.id)
        if failed_at is None:
            return False
        if time() - failed_at >= CLAIM_FAIL_COOLDOWN.total_seconds():
            del self._claim_failures[reward.id]
            return False
        return True

    async def _claim(self, campaign: KickCampaign, reward: KickReward) -> bool:
        try:
            await self.http.post_json(
                CLAIM_URL,
                {"campaign_id": campaign.id, "reward_id": reward.id},
                auth=True,
            )
        except KickAuthError:
            raise
        except KickRequestError as exc:
            logger.warning(f"Kick: couldn't claim \"{reward.name}\": {exc}")
            self._claim_failures[reward.id] = time()
            return False
        reward.claimed = True
        self._claim_failures.pop(reward.id, None)
        logger.info(f"Kick: claimed drop \"{reward.name}\" ({campaign.game})")
        return True


def _shortest_requirement(campaign: KickCampaign) -> int:
    requirements = [
        reward.required_units for reward in campaign.remaining_rewards if reward.required_units > 0
    ]
    return min(requirements) if requirements else MAX_INT
