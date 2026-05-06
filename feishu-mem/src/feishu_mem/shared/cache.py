"""
多级缓存实现，遵循架构设计：
L1缓存：进程内存LRU缓存，TTL=5分钟，容量1000条，延迟<1ms
L2缓存：Redis分布式缓存，TTL=1小时，容量10000条，延迟<5ms
L3缓存：SQLite FTS5全文索引，延迟<50ms
"""
import time
import json
import hashlib
from typing import Any, Optional, Dict
from dataclasses import dataclass
from collections import OrderedDict

try:
    import redis
except ImportError:
    redis = None  # type: ignore[assignment]

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger


@dataclass
class CacheEntry:
    """缓存条目"""
    value: Any
    expires_at: float
    created_at: float


class LRUCache:
    """LRU内存缓存实现"""
    def __init__(self, capacity: int, default_ttl: int):
        self.capacity = capacity
        self.default_ttl = default_ttl
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        
    def get(self, key: str) -> Optional[Any]:
        """获取缓存值，不存在或过期返回None"""
        if key not in self.cache:
            self.misses += 1
            return None
            
        entry = self.cache[key]
        if time.time() > entry.expires_at:
            del self.cache[key]
            self.misses += 1
            return None
            
        # 移到末尾表示最近使用
        self.cache.move_to_end(key)
        self.hits += 1
        return entry.value
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存值"""
        ttl = ttl or self.default_ttl
        expires_at = time.time() + ttl
        
        if key in self.cache:
            self.cache.move_to_end(key)
        elif len(self.cache) >= self.capacity:
            # 删除最久未使用的
            self.cache.popitem(last=False)
            
        self.cache[key] = CacheEntry(
            value=value,
            expires_at=expires_at,
            created_at=time.time()
        )
    
    def delete(self, key: str) -> None:
        """删除缓存"""
        if key in self.cache:
            del self.cache[key]
    
    def invalidate_pattern(self, pattern: str) -> int:
        """按前缀模式失效缓存，返回删除数量"""
        deleted = 0
        keys_to_delete = [k for k in self.cache.keys() if k.startswith(pattern)]
        for key in keys_to_delete:
            del self.cache[key]
            deleted += 1
        return deleted
    
    def clear(self) -> None:
        """清空缓存"""
        self.cache.clear()
    
    def get_hit_rate(self) -> float:
        """获取缓存命中率"""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0
    
    def __len__(self) -> int:
        return len(self.cache)


class RedisCache:
    """Redis分布式缓存实现"""
    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0, password: Optional[str] = None, default_ttl: int = 3600):
        self.default_ttl = default_ttl
        self.connected = False
        self.client = None

        if redis is None:
            logger.warning("redis package not installed, L2 cache disabled")
            return

        try:
            self.client = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                socket_timeout=0.005,  # 5ms超时，超时直接降级到L1+L3
                socket_connect_timeout=0.01
            )
            # 测试连接
            self.client.ping()
            self.connected = True
            logger.info("Redis cache connected successfully")
        except Exception as e:
            logger.warning(f"Redis connection failed, will use in-memory cache only: {e}")
            self.connected = False
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存值"""
        if not self.connected:
            return None
            
        try:
            value = self.client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except Exception as e:
            logger.debug(f"Redis get failed: {e}")
            return None
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存值"""
        if not self.connected:
            return
            
        try:
            ttl = ttl or self.default_ttl
            serialized = json.dumps(value, default=str)
            self.client.setex(key, ttl, serialized)
        except Exception as e:
            logger.debug(f"Redis set failed: {e}")
    
    def delete(self, key: str) -> None:
        """删除缓存"""
        if not self.connected:
            return
            
        try:
            self.client.delete(key)
        except Exception as e:
            logger.debug(f"Redis delete failed: {e}")
    
    def delete_pattern(self, pattern: str) -> int:
        """按模式删除缓存"""
        if not self.connected:
            return 0
            
        try:
            keys = self.client.keys(pattern + "*")
            if keys:
                return self.client.delete(*keys)
            return 0
        except Exception as e:
            logger.debug(f"Redis delete pattern failed: {e}")
            return 0


class MultiLevelCache:
    """多级缓存统一接口"""
    def __init__(self):
        self.l1 = LRUCache(
            capacity=config.l1_cache_size,
            default_ttl=config.l1_cache_ttl
        )
        self.l2 = RedisCache(
            default_ttl=config.l2_cache_ttl
        )
        logger.info("Multi-level cache initialized")
        logger.info(f"L1 cache: size={config.l1_cache_size}, ttl={config.l1_cache_ttl}s")
        logger.info(f"L2 cache: enabled={self.l2.connected}, ttl={config.l2_cache_ttl}s")
    
    def get(self, key: str) -> Optional[Any]:
        """
        多级缓存查询：
        1. 先查L1内存缓存，命中直接返回
        2. 再查L2 Redis缓存，命中则回填L1并返回
        3. 都未命中返回None，由调用方查询L3/SQLite
        """
        # L1查询
        value = self.l1.get(key)
        if value is not None:
            logger.debug(f"L1 cache hit for key: {key}")
            return value
        
        # L2查询
        if self.l2.connected:
            value = self.l2.get(key)
            if value is not None:
                logger.debug(f"L2 cache hit for key: {key}")
                # 回填L1
                self.l1.set(key, value)
                return value
        
        logger.debug(f"Cache miss for key: {key}")
        return None
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """写入缓存，同时写入L1和L2"""
        self.l1.set(key, value, ttl)
        if self.l2.connected:
            self.l2.set(key, value, ttl)
        logger.debug(f"Cache set for key: {key}")
    
    def delete(self, key: str) -> None:
        """删除缓存，同时删除L1和L2"""
        self.l1.delete(key)
        if self.l2.connected:
            self.l2.delete(key)
        logger.debug(f"Cache deleted for key: {key}")
    
    def invalidate(self, pattern: str) -> int:
        """按前缀失效缓存，返回总删除数量"""
        l1_deleted = self.l1.invalidate_pattern(pattern)
        l2_deleted = self.l2.delete_pattern(pattern) if self.l2.connected else 0
        total = l1_deleted + l2_deleted
        logger.debug(f"Cache invalidated for pattern: {pattern}, deleted: {total}")
        return total
    
    def get_metrics(self) -> Dict[str, Any]:
        """获取缓存指标"""
        return {
            "l1_size": len(self.l1),
            "l1_hit_rate": self.l1.get_hit_rate(),
            "l1_hits": self.l1.hits,
            "l1_misses": self.l1.misses,
            "l2_connected": self.l2.connected
        }
    
    def generate_key(self, *parts: str) -> str:
        """生成缓存key，自动哈希处理长key"""
        key_content = ":".join(str(p) for p in parts)
        if len(key_content) > 100:
            return f"hash:{hashlib.sha256(key_content.encode()).hexdigest()[:32]}"
        return key_content


# 全局缓存实例
_l1_cache = None
_multi_level_cache = None


def get_l1_cache() -> LRUCache:
    """获取L1内存缓存实例"""
    global _l1_cache
    if _l1_cache is None:
        _l1_cache = LRUCache(
            capacity=config.l1_cache_size,
            default_ttl=config.l1_cache_ttl
        )
    return _l1_cache


def get_cache() -> MultiLevelCache:
    """获取多级缓存实例"""
    global _multi_level_cache
    if _multi_level_cache is None:
        _multi_level_cache = MultiLevelCache()
    return _multi_level_cache
