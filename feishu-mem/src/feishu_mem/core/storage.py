import sqlite3
import json
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger
from feishu_mem.core.wal import get_wal

@dataclass
class CommandRecord:
    command_id: str
    raw_command: str
    command_name: str
    arguments: List[str]
    options: Dict[str, Any]
    working_dir: str
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    environment: Optional[str] = None
    exit_code: Optional[int] = None
    execution_time: Optional[float] = None
    executed_at: Optional[datetime] = None
    user_id: Optional[str] = None
    source: str = 'shell'
    tags: Optional[List[str]] = None
    is_successful: bool = True
    sensitivity_level: str = 'public'
    is_explicit: bool = False
    usage_count: int = 1
    last_used_at: Optional[datetime] = None

class Storage:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or config.db_path
        self._init_db()
        logger.info(f"Storage initialized, database path: {self.db_path}")
    
    def _init_db(self) -> None:
        """初始化数据库表结构"""
        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = f.read()
        
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.executescript(schema)
            conn.commit()
    
    def _calculate_content_hash(self, record: CommandRecord) -> str:
        """计算命令内容哈希，用于去重
        对参数和选项进行排序，确保相同命令无论参数顺序如何都生成相同哈希
        """
        # 对列表参数排序
        sorted_args = sorted(record.arguments)
        # 对字典选项按键排序
        sorted_options = dict(sorted(record.options.items())) if record.options else {}
        
        content = f"{record.command_name}{json.dumps(sorted_args)}{json.dumps(sorted_options)}{record.project_id}{record.environment}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def add_command(self, record: CommandRecord, vector_store=None) -> Optional[str]:
        """添加命令记录，存在相同哈希则更新使用次数，失败时返回None不崩溃"""
        try:
            # 先写入WAL
            wal = get_wal()
            wal_entry_id = wal.append({
                "type": "add_command",
                "record": {
                    **record.__dict__,
                    "executed_at": record.executed_at.isoformat() if record.executed_at else None,
                    "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None
                }
            })
            
            content_hash = self._calculate_content_hash(record)
            record.command_id = str(uuid.uuid4()) if not record.command_id else record.command_id
            record.executed_at = datetime.now() if not record.executed_at else record.executed_at
            record.last_used_at = datetime.now() if not record.last_used_at else record.last_used_at
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # 检查是否已存在相同哈希的记录
                cursor = conn.execute(
                    "SELECT command_id, usage_count FROM commands WHERE content_hash = ? AND project_id = ? AND environment = ?",
                    (content_hash, record.project_id, record.environment)
                )
                existing = cursor.fetchone()
                
                if existing:
                    # 更新使用次数和最后使用时间
                    command_id, usage_count = existing
                    conn.execute(
                        "UPDATE commands SET usage_count = ?, last_used_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE command_id = ?",
                        (usage_count + 1, command_id)
                    )
                    conn.commit()
                    logger.debug(f"Command updated, id: {command_id}, usage count: {usage_count + 1}")
                    
                    # 更新向量库元数据
                    if vector_store:
                        try:
                            vector_store.update_command_metadata(command_id, {
                                "usage_count": usage_count + 1,
                                "last_used_at": datetime.now().isoformat()
                            })
                        except Exception as vs_error:
                            logger.warning("Failed to update vector store metadata", exception=vs_error, command_id=command_id)
                    
                    # 标记WAL条目为已完成，即使标记失败也不影响主流程，数据已持久化
                    try:
                        wal.mark_complete(wal_entry_id)
                    except Exception as wal_error:
                        logger.error("Failed to mark WAL entry as complete, data already persisted", 
                                   exception=wal_error, entry_id=wal_entry_id)
                    
                    return command_id
                else:
                    # 插入新记录
                    conn.execute(
                        """
                        INSERT INTO commands (
                            command_id, session_id, raw_command, command_name, arguments, options, working_dir,
                            project_id, environment, exit_code, execution_time, executed_at,
                            user_id, source, tags, is_successful, sensitivity_level, content_hash, is_explicit, last_used_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.command_id,
                            record.session_id,
                            record.raw_command,
                            record.command_name,
                            json.dumps(record.arguments),
                            json.dumps(record.options),
                            record.working_dir,
                            record.project_id,
                            record.environment,
                            record.exit_code,
                            record.execution_time,
                            record.executed_at.isoformat(),
                            record.user_id,
                            record.source,
                            json.dumps(record.tags) if record.tags else None,
                            1 if record.is_successful else 0,
                            record.sensitivity_level,
                            content_hash,
                            1 if record.is_explicit else 0,
                            record.last_used_at.isoformat()
                        )
                    )
                    conn.commit()
                    logger.debug(f"New command added, id: {record.command_id}, command: {record.raw_command[:50]}...")
                    
                    # 添加到向量库
                    if vector_store:
                        try:
                            vector_store.add_command(record)
                        except Exception as vs_error:
                            logger.warning("Failed to add command to vector store", exception=vs_error, command_id=record.command_id)
                    
                    # 标记WAL条目为已完成，即使标记失败也不影响主流程，数据已持久化
                    try:
                        wal.mark_complete(wal_entry_id)
                    except Exception as wal_error:
                        logger.error("Failed to mark WAL entry as complete, data already persisted", 
                                   exception=wal_error, entry_id=wal_entry_id)
                    
                    return record.command_id
                    
        except Exception as e:
            logger.error("Failed to add command", exception=e, command=record.raw_command[:100])
            return None
    
    def get_commands_by_ids(self, command_ids: List[str]) -> List[CommandRecord]:
        """根据ID列表批量获取命令记录"""
        if not command_ids:
            return []
        
        placeholders = ", ".join(["?"] * len(command_ids))
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT * FROM commands
                WHERE command_id IN ({placeholders})
                """,
                command_ids
            )
            
            records = []
            for row in cursor.fetchall():
                record = CommandRecord(
                    command_id=row["command_id"],
                    session_id=row["session_id"],
                    raw_command=row["raw_command"],
                    command_name=row["command_name"],
                    arguments=json.loads(row["arguments"]) if row["arguments"] else [],
                    options=json.loads(row["options"]) if row["options"] else {},
                    working_dir=row["working_dir"],
                    project_id=row["project_id"],
                    environment=row["environment"],
                    exit_code=row["exit_code"],
                    execution_time=row["execution_time"],
                    executed_at=datetime.fromisoformat(row["executed_at"]),
                    user_id=row["user_id"],
                    source=row["source"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    is_successful=bool(row["is_successful"]),
                    sensitivity_level=row["sensitivity_level"],
                    is_explicit=bool(row["is_explicit"]),
                    usage_count=row["usage_count"],
                    last_used_at=datetime.fromisoformat(row["last_used_at"])
                )
                records.append(record)
            
            # 保持与输入ID相同的顺序
            id_to_record = {r.command_id: r for r in records}
            return [id_to_record[id] for id in command_ids if id in id_to_record]
    
    def cleanup_expired_memory(self) -> int:
        """清理过期记忆，返回删除的数量"""
        try:
            deleted_count = 0
            now = datetime.now()
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # 清理临时记忆（7天未使用，非显式）
                temp_cutoff = (now - timedelta(days=config.temporary_memory_days)).isoformat()
                cursor = conn.execute(
                    """
                    DELETE FROM commands 
                    WHERE is_explicit = 0 
                    AND last_used_at < ? 
                    AND usage_count < 2
                    """,
                    (temp_cutoff,)
                )
                deleted_count += cursor.rowcount
                
                # 清理短期记忆（30天未使用，非显式）
                short_cutoff = (now - timedelta(days=config.short_term_memory_days)).isoformat()
                cursor = conn.execute(
                    """
                    DELETE FROM commands 
                    WHERE is_explicit = 0 
                    AND last_used_at < ? 
                    AND usage_count < 3
                    """,
                    (short_cutoff,)
                )
                deleted_count += cursor.rowcount
                
                # 清理超过最大保留时间的记忆（非显式）
                max_cutoff = (now - timedelta(days=config.max_memory_days)).isoformat()
                cursor = conn.execute(
                    """
                    DELETE FROM commands 
                    WHERE is_explicit = 0 
                    AND last_used_at < ?
                    """,
                    (max_cutoff,)
                )
                deleted_count += cursor.rowcount
                
                conn.commit()
                
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} expired memory entries")
                
                return deleted_count
                
        except Exception as e:
            logger.error("Failed to cleanup expired memory", exception=e)
            return 0
    
    def search_commands(self, query: str, project_id: Optional[str] = None, environment: Optional[str] = None, limit: int = 20) -> List[CommandRecord]:
        """全文搜索命令"""
        params = []
        conditions = []
        
        if project_id:
            conditions.append("c.project_id = ?")
            params.append(project_id)
        
        if environment:
            conditions.append("c.environment = ?")
            params.append(environment)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.extend([query, limit])
        
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT c.* FROM commands c
                JOIN commands_fts fts ON c.rowid = fts.rowid
                WHERE {where_clause} AND commands_fts MATCH ?
                ORDER BY c.usage_count DESC, c.last_used_at DESC
                LIMIT ?
                """,
                params
            )
            
            records = []
            for row in cursor.fetchall():
                record = CommandRecord(
                    command_id=row["command_id"],
                    raw_command=row["raw_command"],
                    command_name=row["command_name"],
                    arguments=json.loads(row["arguments"]) if row["arguments"] else [],
                    options=json.loads(row["options"]) if row["options"] else {},
                    working_dir=row["working_dir"],
                    project_id=row["project_id"],
                    environment=row["environment"],
                    exit_code=row["exit_code"],
                    execution_time=row["execution_time"],
                    executed_at=datetime.fromisoformat(row["executed_at"]),
                    user_id=row["user_id"],
                    source=row["source"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    is_successful=bool(row["is_successful"]),
                    sensitivity_level=row["sensitivity_level"],
                    is_explicit=bool(row["is_explicit"]),
                    usage_count=row["usage_count"]
                )
                records.append(record)
            
            return records
    
    def get_recent_commands(self, limit: int = 10) -> List[CommandRecord]:
        """获取最近使用的命令"""
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM commands
                ORDER BY last_used_at DESC
                LIMIT ?
                """,
                (limit,)
            )
            
            records = []
            for row in cursor.fetchall():
                record = CommandRecord(
                    command_id=row["command_id"],
                    raw_command=row["raw_command"],
                    command_name=row["command_name"],
                    arguments=json.loads(row["arguments"]) if row["arguments"] else [],
                    options=json.loads(row["options"]) if row["options"] else {},
                    working_dir=row["working_dir"],
                    project_id=row["project_id"],
                    environment=row["environment"],
                    exit_code=row["exit_code"],
                    execution_time=row["execution_time"],
                    executed_at=datetime.fromisoformat(row["executed_at"]),
                    user_id=row["user_id"],
                    source=row["source"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    is_successful=bool(row["is_successful"]),
                    sensitivity_level=row["sensitivity_level"],
                    is_explicit=bool(row["is_explicit"]),
                    usage_count=row["usage_count"],
                    last_used_at=datetime.fromisoformat(row["last_used_at"])
                )
                records.append(record)
            
            return records
    
    def get_prefix_matches(self, prefix: str, project_id: Optional[str] = None, environment: Optional[str] = None, limit: int = 20) -> List[CommandRecord]:
        """前缀匹配命令，用于快速补全"""
        params = []
        conditions = []
        
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        
        if environment:
            conditions.append("environment = ?")
            params.append(environment)
        
        # 前缀匹配规则：命令名或整个命令以前缀开头
        conditions.append("(raw_command LIKE ? OR command_name LIKE ?)")
        params.append(f"{prefix}%")
        params.append(f"{prefix}%")
        
        where_clause = " AND ".join(conditions)
        params.append(limit)
        
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT * FROM commands
                WHERE {where_clause}
                ORDER BY usage_count DESC, last_used_at DESC
                LIMIT ?
                """,
                params
            )
            
            records = []
            for row in cursor.fetchall():
                record = CommandRecord(
                    command_id=row["command_id"],
                    raw_command=row["raw_command"],
                    command_name=row["command_name"],
                    arguments=json.loads(row["arguments"]) if row["arguments"] else [],
                    options=json.loads(row["options"]) if row["options"] else {},
                    working_dir=row["working_dir"],
                    project_id=row["project_id"],
                    environment=row["environment"],
                    exit_code=row["exit_code"],
                    execution_time=row["execution_time"],
                    executed_at=datetime.fromisoformat(row["executed_at"]),
                    user_id=row["user_id"],
                    source=row["source"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    is_successful=bool(row["is_successful"]),
                    sensitivity_level=row["sensitivity_level"],
                    is_explicit=bool(row["is_explicit"]),
                    usage_count=row["usage_count"],
                    last_used_at=datetime.fromisoformat(row["last_used_at"])
                )
                records.append(record)
            
            return records
    
    def get_recent_command_sequences(self, project_id: Optional[str] = None, environment: Optional[str] = None, min_length: int = 2, limit: int = 100) -> List[List[CommandRecord]]:
        """获取最近的命令执行序列，用于工作流挖掘"""
        try:
            params = []
            conditions = []
            
            if project_id:
                conditions.append("project_id = ?")
                params.append(project_id)
            
            if environment:
                conditions.append("environment = ?")
                params.append(environment)
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit * 5)  # 多取一些数据来构建序列
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"""
                    SELECT * FROM commands
                    WHERE {where_clause}
                    ORDER BY executed_at DESC
                    LIMIT ?
                    """,
                    params
                )
                
                records = []
                for row in cursor.fetchall():
                    record = CommandRecord(
                        command_id=row["command_id"],
                        raw_command=row["raw_command"],
                        command_name=row["command_name"],
                        arguments=json.loads(row["arguments"]) if row["arguments"] else [],
                        options=json.loads(row["options"]) if row["options"] else {},
                        working_dir=row["working_dir"],
                        project_id=row["project_id"],
                        environment=row["environment"],
                        exit_code=row["exit_code"],
                        execution_time=row["execution_time"],
                        executed_at=datetime.fromisoformat(row["executed_at"]),
                        user_id=row["user_id"],
                        source=row["source"],
                        tags=json.loads(row["tags"]) if row["tags"] else [],
                        is_successful=bool(row["is_successful"]),
                        sensitivity_level=row["sensitivity_level"],
                        is_explicit=bool(row["is_explicit"]),
                        usage_count=row["usage_count"],
                        last_used_at=datetime.fromisoformat(row["last_used_at"])
                    )
                    records.append(record)
            
            # 按会话分组构建序列
            sequences = []
            current_session = None
            current_sequence = []
            
            for record in sorted(records, key=lambda x: x.executed_at):
                if record.session_id != current_session:
                    if len(current_sequence) >= min_length:
                        sequences.append(current_sequence)
                    current_session = record.session_id
                    current_sequence = []
                current_sequence.append(record)
            
            # 添加最后一个序列
            if len(current_sequence) >= min_length:
                sequences.append(current_sequence)
            
            return sequences[-limit:]  # 返回最近的limit个序列
            
        except Exception as e:
            logger.error("Failed to get recent command sequences", exception=e)
            return []
    

