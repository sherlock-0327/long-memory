"""
向量存储引擎，基于ChromaDB实现语义检索能力
支持命令语义向量存储、更新、搜索功能
采用细粒度向量文档策略（参考claude-mem）：每条命令生成多个向量文档以提升语义搜索精度
"""
import chromadb
import json
import uuid
import os
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from chromadb.config import Settings
from chromadb.utils import embedding_functions

from feishu_mem.core.storage import CommandRecord
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger
from feishu_mem.shared.cache import get_l1_cache


class VectorStore:
    def __init__(self, persist_directory: Optional[Path] = None):
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

        self.persist_dir = persist_directory or config.vector_db_path
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )

        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=config.embedding_model_name,
            device=config.embedding_device
        )

        self.collection = self.client.get_or_create_collection(
            name="command_embeddings",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

        logger.info(f"Vector store initialized, persist directory: {self.persist_dir}")
        logger.info(f"Embedding model: {config.embedding_model_name}, device: {config.embedding_device}")

    def _get_document_content(self, record: CommandRecord) -> str:
        """生成命令的完整文档内容，用于生成主向量"""
        parts = [
            f"命令: {record.raw_command}",
            f"命令名: {record.command_name}",
            f"参数: {' '.join(record.arguments)}" if record.arguments else "",
            f"项目: {record.project_id}" if record.project_id else "",
            f"环境: {record.environment}" if record.environment else "",
            f"标签: {' '.join(record.tags)}" if record.tags else ""
        ]
        return "\n".join(filter(None, parts))

    def _get_granular_documents(self, record: CommandRecord) -> List[Dict[str, str]]:
        """生成细粒度向量文档（参考claude-mem的granular vector documents策略）
        每条命令生成多个文档：完整命令 + 命令动作 + 参数组合
        提升语义搜索时对部分查询的匹配精度
        """
        docs = []
        metadata_base = {
            "command_id": record.command_id,
            "command_name": record.command_name,
            "project_id": record.project_id or "",
            "environment": record.environment or "",
            "is_explicit": record.is_explicit,
            "usage_count": record.usage_count,
            "executed_at": record.executed_at.isoformat() if record.executed_at else "",
            "source": record.source,
        }

        # 文档1：完整命令（主文档）
        full_doc = self._get_document_content(record)
        docs.append({
            "id": record.command_id,
            "document": full_doc,
            "metadata": {**metadata_base, "doc_type": "full"},
        })

        # 文档2：命令动作（command_name + 子命令）
        action_parts = [record.command_name] + record.arguments[:2]
        action_doc = " ".join(action_parts)
        if action_doc != full_doc and len(action_doc) > 2:
            docs.append({
                "id": f"{record.command_id}#action",
                "document": action_doc,
                "metadata": {**metadata_base, "doc_type": "action"},
            })

        # 文档3：参数组合（如果有多个参数）
        if len(record.arguments) > 2:
            args_doc = " ".join(record.arguments)
            docs.append({
                "id": f"{record.command_id}#args",
                "document": args_doc,
                "metadata": {**metadata_base, "doc_type": "args"},
            })

        return docs
    
    def add_command(self, record: CommandRecord) -> Optional[str]:
        """添加命令到向量数据库，使用细粒度向量文档策略"""
        try:
            granular_docs = self._get_granular_documents(record)

            ids = [d["id"] for d in granular_docs]
            documents = [d["document"] for d in granular_docs]
            metadatas = [d["metadata"] for d in granular_docs]

            self.collection.add(ids=ids, documents=documents, metadatas=metadatas)

            logger.debug(f"Added command to vector store with {len(granular_docs)} docs, id: {record.command_id}")
            return record.command_id

        except Exception as e:
            logger.error("Failed to add command to vector store", exception=e, command_id=record.command_id)
            return None
    
    def delete_command(self, command_id: str) -> bool:
        """从向量数据库删除指定命令及其所有细粒度文档"""
        try:
            doc_ids = [command_id, f"{command_id}#action", f"{command_id}#args"]
            self.collection.delete(ids=doc_ids)
            logger.debug(f"Deleted command and granular docs from vector store: {command_id}")

            get_l1_cache().invalidate_pattern("completion:")
            return True
        except Exception as e:
            logger.error("Failed to delete command from vector store", exception=e, command_id=command_id)
            return False

    def update_command_metadata(self, command_id: str, metadata: Dict[str, Any]) -> bool:
        """更新命令的元数据"""
        try:
            self.collection.update(
                ids=[command_id],
                metadatas=[metadata]
            )
            logger.debug(f"Updated command metadata in vector store: {command_id}, metadata: {metadata.keys()}")

            # 失效相关缓存
            get_l1_cache().invalidate_pattern("completion:")
            return True
        except Exception as e:
            logger.error("Failed to update command metadata in vector store", exception=e, command_id=command_id)
            return False
    
    def batch_add_commands(self, records: List[CommandRecord]) -> List[str]:
        """批量添加命令到向量数据库，使用细粒度向量文档策略"""
        if not records:
            return []

        try:
            all_ids = []
            all_documents = []
            all_metadatas = []

            for record in records:
                granular_docs = self._get_granular_documents(record)
                for doc in granular_docs:
                    all_ids.append(doc["id"])
                    all_documents.append(doc["document"])
                    all_metadatas.append(doc["metadata"])

            self.collection.add(ids=all_ids, documents=all_documents, metadatas=all_metadatas)

            logger.debug(f"Batch added {len(records)} commands ({len(all_ids)} docs) to vector store")
            return [r.command_id for r in records]

        except Exception as e:
            logger.error("Failed to batch add commands to vector store", exception=e)
            return []
    
    def search_commands(
        self,
        query: str,
        project_id: Optional[str] = None,
        environment: Optional[str] = None,
        limit: int = 10,
        min_score: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """
        语义搜索相关命令，支持细粒度向量文档的去重合并
        返回格式：[{"command_id": str, "score": float, "metadata": dict}]
        """
        try:
            where = {}
            if project_id:
                where["project_id"] = project_id
            if environment:
                where["environment"] = environment

            results = self.collection.query(
                query_texts=[query],
                n_results=limit * 3,  # 多取一些以覆盖细粒度文档的重复
                where=where if where else None,
            )

            # 去重：同一command_id只保留最高分
            best_by_command: Dict[str, Dict[str, Any]] = {}
            if results and results["ids"] and results["distances"] and results["metadatas"]:
                for i in range(len(results["ids"][0])):
                    raw_id = results["ids"][0][i]
                    distance = results["distances"][0][i]
                    metadata = results["metadatas"][0][i]

                    # 提取command_id（去掉#action/#args后缀）
                    command_id = raw_id.split("#")[0] if "#" in raw_id else raw_id

                    score = max(0.0, 1.0 - min(distance, 2.0) / 2.0)

                    if score >= min_score:
                        if command_id not in best_by_command or score > best_by_command[command_id]["score"]:
                            best_by_command[command_id] = {
                                "command_id": command_id,
                                "score": score,
                                "metadata": metadata,
                            }

            matched = sorted(best_by_command.values(), key=lambda x: -x["score"])[:limit]
            logger.debug(f"Semantic search for '{query}' returned {len(matched)} results")
            return matched

        except Exception as e:
            logger.error("Failed to search commands in vector store", exception=e, query=query)
            return []
    
    def count(self) -> int:
        """获取向量库中的文档总数（包含细粒度文档）"""
        try:
            return self.collection.count()
        except Exception as e:
            logger.error("Failed to get vector store count", exception=e)
            return 0

    def command_count(self) -> int:
        """获取向量库中的唯一命令数量（排除细粒度文档后缀）"""
        try:
            all_ids = self.collection.get()["ids"]
            command_ids = set()
            for doc_id in all_ids:
                command_id = doc_id.split("#")[0] if "#" in doc_id else doc_id
                command_ids.add(command_id)
            return len(command_ids)
        except Exception as e:
            logger.error("Failed to get vector store command count", exception=e)
            return 0
    
    def clear(self) -> None:
        """清空向量库"""
        try:
            self.client.delete_collection("command_embeddings")
            self.collection = self.client.create_collection(
                name="command_embeddings",
                embedding_function=self.embedding_fn,
                metadata={"hnsw:space": "cosine"}
            )
            logger.info("Vector store cleared")
        except Exception as e:
            logger.error("Failed to clear vector store", exception=e)
