import shlex
import re
import os
import hashlib
import uuid
from typing import Tuple, List, Dict, Any, Optional
from pathlib import Path
from dataclasses import dataclass
import pygit2

from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

@dataclass
class ParsedCommand:
    command_name: str
    arguments: List[str]
    options: Dict[str, Any]

@dataclass
class CommandContext:
    working_dir: str
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    environment: Optional[str] = None
    git_branch: Optional[str] = None
    user_id: Optional[str] = None
    last_command: Optional[str] = None
    command_count: int = 0

class SensitiveFilter:
    """多层敏感信息过滤器，按照架构文档实现完整脱敏能力"""
    def __init__(self):
        self.patterns = {
            'password': re.compile(r'(?:password|passwd|pwd)\s*=\s*[^\s]+', re.I),
            'password_flag': re.compile(r'-p\S+', re.I),
            'private_key': re.compile(r'-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----[\s\S]*?-----END (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----', re.I),
            'aws_key': re.compile(r'AKIA[0-9A-Z]{16}', re.I),
            'credential': re.compile(r'(?:-u\s+\S+\s+-p\s+\S+|--user\s+\S+\s+--password\s+\S+)', re.I),
            'token': re.compile(r'(?:api[_-]?key|token|secret|bearer)\s*[=:]\s*[^\s]+', re.I),
            'authorization': re.compile(r'Authorization:\s*\S+\s+\S+', re.I),
            'phone': re.compile(r'1[3-9]\d{9}'),
            'email': re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
        }
        logger.debug("Sensitive filter initialized with {} patterns".format(len(self.patterns)))
    
    def filter_command(self, command: ParsedCommand) -> ParsedCommand:
        """对已解析的命令进行参数脱敏处理"""
        masked_args = []
        for arg in command.arguments:
            masked_arg = self._filter_string(arg)
            masked_args.append(masked_arg)
        
        masked_options = {}
        for key, value in command.options.items():
            if isinstance(value, str):
                masked_options[key] = self._filter_string(value)
            else:
                masked_options[key] = value
        
        return ParsedCommand(
            command_name=command.command_name,
            arguments=masked_args,
            options=masked_options
        )
    
    def filter_raw_command(self, raw_command: str) -> Tuple[str, List[str]]:
        """对原始命令字符串进行脱敏，返回脱敏后命令和检测到的敏感信息列表"""
        filtered = raw_command
        sensitive_info = []
        
        for pattern_name, pattern in self.patterns.items():
            matches = list(pattern.finditer(filtered))
            for match in matches:
                sensitive_text = match.group()
                filtered = filtered.replace(sensitive_text, self._mask_value(sensitive_text, pattern_name))
                sensitive_info.append(f"{pattern_name}:{sensitive_text[:10]}...")
        
        return filtered, sensitive_info
    
    def _filter_string(self, value: str) -> str:
        """过滤单个字符串中的敏感信息"""
        filtered = value
        for pattern_name, pattern in self.patterns.items():
            filtered = pattern.sub(lambda m: self._mask_value(m.group(), pattern_name), filtered)
        return filtered
    
    def _mask_value(self, value: str, pattern_type: str) -> str:
        """根据敏感类型进行不同的脱敏处理"""
        if pattern_type in ['password', 'password_flag', 'token', 'private_key', 'authorization']:
            return '***PASSWORD_HIDDEN***' if 'password' in pattern_type else f'***{pattern_type.upper()}_HIDDEN***'
        elif pattern_type in ['phone', 'email']:
            return f'***{pattern_type.upper()}_HIDDEN***'
        elif pattern_type in ['credential']:
            return '***CREDENTIAL_HIDDEN***'
        elif pattern_type in ['aws_key']:
            return '***AWS_KEY_HIDDEN***'
        return re.sub(r'([=:]\s*)\S+', r'\1***', value)

class CommandCollector:
    def __init__(self):
        self.sensitive_filter = SensitiveFilter()
        self.session_id = os.getenv("FEISHU_MEM_SESSION_ID") or str(uuid.uuid4())
        self.command_count = 0
        self.last_command = None
        logger.info(f"Command collector initialized, session_id: {self.session_id}")
    
    def parse_command(self, raw_command: str) -> ParsedCommand:
        """解析原始命令，拆分命令名、参数、选项"""
        try:
            parts = shlex.split(raw_command.strip())
        except ValueError:
            # 解析失败时降级处理
            parts = raw_command.strip().split()
            logger.debug(f"Command parsing fallback to simple split, raw: {raw_command[:50]}")
        
        if not parts:
            return ParsedCommand(command_name="", arguments=[], options={})
        
        command_name = parts[0]
        arguments = []
        options = {}
        
        i = 1
        while i < len(parts):
            part = parts[i]
            if part.startswith('--'):
                # 长选项
                key = part[2:]
                if '=' in key:
                    key, value = key.split('=', 1)
                    options[key] = value
                    i += 1
                else:
                    if i + 1 < len(parts) and not parts[i+1].startswith('-'):
                        options[key] = parts[i+1]
                        i += 2
                    else:
                        options[key] = True
                        i += 1
            elif part.startswith('-') and len(part) > 1:
                # 短选项
                for j in range(1, len(part)):
                    key = part[j]
                    if j == len(part) - 1 and i + 1 < len(parts) and not parts[i+1].startswith('-'):
                        options[key] = parts[i+1]
                        i += 1
                    else:
                        options[key] = True
                i += 1
            else:
                # 参数
                arguments.append(part)
                i += 1
        
        return ParsedCommand(
            command_name=command_name,
            arguments=arguments,
            options=options
        )
    
    def filter_sensitive_info(self, raw_command: str) -> Tuple[str, List[str]]:
        """过滤敏感信息，替换为脱敏标记，返回过滤后的命令和敏感信息列表"""
        return self.sensitive_filter.filter_raw_command(raw_command)
    
    def extract_context(self, working_dir: Optional[str] = None) -> CommandContext:
        """提取当前上下文信息：会话ID、项目ID、环境、Git分支等"""
        if working_dir is None:
            working_dir = os.getcwd()
        
        context = CommandContext(
            working_dir=working_dir,
            session_id=self.session_id,
            last_command=self.last_command,
            command_count=self.command_count
        )
        
        # 提取Git分支信息
        try:
            repo = pygit2.Repository(working_dir)
            # 使用getattr安全访问shorthand属性，避免detached HEAD等场景抛出异常
            context.git_branch = getattr(repo.head, 'shorthand', None)
            # 用Git仓库URL的SHA-256哈希作为项目ID，减少哈希冲突风险
            if "origin" in repo.remotes.names():
                origin_url = repo.remotes["origin"].url
                url_hash = hashlib.sha256(origin_url.encode()).hexdigest()[:16]
                context.project_id = f"proj_{url_hash}"
                logger.debug(f"Git project detected: {context.project_id}, branch: {context.git_branch}")
        except (pygit2.GitError, KeyError, AttributeError):
            # 捕获更多异常类型：Git错误、键不存在、属性不存在
            # 不是Git仓库，用目录路径的SHA-256哈希作为项目ID
            path_hash = hashlib.sha256(working_dir.encode()).hexdigest()[:16]
            context.project_id = f"proj_{path_hash}"
            logger.debug(f"Non-Git project detected: {context.project_id}")
        
        # 提取环境变量
        context.environment = os.getenv("ENV") or os.getenv("ENVIRONMENT") or "dev"
        
        # 提取用户ID
        context.user_id = os.getenv("USER") or os.getenv("USERNAME") or "unknown"
        
        return context
    
    def collect(self, raw_command: str, exit_code: Optional[int] = None, execution_time: Optional[float] = None, is_explicit: bool = False) -> Tuple[ParsedCommand, CommandContext, str]:
        """采集命令完整流程：过滤敏感信息→解析命令→提取上下文→更新会话状态"""
        try:
            # 过滤敏感信息
            filtered_command, sensitive_info = self.filter_sensitive_info(raw_command)
            if sensitive_info:
                logger.debug(f"Filtered {len(sensitive_info)} sensitive items from command")
            
            # 解析命令
            parsed = self.parse_command(filtered_command)
            
            # 二次过滤解析后的参数
            parsed = self.sensitive_filter.filter_command(parsed)
            
            # 提取上下文
            context = self.extract_context()
            
            # 更新会话状态
            self.command_count += 1
            self.last_command = filtered_command
            
            logger.debug(f"Command collected: {parsed.command_name}, args: {len(parsed.arguments)}, options: {len(parsed.options)}")
            return parsed, context, filtered_command
            
        except Exception as e:
            logger.error("Command collection failed", exception=e)
            # 失败降级：返回空命令结构，确保不崩溃
            return ParsedCommand("", [], {}), CommandContext(working_dir=os.getcwd()), ""
