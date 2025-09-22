# CLI версия не нуждается в кешировании изображений
# Этот файл служит заглушкой для совместимости с импортами

# Пустые классы для совместимости
class ImageCache:
    """Заглушка для кеша изображений в CLI версии"""
    
    def __init__(self, *args, **kwargs):
        pass
    
    def save(self, *, force: bool = False) -> None:
        pass