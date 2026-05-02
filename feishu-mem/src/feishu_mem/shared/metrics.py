"""
监控指标采集与可观测性模块
支持计数器、直方图、计量器等指标类型，提供统一的监控接口
"""
import time
import threading
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from collections import defaultdict
import os

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

from feishu_mem.shared.logger import logger


@dataclass
class Counter:
    """计数器指标：单调递增"""
    name: str
    description: str
    labels: Dict[str, str] = field(default_factory=dict)
    value: int = 0
    created_at: float = field(default_factory=time.time)
    
    def inc(self, amount: int = 1) -> None:
        self.value += amount
    
    def reset(self) -> None:
        self.value = 0


@dataclass
class Gauge:
    """计量器指标：可增可减的瞬时值"""
    name: str
    description: str
    labels: Dict[str, str] = field(default_factory=dict)
    value: float = 0.0
    updated_at: float = field(default_factory=time.time)
    
    def set(self, value: float) -> None:
        self.value = value
        self.updated_at = time.time()
    
    def inc(self, amount: float = 1.0) -> None:
        self.value += amount
        self.updated_at = time.time()
    
    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount
        self.updated_at = time.time()


@dataclass
class Histogram:
    """直方图指标：统计分布情况"""
    name: str
    description: str
    labels: Dict[str, str] = field(default_factory=dict)
    buckets: List[float] = field(default_factory=lambda: [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0])
    counts: Dict[float, int] = field(default_factory=lambda: defaultdict(int))
    sum: float = 0.0
    count: int = 0
    created_at: float = field(default_factory=time.time)
    
    def observe(self, value: float) -> None:
        """观测一个值"""
        self.sum += value
        self.count += 1
        
        # 找到对应的bucket
        for bucket in sorted(self.buckets):
            if value <= bucket:
                self.counts[bucket] += 1
                break
        else:
            # 超过最大bucket
            self.counts["+Inf"] = self.counts.get("+Inf", 0) + 1
    
    def reset(self) -> None:
        self.counts.clear()
        self.sum = 0.0
        self.count = 0


class MetricsCollector:
    """指标采集器，单例模式"""
    _instance: Optional["MetricsCollector"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "MetricsCollector":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_metrics()
        return cls._instance
    
    def _init_metrics(self) -> None:
        """初始化所有指标"""
        self.start_time = time.time()
        
        # 计数器指标
        self.counters: Dict[str, Counter] = {}
        
        # 系统核心指标
        self.create_counter("command_collected_total", "Total number of commands collected")
        self.create_counter("command_added_total", "Total number of commands added to storage")
        self.create_counter("completion_requests_total", "Total number of completion requests")
        self.create_counter("completion_cache_hits_total", "Total number of completion cache hits")
        self.create_counter("completion_cache_misses_total", "Total number of completion cache misses")
        self.create_counter("vector_search_requests_total", "Total number of vector search requests")
        self.create_counter("workflow_executions_total", "Total number of workflow executions")
        self.create_counter("workflow_executions_success_total", "Total number of successful workflow executions")
        self.create_counter("workflow_executions_failed_total", "Total number of failed workflow executions")
        self.create_counter("wal_entries_written_total", "Total number of WAL entries written")
        self.create_counter("wal_entries_replayed_total", "Total number of WAL entries replayed")
        self.create_counter("task_processed_total", "Total number of async tasks processed")
        self.create_counter("task_failed_total", "Total number of failed async tasks")
        self.create_counter("errors_total", "Total number of errors", ["module", "error_type"])
        
        # 直方图指标
        self.histograms: Dict[str, Histogram] = {}
        
        # 延迟指标
        self.create_histogram("command_add_duration_seconds", "Time taken to add a command")
        self.create_histogram("completion_duration_seconds", "Time taken to generate completions")
        self.create_histogram("vector_search_duration_seconds", "Time taken for vector search")
        self.create_histogram("workflow_execution_duration_seconds", "Time taken to execute a workflow")
        self.create_histogram("wal_write_duration_seconds", "Time taken to write to WAL")
        
        # 计量器指标
        self.gauges: Dict[str, Gauge] = {}
        
        self.create_gauge("cache_l1_size", "Current size of L1 cache")
        self.create_gauge("cache_l1_hit_rate", "L1 cache hit rate")
        self.create_gauge("cache_l2_connected", "Whether L2 Redis cache is connected (1=connected, 0=disconnected)")
        self.create_gauge("memory_usage_bytes", "Current process memory usage in bytes")
        self.create_gauge("database_connections", "Number of active database connections")
        self.create_gauge("task_queue_depth", "Current depth of async task queue")
        self.create_gauge("total_commands_stored", "Total number of commands stored in database")
        
        logger.info("Metrics collector initialized")
    
    def create_counter(self, name: str, description: str, labels: List[str] = None) -> Counter:
        """创建计数器指标"""
        counter = Counter(name=name, description=description)
        self.counters[name] = counter
        return counter
    
    def create_gauge(self, name: str, description: str, labels: List[str] = None) -> Gauge:
        """创建计量器指标"""
        gauge = Gauge(name=name, description=description)
        self.gauges[name] = gauge
        return gauge
    
    def create_histogram(self, name: str, description: str, buckets: List[float] = None) -> Histogram:
        """创建直方图指标"""
        if buckets:
            histogram = Histogram(name=name, description=description, buckets=buckets)
        else:
            histogram = Histogram(name=name, description=description)
        self.histograms[name] = histogram
        return histogram
    
    def counter_inc(self, name: str, amount: int = 1, labels: Dict[str, str] = None) -> None:
        """计数器递增"""
        if name in self.counters:
            self.counters[name].inc(amount)
        else:
            logger.debug(f"Counter {name} not found")
    
    def gauge_set(self, name: str, value: float, labels: Dict[str, str] = None) -> None:
        """设置计量器值"""
        if name in self.gauges:
            self.gauges[name].set(value)
        else:
            logger.debug(f"Gauge {name} not found")
    
    def histogram_observe(self, name: str, value: float, labels: Dict[str, str] = None) -> None:
        """观测直方图值"""
        if name in self.histograms:
            self.histograms[name].observe(value)
        else:
            logger.debug(f"Histogram {name} not found")
    
    def record_error(self, module: str, error_type: str) -> None:
        """记录错误"""
        self.counter_inc("errors_total", 1, {"module": module, "error_type": error_type})
    
    def get_metrics(self) -> Dict[str, Any]:
        """获取所有指标的JSON格式"""
        # 先更新动态指标
        self._update_system_metrics()
        
        metrics = {
            "counter": {},
            "gauge": {},
            "histogram": {},
            "system": {
                "uptime_seconds": time.time() - self.start_time,
                "timestamp": time.time()
            }
        }
        
        for name, counter in self.counters.items():
            metrics["counter"][name] = {
                "value": counter.value,
                "description": counter.description,
                "labels": counter.labels
            }
        
        for name, gauge in self.gauges.items():
            metrics["gauge"][name] = {
                "value": gauge.value,
                "description": gauge.description,
                "labels": gauge.labels,
                "updated_at": gauge.updated_at
            }
        
        for name, histogram in self.histograms.items():
            metrics["histogram"][name] = {
                "count": histogram.count,
                "sum": histogram.sum,
                "buckets": histogram.counts,
                "description": histogram.description,
                "labels": histogram.labels
            }
        
        return metrics
    
    def get_prometheus_metrics(self) -> str:
        """获取Prometheus格式的指标"""
        lines = []
        
        metrics = self.get_metrics()
        
        # 计数器
        for name, data in metrics["counter"].items():
            lines.append(f"# HELP {name} {data['description']}")
            lines.append(f"# TYPE {name} counter")
            labels_str = self._format_labels(data["labels"])
            lines.append(f"{name}{labels_str} {data['value']}")
        
        # 计量器
        for name, data in metrics["gauge"].items():
            lines.append(f"# HELP {name} {data['description']}")
            lines.append(f"# TYPE {name} gauge")
            labels_str = self._format_labels(data["labels"])
            lines.append(f"{name}{labels_str} {data['value']}")
        
        # 直方图
        for name, data in metrics["histogram"].items():
            lines.append(f"# HELP {name} {data['description']}")
            lines.append(f"# TYPE {name} histogram")
            labels_str = self._format_labels(data["labels"])
            
            for bucket, count in data["buckets"].items():
                if bucket == "+Inf":
                    lines.append(f'{name}_bucket{{le="+Inf"{labels_str[1:] if labels_str else ""}}} {count}')
                else:
                    lines.append(f'{name}_bucket{{le="{bucket}"{labels_str[1:] if labels_str else ""}}} {count}')
            
            lines.append(f"{name}_sum{labels_str} {data['sum']}")
            lines.append(f"{name}_count{labels_str} {data['count']}")
        
        return "\n".join(lines)
    
    def _format_labels(self, labels: Dict[str, str]) -> str:
        """格式化标签为Prometheus格式"""
        if not labels:
            return ""
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        return "{" + ",".join(parts) + "}"
    
    def _update_system_metrics(self) -> None:
        """更新系统级动态指标"""
        try:
            # 内存使用
            if psutil is None:
                return
            process = psutil.Process(os.getpid())
            memory_info = process.memory_info()
            self.gauge_set("memory_usage_bytes", memory_info.rss)
            
            # 更新缓存指标
            from feishu_mem.shared.cache import get_cache
            cache = get_cache()
            metrics = cache.get_metrics()
            self.gauge_set("cache_l1_size", metrics["l1_size"])
            self.gauge_set("cache_l1_hit_rate", metrics["l1_hit_rate"])
            self.gauge_set("cache_l2_connected", 1 if metrics["l2_connected"] else 0)
            
        except Exception as e:
            logger.debug(f"Failed to update system metrics: {e}")
    
    def reset(self) -> None:
        """重置所有指标（用于测试）"""
        for counter in self.counters.values():
            counter.reset()
        for histogram in self.histograms.values():
            histogram.reset()
        logger.info("Metrics reset")


# 便捷装饰器
def timeit(metric_name: str):
    """方法执行时间统计装饰器"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.perf_counter() - start
                get_metrics().histogram_observe(metric_name, duration)
        return wrapper
    return decorator


# 全局实例
_metrics_collector = None


def get_metrics() -> MetricsCollector:
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector
