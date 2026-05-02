from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime
import json
import hashlib
import time

from .storage import Storage, CommandRecord
from feishu_mem.shared.cache import get_l1_cache
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

@dataclass
class CompletionItem:
    command: str
    score: float
    source: str  # prefix_match / semantic_search / workflow_suggestion
    description: Optional[str] = None
    usage_count: int = 0
    last_used: Optional[datetime] = None

class CompletionEngine:
    """智能补全引擎，实现三层检索架构：前缀匹配→语义检索→智能排序"""
    def __init__(self, storage: Storage, vector_store=None):
        self.storage = storage
        self.vector_store = vector_store
        self.version = "v1.0"
        # 上下文经济统计（参考claude-mem的token economics）
        self._total_queries = 0
        self._cache_hits = 0
        self._total_chars_input = 0
        self._total_chars_suggested = 0
        logger.info(f"Completion engine {self.version} initialized, vector search: {'enabled' if self.vector_store else 'disabled'}")
    
    def _get_cache_key(self, prefix: str, project_id: Optional[str], environment: Optional[str]) -> str:
        """生成缓存key"""
        key_content = f"completion:{self.version}:{prefix}:{project_id}:{environment}"
        return hashlib.md5(key_content.encode()).hexdigest()
    
    def _calculate_score(self, record: CommandRecord, prefix_length: int, context: Optional[Dict[str, Any]] = None) -> float:
        """
        计算补全项的综合得分，权重分配：
        - 频率：40%
        - 新鲜度：30%
        - 匹配精准度：20%
        - 显式标记：10%
        """
        try:
            # 基础得分
            recency = 1.0 / (max((datetime.now() - record.last_used_at).days, 1) + 1) if record.last_used_at else 0.5
            frequency = min(record.usage_count / 10.0, 1.0)
            explicit_bonus = 8.0 if record.is_explicit else 1.0

            # 前缀匹配精准度加分
            if not record.raw_command:
                prefix_bonus = 0.0
            else:
                prefix_bonus = min(prefix_length / len(record.raw_command), 1.0)

            # 上下文匹配加分
            context_bonus = 1.0
            if context and context.get("last_command"):
                # 如果和上一条命令存在序列关联，增加权重
                context_bonus = 1.1

            return (frequency * 0.4 + recency * 0.3 + prefix_bonus * 0.2) * explicit_bonus * context_bonus
        except Exception as e:
            logger.error("Failed to calculate completion score", exception=e)
            return 0.0
    
    def get_completions(self, prefix: str, project_id: Optional[str] = None, environment: Optional[str] = None, limit: int = 5) -> List[CompletionItem]:
        """
        获取补全建议，严格遵循三层检索流程，总耗时控制在100ms以内
        流程：缓存查询 → 前缀匹配 → 语义检索 → 智能排序 → 去重输出
        """
        start_time = time.perf_counter()
        self._total_queries += 1
        self._total_chars_input += len(prefix)

        # 太短的前缀不返回结果，避免误匹配
        if len(prefix) < config.min_prefix_length:
            return []
        
        # 尝试从缓存获取
        cache_key = self._get_cache_key(prefix, project_id, environment)
        cached = get_l1_cache().get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            logger.debug(f"Completion cache hit for prefix: '{prefix}', elapsed: {time.perf_counter() - start_time:.3f}ms")
            try:
                items = [CompletionItem(**item) for item in cached]
                self._total_chars_suggested += sum(len(c.command) for c in items)
                return items
            except Exception as e:
                logger.warning("Failed to deserialize cached completion items", exception=e)
                # 缓存损坏，继续执行正常流程
        
        try:
            completions = []
            context = {"project_id": project_id, "environment": environment}
            
            # 第一层：前缀匹配（快速，目标耗时<30ms）
            prefix_start = time.perf_counter()
            prefix_matches = []
            try:
                prefix_matches = self.storage.get_prefix_matches(prefix, project_id, environment, limit=limit*2)
            except Exception as db_error:
                logger.warning("Prefix match query failed", exception=db_error)
            
            for record in prefix_matches:
                try:
                    score = self._calculate_score(record, len(prefix), context)
                    completions.append(CompletionItem(
                        command=record.raw_command,
                        score=score,
                        source="prefix_match",
                        usage_count=record.usage_count,
                        last_used=record.last_used_at
                    ))
                except Exception as item_error:
                    logger.warning("Failed to process prefix match item", exception=item_error)
                    continue
                    
            logger.debug(f"Prefix match found {len(prefix_matches)} results, elapsed: {time.perf_counter() - prefix_start:.3f}ms")
            
            # 第二层：语义搜索（如果前缀匹配不够，目标耗时<50ms）
            semantic_time_remaining = max(0, 0.05 - (time.perf_counter() - start_time))  # 语义搜索最多留50ms
            if len(completions) < limit and len(prefix) > 2 and self.vector_store and semantic_time_remaining > 0.01:
                semantic_start = time.perf_counter()
                vector_results = []
                try:
                    # 先从向量库搜索
                    vector_results = self.vector_store.search_commands(
                        prefix, 
                        project_id=project_id, 
                        environment=environment,
                        limit=limit,
                        min_score=config.vector_search_min_score
                    )
                except Exception as vs_error:
                    logger.warning("Vector search failed, falling back to prefix only", exception=vs_error)
                
                # 根据command_id获取完整命令记录
                if vector_results:
                    try:
                        command_ids = [res["command_id"] for res in vector_results]
                        search_records = self.storage.get_commands_by_ids(command_ids)
                        
                        for record, vector_result in zip(search_records, vector_results):
                            try:
                                # 避免重复
                                if not any(c.command == record.raw_command for c in completions):
                                    # 语义搜索得分基于向量相似度
                                    score = self._calculate_score(record, 0, context) * vector_result["score"]
                                    completions.append(CompletionItem(
                                        command=record.raw_command,
                                        score=score,
                                        source="semantic_search",
                                        usage_count=record.usage_count,
                                        last_used=record.last_used_at
                                    ))
                            except Exception as item_error:
                                logger.warning("Failed to process semantic search item", exception=item_error)
                                continue
                    except Exception as db_error:
                        logger.warning("Failed to get command records for semantic results", exception=db_error)
                        
                logger.debug(f"Semantic search found {len(vector_results)} results, elapsed: {time.perf_counter() - semantic_start:.3f}ms")
            
            # 第三层：智能排序和去重（目标耗时<20ms）
            sort_start = time.perf_counter()
            seen = set()
            unique_completions = []
            # 按来源优先级（prefix_match > semantic_search > workflow_suggestion）和得分降序排序
            source_priority = {"prefix_match": 0, "semantic_search": 1, "workflow_suggestion": 2}
            for item in sorted(completions, key=lambda x: (source_priority.get(x.source, 9), -x.score)):
                if item.command not in seen:
                    seen.add(item.command)
                    unique_completions.append(item)
                    if len(unique_completions) >= limit:
                        break
            
            logger.debug(f"Sorting and deduplication done, {len(unique_completions)} unique results, elapsed: {time.perf_counter() - sort_start:.3f}ms")
            
            # 缓存结果1分钟（仅当有结果时缓存）
            if unique_completions:
                try:
                    cache_value = [
                        {
                            "command": item.command, 
                            "score": item.score, 
                            "source": item.source, 
                            "description": item.description,
                            "usage_count": item.usage_count,
                            "last_used": item.last_used.isoformat() if item.last_used else None
                        }
                        for item in unique_completions
                    ]
                    get_l1_cache().set(cache_key, cache_value, ttl=60)
                except Exception as cache_error:
                    logger.warning("Failed to cache completion results", exception=cache_error)
            
            total_elapsed = time.perf_counter() - start_time
            self._total_chars_suggested += sum(len(c.command) for c in unique_completions)
            logger.debug(f"Completion request total elapsed: {total_elapsed*1000:.3f}ms, returned {len(unique_completions)} items")

            # 超过100ms的请求记录警告
            if total_elapsed > 0.1:
                logger.warning(f"Completion request exceeded 100ms: {total_elapsed*1000:.3f}ms for prefix '{prefix}'")

            return unique_completions
            
        except Exception as e:
            logger.error("Failed to get completions", exception=e, prefix=prefix)
            return []
    
    def get_workflow_suggestions(self, last_command: str, project_id: Optional[str] = None, environment: Optional[str] = None, limit: int = 3) -> List[CompletionItem]:
        """根据上一条命令生成工作流建议，预测下一个可能执行的命令"""
        try:
            # 简单的序列模式匹配，基于历史执行序列
            # 生产版本可以使用更复杂的序列挖掘算法
            recent_sequences = self.storage.get_recent_command_sequences(project_id, environment, min_length=2)
            
            suggestions = []
            for sequence in recent_sequences:
                if len(sequence) >= 2 and sequence[-2].raw_command == last_command:
                    next_cmd = sequence[-1]
                    score = 0.7  # 固定权重，后续可优化
                    suggestions.append(CompletionItem(
                        command=next_cmd.raw_command,
                        score=score,
                        source="workflow_suggestion",
                        description=f"工作流推荐: 执行`{last_command}`后常用命令"
                    ))
            
            # 去重排序
            seen = set()
            unique_suggestions = []
            for item in sorted(suggestions, key=lambda x: x.score, reverse=True):
                if item.command not in seen:
                    seen.add(item.command)
                    unique_suggestions.append(item)
                    if len(unique_suggestions) >= limit:
                        break
            
            return unique_suggestions
            
        except Exception as e:
            logger.error("Failed to get workflow suggestions", exception=e)
            return []

    def get_economics(self) -> Dict[str, Any]:
        """获取补全引擎的上下文经济学统计（参考claude-mem token economics）"""
        cache_hit_rate = (self._cache_hits / self._total_queries * 100) if self._total_queries > 0 else 0.0
        char_savings_rate = (
            (1 - self._total_chars_input / max(self._total_chars_suggested, 1)) * 100
            if self._total_chars_suggested > 0 else 0.0
        )
        return {
            "total_queries": self._total_queries,
            "cache_hits": self._cache_hits,
            "cache_hit_rate_percent": round(cache_hit_rate, 1),
            "total_chars_input": self._total_chars_input,
            "total_chars_suggested": self._total_chars_suggested,
            "char_savings_rate_percent": round(char_savings_rate, 1),
        }
