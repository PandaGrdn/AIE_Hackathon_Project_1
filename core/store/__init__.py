from .base import Store
from .memory import MemoryStore


def get_store() -> Store:
    from core.config import settings

    if settings.use_memory_store or not settings.mongodb_uri:
        return MemoryStore()
    from .mongo import MongoStore

    return MongoStore(settings.mongodb_uri, settings.mongodb_db)


__all__ = ["Store", "MemoryStore", "get_store"]
