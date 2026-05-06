"""
工作流引擎实现，支持：
1. 自动从历史命令中识别常用工作流序列
2. 工作流参数模板化与一键执行
3. 团队共享工作流管理
4. 条件执行与错误处理
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
import uuid

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger
from .storage import CommandRecord, Storage


@dataclass
class WorkflowStep:
    """工作流步骤"""
    step_id: str
    command: str
    description: Optional[str] = None
    parameters: Dict[str, str] = field(default_factory=dict)  # 参数模板: {参数名: 描述/默认值}
    condition: Optional[str] = None  # 执行条件: "last_exit_code == 0"
    continue_on_failure: bool = False


@dataclass
class Workflow:
    """工作流定义"""
    workflow_id: str
    name: str
    description: str
    steps: List[WorkflowStep]
    created_by: str
    created_at: datetime
    updated_at: datetime
    is_shared: bool = False
    team_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    usage_count: int = 0
    last_used_at: Optional[datetime] = None


@dataclass
class WorkflowExecutionResult:
    """工作流执行结果"""
    workflow_id: str
    execution_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    status: str = "pending"  # pending/running/completed/failed
    step_results: List[Dict[str, Any]] = field(default_factory=list)
    error_message: Optional[str] = None
    parameters_used: Dict[str, Any] = field(default_factory=dict)


class WorkflowEngine:
    """工作流引擎核心实现"""
    
    def __init__(self, storage: Storage, db_path: Path = None):
        self.storage = storage
        self.db_path = db_path or config.db_path
        self._init_workflow_tables()
        logger.info("Workflow engine initialized")
    
    def _init_workflow_tables(self) -> None:
        """初始化工作流相关数据库表"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # 工作流定义表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS workflows (
                        workflow_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT,
                        steps TEXT NOT NULL,  # JSON格式存储步骤
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        is_shared INTEGER DEFAULT 0,
                        team_id TEXT,
                        tags TEXT,
                        usage_count INTEGER DEFAULT 0,
                        last_used_at TEXT
                    )
                """)
                
                # 工作流执行记录表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS workflow_executions (
                        execution_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        start_time TEXT NOT NULL,
                        end_time TEXT,
                        status TEXT NOT NULL,
                        step_results TEXT,
                        error_message TEXT,
                        parameters_used TEXT,
                        FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
                    )
                """)
                
                conn.execute("CREATE INDEX IF NOT EXISTS idx_workflows_name ON workflows(name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_workflows_shared ON workflows(is_shared, team_id)")
                conn.commit()
        except Exception as e:
            logger.error("Failed to initialize workflow tables", exception=e)
    
    def discover_workflows_from_history(self, user_id: str, min_sequence_length: int = 3, min_support: float = 0.3) -> List[Workflow]:
        """
        从历史命令中自动发现工作流序列
        基于PrefixSpan序列模式挖掘算法简化实现
        """
        try:
            # 获取用户最近的命令序列，按会话分组
            sessions = self._get_user_command_sessions(user_id, days=30)
            if not sessions:
                return []
            
            # 提取所有有效序列
            sequences = []
            for session in sessions:
                if len(session) >= min_sequence_length:
                    # 只保留成功执行的命令
                    successful_commands = [cmd for cmd in session if cmd.is_successful]
                    if len(successful_commands) >= min_sequence_length:
                        sequences.append([cmd.command_name + " " + " ".join(cmd.arguments) for cmd in successful_commands])
            
            if not sequences:
                return []
            
            # 挖掘频繁序列
            frequent_sequences = self._mine_frequent_sequences(sequences, min_support, min_sequence_length)
            if not frequent_sequences:
                return []
            
            # 转换为工作流对象
            workflows = []
            for idx, sequence in enumerate(frequent_sequences):
                steps = []
                for i, cmd in enumerate(sequence):
                    step = WorkflowStep(
                        step_id=f"step_{i+1}",
                        command=cmd,
                        description=f"步骤 {i+1}"
                    )
                    steps.append(step)
                
                workflow = Workflow(
                    workflow_id=str(uuid.uuid4()),
                    name=f"自动发现工作流 {idx+1}",
                    description=f"从历史命令中自动发现的常用序列，包含{len(steps)}个步骤",
                    steps=steps,
                    created_by=user_id,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                    tags=["auto-discovered"]
                )
                workflows.append(workflow)
            
            logger.info(f"Discovered {len(workflows)} potential workflows from history")
            return workflows
            
        except Exception as e:
            logger.error("Failed to discover workflows from history", exception=e)
            return []
    
    def _get_user_command_sessions(self, user_id: str, days: int = 30) -> List[List[CommandRecord]]:
        """获取用户按会话分组的命令历史"""
        try:
            cutoff = datetime.now() - timedelta(days=days)
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT session_id, command_id FROM commands 
                    WHERE user_id = ? AND executed_at > ?
                    ORDER BY session_id, executed_at ASC
                """, (user_id, cutoff.isoformat()))
                
                session_commands = {}
                for row in cursor.fetchall():
                    session_id = row["session_id"]
                    if session_id not in session_commands:
                        session_commands[session_id] = []
                    session_commands[session_id].append(row["command_id"])
                
                # 批量获取命令详情
                all_command_ids = [cmd_id for cmd_ids in session_commands.values() for cmd_id in cmd_ids]
                command_map = {cmd.command_id: cmd for cmd in self.storage.get_commands_by_ids(all_command_ids)}
                
                # 构建会话序列
                sessions = []
                for cmd_ids in session_commands.values():
                    session = [command_map[cmd_id] for cmd_id in cmd_ids if cmd_id in command_map]
                    if session:
                        sessions.append(session)
                
                return sessions
                
        except Exception as e:
            logger.error("Failed to get user command sessions", exception=e)
            return []
    
    def _mine_frequent_sequences(self, sequences: List[List[str]], min_support: float, min_length: int) -> List[List[str]]:
        """简化的序列模式挖掘，查找频繁出现的连续命令序列"""
        # 统计所有连续子序列的出现频率
        sequence_counts = {}
        total_sequences = len(sequences)
        
        for seq in sequences:
            for i in range(len(seq)):
                for length in range(min_length, len(seq) - i + 1):
                    subseq = tuple(seq[i:i+length])
                    sequence_counts[subseq] = sequence_counts.get(subseq, 0) + 1
        
        # 筛选满足最小支持度的序列
        frequent = []
        min_count = int(total_sequences * min_support)
        for subseq, count in sequence_counts.items():
            if count >= min_count:
                frequent.append(list(subseq))
        
        # 按长度降序排序，优先返回更长的序列
        frequent.sort(key=lambda x: (-len(x), -sequence_counts[tuple(x)]))
        return frequent[:10]  # 最多返回10个
    
    def create_workflow(self, name: str, steps: List[WorkflowStep], description: str = "", 
                       created_by: str = "user", is_shared: bool = False, team_id: str = None) -> Optional[Workflow]:
        """创建新工作流"""
        try:
            workflow_id = str(uuid.uuid4())
            now = datetime.now()
            
            workflow = Workflow(
                workflow_id=workflow_id,
                name=name,
                description=description,
                steps=steps,
                created_by=created_by,
                created_at=now,
                updated_at=now,
                is_shared=is_shared,
                team_id=team_id
            )
            
            # 序列化步骤
            steps_json = json.dumps([{
                "step_id": step.step_id,
                "command": step.command,
                "description": step.description,
                "parameters": step.parameters,
                "condition": step.condition,
                "continue_on_failure": step.continue_on_failure
            } for step in steps])
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    INSERT INTO workflows (
                        workflow_id, name, description, steps, created_by,
                        created_at, updated_at, is_shared, team_id, tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workflow_id, name, description, steps_json, created_by,
                    now.isoformat(), now.isoformat(), 1 if is_shared else 0, team_id,
                    json.dumps(workflow.tags) if workflow.tags else None
                ))
                conn.commit()
            
            logger.info(f"Created workflow: {name} (id: {workflow_id}, steps: {len(steps)})")
            return workflow
            
        except sqlite3.IntegrityError:
            logger.error(f"Workflow name '{name}' already exists")
            return None
        except Exception as e:
            logger.error(f"Failed to create workflow '{name}'", exception=e)
            return None
    
    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """根据ID获取工作流"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM workflows WHERE workflow_id = ?
                """, (workflow_id,))
                
                row = cursor.fetchone()
                if not row:
                    return None
                
                return self._row_to_workflow(row)
                
        except Exception as e:
            logger.error(f"Failed to get workflow {workflow_id}", exception=e)
            return None
    
    def get_workflow_by_name(self, name: str) -> Optional[Workflow]:
        """根据名称获取工作流"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM workflows WHERE name = ?
                """, (name,))
                
                row = cursor.fetchone()
                if not row:
                    return None
                
                return self._row_to_workflow(row)
                
        except Exception as e:
            logger.error(f"Failed to get workflow by name '{name}'", exception=e)
            return None
    
    def list_workflows(self, user_id: str = None, team_id: str = None, include_shared: bool = True) -> List[Workflow]:
        """列出用户可用的工作流"""
        try:
            params = []
            conditions = []
            
            if user_id:
                conditions.append("(created_by = ?")
                params.append(user_id)
                if include_shared:
                    conditions.append("OR (is_shared = 1")
                    if team_id:
                        conditions.append("AND team_id = ?")
                        params.append(team_id)
                    conditions.append(")")
                conditions.append(")")
            elif team_id:
                conditions.append("is_shared = 1 AND team_id = ?")
                params.append(team_id)
            
            where_clause = "WHERE " + " ".join(conditions) if conditions else ""
            
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                query = f"""
                    SELECT * FROM workflows {where_clause}
                    ORDER BY usage_count DESC, updated_at DESC
                """
                cursor = conn.execute(query, params)
                
                workflows = []
                for row in cursor.fetchall():
                    workflows.append(self._row_to_workflow(row))
                
                return workflows
                
        except Exception as e:
            logger.error("Failed to list workflows", exception=e)
            return []
    
    def execute_workflow(self, workflow_id: str, parameters: Dict[str, Any] = None, 
                        dry_run: bool = False) -> WorkflowExecutionResult:
        """
        执行工作流，支持参数替换
        如果dry_run=True，只生成执行计划不实际执行
        """
        execution_id = str(uuid.uuid4())
        start_time = datetime.now()
        parameters = parameters or {}

        result = WorkflowExecutionResult(
            workflow_id=workflow_id,
            execution_id=execution_id,
            start_time=start_time,
            parameters_used=parameters,
        )
        
        try:
            workflow = self.get_workflow(workflow_id)
            if not workflow:
                result.status = "failed"
                result.error_message = f"Workflow {workflow_id} not found"
                return result
            
            logger.info(f"Executing workflow: {workflow.name} (id: {workflow_id}, execution: {execution_id})")
            
            # 更新使用统计
            self._update_workflow_usage(workflow_id)
            
            if dry_run:
                # 预览模式，生成执行计划
                result.status = "completed"
                for step in workflow.steps:
                    rendered_command = self._render_command_template(step.command, parameters)
                    result.step_results.append({
                        "step_id": step.step_id,
                        "command": rendered_command,
                        "description": step.description,
                        "status": "preview"
                    })
                return result
            
            # 实际执行步骤
            last_exit_code = 0
            for i, step in enumerate(workflow.steps):
                step_start = datetime.now()
                
                # 检查执行条件
                if step.condition:
                    condition_met = self._evaluate_condition(step.condition, last_exit_code, parameters)
                    if not condition_met:
                        result.step_results.append({
                            "step_id": step.step_id,
                            "command": step.command,
                            "description": step.description,
                            "status": "skipped",
                            "duration": 0,
                            "exit_code": 0
                        })
                        continue
                
                # 渲染命令模板
                try:
                    rendered_command = self._render_command_template(step.command, parameters)
                except Exception as e:
                    error_msg = f"Step {step.step_id} parameter rendering failed: {str(e)}"
                    result.step_results.append({
                        "step_id": step.step_id,
                        "command": step.command,
                        "description": step.description,
                        "status": "failed",
                        "error": error_msg,
                        "duration": 0
                    })
                    if not step.continue_on_failure:
                        result.status = "failed"
                        result.error_message = error_msg
                        break
                    continue
                
                # 执行命令
                logger.debug(f"Executing workflow step {step.step_id}: {rendered_command}")
                import subprocess
                try:
                    proc = subprocess.run(
                        rendered_command, 
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=300  # 5分钟超时
                    )
                    exit_code = proc.returncode
                    last_exit_code = exit_code
                    
                    step_result = {
                        "step_id": step.step_id,
                        "command": rendered_command,
                        "description": step.description,
                        "status": "completed" if exit_code == 0 else "failed",
                        "exit_code": exit_code,
                        "stdout": proc.stdout[:1000],  # 截断过长输出
                        "stderr": proc.stderr[:1000],
                        "duration": (datetime.now() - step_start).total_seconds()
                    }
                    result.step_results.append(step_result)
                    
                    if exit_code != 0 and not step.continue_on_failure:
                        result.status = "failed"
                        result.error_message = f"Step {step.step_id} failed with exit code {exit_code}"
                        break
                        
                except subprocess.TimeoutExpired:
                    error_msg = f"Step {step.step_id} timed out after 5 minutes"
                    result.step_results.append({
                        "step_id": step.step_id,
                        "command": rendered_command,
                        "description": step.description,
                        "status": "timeout",
                        "error": error_msg,
                        "duration": 300
                    })
                    if not step.continue_on_failure:
                        result.status = "failed"
                        result.error_message = error_msg
                        break
                except Exception as e:
                    error_msg = f"Step {step.step_id} execution failed: {str(e)}"
                    result.step_results.append({
                        "step_id": step.step_id,
                        "command": rendered_command,
                        "description": step.description,
                        "status": "failed",
                        "error": error_msg,
                        "duration": (datetime.now() - step_start).total_seconds()
                    })
                    if not step.continue_on_failure:
                        result.status = "failed"
                        result.error_message = error_msg
                        break
            
            else:
                # 所有步骤执行完成
                result.status = "completed"
            
        except Exception as e:
            result.status = "failed"
            result.error_message = f"Workflow execution failed: {str(e)}"
            logger.error(f"Workflow execution failed", exception=e)
        finally:
            result.end_time = datetime.now()
            # 记录执行结果
            self._record_execution_result(result)
        
        logger.info(f"Workflow execution {execution_id} completed with status: {result.status}")
        return result
    
    def _render_command_template(self, command: str, parameters: Dict[str, Any]) -> str:
        """渲染命令模板，替换{{参数名}}占位符"""
        def replace_param(match):
            param_name = match.group(1)
            if param_name not in parameters:
                raise ValueError(f"Missing required parameter: {param_name}")
            return str(parameters[param_name])
        
        return re.sub(r'\{\{(\w+)\}\}', replace_param, command)
    
    def _evaluate_condition(self, condition: str, last_exit_code: int, parameters: Dict[str, Any]) -> bool:
        """安全的条件表达式求值，支持:
        - last_exit_code == 0
        - {{param}} == 'value'
        禁止使用eval()，仅支持简单的比较操作
        """
        import ast
        import operator

        # 替换参数
        condition = self._render_command_template(condition, parameters)
        # 替换变量
        condition = condition.replace("last_exit_code", str(last_exit_code))

        # 支持的比较操作符
        ops = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
        }

        try:
            tree = ast.parse(condition, mode="eval")
            node = tree.body

            # 只允许 Compare 节点：left op right
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
                logger.warning(f"Unsupported condition expression (not a simple comparison): {condition}")
                return True

            op_type = type(node.ops[0])
            if op_type not in ops:
                logger.warning(f"Unsupported operator in condition: {condition}")
                return True

            def _eval_value(val_node):
                if isinstance(val_node, ast.Constant):
                    return val_node.value
                elif isinstance(val_node, ast.UnaryOp) and isinstance(val_node.op, ast.USub):
                    inner = _eval_value(val_node.operand)
                    return -inner if isinstance(inner, (int, float)) else None
                return None

            left = _eval_value(node.left)
            right = _eval_value(node.comparators[0])
            if left is None or right is None:
                logger.warning(f"Cannot evaluate condition values: {condition}")
                return True

            return bool(ops[op_type](left, right))
        except Exception:
            logger.warning(f"Invalid condition expression: {condition}")
            return True  # 条件表达式无效时默认执行
    
    def _update_workflow_usage(self, workflow_id: str) -> None:
        """更新工作流使用统计"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    UPDATE workflows 
                    SET usage_count = usage_count + 1, last_used_at = CURRENT_TIMESTAMP
                    WHERE workflow_id = ?
                """, (workflow_id,))
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update workflow usage: {e}")
    
    def _record_execution_result(self, result: WorkflowExecutionResult) -> None:
        """记录工作流执行结果"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("""
                    INSERT INTO workflow_executions (
                        execution_id, workflow_id, start_time, end_time, status,
                        step_results, error_message, parameters_used
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    result.execution_id,
                    result.workflow_id,
                    result.start_time.isoformat(),
                    result.end_time.isoformat() if result.end_time else None,
                    result.status,
                    json.dumps(result.step_results) if result.step_results else None,
                    result.error_message,
                    json.dumps(result.parameters_used) if result.parameters_used else None
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record workflow execution: {e}")
    
    def _row_to_workflow(self, row: sqlite3.Row) -> Workflow:
        """将数据库行转换为Workflow对象"""
        steps_data = json.loads(row["steps"])
        steps = []
        for step_dict in steps_data:
            step = WorkflowStep(
                step_id=step_dict["step_id"],
                command=step_dict["command"],
                description=step_dict.get("description"),
                parameters=step_dict.get("parameters", {}),
                condition=step_dict.get("condition"),
                continue_on_failure=step_dict.get("continue_on_failure", False)
            )
            steps.append(step)
        
        return Workflow(
            workflow_id=row["workflow_id"],
            name=row["name"],
            description=row["description"] or "",
            steps=steps,
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            is_shared=bool(row["is_shared"]),
            team_id=row["team_id"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            usage_count=row["usage_count"],
            last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None
        )
    
    def delete_workflow(self, workflow_id: str, created_by: str = None) -> bool:
        """删除工作流"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                if created_by:
                    cursor = conn.execute("""
                        DELETE FROM workflows WHERE workflow_id = ? AND created_by = ?
                    """, (workflow_id, created_by))
                else:
                    cursor = conn.execute("""
                        DELETE FROM workflows WHERE workflow_id = ?
                    """, (workflow_id,))
                
                deleted = cursor.rowcount > 0
                conn.commit()
                
                if deleted:
                    logger.info(f"Deleted workflow: {workflow_id}")
                return deleted
                
        except Exception as e:
            logger.error(f"Failed to delete workflow {workflow_id}", exception=e)
            return False


# 全局实例
_workflow_engine = None


def get_workflow_engine(storage: Storage = None) -> WorkflowEngine:
    global _workflow_engine
    if _workflow_engine is None and storage is not None:
        _workflow_engine = WorkflowEngine(storage)
    return _workflow_engine
