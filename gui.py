# CLI версия не нуждается в GUI
# Этот файл служит заглушкой для совместимости с импортами

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


class GUIManager:
    """Заглушка для GUI Manager в CLI версии"""
    
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._close_requested = False
    
    @property
    def close_requested(self) -> bool:
        return self._close_requested
    
    def prevent_close(self):
        self._close_requested = False
    
    def print(self, message: str):
        """Выводит сообщения через логгер вместо GUI"""
        logger.info(message)
    
    def save(self, *, force: bool = False) -> None:
        """Заглушка для сохранения состояния"""
        pass