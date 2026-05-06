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
from feishu_mem.core.session_manager import get_session_manager
from feishu_mem.cli.hooks.installer import HookInstaller
from feishu_mem.shared.config import config
from feishu_mem.shared.logger import logger

# 全局实例，支持优雅降级
try:
    from feishu_mem.core.wal import get_wal
    wal = get_wal()

    storage = Storage()
    vector_store = VectorStore() if config.vector_search_enabled else None

    wal.replay_incomplete(storage, vector_store)

    collector = CommandCollector()
    completion_engine = CompletionEngine(storage, vector_store)
    hook_installer = HookInstaller()
    session_mgr = get_session_manager()

    logger.info("Feishu-Mem CLI initialized successfully")
except Exception as e:
    logger.error("Failed to initialize Feishu-Mem", exception=e)
    storage = None
    vector_store = None
    wal = None
    collector = None
    completion_engine = None
    hook_installer = None
    session_mgr = None

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
def uninstall():
    """卸载Shell钩子"""
    try:
        hook_installer.uninstall()
        click.echo("✅ Feishu-Mem Shell钩子已卸载。")
    except Exception as e:
        click.echo(f"❌ 卸载失败：{str(e)}", err=True)
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
            sys.exit(0)

        if start_time and end_time and not execution_time:
            execution_time = max(end_time - start_time, 0)

        parsed, context, filtered_command = collector.collect(
            raw_command,
            exit_code=exit_code,
            execution_time=execution_time,
        )

        if parsed.command_name:
            # 获取或创建活跃会话
            session_id = None
            if session_mgr:
                try:
                    session = session_mgr.get_or_create_active_session(
                        user_id=context.user_id,
                        project_id=context.project_id,
                        environment=context.environment,
                    )
                    session_id = session.session_id
                    session_mgr.record_command(session_id)
                except Exception:
                    pass

            record = CommandRecord(
                command_id="",
                raw_command=filtered_command,
                command_name=parsed.command_name,
                arguments=parsed.arguments,
                options=parsed.options,
                working_dir=context.working_dir,
                session_id=session_id or context.session_id,
                project_id=context.project_id,
                environment=context.environment,
                exit_code=exit_code,
                execution_time=execution_time,
                user_id=context.user_id,
            )
            storage.add_command(record, vector_store)

            # 更新采集指标
            try:
                from feishu_mem.shared.metrics import get_metrics
                get_metrics().counter_inc("command_collected_total")
            except Exception:
                pass

        # 定期清理过期记忆（每100次钩子调用执行一次）
        import random
        if random.randint(1, 100) == 1:
            storage.cleanup_expired_memory()
            # 清理僵尸会话
            if session_mgr:
                try:
                    session_mgr.abandon_stale_sessions()
                except Exception:
                    pass
            
    except Exception as e:
        # 钩子执行失败时静默退出，绝对不影响用户正常使用
        logger.debug("Hook execution failed", exception=e, command=raw_command[:50])
        sys.exit(0)

@cli.command()
@click.argument("command_id")
@click.option("--command", "-c", help="新的命令内容")
@click.option("--description", "-d", help="命令描述")
@click.option("--tags", "-t", multiple=True, help="标签")
def edit(command_id, command, description, tags):
    """编辑已记忆的命令"""
    try:
        records = storage.get_commands_by_ids([command_id])
        if not records:
            click.echo(f"❌ 未找到ID为 {command_id} 的命令")
            sys.exit(1)
        
        record = records[0]
        
        # 更新字段
        if command:
            # 重新解析命令
            parsed = collector.parse_command(command)
            record.raw_command = command
            record.command_name = parsed.command_name
            record.arguments = parsed.arguments
            record.options = parsed.options
        
        if description:
            # 描述暂时保存在tags里或者新增字段，先存在tags里
            if not record.tags:
                record.tags = []
            record.tags = [t for t in record.tags if not t.startswith("desc:")]
            record.tags.append(f"desc:{description}")
        
        if tags:
            record.tags = list(tags)
        
        # 更新到数据库
        storage.update_command(record)
        click.echo("✅ 命令已更新！")
        click.echo(f"命令: {record.raw_command}")
        
    except Exception as e:
        click.echo(f"❌ 编辑失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command()
@click.argument("command_id")
@click.confirmation_option(prompt="确定要删除这条命令吗？")
def delete(command_id):
    """删除已记忆的命令"""
    try:
        deleted = storage.delete_command(command_id, vector_store=vector_store)
        if deleted > 0:
            click.echo(f"✅ 命令 {command_id} 已删除")
        else:
            click.echo(f"❌ 未找到ID为 {command_id} 的命令")
            sys.exit(1)
    except Exception as e:
        click.echo(f"❌ 删除失败：{str(e)}", err=True)
        sys.exit(1)

@cli.group()
def workflow():
    """工作流管理命令"""
    pass

@workflow.command("list")
@click.option("--all", "-a", is_flag=True, help="显示所有共享工作流")
def workflow_list(all):
    """列出可用工作流"""
    try:
        from feishu_mem.core.workflow import get_workflow_engine
        engine = get_workflow_engine(storage)
        
        if all:
            workflows = engine.list_workflows(include_shared=True)
        else:
            workflows = engine.list_workflows(user_id=os.getenv("USER") or os.getenv("USERNAME") or "unknown")
        
        if not workflows:
            click.echo("还没有可用的工作流")
            return
        
        click.echo(f"找到 {len(workflows)} 个工作流：")
        for i, wf in enumerate(workflows, 1):
            shared = " 🔄" if wf.is_shared else ""
            click.echo(f"{i:2d}. {wf.name}{shared} (步骤: {len(wf.steps)}, 使用次数: {wf.usage_count})")
            if wf.description:
                click.echo(f"     描述: {wf.description}")
    except Exception as e:
        click.echo(f"❌ 获取工作流列表失败：{str(e)}", err=True)
        sys.exit(1)

@workflow.command("run")
@click.argument("name")
@click.option("--param", "-p", multiple=True, help="参数，格式: key=value")
@click.option("--dry-run", is_flag=True, help="只预览执行计划，不实际执行")
def workflow_run(name, param, dry_run):
    """执行工作流"""
    try:
        from feishu_mem.core.workflow import get_workflow_engine
        engine = get_workflow_engine(storage)
        
        workflow = engine.get_workflow_by_name(name)
        if not workflow:
            click.echo(f"❌ 未找到名为 {name} 的工作流")
            sys.exit(1)
        
        # 解析参数
        params = {}
        for p in param:
            if "=" in p:
                key, value = p.split("=", 1)
                params[key.strip()] = value.strip()
        
        click.echo(f"🚀 开始执行工作流: {workflow.name}")
        if params:
            click.echo(f"参数: {params}")
        
        result = engine.execute_workflow(workflow.workflow_id, parameters=params, dry_run=dry_run)
        
        if dry_run:
            click.echo("\n📋 执行计划:")
            for step in result.step_results:
                click.echo(f"  {step['step_id']}: {step['command']}")
            return
        
        click.echo(f"\n执行结果: {'✅ 成功' if result.status == 'completed' else '❌ 失败'}")
        click.echo(f"总耗时: {(result.end_time - result.start_time).total_seconds():.2f}秒")
        
        for step in result.step_results:
            status_icon = "✅" if step["status"] == "completed" else "❌" if step["status"] == "failed" else "⏭️"
            click.echo(f"\n{status_icon} {step['step_id']}: {step['command']}")
            if step["status"] == "failed":
                click.echo(f"   错误: {step.get('error', '未知错误')}")
            elif step["status"] == "completed" and step.get("stdout"):
                click.echo(f"   输出: {step['stdout'][:200]}")
        
        if result.status == "failed":
            click.echo(f"\n❌ 工作流执行失败: {result.error_message}")
            sys.exit(1)
        
    except Exception as e:
        click.echo(f"❌ 工作流执行失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command()
def stats():
    """查看系统统计指标"""
    try:
        from feishu_mem.shared.metrics import get_metrics
        metrics = get_metrics().get_metrics()

        click.echo("📊 系统统计指标")
        click.echo(f"运行时间: {int(metrics['system']['uptime_seconds']/3600)}小时 {int(metrics['system']['uptime_seconds']%3600/60)}分钟")
        click.echo("")

        click.echo("🔢 核心指标:")
        click.echo(f"  命令采集总数: {metrics['counter']['command_collected_total']['value']}")
        click.echo(f"  命令存储总数: {metrics['counter']['command_added_total']['value']}")
        click.echo(f"  补全请求总数: {metrics['counter']['completion_requests_total']['value']}")
        click.echo(f"  缓存命中率: {metrics['gauge']['cache_l1_hit_rate']['value']:.1%}")
        click.echo(f"  工作流执行总数: {metrics['counter']['workflow_executions_total']['value']}")
        click.echo(f"  工作流成功率: {metrics['counter']['workflow_executions_success_total']['value'] / max(metrics['counter']['workflow_executions_total']['value'], 1):.1%}")
        click.echo("")

        click.echo("⚡ 性能指标:")
        if metrics['histogram']['completion_duration_seconds']['count'] > 0:
            avg_completion = metrics['histogram']['completion_duration_seconds']['sum'] / metrics['histogram']['completion_duration_seconds']['count']
            click.echo(f"  平均补全响应时间: {avg_completion*1000:.1f}ms")

        if metrics['histogram']['command_add_duration_seconds']['count'] > 0:
            avg_add = metrics['histogram']['command_add_duration_seconds']['sum'] / metrics['histogram']['command_add_duration_seconds']['count']
            click.echo(f"  平均命令存储时间: {avg_add*1000:.1f}ms")

        # 补全引擎上下文经济学
        if completion_engine:
            eco = completion_engine.get_economics()
            click.echo("")
            click.echo("💡 补全效率统计:")
            click.echo(f"  总查询次数: {eco['total_queries']}")
            click.echo(f"  缓存命中次数: {eco['cache_hits']}")
            click.echo(f"  缓存命中率: {eco['cache_hit_rate_percent']:.1f}%")
            click.echo(f"  输入字符总数: {eco['total_chars_input']}")
            click.echo(f"  推荐字符总数: {eco['total_chars_suggested']}")
            click.echo(f"  字符节省率: {eco['char_savings_rate_percent']:.1f}%")

    except Exception as e:
        click.echo(f"❌ 获取统计信息失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command()
def cleanup():
    """手动清理过期记忆"""
    try:
        from feishu_mem.core.forgetting_engine import get_forgetting_engine
        engine = get_forgetting_engine()
        
        click.echo("🧹 开始清理过期记忆...")
        deleted_expired = engine.auto_cleanup_expired_memory()
        deleted_low_value = engine.forget_low_value_memory()
        
        click.echo(f"✅ 清理完成：")
        click.echo(f"  过期记忆删除: {deleted_expired} 条")
        click.echo(f"  低价值记忆遗忘: {deleted_low_value} 条")
        
    except Exception as e:
        click.echo(f"❌ 清理失败：{str(e)}", err=True)
        sys.exit(1)

@cli.command(hidden=True)
@click.argument("prefix", required=False)
def completion(prefix=""):
    """命令补全接口，供Shell补全系统调用"""
    try:
        if not completion_engine:
            return
        
        # 获取上下文信息
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

@cli.group()
def session():
    """会话管理命令"""
    pass


@session.command("list")
@click.option("--limit", "-l", default=10, help="显示数量")
@click.option("--status", "-s", help="按状态过滤: active/completed/abandoned")
def session_list(limit, status):
    """列出最近的会话"""
    try:
        if not session_mgr:
            click.echo("会话管理器未初始化")
            sys.exit(1)

        user_id = os.getenv("USER") or os.getenv("USERNAME") or "unknown"
        sessions = session_mgr.get_user_sessions(user_id, limit=limit, status=status)

        if not sessions:
            click.echo("还没有会话记录")
            return

        click.echo(f"最近 {len(sessions)} 个会话：")
        for i, s in enumerate(sessions, 1):
            status_icon = {"active": "🟢", "completed": "✅", "abandoned": "⚠️"}.get(s["status"], "❓")
            duration_min = s["duration_seconds"] / 60
            click.echo(
                f"{i:2d}. {status_icon} [{s['status']}] "
                f"命令数: {s['command_count']}, "
                f"时长: {duration_min:.1f}分钟, "
                f"开始: {s['started_at']}"
            )
    except Exception as e:
        click.echo(f"获取会话列表失败：{str(e)}", err=True)
        sys.exit(1)


@cli.command()
def version():
    """显示版本信息"""
    import importlib.metadata
    try:
        ver = importlib.metadata.version("feishu-mem")
    except importlib.metadata.PackageNotFoundError:
        ver = "dev"
    click.echo(f"Feishu-Mem v{ver}")
    click.echo("企业级长程协作Memory系统")

if __name__ == "__main__":
    cli()
