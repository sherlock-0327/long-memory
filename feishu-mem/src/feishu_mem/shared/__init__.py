__all__ = [
    "config",
    "Config",
    "ConfigManager",
    "logger",
    "Logger",
    "get_l1_cache",
    "get_cache",
    "LRUCache",
    "CacheEntry",
    "MultiLevelCache",
]


def __getattr__(name):
    if name in {"config", "Config", "ConfigManager"}:
        from .config import Config, ConfigManager, config

        return {"config": config, "Config": Config, "ConfigManager": ConfigManager}[name]

    if name in {"logger", "Logger"}:
        from .logger import Logger, logger

        return {"logger": logger, "Logger": Logger}[name]

    if name in {"get_l1_cache", "get_cache", "LRUCache", "CacheEntry", "MultiLevelCache"}:
        from .cache import CacheEntry, LRUCache, MultiLevelCache, get_cache, get_l1_cache

        return {
            "get_l1_cache": get_l1_cache,
            "get_cache": get_cache,
            "LRUCache": LRUCache,
            "CacheEntry": CacheEntry,
            "MultiLevelCache": MultiLevelCache,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
