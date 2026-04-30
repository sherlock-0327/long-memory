import time
from typing import Any, Optional, Dict
from dataclasses import dataclass
from .config import config

@dataclass
class CacheItem:
    """缓存项"""
    value: Any
    expire_at: float  # 过期时间戳

class LRUCache:
    """LRU缓存实现"""
    def __init__(self, max_size: int, ttl: int):
        self.max_size = max_size
        self.ttl = ttl
        self.cache: Dict[str, CacheItem] = {}
        self.access_order: Dict[str, float] = {}  # key -> 最后访问时间
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存项"""
        if key not in self.cache:
            return None
        
        item = self.cache[key]
        if time.time() > item.expire_at:
            # 缓存已过期
            del self.cache[key]
            del self.access_order[key]
            return None
        
        # 更新访问时间
        self.access_order[key] = time.time()
        return item.value
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存项"""
        if ttl is None:
            ttl = self.ttl
        
        # 如果缓存已满，删除最久未使用的项
        if len(self.cache) >= self.max_size and key not in self.cache:
            lru_key = min(self.access_order.items(), key=lambda x: x[1])[0]
            del self.cache[lru_key]
            del self.access_order[lru_key]
        
        self.cache[key] = CacheItem(
            value=value,
            expire_at=time.time() + ttl
        )
        self.access_order[key] = time.time()
    
    def delete(self, key: str) -> None:
        """删除缓存项"""
        if key in self.cache:
            del self.cache[key]
            del self.access_order[key]
    
    def clear(self) -> None:
        """清空缓存"""
        self.cache.clear()
        self.access_order.clear()
    
    def cleanup_expired(self) -> int:
        """清理过期缓存，返回清理的数量"""
        now = time.time()
        expired_keys = [
            key for key, item in self.cache.items()
            if now > item.expire_at
        ]
        
        for key in expired_keys:
            del self.cache[key]
            del self.access_order[key]
        
        return len(expired_keys)

# 全局缓存实例
l1_cache = LRUCache(max_size=config.l1_cache_size, ttl=config.l1_cache_ttl)
