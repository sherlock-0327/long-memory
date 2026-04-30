#!/usr/bin/env python3
"""
Feishu-Mem 生产级启动脚本
实现非阻塞后台Worker服务，处理异步任务
"""
import os
import sys
import time
import signal
import logging
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from feishu_mem.core.task_queue import AsyncTaskQueue, TaskWorker
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

class WorkerService:
    """后台Worker服务"""
    def __init__(self):
        self.running = False
        self.queue = AsyncTaskQueue()
        self.worker = TaskWorker(self.queue, worker_id="main_worker")
        
        # 注册任务处理器
        self._register_handlers()
        
        # 注册信号处理
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
    
    def _register_handlers(self):
        """注册任务处理器"""
        # 向量嵌入生成任务
        def handle_embedding_generation(payload):
            from feishu_mem.core.vector_store import VectorStore
            from feishu_mem.core.storage import Storage
            
            vector_store = VectorStore()
            storage = Storage()
            
            command_id = payload.get("command_id")
            if not command_id:
                logger.warning("Missing command_id in embedding task")
                return
            
            records = storage.get_commands_by_ids([command_id])
            if records:
                vector_store.add_command(records[0])
                logger.debug(f"Generated embedding for command: {command_id}")
            
            return {"status": "success"}
        
        self.worker.register_handler("generate_embedding", handle_embedding_generation)
        
        # 模式分析任务
        def handle_pattern_analysis(payload):
            # TODO: 实现模式分析任务处理
            user_id = payload.get("user_id", "unknown")
            logger.debug(f"Running pattern analysis for user: {user_id}")
            return {"status": "success"}
        
        self.worker.register_handler("pattern_analysis", handle_pattern_analysis)
        
        # 工作流发现任务
        def handle_workflow_discovery(payload):
            # TODO: 实现工作流发现任务处理
            project_id = payload.get("project_id")
            logger.debug(f"Discovering workflows for project: {project_id}")
            return {"status": "success"}
        
        self.worker.register_handler("workflow_discovery", handle_workflow_discovery)
        
        # 记忆清理任务
        def handle_memory_cleanup(payload):
            from feishu_mem.core.storage import Storage
            storage = Storage()
            deleted = storage.cleanup_expired_memory()
            logger.info(f"Cleaned up {deleted} expired memory entries")
            return {"deleted": deleted}
        
        self.worker.register_handler("memory_cleanup", handle_memory_cleanup)
        
        # WAL清理任务
        def handle_wal_cleanup(payload):
            from feishu_mem.core.wal import WriteAheadLog
            wal = WriteAheadLog()
            days = payload.get("days", 7)
            deleted = wal.cleanup_completed(days=days)
            logger.info(f"Cleaned up {deleted} old WAL files")
            return {"deleted": deleted}
        
        self.worker.register_handler("wal_cleanup", handle_wal_cleanup)
        
        logger.info("All task handlers registered")
    
    def _handle_shutdown(self, signum, frame):
        """处理关闭信号"""
        logger.info(f"Received shutdown signal: {signum}")
        self.running = False
        self.worker.stop()
    
    def run(self):
        """启动服务"""
        logger.info("Feishu-Mem Worker service starting...")
        self.running = True
        
        # 启动Worker
        self.worker.start(poll_interval=1.0)
        
        logger.info("Feishu-Mem Worker service stopped")

def main():
    """主函数"""
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        # 后台运行
        pid = os.fork()
        if pid > 0:
            print(f"Feishu-Mem Worker started in background, PID: {pid}")
            sys.exit(0)
        
        # 子进程
        os.setsid()
        os.umask(0)
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
        
        # 重定向标准流
        sys.stdout.flush()
        sys.stderr.flush()
        
        with open('/dev/null', 'r') as dev_null:
            os.dup2(dev_null.fileno(), sys.stdin.fileno())
        with open(config.log_dir / "worker.log", 'a+') as f:
            os.dup2(f.fileno(), sys.stdout.fileno())
            os.dup2(f.fileno(), sys.stderr.fileno())
    
    # 启动服务
    service = WorkerService()
    service.run()

if __name__ == "__main__":
    main()