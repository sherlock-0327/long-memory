"""
向量存储引擎，基于ChromaDB实现语义检索能力
支持命令语义向量存储、更新、搜索功能
"""
import chromadb
import json
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from chromadb.utils import embedding_functions

from feishu_mem.core.storage import CommandRecord
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger
from feishu_mem.shared.cache import l1_cache

class VectorStore:
    def __init__(self, persist_directory: Optional[Path] = None):
        self.persist_dir = persist_directory or config.vector_db_path
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化ChromaDB客户端
        self.client = chromadb.PersistentClient(path=str(self.persist_dir))
        
        # 使用本地Embedding模型，不依赖外部API
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=config.embedding_model_name,
            device=config.embedding_device
        )
        
        # 获取或创建集合
        self.collection = self.client.get_or_create_collection(
            name="command_embeddings",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}  # 使用余弦相似度
        )
        
        logger.info(f"Vector store initialized, persist directory: {self.persist_dir}")
        logger.info(f"Embedding model: {config.embedding_model_name}, device: {config.embedding_device}")
    
    def _get_document_content(self, record: CommandRecord) -> str:
        """生成命令的文档内容，用于生成向量"""
        parts = [
            f"命令: {record.raw_command}",
            f"命令名: {record.command_name}",
            f"参数: {' '.join(record.arguments)}" if record.arguments else "",
            f"项目: {record.project_id}" if record.project_id else "",
            f"环境: {record.environment}" if record.environment else "",
            f"标签: {' '.join(record.tags)}" if record.tags else ""
        ]
        return "\n".join(filter(None, parts))
    
    def add_command(self, record: CommandRecord) -> Optional[str]:
        """添加命令到向量数据库"""
        try:
            # 生成文档内容
            document = self._get_document_content(record)
            
            # 元数据
            metadata = {
                "command_id": record.command_id,
                "command_name": record.command_name,
                "project_id": record.project_id or "",
                "environment": record.environment or "",
                "is_explicit": record.is_explicit,
                "usage_count": record.usage_count,
                "executed_at": record.executed_at.isoformat() if record.executed_at else "",
                "source": record.source
            }
            
            # 添加到集合
            self.collection.add(
                ids=[record.command_id],
                documents=[document],
                metadatas=[metadata]
            )
            
            logger.debug(f"Added command to vector store, id: {record.command_id}, command: {record.raw_command[:50]}...")
            return record.command_id
            
        except Exception as e:
            logger.error("Failed to add command to vector store", exception=e, command_id=record.command_id)
            return None
    
    def batch_add_commands(self, records: List[CommandRecord]) -> List[str]:
        """批量添加命令到向量数据库"""
        if not records:
            return []
        
        try:
            ids = []
            documents = []
            metadatas = []
            
            for record in records:
                ids.append(record.command_id)
                documents.append(self._get_document_content(record))
                metadatas.append({
                    "command_id": record.command_id,
                    "command_name": record.command_name,
                    "project_id": record.project_id or "",
                    "environment": record.environment or "",
                    "is_explicit": record.is_explicit,
                    "usage_count": record.usage_count,
                    "executed_at": record.executed_at.isoformat() if record.executed_at else "",
                    "source": record.source
                })
            
            self.collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas
            )
            
            logger.debug(f"Batch added {len(records)} commands to vector store")
            return ids
            
        except Exception as e:
            logger.error("Failed to batch add commands to vector store", exception=e)
            return []
    
    def search_commands(
        self, 
        query: str, 
        project_id: Optional[str] = None, 
        environment: Optional[str] = None,
        limit: int = 10,
        min_score: float = 0.5
    ) -> List[Dict[str, Any]]:
        """
        语义搜索相关命令
        返回格式：[{"command_id": str, "score": float, "metadata": dict}]
        """
        try:
            # 构建过滤条件
            where = {}
            if project_id:
                where["project_id"] = project_id
            if environment:
                where["environment"] = environment
            
            # 执行搜索
            results = self.collection.query(
                query_texts=[query],
                n_results=limit,
                where=where if where else None
            )
            
            # 处理结果
            matched = []
            if results and results["ids"] and results["distances"] and results["metadatas"]:
                for i in range(len(results["ids"][0])):
                    command_id = results["ids"][0][i]
                    distance = results["distances"][0][i]
                    metadata = results["metadatas"][0][i]
                    
                    # ChromaDB返回的是余弦距离，范围是[0, 2]，转换为[0,1]范围的相似度得分，越大越相似
                    score = max(0.0, 1.0 - min(distance, 2.0) / 2.0)
                    
                    if score >= min_score:
                        matched.append({
                            "command_id": command_id,
                            "score": score,
                            "metadata": metadata
                        })
            
            logger.debug(f"Semantic search for '{query}' returned {len(matched)} results")
            return matched
            
        except Exception as e:
            logger.error("Failed to search commands in vector store", exception=e, query=query)
            return []
    
    def delete_command(self, command_id: str) -> bool:
        """从向量库删除命令"""
        try:
            self.collection.delete(ids=[command_id])
            logger.debug(f"Deleted command from vector store, id: {command_id}")
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
            logger.debug(f"Updated command metadata in vector store, id: {command_id}")
            return True
        except Exception as e:
            logger.error("Failed to update command metadata in vector store", exception=e, command_id=command_id)
            return False
    
    def count(self) -> int:
        """获取向量库中的命令数量"""
        try:
            return self.collection.count()
        except Exception as e:
            logger.error("Failed to get vector store count", exception=e)
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