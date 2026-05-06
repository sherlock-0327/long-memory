import os
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv
import platformdirs

@dataclass
class Config:
    """全局配置类"""
    # 基础配置
    app_name: str = "feishu-mem"
    app_author: str = "feishu"
    debug: bool = False
    log_level: str = "INFO"
    
    # 存储配置
    db_path: Optional[Path] = None
    data_dir: Path = field(default_factory=lambda: Path(platformdirs.user_data_dir("feishu-mem", "feishu")))
    cache_dir: Path = field(default_factory=lambda: Path(platformdirs.user_cache_dir("feishu-mem", "feishu")))
    log_dir: Path = field(default_factory=lambda: Path(platformdirs.user_log_dir("feishu-mem", "feishu")))
    
    # 补全配置
    completion_limit: int = 5
    prefix_match_limit: int = 20
    search_limit: int = 20
    min_prefix_length: int = 2
    
    # 缓存配置
    l1_cache_size: int = 100  # 内存缓存大小
    l1_cache_ttl: int = 300  # 5分钟
    l2_cache_size: int = 1000
    l2_cache_ttl: int = 3600  # 1小时
    
    # 记忆配置
    max_memory_days: int = 365  # 最长保留时间
    temporary_memory_days: int = 7  # 临时记忆保留时间
    short_term_memory_days: int = 30  # 短期记忆保留时间
    explicit_memory_protected: bool = True  # 显式记忆永不自动删除
    
    # 敏感信息配置
    enable_sensitive_filter: bool = True
    custom_sensitive_patterns: list = field(default_factory=list)
    
    # 钩子配置
    hook_async_execution: bool = True
    hook_timeout: int = 1000  # 钩子超时时间(ms)
    
    # 向量存储配置
    vector_db_path: Optional[Path] = None
    embedding_model_name: str = "all-MiniLM-L6-v2"  # 轻量级embedding模型，速度快精度适中
    embedding_device: str = "cpu"  # 可选cuda/mps
    vector_search_enabled: bool = True
    vector_search_limit: int = 10
    vector_search_min_score: float = 0.2  # 最低匹配得分
    
    def __post_init__(self):
        """初始化后处理"""
        # 创建必要目录
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 设置默认数据库路径
        if self.db_path is None:
            self.db_path = self.data_dir / "feishu_mem.db"
        
        # 设置默认向量数据库路径
        if self.vector_db_path is None:
            self.vector_db_path = self.data_dir / "chromadb"

class ConfigManager:
    """配置管理器"""
    _instance: Optional["ConfigManager"] = None
    _config: Optional[Config] = None
    
    def __new__(cls) -> "ConfigManager":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance
    
    def _load_config(self) -> None:
        """加载配置，优先级：环境变量 > 配置文件 > 默认值"""
        # 加载环境变量
        load_dotenv()
        
        config_kwargs = {}
        
        # 从环境变量加载配置
        if os.getenv("FEISHU_MEM_DEBUG"):
            config_kwargs["debug"] = os.getenv("FEISHU_MEM_DEBUG", "").lower() in ("true", "1", "yes")
        
        if os.getenv("FEISHU_MEM_LOG_LEVEL"):
            config_kwargs["log_level"] = os.getenv("FEISHU_MEM_LOG_LEVEL", "INFO")
        
        if os.getenv("FEISHU_MEM_DB_PATH"):
            config_kwargs["db_path"] = Path(os.getenv("FEISHU_MEM_DB_PATH"))
        
        if os.getenv("FEISHU_MEM_DATA_DIR"):
            config_kwargs["data_dir"] = Path(os.getenv("FEISHU_MEM_DATA_DIR"))

        if os.getenv("FEISHU_MEM_CACHE_DIR"):
            config_kwargs["cache_dir"] = Path(os.getenv("FEISHU_MEM_CACHE_DIR"))

        if os.getenv("FEISHU_MEM_LOG_DIR"):
            config_kwargs["log_dir"] = Path(os.getenv("FEISHU_MEM_LOG_DIR"))
        
        # 加载用户配置文件
        config_file = Path.home() / ".feishu-mem" / "config.json"
        if config_file.exists():
            import json
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
                    config_kwargs.update(file_config)
            except Exception:
                pass  # 配置文件损坏时忽略
        
        self._config = Config(**config_kwargs)
    
    def get(self) -> Config:
        """获取配置实例"""
        if self._config is None:
            self._load_config()
        return self._config
    
    def update(self, updates: Dict[str, Any]) -> None:
        """更新配置并持久化到配置文件"""
        if self._config is None:
            self._load_config()
        
        # 更新配置
        for key, value in updates.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
        
        # 持久化到配置文件
        config_file = Path.home() / ".feishu-mem" / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        
        config_dict = {
            k: v for k, v in self._config.__dict__.items()
            if not k.startswith("_") and not isinstance(v, Path)
        }
        
        import json
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)

# 全局配置实例
config = ConfigManager().get()
