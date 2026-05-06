from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass
from datetime import datetime
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
    source: str  # prefix_match / semantic_search / hybrid_boost / workflow_suggestion
    description: Optional[str] = None
    usage_count: int = 0
    last_used: Optional[datetime] = None


class CompletionEngine:
    """
    智能补全引擎，实现三层检索架构：前缀匹配→语义检索→智能排序
    采用混合搜索策略（参考claude-mem的HybridSearchStrategy）：
    1. SQLite前缀匹配获取候选集
    2. ChromaDB语义搜索获取语义相关集
    3. 交集提升（出现在两种结果中的命令获得额外权重）
    4. 综合排序输出
    """

    def __init__(self, storage: Storage, vector_store=None):
        self.storage = storage
        self.vector_store = vector_store
        self.version = "v2.0"
        # 上下文经济统计（参考claude-mem的token economics）
        self._total_queries = 0
        self._cache_hits = 0
        self._total_chars_input = 0
        self._total_chars_suggested = 0
        self._prefix_match_count = 0
        self._semantic_match_count = 0
        self._hybrid_boost_count = 0
        logger.info(
            f"Completion engine {self.version} initialized, "
            f"vector search: {'enabled' if self.vector_store else 'disabled'}"
        )

    def _get_cache_key(self, prefix: str, project_id: Optional[str], environment: Optional[str]) -> str:
        key_content = f"completion:{self.version}:{prefix}:{project_id}:{environment}"
        return hashlib.sha256(key_content.encode()).hexdigest()[:32]

    def _calculate_score(
        self,
        record: CommandRecord,
        prefix_length: int,
        context: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        计算补全项的综合得分，权重分配：
        - 频率：40%
        - 新鲜度：30%
        - 匹配精准度：20%
        - 显式标记加成
        - 上下文关联加成
        """
        try:
            recency = (
                1.0 / (max((datetime.now() - record.last_used_at).days, 1) + 1)
                if record.last_used_at
                else 0.5
            )
            frequency = min(record.usage_count / 10.0, 1.0)
            explicit_bonus = 8.0 if record.is_explicit else 1.0

            if not record.raw_command:
                prefix_bonus = 0.0
            else:
                prefix_bonus = min(prefix_length / len(record.raw_command), 1.0)

            context_bonus = 1.0
            if context and context.get("last_command"):
                context_bonus = 1.1

            return (frequency * 0.4 + recency * 0.3 + prefix_bonus * 0.2) * explicit_bonus * context_bonus
        except Exception as e:
            logger.error("Failed to calculate completion score", exception=e)
            return 0.0

    def _hybrid_merge(
        self,
        prefix_items: Dict[str, CompletionItem],
        semantic_items: Dict[str, CompletionItem],
        limit: int,
    ) -> List[CompletionItem]:
        """
        混合搜索合并策略（参考claude-mem HybridSearchStrategy）：
        - 前缀匹配结果保留
        - 语义搜索结果保留
        - 同时出现在两种结果中的命令获得额外权重提升
        - 按综合得分降序排列
        """
        merged: Dict[str, CompletionItem] = {}
        prefix_keys: Set[str] = set(prefix_items.keys())
        semantic_keys: Set[str] = set(semantic_items.keys())

        # 交集提升：同时命中前缀和语义的命令
        intersection = prefix_keys & semantic_keys
        for cmd in intersection:
            item = prefix_items[cmd]
            # 混合提升：取两种得分的加权平均，额外乘以1.3倍
            semantic_score = semantic_items[cmd].score
            boosted_score = (item.score * 0.6 + semantic_score * 0.4) * 1.3
            merged[cmd] = CompletionItem(
                command=item.command,
                score=boosted_score,
                source="hybrid_boost",
                usage_count=item.usage_count,
                last_used=item.last_used,
            )
            self._hybrid_boost_count += 1

        # 仅前缀匹配的结果
        for cmd in prefix_keys - intersection:
            merged[cmd] = prefix_items[cmd]

        # 仅语义搜索的结果
        for cmd in semantic_keys - intersection:
            merged[cmd] = semantic_items[cmd]

        # 按得分降序排列
        sorted_items = sorted(merged.values(), key=lambda x: -x.score)
        return sorted_items[:limit]

    def get_completions(
        self,
        prefix: str,
        project_id: Optional[str] = None,
        environment: Optional[str] = None,
        limit: int = 5,
    ) -> List[CompletionItem]:
        """
        获取补全建议，采用混合搜索策略，总耗时控制在100ms以内
        流程：缓存查询 → 前缀匹配 + 语义检索 → 混合合并 → 去重输出
        """
        start_time = time.perf_counter()
        self._total_queries += 1
        self._total_chars_input += len(prefix)

        if len(prefix) < config.min_prefix_length:
            return []

        # 缓存查询
        cache_key = self._get_cache_key(prefix, project_id, environment)
        cached = get_l1_cache().get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            try:
                items = [CompletionItem(**item) for item in cached]
                self._total_chars_suggested += sum(len(c.command) for c in items)
                return items
            except Exception as e:
                logger.warning("Failed to deserialize cached completion items", exception=e)

        try:
            context = {"project_id": project_id, "environment": environment}

            # 第一层：前缀匹配（目标<30ms）
            prefix_start = time.perf_counter()
            prefix_items: Dict[str, CompletionItem] = {}
            try:
                prefix_matches = self.storage.get_prefix_matches(prefix, project_id, environment, limit=limit * 2)
                for record in prefix_matches:
                    score = self._calculate_score(record, len(prefix), context)
                    prefix_items[record.raw_command] = CompletionItem(
                        command=record.raw_command,
                        score=score,
                        source="prefix_match",
                        usage_count=record.usage_count,
                        last_used=record.last_used_at,
                    )
                    self._prefix_match_count += 1
            except Exception as db_error:
                logger.warning("Prefix match query failed", exception=db_error)

            prefix_elapsed = time.perf_counter() - prefix_start
            logger.debug(f"Prefix match: {len(prefix_items)} results, {prefix_elapsed*1000:.1f}ms")

            # 第二层：语义搜索（目标<50ms）
            semantic_items: Dict[str, CompletionItem] = {}
            semantic_time_remaining = max(0, 0.05 - (time.perf_counter() - start_time))
            if len(prefix) > 2 and self.vector_store and semantic_time_remaining > 0.01:
                semantic_start = time.perf_counter()
                try:
                    vector_results = self.vector_store.search_commands(
                        prefix,
                        project_id=project_id,
                        environment=environment,
                        limit=limit * 2,
                        min_score=config.vector_search_min_score,
                    )

                    if vector_results:
                        command_ids = [res["command_id"] for res in vector_results]
                        search_records = self.storage.get_commands_by_ids(command_ids)
                        id_to_record = {r.command_id: r for r in search_records}

                        for vector_result in vector_results:
                            record = id_to_record.get(vector_result["command_id"])
                            if record and record.raw_command not in prefix_items:
                                score = self._calculate_score(record, 0, context) * vector_result["score"]
                                semantic_items[record.raw_command] = CompletionItem(
                                    command=record.raw_command,
                                    score=score,
                                    source="semantic_search",
                                    usage_count=record.usage_count,
                                    last_used=record.last_used_at,
                                )
                                self._semantic_match_count += 1
                except Exception as vs_error:
                    logger.warning("Vector search failed, falling back to prefix only", exception=vs_error)

                semantic_elapsed = time.perf_counter() - semantic_start
                logger.debug(f"Semantic search: {len(semantic_items)} results, {semantic_elapsed*1000:.1f}ms")

            # 第三层：混合合并与排序
            if semantic_items:
                unique_completions = self._hybrid_merge(prefix_items, semantic_items, limit)
            else:
                unique_completions = sorted(prefix_items.values(), key=lambda x: -x.score)[:limit]

            # 缓存结果
            if unique_completions:
                try:
                    cache_value = [
                        {
                            "command": item.command,
                            "score": item.score,
                            "source": item.source,
                            "description": item.description,
                            "usage_count": item.usage_count,
                            "last_used": item.last_used.isoformat() if item.last_used else None,
                        }
                        for item in unique_completions
                    ]
                    get_l1_cache().set(cache_key, cache_value, ttl=60)
                except Exception as cache_error:
                    logger.warning("Failed to cache completion results", exception=cache_error)

            total_elapsed = time.perf_counter() - start_time
            self._total_chars_suggested += sum(len(c.command) for c in unique_completions)

            if total_elapsed > 0.1:
                logger.warning(f"Completion exceeded 100ms: {total_elapsed*1000:.1f}ms for '{prefix}'")

            return unique_completions

        except Exception as e:
            logger.error("Failed to get completions", exception=e, prefix=prefix)
            return []

    def get_workflow_suggestions(
        self,
        last_command: str,
        project_id: Optional[str] = None,
        environment: Optional[str] = None,
        limit: int = 3,
    ) -> List[CompletionItem]:
        """根据上一条命令生成工作流建议，预测下一个可能执行的命令"""
        try:
            recent_sequences = self.storage.get_recent_command_sequences(project_id, environment, min_length=2)

            suggestions = []
            for sequence in recent_sequences:
                if len(sequence) >= 2 and sequence[-2].raw_command == last_command:
                    next_cmd = sequence[-1]
                    score = 0.7
                    suggestions.append(CompletionItem(
                        command=next_cmd.raw_command,
                        score=score,
                        source="workflow_suggestion",
                        description=f"工作流推荐: 执行`{last_command}`后常用命令",
                    ))

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
            if self._total_chars_suggested > 0
            else 0.0
        )
        return {
            "total_queries": self._total_queries,
            "cache_hits": self._cache_hits,
            "cache_hit_rate_percent": round(cache_hit_rate, 1),
            "total_chars_input": self._total_chars_input,
            "total_chars_suggested": self._total_chars_suggested,
            "char_savings_rate_percent": round(char_savings_rate, 1),
            "prefix_match_count": self._prefix_match_count,
            "semantic_match_count": self._semantic_match_count,
            "hybrid_boost_count": self._hybrid_boost_count,
        }
