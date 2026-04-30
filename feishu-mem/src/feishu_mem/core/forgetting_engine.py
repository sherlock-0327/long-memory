import json
from datetime import datetime, timedelta
from typing import List, Tuple
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger
from .storage import CommandRecord


@dataclass
class MemoryScore:
    command_id: str
    score: float
    reason: str


class ForgettingEngine:
    """
    遗忘引擎，基于Ebbinghaus遗忘曲线和价值评估实现智能记忆管理
    记忆价值评分 = 新鲜度权重 * 频率权重 * 显式标记权重 * 上下文权重
    """
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.db_path
        # 遗忘参数配置 - 使用全局配置项
        self.recency_decay = 0.5  # 新鲜度衰减系数
        self.frequency_threshold = 10.0  # 频率得分上限阈值
        self.explicit_weight = 2.0  # 显式标记记忆权重
        self.forget_threshold = 0.1  # 低价值记忆遗忘阈值
    
    def calculate_memory_score(self, record: CommandRecord) -> MemoryScore:
        """计算单条记忆的价值得分"""
        # 1. 新鲜度得分：最近使用的时间越近，得分越高，符合遗忘曲线
        days_since_last_use = (datetime.now() - record.last_used_at).days if record.last_used_at else 365
        recency_score = 1.0 / (days_since_last_use ** self.recency_decay + 1)
        
        # 2. 频率得分：使用次数越多，得分越高，上限为1.0
        frequency_score = min(record.usage_count / self.frequency_threshold, 1.0)
        
        # 3. 显式标记权重：用户主动教学的记忆权重更高
        explicit_bonus = self.explicit_weight if record.is_explicit else 1.0
        
        # 4. 成功率权重：执行成功的命令权重更高
        success_bonus = 1.2 if record.is_successful else 0.8
        
        # 5. 综合得分
        total_score = recency_score * 0.4 + frequency_score * 0.3
        total_score *= explicit_bonus * success_bonus
        
        reason = (
            f"新鲜度:{recency_score:.2f}, 频率:{frequency_score:.2f}, "
            f"显式标记:{explicit_bonus:.1f}x, 成功:{success_bonus:.1f}x"
        )
        
        return MemoryScore(
            command_id=record.command_id,
            score=total_score,
            reason=reason
        )
    
    def get_low_value_memories(self, threshold: float = None, limit: int = 100) -> List[Tuple[CommandRecord, MemoryScore]]:
        """获取低于阈值的低价值记忆，准备遗忘"""
        threshold = threshold or self.forget_threshold
        results = []
        
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                # 只查询非显式标记的记忆，显式标记的记忆永不自动遗忘
                cursor = conn.execute("""
                    SELECT * FROM commands 
                    WHERE is_explicit = 0 
                    ORDER BY usage_count ASC, last_used_at ASC 
                    LIMIT ?
                """, (limit,))
                
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
                    
                    score = self.calculate_memory_score(record)
                    if score.score < threshold:
                        results.append((record, score))
            
            logger.debug(f"Found {len(results)} low value memories below threshold {threshold}")
            return results
            
        except Exception as e:
            logger.error("Failed to get low value memories", exception=e)
            return []
    
    def forget_low_value_memory(self, threshold: float = None, dry_run: bool = False) -> int:
        """遗忘低价值记忆，返回删除的数量"""
        low_value = self.get_low_value_memories(threshold)
        if not low_value:
            return 0
        
        delete_count = 0
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                for record, score in low_value:
                    logger.debug(f"Forgetting memory: {record.command_id}, score: {score.score:.2f}, reason: {score.reason}, command: {record.raw_command[:50]}")
                    
                    if not dry_run:
                        conn.execute("DELETE FROM commands WHERE command_id = ?", (record.command_id,))
                        delete_count += 1
                
                if not dry_run:
                    conn.commit()
            
            if delete_count > 0:
                logger.info(f"Forgot {delete_count} low value memories")
            
            return delete_count
            
        except Exception as e:
            logger.error("Failed to forget low value memories", exception=e)
            return 0
    
    def auto_cleanup_expired_memory(self) -> int:
        """自动清理过期记忆：使用全局配置的时间参数
        - 临时记忆（使用次数=1）：保留temporary_memory_days天
        - 短期记忆（使用次数<3）：保留short_term_memory_days天
        - 所有非显式记忆：最长保留max_memory_days天
        """
        deleted = 0
        now = datetime.now()
        
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # 清理过期的临时记忆（使用次数=1）
                temp_cutoff = now - timedelta(days=config.temporary_memory_days)
                cursor = conn.execute("""
                    DELETE FROM commands 
                    WHERE usage_count = 1 
                      AND last_used_at < ? 
                      AND is_explicit = 0
                """, (temp_cutoff.isoformat(),))
                deleted += cursor.rowcount
                logger.debug(f"Cleaned up {cursor.rowcount} temporary memories (>{config.temporary_memory_days} days)")
                
                # 清理过期的短期记忆（使用次数<3）
                short_cutoff = now - timedelta(days=config.short_term_memory_days)
                cursor = conn.execute("""
                    DELETE FROM commands 
                    WHERE usage_count < 3 
                      AND last_used_at < ? 
                      AND is_explicit = 0
                """, (short_cutoff.isoformat(),))
                deleted += cursor.rowcount
                logger.debug(f"Cleaned up {cursor.rowcount} short term memories (>{config.short_term_memory_days} days)")
                
                # 清理超过最长保留时间的所有非显式记忆
                long_cutoff = now - timedelta(days=config.max_memory_days)
                cursor = conn.execute("""
                    DELETE FROM commands 
                    WHERE last_used_at < ? 
                      AND is_explicit = 0
                """, (long_cutoff.isoformat(),))
                deleted += cursor.rowcount
                logger.debug(f"Cleaned up {cursor.rowcount} expired memories (>{config.max_memory_days} days)")
                
                conn.commit()
                
            if deleted > 0:
                logger.info(f"Total expired memories cleaned up: {deleted}")
            
            return deleted
            
        except Exception as e:
            logger.error("Failed to clean up expired memory", exception=e)
            return 0
    
    def protect_explicit_memory(self) -> None:
        """保护所有显式标记的记忆，设置为永不自动遗忘"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # 确保显式记忆的使用次数足够高，不会被遗忘算法清理
                conn.execute("""
                    UPDATE commands 
                    SET usage_count = MAX(usage_count, 100) 
                    WHERE is_explicit = 1
                """)
                conn.commit()
                logger.debug("Explicit memories protected")
        except Exception as e:
            logger.error("Failed to protect explicit memory", exception=e)


# 全局实例
_forgetting_engine = None


def get_forgetting_engine() -> ForgettingEngine:
    global _forgetting_engine
    if _forgetting_engine is None:
        _forgetting_engine = ForgettingEngine()
    return _forgetting_engine
