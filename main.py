from __future__ import annotations

# import an additional thing for proper PyInstaller freeze support
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import sys
    import signal
    import asyncio
    import logging
    import argparse
    import warnings
    import traceback
    import contextlib
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from kick import KickMiner
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired
    from utils import lock_file
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH
    from telegram_logger import TelegramHandler

    if TYPE_CHECKING:
        pass

    warnings.simplefilter("default", ResourceWarning)

    # import tracemalloc
    # tracemalloc.start(3)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        dump: bool
        kick_check: bool

        # TODO: replace int with union of literal values once typeshed updates
        @property
        def logging_level(self) -> int:
            return LOGGING_LEVELS[min(self._verbose, 4)]

        @property
        def debug_ws(self) -> int:
            """
            If the debug flag is True, return DEBUG.
            If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
            Otherwise, return NOTSET to inherit the global logging level.
            """
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    parser = argparse.ArgumentParser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    parser.add_argument(
        "--kick-check",
        dest="kick_check",
        action="store_true",
        help="list the current Kick drop campaigns and exit (no login required)",
    )

    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )

    default_args = ParsedArgs()
    default_args._verbose = 3
    default_args._debug_ws = False
    default_args._debug_gql = False
    default_args.log = True
    default_args.dump = False
    default_args.kick_check = False

    # парсер только дополняет/переопределяет эти значения, если что-то передано из CLI
    args = parser.parse_args(namespace=default_args)
    
    # load settings
    try:
        settings = Settings(args)
    except Exception:
        print(f"Settings error: {traceback.format_exc()}", file=sys.stderr)
        sys.exit(4)

    # Kick smoke test: fetches the campaign list (and progress, if a token is configured),
    # prints it and exits. Doesn't need the Twitch login, nor the single-instance lock.
    if args.kick_check:
        async def kick_check() -> int:
            from kick.auth import resolve_session_token
            from kick.http import KickHTTP, KickRequestError
            from kick.constants import CAMPAIGNS_URL, PROGRESS_URL, KICK_COOKIES_PATH
            from kick.inventory import parse_campaigns, merge_progress

            http = KickHTTP(settings)
            try:
                campaigns = parse_campaigns(await http.get_json(CAMPAIGNS_URL))
            except KickRequestError as exc:
                print(f"Kick: {exc}", file=sys.stderr)
                return 1
            active = [campaign for campaign in campaigns if campaign.active]
            if (token := resolve_session_token()) is not None:
                http.session_token = token
                try:
                    merge_progress(active, await http.get_json(PROGRESS_URL, auth=True))
                    print("Session token: OK")
                except KickRequestError as exc:
                    print(f"Session token: FAILED ({exc})")
            else:
                print(
                    f"Session token: not found - export your kick.com cookies to "
                    f"{KICK_COOKIES_PATH} (progress unavailable)"
                )
            print(f"Campaigns: {len(active)} active out of {len(campaigns)} total")
            for campaign in active:
                where = (
                    "any channel" if campaign.is_general
                    else ', '.join(campaign.channels[:5]) + (
                        f" (+{len(campaign.channels) - 5})" if len(campaign.channels) > 5 else ''
                    )
                )
                print(f"\n  {campaign.game} | {campaign.name}")
                print(f"    ends: {campaign.ends_at}  channels: {where}")
                for reward in campaign.rewards:
                    status = "claimed" if reward.claimed else f"{reward.progress}"
                    print(f"    - {reward.name}: {status}/{reward.required_units} min")
            return 0

        sys.exit(asyncio.run(kick_check()))

    # client run
    async def main():
        print("Application starting...")  # Тестовое сообщение
        
        # set language
        try:
            _.set_language(settings.language)
        except ValueError:
            # this language doesn't exist - stick to English
            pass


        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(settings.logging_level)

        # Clear any existing handlers to avoid conflicts
        logger.handlers.clear()

        # Setup console handler for CLI output
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "{asctime} {levelname}: {message}",
            style='{',
            datefmt="%H:%M:%S"
        ))

        logger.addHandler(console_handler)

        if settings.log:
            handler = logging.FileHandler(LOG_PATH)
            handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(handler)

        # Setup Telegram handler (if configured)
        telegram_handler: TelegramHandler | None = None
        if settings.telegram_bot_token and settings.telegram_chat_id:
            telegram_handler = TelegramHandler(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                update_interval=3.0,
                tail_lines=50,
            )
            telegram_handler.setFormatter(logging.Formatter(
                "{asctime} {levelname}: {message}",
                style='{',
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(telegram_handler)
            telegram_handler.start()

        # Disable root logger to prevent double messages
        if settings.logging_level > logging.DEBUG:
            logging.getLogger().setLevel(logging.WARNING)
        else:
            logging.getLogger().setLevel(logging.DEBUG)

        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            logger.info(f"Logging level: {logging.getLevelName(logging_level)}")

        exit_status = 0
        client = Twitch(settings)
        # Kick mining runs next to Twitch, in the same loop. It's fully self-contained:
        # its failures are logged and retried internally, and never affect Twitch mining.
        kick_miner: KickMiner | None = None
        kick_task: asyncio.Task[None] | None = None
        if settings.kick_enabled:
            kick_miner = KickMiner(settings)
        loop = asyncio.get_running_loop()

        # Setup signal handlers for clean shutdown
        def signal_handler():
            logger.info("Received shutdown signal, stopping...")
            client.close()
            if kick_miner is not None:
                kick_miner.close()

        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)
        else:
            # На Windows используем обработчик KeyboardInterrupt
            def keyboard_interrupt_handler():
                try:
                    loop.run_forever()
                except KeyboardInterrupt:
                    signal_handler()
        
        try:
            logger.info("Starting Twitch Drops Miner...")
            logger.info("Use Ctrl+C to stop the application")
            if kick_miner is not None:
                kick_task = asyncio.create_task(kick_miner.run())
            await client.run()
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            logger.error(_("error", "captcha"))
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception:
            exit_status = 1
            client.prevent_close()
            logger.error("Fatal error encountered:")
            logger.error(traceback.format_exc())
        finally:
            if sys.platform != "win32":
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            logger.info(_("gui", "status", "exiting"))
            # stop the Kick task before its session gets closed, not the other way around
            if kick_miner is not None:
                kick_miner.close()
            if kick_task is not None:
                kick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await kick_task
            if kick_miner is not None:
                await kick_miner.shutdown()
            if telegram_handler is not None:
                await telegram_handler.async_close()
            await client.shutdown()
            
        if not client.close_requested:
            logger.info(_("status", "terminated"))
            
        sys.exit(exit_status)

    try:
        # use lock_file to check if we're not already running
        success, file = lock_file(LOCK_PATH)
        if not success:
            # already running - exit
            print("Application is already running", file=sys.stderr)
            sys.exit(3)

        if sys.platform == "win32":
            # На Windows запускаем через отдельный обработчик для Ctrl+C
            try:
                asyncio.run(main())
            except KeyboardInterrupt:
                print("\nInterrupted by user")
                sys.exit(0)
        else:
            asyncio.run(main())
    finally:
        file.close()