# CLI версия не нуждается в работе с реестром Windows
# Этот файл служит заглушкой для совместимости с импортами

# Заглушки для классов
class RegistryError(Exception):
    pass

class ValueNotFound(RegistryError):
    pass

class RegistryKey:
    """Заглушка для работы с реестром в CLI версии"""
    
    def __init__(self, *args, **kwargs):
        pass
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass
    
    def get(self, name: str):
        raise ValueNotFound(name)
    
    def set(self, name: str, value_type, value) -> bool:
        return False
    
    def delete(self, name: str, *, silent: bool = False) -> bool:
        if not silent:
            raise ValueNotFound(name)
        return False

# Заглушки для enum'ов
class ValueType:
    REG_SZ = "REG_SZ"