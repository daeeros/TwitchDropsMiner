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
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired
    from utils import lock_file
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH

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

    # handle input parameters
    parser = argparse.ArgumentParser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    # undocumented debug args
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(namespace=ParsedArgs())
    
    # load settings
    try:
        settings = Settings(args)
    except Exception:
        print(f"Settings error: {traceback.format_exc()}", file=sys.stderr)
        sys.exit(4)

    # client run
    async def main():
        print("Application starting...")  # Тестовое сообщение
        
        # # set language
        # try:
        #     _.set_language(settings.language)
        # except ValueError:
        #     # this language doesn't exist - stick to English
        #     pass

        print("1")
        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(settings.logging_level)
        print("2")
        # Clear any existing handlers to avoid conflicts
        logger.handlers.clear()
        print("3")
        # Setup console handler for CLI output
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "{asctime} {levelname}: {message}",
            style='{',
            datefmt="%H:%M:%S"
        ))
        print("4")
        logger.addHandler(console_handler)
        print("5")
        if settings.log:
            handler = logging.FileHandler(LOG_PATH)
            handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(handler)
        print("6")
        # Disable root logger to prevent double messages
        if settings.logging_level > logging.DEBUG:
            logging.getLogger().setLevel(logging.WARNING)
        else:
            logging.getLogger().setLevel(logging.DEBUG)
        print("7")
        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)
        print("8")
        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            logger.info(f"Logging level: {logging.getLevelName(logging_level)}")

        print("9")
        exit_status = 0
        client = Twitch(settings)
        loop = asyncio.get_running_loop()

        print("10")
        # Setup signal handlers for clean shutdown
        def signal_handler():
            logger.info("Received shutdown signal, stopping...")
            client.close()

        print("11")
        
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

        print("12")
        
        try:
            logger.info("Starting Twitch Drops Miner...")
            logger.info("Use Ctrl+C to stop the application")
            await client.run()
            print("13")
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            logger.error(_("error", "captcha"))
            print("14")
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception:
            exit_status = 1
            client.prevent_close()
            logger.error("Fatal error encountered:")
            logger.error(traceback.format_exc())
            print("15")
        finally:
            if sys.platform != "win32":
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            logger.info(_("gui", "status", "exiting"))
            await client.shutdown()
            print("16")
            
        if not client.close_requested:
            logger.info(_("status", "terminated"))

        print("17")
        # save the application state
        client.save(force=True)
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