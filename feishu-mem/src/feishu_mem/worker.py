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
        
        # 模式分析任务：基于历史命令发现高频使用模式
        def handle_pattern_analysis(payload):
            from feishu_mem.core.storage import Storage
            storage = Storage()
            user_id = payload.get("user_id", "unknown")
            limit = payload.get("limit", 200)

            recent = storage.get_recent_commands(limit=limit)
            if not recent:
                return {"status": "success", "patterns_found": 0}

            # 统计命令名频率
            from collections import Counter
            cmd_counter = Counter(r.command_name for r in recent)
            # 统计完整命令频率
            full_counter = Counter(r.raw_command for r in recent if r.usage_count >= 2)

            patterns = []
            for cmd_name, count in cmd_counter.most_common(20):
                patterns.append({"command": cmd_name, "count": count, "type": "command_name"})
            for raw_cmd, count in full_counter.most_common(20):
                patterns.append({"command": raw_cmd, "count": count, "type": "full_command"})

            logger.info(f"Pattern analysis for user {user_id}: found {len(patterns)} patterns")
            return {"status": "success", "patterns_found": len(patterns)}
        
        self.worker.register_handler("pattern_analysis", handle_pattern_analysis)
        
        # 工作流发现任务：从命令序列中挖掘工作流
        def handle_workflow_discovery(payload):
            from feishu_mem.core.storage import Storage
            from feishu_mem.core.workflow import WorkflowEngine
            storage = Storage()
            project_id = payload.get("project_id")
            min_length = payload.get("min_length", 2)
            limit = payload.get("limit", 100)

            sequences = storage.get_recent_command_sequences(
                project_id=project_id, min_length=min_length, limit=limit
            )
            if not sequences:
                return {"status": "success", "workflows_found": 0}

            # 简单的序列频率统计
            from collections import Counter
            pair_counter: Counter = Counter()
            for seq in sequences:
                for i in range(len(seq) - 1):
                    pair = (seq[i].command_name, seq[i + 1].command_name)
                    pair_counter[pair] += 1

            workflows = []
            for (cmd_a, cmd_b), count in pair_counter.most_common(20):
                if count >= 2:
                    workflows.append({"from": cmd_a, "to": cmd_b, "count": count})

            logger.info(f"Workflow discovery for project {project_id}: found {len(workflows)} workflows")
            return {"status": "success", "workflows_found": len(workflows)}
        
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
    import subprocess

    if len(sys.argv) > 1 and sys.argv[1] == "start":
        # 跨平台后台运行：使用subprocess启动独立进程
        log_path = config.log_dir / "worker.log"
        config.log_dir.mkdir(parents=True, exist_ok=True)

        with open(log_path, "a+", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, __file__],
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=True if os.name != "nt" else False,
            )
        print(f"Feishu-Mem Worker started in background, PID: {process.pid}")
        sys.exit(0)

    # 启动服务
    service = WorkerService()
    service.run()

if __name__ == "__main__":
    main()