import json
import hashlib
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger
from .storage import CommandRecord


@dataclass
class MemoryVersion:
    version_id: str
    command_id: str
    content_hash: str
    content: dict
    created_at: datetime
    created_by: str
    change_reason: str
    confidence: float
    user_feedback_score: float = 0.0


class VersionManager:
    """
    版本管理器，实现记忆的版本控制、冲突解决和历史追溯
    冲突解决策略：时序优先 > 置信度优先 > 用户反馈优先
    """
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.db_path
        self._init_version_table()
    
    def _init_version_table(self) -> None:
        """初始化版本记录表"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_versions (
                        version_id TEXT PRIMARY KEY,
                        command_id TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        change_reason TEXT,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        user_feedback_score REAL NOT NULL DEFAULT 0.0,
                        FOREIGN KEY (command_id) REFERENCES commands(command_id) ON DELETE CASCADE
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_command_id ON memory_versions(command_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_content_hash ON memory_versions(content_hash)")
                conn.commit()
        except Exception as e:
            logger.error("Failed to initialize version table", exception=e)
    
    def calculate_content_hash(self, command: CommandRecord) -> str:
        """计算命令内容哈希，用于去重和版本识别"""
        content = f"{command.command_name}{json.dumps(command.arguments)}{json.dumps(command.options)}{command.project_id}{command.environment}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def create_version(self, record: CommandRecord, change_reason: str = "initial", created_by: str = "system") -> str:
        """创建新版本记录"""
        try:
            version_id = uuid.uuid4().hex
            content_hash = self.calculate_content_hash(record)
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    INSERT INTO memory_versions (
                        version_id, command_id, content_hash, content, created_at,
                        created_by, change_reason, confidence, user_feedback_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    version_id,
                    record.command_id,
                    content_hash,
                    json.dumps(record.__dict__, default=str),
                    datetime.now().isoformat(),
                    created_by,
                    change_reason,
                    0.8 if record.is_explicit else 0.5,
                    1.0 if record.is_successful else 0.0
                ))
                conn.commit()
            
            logger.debug(f"Created version {version_id} for command {record.command_id}")
            return version_id
            
        except Exception as e:
            logger.error("Failed to create version", exception=e)
            return ""
    
    @staticmethod
    def _compute_confidence(record: CommandRecord) -> float:
        """根据CommandRecord可用字段计算置信度"""
        base = 0.8 if record.is_explicit else 0.5
        if record.is_successful:
            base *= 1.2
        return min(base, 1.0)

    @staticmethod
    def _compute_feedback_score(record: CommandRecord) -> float:
        """根据CommandRecord可用字段计算反馈评分"""
        score = 1.0 if record.is_successful else 0.0
        if record.is_explicit:
            score += 0.5
        return min(score, 1.5)

    def resolve_conflict(self, existing: CommandRecord, new: CommandRecord) -> Tuple[CommandRecord, str]:
        """
        解决冲突，返回优胜的记忆项和原因
        优先级：
        1. 时间戳更新优先
        2. 显式记忆优先（is_explicit）
        3. 使用次数更多优先
        4. 成功执行优先
        """
        reasons = []

        new_confidence = self._compute_confidence(new)
        existing_confidence = self._compute_confidence(existing)
        new_feedback = self._compute_feedback_score(new)
        existing_feedback = self._compute_feedback_score(existing)

        # 规则1：时间戳优先
        if new.executed_at and existing.executed_at and new.executed_at > existing.executed_at:
            reasons.append(f"新记录时间戳更新 ({new.executed_at} > {existing.executed_at})")
            winner = new
        # 规则2：显式记忆优先
        elif new.is_explicit and not existing.is_explicit:
            reasons.append("新记录为显式记忆，优先级更高")
            winner = new
        # 规则3：置信度优先
        elif new_confidence > existing_confidence:
            reasons.append(f"新记录置信度更高 ({new_confidence:.2f} > {existing_confidence:.2f})")
            winner = new
        # 规则4：用户反馈优先
        elif new_feedback > existing_feedback:
            reasons.append(f"新记录反馈评分更高 ({new_feedback:.2f} > {existing_feedback:.2f})")
            winner = new
        # 规则5：使用次数优先
        elif new.usage_count > existing.usage_count:
            reasons.append(f"新记录使用次数更多 ({new.usage_count} > {existing.usage_count})")
            winner = new
        else:
            reasons.append("现有记录各项指标更优")
            winner = existing

        reason = " | ".join(reasons)
        logger.debug(f"Conflict resolved: {reason}, winner command: {winner.raw_command[:50]}")
        return winner, reason
    
    def get_version_history(self, command_id: str) -> List[MemoryVersion]:
        """获取记忆的所有历史版本"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM memory_versions 
                    WHERE command_id = ? 
                    ORDER BY created_at DESC
                """, (command_id,))
                
                versions = []
                for row in cursor.fetchall():
                    versions.append(MemoryVersion(
                        version_id=row["version_id"],
                        command_id=row["command_id"],
                        content_hash=row["content_hash"],
                        content=json.loads(row["content"]),
                        created_at=datetime.fromisoformat(row["created_at"]),
                        created_by=row["created_by"],
                        change_reason=row["change_reason"],
                        confidence=row["confidence"],
                        user_feedback_score=row["user_feedback_score"]
                    ))
                
                return versions
                
        except Exception as e:
            logger.error(f"Failed to get version history for command {command_id}", exception=e)
            return []
    
    def rollback_to_version(self, command_id: str, version_id: str) -> bool:
        """回滚到指定版本"""
        try:
            # 先在目标命令的版本历史中查找
            versions = self.get_version_history(command_id)
            target_version = next((v for v in versions if v.version_id == version_id), None)

            # 如果没找到，跨所有版本记录查找（支持跨命令回滚）
            if not target_version:
                with sqlite3.connect(self.db_path, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.execute(
                        "SELECT * FROM memory_versions WHERE version_id = ?", (version_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        target_version = MemoryVersion(
                            version_id=row["version_id"],
                            command_id=row["command_id"],
                            content_hash=row["content_hash"],
                            content=json.loads(row["content"]),
                            created_at=datetime.fromisoformat(row["created_at"]),
                            created_by=row["created_by"],
                            change_reason=row["change_reason"],
                            confidence=row["confidence"],
                            user_feedback_score=row["user_feedback_score"]
                        )
            
            if not target_version:
                logger.error(f"Version {version_id} not found for command {command_id}")
                return False
            
            # 更新主记录为目标版本内容
            content = target_version.content
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    UPDATE commands SET
                        raw_command = ?,
                        command_name = ?,
                        arguments = ?,
                        options = ?,
                        working_dir = ?,
                        project_id = ?,
                        environment = ?,
                        exit_code = ?,
                        execution_time = ?,
                        executed_at = ?,
                        user_id = ?,
                        source = ?,
                        tags = ?,
                        is_successful = ?,
                        sensitivity_level = ?,
                        is_explicit = ?,
                        usage_count = usage_count + 1,
                        last_used_at = CURRENT_TIMESTAMP
                    WHERE command_id = ?
                """, (
                    content["raw_command"],
                    content["command_name"],
                    json.dumps(content["arguments"]),
                    json.dumps(content["options"]),
                    content["working_dir"],
                    content["project_id"],
                    content["environment"],
                    content["exit_code"],
                    content["execution_time"],
                    content["executed_at"],
                    content["user_id"],
                    content["source"],
                    json.dumps(content["tags"]) if content["tags"] else None,
                    1 if content["is_successful"] else 0,
                    content["sensitivity_level"],
                    1 if content["is_explicit"] else 0,
                    command_id
                ))
                conn.commit()
            
            # 创建新版本记录回滚操作
            # JSON中的datetime是字符串，需要转换回datetime对象
            def _parse_dt(val):
                if isinstance(val, str) and val:
                    try:
                        return datetime.fromisoformat(val)
                    except ValueError:
                        return None
                return val

            record = CommandRecord(
                command_id=command_id,
                raw_command=content.get("raw_command", ""),
                command_name=content.get("command_name", ""),
                arguments=content.get("arguments", []),
                options=content.get("options", {}),
                working_dir=content.get("working_dir", ""),
                session_id=content.get("session_id"),
                project_id=content.get("project_id"),
                environment=content.get("environment"),
                exit_code=content.get("exit_code"),
                execution_time=content.get("execution_time"),
                executed_at=_parse_dt(content.get("executed_at")),
                user_id=content.get("user_id"),
                source=content.get("source", "shell"),
                tags=content.get("tags"),
                is_successful=content.get("is_successful", True),
                sensitivity_level=content.get("sensitivity_level", "public"),
                is_explicit=content.get("is_explicit", False),
                usage_count=content.get("usage_count", 1),
                last_used_at=_parse_dt(content.get("last_used_at")),
            )
            self.create_version(record, change_reason=f"rollback to version {version_id}")
            
            logger.info(f"Rolled back command {command_id} to version {version_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to rollback command {command_id} to version {version_id}", exception=e)
            return False
    
    def delete_version_history(self, command_id: str) -> int:
        """删除指定命令的所有版本历史"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute("DELETE FROM memory_versions WHERE command_id = ?", (command_id,))
                deleted = cursor.rowcount
                conn.commit()
                logger.debug(f"Deleted {deleted} versions for command {command_id}")
                return deleted
        except Exception as e:
            logger.error(f"Failed to delete version history for command {command_id}", exception=e)
            return 0


# 全局实例
_version_manager = None


def get_version_manager() -> VersionManager:
    global _version_manager
    if _version_manager is None:
        _version_manager = VersionManager()
    return _version_manager
