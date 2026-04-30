import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional
from .config import config

class Logger:
    """结构化日志系统"""
    _instance: Optional["Logger"] = None
    _logger: Optional[logging.Logger] = None
    
    def __new__(cls) -> "Logger":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_logger()
        return cls._instance
    
    def _init_logger(self) -> None:
        """初始化日志系统"""
        self._logger = logging.getLogger("feishu-mem")
        self._logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
        self._logger.propagate = False
        
        # 避免重复添加处理器
        if self._logger.handlers:
            return
        
        # 日志格式
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        formatter = logging.Formatter(log_format)
        
        # 控制台处理器（仅在debug模式下）
        if config.debug:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.DEBUG)
            console_handler.setFormatter(formatter)
            self._logger.addHandler(console_handler)
        
        # 文件处理器
        log_file = config.log_dir / "feishu_mem.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        self._logger.addHandler(file_handler)
    
    def debug(self, message: str, **kwargs) -> None:
        """调试日志"""
        if self._logger:
            extra = {f"extra_{k}": v for k, v in kwargs.items()}
            self._logger.debug(message, extra=extra)
    
    def info(self, message: str, **kwargs) -> None:
        """信息日志"""
        if self._logger:
            extra = {f"extra_{k}": v for k, v in kwargs.items()}
            self._logger.info(message, extra=extra)
    
    def warning(self, message: str, **kwargs) -> None:
        """警告日志"""
        if self._logger:
            extra = {f"extra_{k}": v for k, v in kwargs.items()}
            self._logger.warning(message, extra=extra)
    
    def error(self, message: str, exception: Optional[Exception] = None, **kwargs) -> None:
        """错误日志"""
        if self._logger:
            extra = {f"extra_{k}": v for k, v in kwargs.items()}
            if exception:
                self._logger.error(f"{message}: {str(exception)}", exc_info=True, extra=extra)
            else:
                self._logger.error(message, extra=extra)

# 全局日志实例
logger = Logger()
