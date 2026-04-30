import json
import time
import uuid
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass
from enum import Enum
import sqlite3
from pathlib import Path

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"


@dataclass
class Task:
    task_id: str
    task_type: str
    payload: Dict[str, Any]
    status: TaskStatus
    priority: int = 0
    retries: int = 0
    max_retries: int = 3
    created_at: float = None
    claimed_at: float = None
    completed_at: float = None
    last_error: str = None
    worker_id: str = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()


class AsyncTaskQueue:
    """
    异步任务队列，基于SQLite实现轻量级任务调度
    采用CLAIM-CONFIRM模式，支持重试、死信队列、最终一致性
    """
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.db_path
        self.worker_id = str(uuid.uuid4())[:8]
        self._init_queue_table()
        logger.info(f"Async task queue initialized, worker id: {self.worker_id}")
    
    def _init_queue_table(self) -> None:
        """初始化任务队列表"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_queue (
                        task_id TEXT PRIMARY KEY,
                        task_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        priority INTEGER NOT NULL DEFAULT 0,
                        retries INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 3,
                        created_at REAL NOT NULL,
                        claimed_at REAL,
                        completed_at REAL,
                        last_error TEXT,
                        worker_id TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON task_queue(status, priority DESC, created_at ASC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_claimed ON task_queue(claimed_at)")
                conn.commit()
        except Exception as e:
            logger.error("Failed to initialize task queue table", exception=e)
    
    def enqueue(self, task_type: str, payload: Dict[str, Any], priority: int = 0, max_retries: int = 3) -> str:
        """任务入队，返回全局唯一任务ID"""
        try:
            task_id = str(uuid.uuid4())
            task = Task(
                task_id=task_id,
                task_type=task_type,
                payload=payload,
                status=TaskStatus.PENDING,
                priority=priority,
                max_retries=max_retries,
                created_at=time.time()
            )
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    INSERT INTO task_queue (
                        task_id, task_type, payload, status, priority, 
                        retries, max_retries, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task.task_id,
                    task.task_type,
                    json.dumps(task.payload),
                    task.status.value,
                    task.priority,
                    task.retries,
                    task.max_retries,
                    task.created_at
                ))
                conn.commit()
            
            logger.debug(f"Task enqueued: {task_id}, type: {task_type}, priority: {priority}")
            return task_id
            
        except Exception as e:
            logger.error("Failed to enqueue task", exception=e)
            return ""
    
    def claim_next(self, timeout: int = 30) -> Optional[Task]:
        """原子性获取下一个待处理任务，标记为处理中状态"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                
                # 查找最老的pending任务，按优先级排序
                cursor = conn.execute("""
                    SELECT * FROM task_queue 
                    WHERE status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                """)
                
                row = cursor.fetchone()
                if not row:
                    conn.rollback()
                    return None
                
                task_id = row["task_id"]
                # 标记为处理中
                now = time.time()
                conn.execute("""
                    UPDATE task_queue 
                    SET status = 'processing', claimed_at = ?, worker_id = ?
                    WHERE task_id = ?
                """, (now, self.worker_id, task_id))
                
                conn.commit()
                
                # 构造Task对象
                task = Task(
                    task_id=row["task_id"],
                    task_type=row["task_type"],
                    payload=json.loads(row["payload"]),
                    status=TaskStatus(row["status"]),
                    priority=row["priority"],
                    retries=row["retries"],
                    max_retries=row["max_retries"],
                    created_at=row["created_at"],
                    claimed_at=row["claimed_at"],
                    completed_at=row["completed_at"],
                    last_error=row["last_error"],
                    worker_id=row["worker_id"]
                )
                
                logger.debug(f"Task claimed: {task_id}, type: {task.task_type}, worker: {self.worker_id}")
                return task
                
        except Exception as e:
            logger.error("Failed to claim task", exception=e)
            return None
    
    def confirm(self, task_id: str, result: Any = None) -> bool:
        """确认任务处理成功，标记为已完成"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                now = time.time()
                cursor = conn.execute("""
                    UPDATE task_queue 
                    SET status = 'completed', completed_at = ?
                    WHERE task_id = ? AND status = 'processing' AND worker_id = ?
                """, (now, task_id, self.worker_id))
                
                if cursor.rowcount == 0:
                    logger.warning(f"Task {task_id} not found or not owned by worker {self.worker_id}")
                    return False
                
                conn.commit()
                logger.debug(f"Task confirmed: {task_id}, completed in: {now - self._get_task_created_time(task_id):.2f}s")
                return True
                
        except Exception as e:
            logger.error(f"Failed to confirm task {task_id}", exception=e)
            return False
    
    def mark_failed(self, task_id: str, error: str) -> bool:
        """标记任务失败，重试次数+1，超过阈值进入死信队列"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                # 获取当前任务信息
                cursor = conn.execute("""
                    SELECT retries, max_retries FROM task_queue 
                    WHERE task_id = ? AND status = 'processing' AND worker_id = ?
                """, (task_id, self.worker_id))
                
                row = cursor.fetchone()
                if not row:
                    logger.warning(f"Task {task_id} not found or not owned by worker {self.worker_id}")
                    return False
                
                retries = row["retries"] + 1
                max_retries = row["max_retries"]
                
                if retries >= max_retries:
                    # 超过最大重试次数，标记为dead
                    new_status = TaskStatus.DEAD.value
                    logger.error(f"Task {task_id} failed permanently after {retries} retries, error: {error}")
                else:
                    # 重置为pending，等待重试
                    new_status = TaskStatus.PENDING.value
                    logger.warning(f"Task {task_id} failed, retry {retries}/{max_retries}, error: {error}")
                
                conn.execute("""
                    UPDATE task_queue 
                    SET status = ?, retries = ?, last_error = ?, claimed_at = NULL, worker_id = NULL
                    WHERE task_id = ?
                """, (new_status, retries, error[:500], task_id))
                
                conn.commit()
                return True
                
        except Exception as e:
            logger.error(f"Failed to mark task {task_id} as failed", exception=e)
            return False
    
    def heartbeat(self, task_id: str) -> bool:
        """更新任务心跳，防止长时间无响应导致任务被重新调度"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute("""
                    UPDATE task_queue 
                    SET claimed_at = ?
                    WHERE task_id = ? AND status = 'processing' AND worker_id = ?
                """, (time.time(), task_id, self.worker_id))
                
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Failed to send heartbeat for task {task_id}", exception=e)
            return False
    
    def recover_stuck_tasks(self, timeout_seconds: int = 120) -> int:
        """恢复卡住的任务：处理时间超过阈值的任务重置为pending"""
        try:
            cutoff_time = time.time() - timeout_seconds
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # 查找处理中超时的任务
                cursor = conn.execute("""
                    UPDATE task_queue 
                    SET status = 'pending', retries = retries + 1, claimed_at = NULL, worker_id = NULL, last_error = 'Task timed out'
                    WHERE status = 'processing' AND claimed_at < ? AND retries < max_retries
                """, (cutoff_time,))
                
                recovered = cursor.rowcount
                if recovered > 0:
                    logger.info(f"Recovered {recovered} stuck tasks")
                
                # 标记超过重试次数的卡住任务为dead
                cursor = conn.execute("""
                    UPDATE task_queue 
                    SET status = 'dead', last_error = 'Task timed out permanently'
                    WHERE status = 'processing' AND claimed_at < ? AND retries >= max_retries
                """, (cutoff_time,))
                
                dead = cursor.rowcount
                if dead > 0:
                    logger.warning(f"Marked {dead} stuck tasks as dead")
                
                conn.commit()
                return recovered + dead
                
        except Exception as e:
            logger.error("Failed to recover stuck tasks", exception=e)
            return 0
    
    def get_dead_tasks(self, limit: int = 100) -> List[Task]:
        """获取死信队列中的任务"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM task_queue 
                    WHERE status = 'dead'
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))
                
                tasks = []
                for row in cursor.fetchall():
                    tasks.append(Task(
                        task_id=row["task_id"],
                        task_type=row["task_type"],
                        payload=json.loads(row["payload"]),
                        status=TaskStatus(row["status"]),
                        priority=row["priority"],
                        retries=row["retries"],
                        max_retries=row["max_retries"],
                        created_at=row["created_at"],
                        claimed_at=row["claimed_at"],
                        completed_at=row["completed_at"],
                        last_error=row["last_error"],
                        worker_id=row["worker_id"]
                    ))
                
                return tasks
                
        except Exception as e:
            logger.error("Failed to get dead tasks", exception=e)
            return []
    
    def retry_dead_task(self, task_id: str) -> bool:
        """重试死信队列中的任务"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute("""
                    UPDATE task_queue 
                    SET status = 'pending', retries = 0, last_error = NULL, claimed_at = NULL, worker_id = NULL
                    WHERE task_id = ? AND status = 'dead'
                """, (task_id,))
                
                if cursor.rowcount > 0:
                    logger.info(f"Retried dead task {task_id}")
                    conn.commit()
                    return True
                return False
                
        except Exception as e:
            logger.error(f"Failed to retry dead task {task_id}", exception=e)
            return False
    
    def cleanup_completed_tasks(self, days_to_keep: int = 7) -> int:
        """清理已完成的任务，保留指定天数"""
        try:
            cutoff_time = time.time() - (days_to_keep * 86400)
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute("""
                    DELETE FROM task_queue 
                    WHERE status = 'completed' AND completed_at < ?
                """, (cutoff_time,))
                
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.debug(f"Cleaned up {deleted} completed tasks older than {days_to_keep} days")
                conn.commit()
                return deleted
                
        except Exception as e:
            logger.error("Failed to cleanup completed tasks", exception=e)
            return 0
    
    def _get_task_created_time(self, task_id: str) -> float:
        """获取任务创建时间"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute("SELECT created_at FROM task_queue WHERE task_id = ?", (task_id,))
                row = cursor.fetchone()
                return row[0] if row else time.time()
        except Exception:
            return time.time()


# 全局实例
_task_queue = None


def get_task_queue() -> AsyncTaskQueue:
    global _task_queue
    if _task_queue is None:
        _task_queue = AsyncTaskQueue()
    return _task_queue
