"""
写前日志（WAL）实现，确保数据持久化可靠性
用于故障恢复和数据一致性保证
"""
import json
import os
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import sqlite3

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

class WriteAheadLog:
    """
    写前日志实现，遵循以下原则：
    1. 所有写入操作先写入WAL再提交到主数据库
    2. 系统崩溃后可以通过WAL重放恢复未提交的操作
    3. 定期清理已提交的日志条目
    """
    def __init__(self, wal_path: Optional[Path] = None):
        self.wal_dir = wal_path or config.data_dir / "wal"
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        self.current_wal_file = self._get_current_wal_file()
        self.max_wal_size = 100 * 1024 * 1024  # 100MB
        logger.info(f"Write-Ahead Log initialized, directory: {self.wal_dir}")
    
    def _get_current_wal_file(self) -> Path:
        """获取当前WAL文件路径，不存在则创建"""
        # 查找最新的WAL文件
        wal_files = sorted(self.wal_dir.glob("wal_*.log"))
        if wal_files:
            return wal_files[-1]
        # 创建新的WAL文件
        timestamp = int(time.time())
        return self.wal_dir / f"wal_{timestamp}.log"
    
    def _rotate_wal_file(self) -> None:
        """轮转WAL文件，当文件超过大小时创建新文件"""
        if self.current_wal_file.exists() and self.current_wal_file.stat().st_size >= self.max_wal_size:
            timestamp = int(time.time())
            self.current_wal_file = self.wal_dir / f"wal_{timestamp}.log"
            logger.info(f"Rotated WAL file to: {self.current_wal_file}")
    
    def append(self, operation: Dict[str, Any]) -> str:
        """
        追加操作到WAL
        返回生成的日志条目ID
        """
        try:
            self._rotate_wal_file()
            
            entry_id = f"wal_{int(time.time() * 1000000)}_{os.urandom(2).hex()}"
            entry = {
                "entry_id": entry_id,
                "timestamp": datetime.now().isoformat(),
                "operation": operation
            }
            
            # 追加到WAL文件，使用fsync确保持久化
            with open(self.current_wal_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())  # 确保数据写入磁盘
            
            logger.debug(f"Appended WAL entry: {entry_id}, operation: {operation.get('type', 'unknown')}")
            return entry_id
            
        except Exception as e:
            logger.error("Failed to append to WAL", exception=e)
            raise
    
    def mark_complete(self, entry_id: str) -> None:
        """标记WAL条目已成功提交到主数据库"""
        try:
            # 记录已完成的条目ID到checkpoint文件
            checkpoint_file = self.wal_dir / "checkpoint"
            with open(checkpoint_file, "a", encoding="utf-8") as f:
                f.write(f"{entry_id}\n")
                f.flush()
            
            logger.debug(f"Marked WAL entry as complete: {entry_id}")
            
        except Exception as e:
            logger.error("Failed to mark WAL entry as complete", exception=e)
    
    def get_incomplete_entries(self) -> List[Dict[str, Any]]:
        """获取所有未完成的WAL条目（用于故障恢复）"""
        try:
            # 读取已完成的条目ID
            completed_ids = set()
            checkpoint_file = self.wal_dir / "checkpoint"
            if checkpoint_file.exists():
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            completed_ids.add(line)
            
            # 读取所有WAL文件，收集未完成的条目
            incomplete_entries = []
            wal_files = sorted(self.wal_dir.glob("wal_*.log"))
            
            for wal_file in wal_files:
                with open(wal_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry["entry_id"] not in completed_ids:
                                incomplete_entries.append(entry)
                        except json.JSONDecodeError:
                            logger.warning(f"Invalid JSON in WAL file {wal_file}: {line[:100]}...")
            
            logger.info(f"Found {len(incomplete_entries)} incomplete WAL entries")
            return incomplete_entries
            
        except Exception as e:
            logger.error("Failed to get incomplete WAL entries", exception=e)
            return []
    
    def replay_incomplete(self, storage, vector_store=None) -> int:
        """
        重放未完成的WAL条目，恢复数据
        返回恢复的条目数量
        """
        incomplete_entries = self.get_incomplete_entries()
        if not incomplete_entries:
            return 0
        
        logger.info(f"Replaying {len(incomplete_entries)} incomplete WAL entries")
        recovered = 0
        
        for entry in incomplete_entries:
            try:
                operation = entry["operation"]
                op_type = operation.get("type")
                success = False
                
                if op_type == "add_command":
                    # 恢复添加命令操作
                    from feishu_mem.core.storage import CommandRecord
                    
                    record_data = operation["record"]
                    # 转换datetime字段
                    if record_data.get("executed_at"):
                        record_data["executed_at"] = datetime.fromisoformat(record_data["executed_at"])
                    if record_data.get("last_used_at"):
                        record_data["last_used_at"] = datetime.fromisoformat(record_data["last_used_at"])
                    
                    record = CommandRecord(**record_data)
                    
                    # 幂等性检查：先判断该命令是否已经被成功执行过
                    # 计算content_hash（和storage层逻辑一致）
                    import hashlib
                    import json
                    content = f"{record.command_name}{json.dumps(record.arguments)}{json.dumps(record.options)}{record.project_id}{record.environment}"
                    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                    
                    # 查询是否已存在相同哈希的记录
                    already_exists = False
                    try:
                        with sqlite3.connect(storage.db_path, timeout=5) as conn:
                            cursor = conn.execute(
                                "SELECT 1 FROM commands WHERE content_hash = ? AND project_id = ? AND environment = ? LIMIT 1",
                                (content_hash, record.project_id, record.environment)
                            )
                            already_exists = cursor.fetchone() is not None
                    except Exception as e:
                        logger.warning(f"Failed to check command existence for WAL entry {entry['entry_id']}", exception=e)
                    
                    if already_exists:
                        # 命令已经存在，说明之前操作已成功，直接标记完成
                        logger.debug(f"Command already exists for WAL entry {entry['entry_id']}, skipping execution")
                        success = True
                    else:
                        # 不存在才执行添加操作
                        result = storage.add_command(record, vector_store)
                        success = result is not None
                        if not success:
                            logger.warning(f"Add command operation failed for WAL entry {entry['entry_id']}, command: {record.raw_command[:50]}")
                    
                elif op_type == "delete_command":
                    # 恢复删除命令操作
                    command_id = operation["command_id"]
                    # 检查删除操作是否成功实现
                    if hasattr(storage, 'delete_command'):
                        deleted = storage.delete_command(command_id)
                        if vector_store and hasattr(vector_store, 'delete_command'):
                            vector_store.delete_command(command_id)
                        success = deleted is not None and deleted > 0
                    else:
                        logger.warning(f"Delete command operation not implemented, skipping WAL entry {entry['entry_id']}")
                        success = True  # 标记为成功避免重复重试
                
                # 只有操作真正成功才标记为已完成
                if success:
                    self.mark_complete(entry["entry_id"])
                    recovered += 1
                else:
                    logger.error(f"WAL entry {entry['entry_id']} operation failed, will retry on next recovery")
                
            except Exception as e:
                logger.error(f"Failed to replay WAL entry {entry['entry_id']}", exception=e)
                continue
        
        logger.info(f"Successfully recovered {recovered} entries from WAL")
        return recovered
    
    def cleanup_completed(self, days: int = 7) -> int:
        """清理N天前的已完成WAL文件，返回删除的文件数量"""
        try:
            cutoff = time.time() - days * 86400
            deleted = 0
            
            # 读取已完成的最大条目ID
            completed_ids = set()
            checkpoint_file = self.wal_dir / "checkpoint"
            if checkpoint_file.exists():
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            completed_ids.add(line)
            
            # 检查WAL文件
            wal_files = sorted(self.wal_dir.glob("wal_*.log"))
            for wal_file in wal_files:
                # 检查文件是否足够旧
                if wal_file.stat().st_mtime < cutoff:
                    # 检查文件中的所有条目是否都已完成
                    all_completed = True
                    with open(wal_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                                if entry["entry_id"] not in completed_ids:
                                    all_completed = False
                                    break
                            except json.JSONDecodeError:
                                continue
                    
                    if all_completed:
                        wal_file.unlink()
                        deleted += 1
                        logger.debug(f"Deleted old WAL file: {wal_file}")
            
            # 清理checkpoint文件中的旧条目
            if checkpoint_file.exists() and checkpoint_file.stat().st_mtime < cutoff:
                # 保留最近10000条已完成记录
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > 10000:
                    with open(checkpoint_file, "w", encoding="utf-8") as f:
                        f.writelines(lines[-10000:])
            
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old WAL files")
            
            return deleted
            
        except Exception as e:
            logger.error("Failed to cleanup completed WAL files", exception=e)
            return 0

# 全局WAL实例
_wal: Optional[WriteAheadLog] = None

def get_wal() -> WriteAheadLog:
    """获取全局WAL实例"""
    global _wal
    if _wal is None:
        _wal = WriteAheadLog()
    return _wal