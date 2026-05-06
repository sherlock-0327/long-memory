"""
会话生命周期管理器（参考claude-mem的SessionCompletionHandler）
管理Shell会话的创建、活跃、完成和异常终止状态
支持会话级别的记忆聚合和统计
"""
import sqlite3
import uuid
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from pathlib import Path

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger


@dataclass
class SessionInfo:
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    environment: Optional[str] = None
    status: str = "active"  # active / completed / abandoned
    command_count: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    last_activity_at: datetime = field(default_factory=datetime.now)


class SessionManager:
    """
    会话生命周期管理器
    职责：
    1. 跟踪Shell会话的创建和销毁
    2. 统计会话级别的命令数量和活跃时长
    3. 在会话完成时触发记忆聚合
    4. 检测并标记超时的僵尸会话
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or config.db_path
        self._init_session_table()
        self._active_session: Optional[SessionInfo] = None
        logger.info("Session manager initialized")

    def _init_session_table(self) -> None:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        project_id TEXT,
                        environment TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        command_count INTEGER DEFAULT 0,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        last_activity_at TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC)")
                conn.commit()
        except Exception as e:
            logger.error("Failed to initialize session table", exception=e)

    def create_session(
        self,
        user_id: str,
        project_id: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> SessionInfo:
        """创建新会话"""
        session = SessionInfo(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            project_id=project_id,
            environment=environment,
        )

        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, user_id, project_id, environment, status,
                                          command_count, started_at, last_activity_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.user_id,
                        session.project_id,
                        session.environment,
                        session.status,
                        session.command_count,
                        session.started_at.isoformat(),
                        session.last_activity_at.isoformat(),
                    ),
                )
                conn.commit()

            self._active_session = session
            logger.debug(f"Session created: {session.session_id}, user: {user_id}")
            return session

        except Exception as e:
            logger.error("Failed to create session", exception=e)
            return session

    def get_or_create_active_session(
        self,
        user_id: str,
        project_id: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> SessionInfo:
        """获取当前活跃会话，不存在则创建"""
        if self._active_session and self._active_session.status == "active":
            return self._active_session

        # 尝试从数据库恢复最近的活跃会话
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE user_id = ? AND status = 'active'
                    ORDER BY last_activity_at DESC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()
                if row:
                    session = SessionInfo(
                        session_id=row["session_id"],
                        user_id=row["user_id"],
                        project_id=row["project_id"],
                        environment=row["environment"],
                        status=row["status"],
                        command_count=row["command_count"],
                        started_at=datetime.fromisoformat(row["started_at"]),
                        completed_at=(
                            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
                        ),
                        last_activity_at=datetime.fromisoformat(row["last_activity_at"]),
                    )
                    self._active_session = session
                    return session
        except Exception as e:
            logger.warning("Failed to restore active session", exception=e)

        return self.create_session(user_id, project_id, environment)

    def record_command(self, session_id: str) -> None:
        """记录会话中的命令执行"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET command_count = command_count + 1, last_activity_at = ?
                    WHERE session_id = ?
                    """,
                    (datetime.now().isoformat(), session_id),
                )
                conn.commit()

            if self._active_session and self._active_session.session_id == session_id:
                self._active_session.command_count += 1
                self._active_session.last_activity_at = datetime.now()

        except Exception as e:
            logger.warning("Failed to record command in session", exception=e)

    def complete_session(self, session_id: str) -> bool:
        """标记会话为完成状态"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'completed', completed_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (datetime.now().isoformat(), session_id),
                )
                conn.commit()

            if self._active_session and self._active_session.session_id == session_id:
                self._active_session.status = "completed"
                self._active_session.completed_at = datetime.now()
                self._active_session = None

            logger.debug(f"Session completed: {session_id}")
            return True

        except Exception as e:
            logger.error("Failed to complete session", exception=e)
            return False

    def abandon_stale_sessions(self, timeout_minutes: int = 30) -> int:
        """标记超时的活跃会话为异常终止"""
        cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'abandoned', completed_at = ?
                    WHERE status = 'active' AND last_activity_at < ?
                    """,
                    (datetime.now().isoformat(), cutoff.isoformat()),
                )
                abandoned = cursor.rowcount
                conn.commit()

            if abandoned > 0:
                logger.info(f"Abandoned {abandoned} stale sessions")

            if self._active_session and self._active_session.last_activity_at < cutoff:
                self._active_session = None

            return abandoned

        except Exception as e:
            logger.error("Failed to abandon stale sessions", exception=e)
            return 0

    def get_session_stats(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话统计信息"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
                row = cursor.fetchone()
                if not row:
                    return None

                started = datetime.fromisoformat(row["started_at"])
                completed = datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else datetime.now()
                duration = (completed - started).total_seconds()

                return {
                    "session_id": row["session_id"],
                    "user_id": row["user_id"],
                    "project_id": row["project_id"],
                    "environment": row["environment"],
                    "status": row["status"],
                    "command_count": row["command_count"],
                    "duration_seconds": round(duration, 1),
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                }

        except Exception as e:
            logger.error("Failed to get session stats", exception=e)
            return None

    def get_user_sessions(
        self, user_id: str, limit: int = 10, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """获取用户的会话列表"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                params: list = [user_id]
                status_clause = ""
                if status:
                    status_clause = "AND status = ?"
                    params.append(status)
                params.append(limit)

                cursor = conn.execute(
                    f"""
                    SELECT * FROM sessions
                    WHERE user_id = ? {status_clause}
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    params,
                )

                sessions = []
                for row in cursor.fetchall():
                    started = datetime.fromisoformat(row["started_at"])
                    completed = (
                        datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else datetime.now()
                    )
                    sessions.append({
                        "session_id": row["session_id"],
                        "status": row["status"],
                        "command_count": row["command_count"],
                        "duration_seconds": round((completed - started).total_seconds(), 1),
                        "started_at": row["started_at"],
                    })

                return sessions

        except Exception as e:
            logger.error("Failed to get user sessions", exception=e)
            return []


# 全局实例
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
