import click
import os
import sys
import traceback
from pathlib import Path
from datetime import datetime

from feishu_mem.core.storage import Storage, CommandRecord
from feishu_mem.core.collector import CommandCollector
from feishu_mem.core.completion import CompletionEngine
from feishu_mem.core.vector_store import VectorStore
from feishu_mem.cli.hooks.installer import HookInstaller
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

# 全局实例，支持优雅降级
try:
    # 初始化WAL并执行故障恢复
    from feishu_mem.core.wal import get_wal
    wal = get_wal()
    
    storage = Storage()
    vector_store = VectorStore() if config.vector_search_enabled else None
    
    # 重放未完成的WAL条目，恢复数据一致性
    wal.replay_incomplete(storage, vector_store)
    
    collector = CommandCollector()
    completion_engine = CompletionEngine(storage, vector_store)
    hook_installer = HookInstaller()
    
    logger.info("Feishu-Mem CLI initialized successfully")
except Exception as e:
    logger.error("Failed to initialize Feishu-Mem", exception=e)
    # 初始化失败时设置为None，后续操作优雅降级
    storage = None
    vector_store = None
    wal = None
    collector = None
    completion_engine = None
    hook_installer = None

@click.group()
def cli():
    """Feishu-Mem: 企业级长程协作Memory系统"""
    pass

@cli.command()
def install():
    """安装Shell钩子，开启命令自动采集"""
    try:
        hook_installer.install()
        click.echo("✅ Feishu-Mem 安装成功！Shell钩子已配置。")
        click.echo("请重启终端或执行 `source ~/.zshrc`（或对应Shell配置文件）生效。")
    except Exception as e:
        click.echo(f"❌ 安装失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command()
@click.argument("query")
@click.option("--limit", "-l", default=10, help="返回结果数量")
@click.option("--project", "-p", help="指定项目ID")
@click.option("--env", "-e", help="指定环境")
def search(query, limit, project, env):
    """搜索历史命令"""
    try:
        results = storage.search_commands(query, project_id=project, environment=env, limit=limit)
        if not results:
            click.echo("没有找到匹配的命令")
            return
        
        click.echo(f"找到 {len(results)} 条匹配命令：")
        for i, record in enumerate(results, 1):
            env_info = f" [{record.environment}]" if record.environment else ""
            explicit = " ⭐" if record.is_explicit else ""
            click.echo(f"{i:2d}. {record.raw_command}{env_info}{explicit} (使用次数: {record.usage_count})")
    except Exception as e:
        click.echo(f"❌ 搜索失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command()
@click.argument("command")
@click.argument("description", required=False)
@click.option("--tags", "-t", multiple=True, help="标签")
def teach(command, description, tags):
    """主动教学记忆命令"""
    try:
        parsed, context, filtered_command = collector.collect(command, is_explicit=True)
        
        record = CommandRecord(
            command_id="",
            raw_command=filtered_command,
            command_name=parsed.command_name,
            arguments=parsed.arguments,
            options=parsed.options,
            working_dir=context.working_dir,
            project_id=context.project_id,
            environment=context.environment,
            user_id=context.user_id,
            tags=list(tags),
            is_explicit=True
        )
        
        command_id = storage.add_command(record)
        click.echo(f"✅ 命令已记忆！ID: {command_id}")
        click.echo(f"命令: {filtered_command}")
    except Exception as e:
        click.echo(f"❌ 记忆失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command()
@click.option("--limit", "-l", default=10, help="显示数量")
def list(limit):
    """列出最近使用的命令"""
    try:
        records = storage.get_recent_commands(limit=limit)
        if not records:
            click.echo("还没有历史命令记录")
            return
        
        click.echo(f"最近 {len(records)} 条命令：")
        for i, record in enumerate(records, 1):
            time_str = record.executed_at.strftime("%m-%d %H:%M")
            env_info = f" [{record.environment}]" if record.environment else ""
            explicit = " ⭐" if record.is_explicit else ""
            click.echo(f"{i:2d}. [{time_str}] {record.raw_command}{env_info}{explicit}")
    except Exception as e:
        click.echo(f"❌ 获取命令列表失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command(hidden=True)
@click.argument("raw_command")
@click.option("--exit-code", type=int, help="命令退出码")
@click.option("--execution-time", type=float, help="命令执行时间(秒)")
@click.option("--start-time", type=int, help="命令开始时间戳(秒)")
@click.option("--end-time", type=int, help="命令结束时间戳(秒)")
def hook(raw_command, exit_code, execution_time, start_time, end_time):
    """Shell钩子调用的命令采集接口（内部使用）"""
    # 钩子执行必须保证绝对不崩溃，不影响用户正常使用
    try:
        if not collector or not storage:
            # 初始化失败时直接退出
            sys.exit(0)
        
        # 计算执行时间，如果提供了start和end时间
        if start_time and end_time and not execution_time:
            execution_time = max(end_time - start_time, 0)
        
        parsed, context, filtered_command = collector.collect(
            raw_command, 
            exit_code=exit_code, 
            execution_time=execution_time
        )
        
        if parsed.command_name:  # 忽略空命令
            record = CommandRecord(
                command_id="",
                raw_command=filtered_command,
                command_name=parsed.command_name,
                arguments=parsed.arguments,
                options=parsed.options,
                working_dir=context.working_dir,
                project_id=context.project_id,
                environment=context.environment,
                exit_code=exit_code,
                execution_time=execution_time,
                user_id=context.user_id
            )
            storage.add_command(record, vector_store)
            
        # 定期清理过期记忆（每100次钩子调用执行一次，避免频繁IO）
        import random
        if random.randint(1, 100) == 1:
            storage.cleanup_expired_memory()
            
    except Exception as e:
        # 钩子执行失败时静默退出，绝对不影响用户正常使用
        logger.debug("Hook execution failed", exception=e, command=raw_command[:50])
        sys.exit(0)

@cli.command(hidden=True)
@click.argument("prefix", required=False)
def completion(prefix=""):
    """命令补全接口，供Shell补全系统调用"""
    try:
        if not completion_engine:
            return
        
        # 获取上下文信息
        from feishu_mem.core.collector import CommandCollector
        collector = CommandCollector()
        context = collector.extract_context()
        
        completions = completion_engine.get_completions(
            prefix, 
            project_id=context.project_id, 
            environment=context.environment
        )
        
        # 输出补全结果，每行一个
        for item in completions:
            click.echo(item.command)
            
    except Exception:
        # 补全接口绝对不允许崩溃，避免影响用户输入体验
        sys.exit(0)

@cli.command()
def version():
    """显示版本信息"""
    import importlib.metadata
    try:
        version = importlib.metadata.version("feishu-mem")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    click.echo(f"Feishu-Mem v{version}")
    click.echo("企业级长程协作Memory系统")

@cli.command()
def stats():
    """显示记忆统计信息"""
    try:
        if not storage:
            click.echo("存储未初始化")
            return
        
        # TODO: 实现统计功能
        click.echo("统计功能开发中...")
        
    except Exception as e:
        click.echo(f"❌ 获取统计信息失败：{str(e)}", err=True)
        sys.exit(1)

if __name__ == "__main__":
    cli()
