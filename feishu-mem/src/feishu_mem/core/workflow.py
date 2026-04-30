"""
工作流引擎，实现命令序列挖掘、一键执行和团队共享功能
"""
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
import json
import uuid
import sqlite3

from feishu_mem.core.storage import CommandRecord
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

@dataclass
class WorkflowStep:
    """工作流步骤"""
    step_id: str
    command: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    condition: Optional[str] = None  # 执行条件表达式

@dataclass
class Workflow:
    """工作流定义"""
    workflow_id: str
    name: str
    description: str
    steps: List[WorkflowStep]
    created_at: datetime
    created_by: str
    project_id: Optional[str] = None
    environment: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    is_shared: bool = False
    usage_count: int = 0
    last_used_at: Optional[datetime] = None

class WorkflowEngine:
    """工作流引擎核心实现"""
    def __init__(self, storage, db_path=None):
        self.storage = storage
        self.db_path = db_path or config.db_path
        self._init_workflow_table()
        logger.info("Workflow engine initialized")
    
    def _init_workflow_table(self) -> None:
        """初始化工作流相关数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            # 工作流定义表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    steps TEXT NOT NULL,  # JSON格式的步骤列表
                    created_at TIMESTAMP NOT NULL,
                    created_by TEXT NOT NULL,
                    project_id TEXT,
                    environment TEXT,
                    tags TEXT,  # JSON格式的标签列表
                    is_shared BOOLEAN DEFAULT 0,
                    usage_count INTEGER DEFAULT 0,
                    last_used_at TIMESTAMP
                )
            """)
            
            # 工作流执行历史表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_executions (
                    execution_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    executed_at TIMESTAMP NOT NULL,
                    executed_by TEXT NOT NULL,
                    parameters TEXT,  # JSON格式的执行参数
                    exit_code INTEGER,
                    execution_time REAL,
                    status TEXT NOT NULL,  # success/failed/running
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
                )
            """)
            
            conn.commit()
    
    def create_workflow(
        self, 
        name: str, 
        steps: List[WorkflowStep], 
        description: str = "",
        created_by: str = "",
        project_id: Optional[str] = None,
        environment: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_shared: bool = False
    ) -> Optional[Workflow]:
        """创建新工作流"""
        try:
            workflow_id = f"wf_{uuid.uuid4().hex[:16]}"
            now = datetime.now()
            
            workflow = Workflow(
                workflow_id=workflow_id,
                name=name,
                description=description,
                steps=steps,
                created_at=now,
                created_by=created_by,
                project_id=project_id,
                environment=environment,
                tags=tags or [],
                is_shared=is_shared,
                last_used_at=now
            )
            
            # 保存到数据库
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, name, description, steps, created_at, created_by,
                        project_id, environment, tags, is_shared, usage_count, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        name,
                        description,
                        json.dumps([{
                            "step_id": s.step_id,
                            "command": s.command,
                            "description": s.description,
                            "parameters": s.parameters,
                            "condition": s.condition
                        } for s in steps]),
                        now.isoformat(),
                        created_by,
                        project_id,
                        environment,
                        json.dumps(tags or []),
                        1 if is_shared else 0,
                        0,
                        now.isoformat()
                    )
                )
                conn.commit()
            
            logger.info(f"Workflow created: {workflow_id} - {name}")
            return workflow
            
        except Exception as e:
            logger.error("Failed to create workflow", exception=e, name=name)
            return None
    
    def discover_workflows_from_history(
        self, 
        user_id: str, 
        project_id: Optional[str] = None, 
        min_sequence_length: int = 3,
        min_support: float = 0.3
    ) -> List[Workflow]:
        """从历史命令中自动发现工作流序列"""
        try:
            # 获取命令序列
            sequences = self.storage.get_recent_command_sequences(
                project_id=project_id, 
                min_length=min_sequence_length,
                limit=100
            )
            
            if not sequences:
                return []
            
            # 简单的频繁序列挖掘实现
            # 生产版本可使用PrefixSpan等更高效的算法
            candidate_patterns = {}
            
            for sequence in sequences:
                # 提取命令字符串序列
                cmd_sequence = [cmd.raw_command for cmd in sequence]
                
                # 生成所有可能的连续子序列
                for i in range(len(cmd_sequence) - min_sequence_length + 1):
                    pattern = tuple(cmd_sequence[i:i+min_sequence_length])
                    candidate_patterns[pattern] = candidate_patterns.get(pattern, 0) + 1
            
            # 过滤满足支持度的模式
            total_sequences = len(sequences)
            frequent_patterns = [
                (pattern, count) 
                for pattern, count in candidate_patterns.items() 
                if count / total_sequences >= min_support
            ]
            
            # 转换为工作流对象
            workflows = []
            for pattern, count in frequent_patterns:
                steps = []
                for i, cmd in enumerate(pattern):
                    steps.append(WorkflowStep(
                        step_id=f"step_{i+1}",
                        command=cmd,
                        description=f"步骤 {i+1}"
                    ))
                
                workflow = Workflow(
                    workflow_id=f"wf_discovered_{uuid.uuid4().hex[:8]}",
                    name=f"自动发现的工作流 ({len(steps)}步)",
                    description=f"从历史命令中自动发现，出现次数: {count}",
                    steps=steps,
                    created_at=datetime.now(),
                    created_by=user_id,
                    project_id=project_id,
                    tags=["自动发现"]
                )
                workflows.append(workflow)
            
            logger.info(f"Discovered {len(workflows)} candidate workflows from history")
            return workflows
            
        except Exception as e:
            logger.error("Failed to discover workflows from history", exception=e)
            return []
    
    def execute_workflow(
        self, 
        workflow_id: str, 
        parameters: Optional[Dict[str, Any]] = None,
        executed_by: str = ""
    ) -> Dict[str, Any]:
        """执行工作流"""
        try:
            # 获取工作流定义
            workflow = self.get_workflow(workflow_id)
            if not workflow:
                return {"success": False, "error": "工作流不存在"}
            
            execution_id = f"exec_{uuid.uuid4().hex[:16]}"
            start_time = datetime.now()
            parameters = parameters or {}
            
            logger.info(f"Executing workflow: {workflow.name} ({workflow_id}), execution_id: {execution_id}")
            
            # 记录执行历史
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO workflow_executions (
                        execution_id, workflow_id, executed_at, executed_by, parameters, status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        workflow_id,
                        start_time.isoformat(),
                        executed_by,
                        json.dumps(parameters),
                        "running"
                    )
                )
                conn.commit()
            
            # 执行步骤
            results = []
            success = True
            total_exit_code = 0
            
            import subprocess
            import shlex
            
            for step in workflow.steps:
                try:
                    # 替换参数
                    cmd = step.command
                    for key, value in parameters.items():
                        cmd = cmd.replace(f"{{{{{key}}}}}", str(value))
                    
                    logger.debug(f"Executing workflow step: {cmd}")
                    
                    # 执行命令
                    result = subprocess.run(
                        shlex.split(cmd),
                        capture_output=True,
                        text=True,
                        timeout=300  # 单步超时5分钟
                    )
                    
                    step_result = {
                        "step_id": step.step_id,
                        "command": cmd,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "success": result.returncode == 0
                    }
                    results.append(step_result)
                    
                    if result.returncode != 0:
                        success = False
                        total_exit_code = result.returncode
                        break  # 步骤失败，终止执行
                        
                except Exception as e:
                    logger.error(f"Workflow step failed: {step.step_id}", exception=e)
                    results.append({
                        "step_id": step.step_id,
                        "command": step.command,
                        "exit_code": -1,
                        "error": str(e),
                        "success": False
                    })
                    success = False
                    total_exit_code = -1
                    break
            
            # 更新执行状态
            execution_time = (datetime.now() - start_time).total_seconds()
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE workflow_executions 
                    SET status = ?, exit_code = ?, execution_time = ?
                    WHERE execution_id = ?
                    """,
                    (
                        "success" if success else "failed",
                        total_exit_code,
                        execution_time,
                        execution_id
                    )
                )
                
                # 更新工作流使用统计
                conn.execute(
                    """
                    UPDATE workflows 
                    SET usage_count = usage_count + 1, last_used_at = CURRENT_TIMESTAMP
                    WHERE workflow_id = ?
                    """,
                    (workflow_id,)
                )
                conn.commit()
            
            logger.info(f"Workflow execution completed: {execution_id}, success: {success}, duration: {execution_time:.2f}s")
            
            return {
                "success": success,
                "execution_id": execution_id,
                "workflow_id": workflow_id,
                "workflow_name": workflow.name,
                "execution_time": execution_time,
                "steps": results,
                "total_exit_code": total_exit_code
            }
            
        except Exception as e:
            logger.error("Failed to execute workflow", exception=e, workflow_id=workflow_id)
            return {"success": False, "error": str(e)}
    
    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """根据ID获取工作流"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM workflows WHERE workflow_id = ?",
                    (workflow_id,)
                )
                row = cursor.fetchone()
                
                if not row:
                    return None
                
                steps = []
                steps_data = json.loads(row["steps"])
                for step_data in steps_data:
                    steps.append(WorkflowStep(
                        step_id=step_data["step_id"],
                        command=step_data["command"],
                        description=step_data.get("description", ""),
                        parameters=step_data.get("parameters", {}),
                        condition=step_data.get("condition")
                    ))
                
                return Workflow(
                    workflow_id=row["workflow_id"],
                    name=row["name"],
                    description=row["description"],
                    steps=steps,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    created_by=row["created_by"],
                    project_id=row["project_id"],
                    environment=row["environment"],
                    tags=json.loads(row["tags"]) if row["tags"] else [],
                    is_shared=bool(row["is_shared"]),
                    usage_count=row["usage_count"],
                    last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None
                )
                
        except Exception as e:
            logger.error("Failed to get workflow", exception=e, workflow_id=workflow_id)
            return None
    
    def list_workflows(
        self, 
        user_id: Optional[str] = None, 
        project_id: Optional[str] = None,
        include_shared: bool = True,
        limit: int = 20
    ) -> List[Workflow]:
        """列出可用的工作流"""
        try:
            params = []
            conditions = []
            
            if user_id:
                conditions.append("created_by = ?")
                params.append(user_id)
            
            if project_id:
                conditions.append("project_id = ?")
                params.append(project_id)
            
            if include_shared:
                conditions.append("(is_shared = 1 OR created_by = ?)")
                params.append(user_id or "")
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit)
            
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"""
                    SELECT * FROM workflows
                    WHERE {where_clause}
                    ORDER BY usage_count DESC, last_used_at DESC
                    LIMIT ?
                    """,
                    params
                )
                
                workflows = []
                for row in cursor.fetchall():
                    steps = []
                    steps_data = json.loads(row["steps"])
                    for step_data in steps_data:
                        steps.append(WorkflowStep(
                            step_id=step_data["step_id"],
                            command=step_data["command"],
                            description=step_data.get("description", ""),
                            parameters=step_data.get("parameters", {}),
                            condition=step_data.get("condition")
                        ))
                    
                    workflows.append(Workflow(
                        workflow_id=row["workflow_id"],
                        name=row["name"],
                        description=row["description"],
                        steps=steps,
                        created_at=datetime.fromisoformat(row["created_at"]),
                        created_by=row["created_by"],
                        project_id=row["project_id"],
                        environment=row["environment"],
                        tags=json.loads(row["tags"]) if row["tags"] else [],
                        is_shared=bool(row["is_shared"]),
                        usage_count=row["usage_count"],
                        last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None
                    ))
                
                return workflows
                
        except Exception as e:
            logger.error("Failed to list workflows", exception=e)
            return []
    
    def delete_workflow(self, workflow_id: str) -> bool:
        """删除工作流"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM workflows WHERE workflow_id = ?", (workflow_id,))
                conn.commit()
            
            logger.info(f"Workflow deleted: {workflow_id}")
            return True
        except Exception as e:
            logger.error("Failed to delete workflow", exception=e, workflow_id=workflow_id)
            return False