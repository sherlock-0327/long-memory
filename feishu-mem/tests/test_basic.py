import os
import tempfile
import json
from pathlib import Path
from datetime import datetime
import pytest
from feishu_mem.core.storage import Storage, CommandRecord
from feishu_mem.core.collector import CommandCollector, SensitiveFilter
from feishu_mem.core.completion import CompletionEngine
from feishu_mem.core.vector_store import VectorStore
from feishu_mem.core.task_queue import AsyncTaskQueue, TaskStatus
from feishu_mem.core.wal import WriteAheadLog

@pytest.fixture
def temp_storage():
    """临时数据库fixture"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    storage = Storage(db_path=db_path)
    yield storage

    # 清理（Windows需要强制GC释放SQLite连接）
    import gc
    gc.collect()
    try:
        db_path.unlink()
    except PermissionError:
        pass  # Windows文件锁定，跳过清理

@pytest.fixture
def temp_vector_dir():
    """临时向量存储目录"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        yield Path(tmpdir)

@pytest.fixture
def temp_wal_dir():
    """临时WAL目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)

@pytest.fixture
def collector():
    return CommandCollector()

@pytest.fixture
def sensitive_filter():
    return SensitiveFilter()

@pytest.fixture
def completion_engine(temp_storage):
    return CompletionEngine(temp_storage)

@pytest.fixture
def task_queue():
    """临时任务队列"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    queue = AsyncTaskQueue(db_path=db_path)
    yield queue

    import gc
    gc.collect()
    try:
        db_path.unlink()
    except PermissionError:
        pass

def test_command_parsing(collector):
    """测试命令解析功能"""
    # 测试简单命令
    parsed = collector.parse_command("git pull origin main")
    assert parsed.command_name == "git"
    assert parsed.arguments == ["pull", "origin", "main"]
    assert parsed.options == {}
    
    # 测试带选项的命令
    parsed = collector.parse_command("ls -l -a --color=auto /home")
    assert parsed.command_name == "ls"
    assert parsed.arguments == ["/home"]
    assert parsed.options == {"l": True, "a": True, "color": "auto"}
    
    # 测试带引号的命令
    parsed = collector.parse_command('git commit -m "fix bug"')
    assert parsed.command_name == "git"
    assert parsed.arguments == ["commit"]
    assert parsed.options == {"m": "fix bug"}

def test_sensitive_filter_basic(sensitive_filter):
    """测试基础敏感信息过滤"""
    # 测试密码过滤
    test_cmd = "mysql -u root -p123456"
    filtered, sensitive = sensitive_filter.filter_raw_command(test_cmd)
    assert "***PASSWORD_HIDDEN***" in filtered
    assert len(sensitive) > 0
    
    # 测试Token过滤（Authorization头整体脱敏）
    test_cmd = "curl -H 'Authorization: Bearer abcdef123456' https://api.example.com"
    filtered, sensitive = sensitive_filter.filter_raw_command(test_cmd)
    assert "HIDDEN" in filtered
    assert "abcdef123456" not in filtered
    
    # 测试AWS密钥过滤
    test_cmd = "export AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF"
    filtered, sensitive = sensitive_filter.filter_raw_command(test_cmd)
    assert "AKIA" not in filtered
    
    # 测试手机号过滤
    test_cmd = "curl -d 'phone=13812345678' https://api.example.com"
    filtered, sensitive = sensitive_filter.filter_raw_command(test_cmd)
    assert "***PHONE_HIDDEN***" in filtered
    
    # 测试邮箱过滤
    test_cmd = "git config user.email test@example.com"
    filtered, sensitive = sensitive_filter.filter_raw_command(test_cmd)
    assert "***EMAIL_HIDDEN***" in filtered

def test_sensitive_filter_parsed_command(sensitive_filter, collector):
    """测试已解析命令的敏感信息过滤"""
    # 测试密码=格式的脱敏（原始命令级别）
    raw_cmd = "mysql --password=secret123 -h db.example.com"
    filtered_raw, sensitive = sensitive_filter.filter_raw_command(raw_cmd)
    assert "***PASSWORD_HIDDEN***" in filtered_raw
    assert "secret123" not in filtered_raw

def test_project_id_generation(collector, monkeypatch, tmp_path):
    """测试项目ID生成算法，验证SHA-256哈希减少冲突风险"""
    # 创建临时Git仓库
    import pygit2
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    
    # 初始化Git仓库
    repo = pygit2.init_repository(repo_path)
    # 创建一个空提交
    author = pygit2.Signature('Test User', 'test@example.com')
    committer = pygit2.Signature('Test User', 'test@example.com')
    tree = repo.TreeBuilder().write()
    repo.create_commit('HEAD', author, committer, 'Initial commit', tree, [])
    
    # 添加远程origin
    repo.remotes.create('origin', 'git@github.com:test/test-repo.git')
    
    # 切换到临时目录
    monkeypatch.chdir(repo_path)
    
    # 提取上下文
    context = collector.extract_context()
    
    # 验证项目ID格式（应该是proj_加上16位十六进制哈希）
    assert context.project_id.startswith("proj_")
    assert len(context.project_id) == len("proj_") + 16  # proj_ + 16 chars
    # 验证哈希是十六进制字符
    assert all(c in "0123456789abcdef" for c in context.project_id[5:])
    
    # 测试非Git仓库的项目ID生成
    non_git_path = tmp_path / "non_git_project"
    non_git_path.mkdir()
    monkeypatch.chdir(non_git_path)
    
    context2 = collector.extract_context()
    assert context2.project_id.startswith("proj_")
    assert len(context2.project_id) == len("proj_") + 16
    assert all(c in "0123456789abcdef" for c in context2.project_id[5:])
    
    # 确保不同路径生成不同ID
    assert context.project_id != context2.project_id

def test_storage_add_command(temp_storage):
    """测试存储添加命令"""
    record = CommandRecord(
        command_id="",
        raw_command="git pull origin main",
        command_name="git",
        arguments=["pull", "origin", "main"],
        options={},
        working_dir="/tmp/test",
        is_explicit=False
    )
    
    command_id = temp_storage.add_command(record)
    assert command_id is not None
    
    # 检查是否能查询到
    recent = temp_storage.get_recent_commands(limit=1)
    assert len(recent) == 1
    assert recent[0].raw_command == "git pull origin main"
    assert recent[0].usage_count == 1
    
    # 重复添加相同命令，应该更新使用次数
    record2 = CommandRecord(
        command_id="",
        raw_command="git pull origin main",
        command_name="git",
        arguments=["pull", "origin", "main"],
        options={},
        working_dir="/tmp/test",
        is_explicit=False
    )
    
    command_id2 = temp_storage.add_command(record2)
    assert command_id2 == command_id  # 相同哈希，返回相同ID
    
    recent = temp_storage.get_recent_commands(limit=1)
    assert recent[0].usage_count == 2  # 使用次数增加

def test_storage_prefix_match(temp_storage):
    """测试前缀匹配功能"""
    # 添加测试数据
    commands = [
        "git pull origin main",
        "git push origin main",
        "git status",
        "npm run build",
        "npm run dev",
        "ls -l"
    ]
    
    for cmd in commands:
        record = CommandRecord(
            command_id="",
            raw_command=cmd,
            command_name=cmd.split()[0],
            arguments=cmd.split()[1:],
            options={},
            working_dir="/tmp/test"
        )
        temp_storage.add_command(record)
    
    # 测试git前缀匹配
    matches = temp_storage.get_prefix_matches("git", limit=10)
    assert len(matches) == 3
    
    # 测试npm前缀匹配
    matches = temp_storage.get_prefix_matches("npm", limit=10)
    assert len(matches) == 2
    
    # 测试更精确的前缀
    matches = temp_storage.get_prefix_matches("git pu", limit=10)
    assert len(matches) == 2

def test_storage_get_by_ids(temp_storage):
    """测试根据ID批量查询命令"""
    # 添加测试数据
    ids = []
    for i in range(5):
        record = CommandRecord(
            command_id="",
            raw_command=f"echo test {i}",
            command_name="echo",
            arguments=[f"test {i}"],
            options={},
            working_dir="/tmp/test"
        )
        cmd_id = temp_storage.add_command(record)
        ids.append(cmd_id)
    
    # 批量查询
    records = temp_storage.get_commands_by_ids(ids)
    assert len(records) == 5
    assert {r.command_id for r in records} == set(ids)
    
    # 保持顺序
    for i, record in enumerate(records):
        assert record.command_id == ids[i]

def test_completion_basic(completion_engine, temp_storage):
    """测试基础补全功能"""
    # 添加测试数据
    commands = [
        "git pull origin main",
        "git push origin main",
        "git status",
        "npm run build",
        "npm run dev"
    ]
    
    for cmd in commands:
        record = CommandRecord(
            command_id="",
            raw_command=cmd,
            command_name=cmd.split()[0],
            arguments=cmd.split()[1:],
            options={},
            working_dir="/tmp/test"
        )
        temp_storage.add_command(record)
    
    # 测试补全
    completions = completion_engine.get_completions("git", limit=5)
    assert len(completions) >= 3
    assert all("git" in c.command for c in completions)
    
    # 测试太短的前缀不返回结果
    completions = completion_engine.get_completions("g", limit=5)
    assert len(completions) == 0

def test_completion_empty_command(completion_engine, temp_storage):
    """测试空命令的补全得分计算，验证除零错误修复"""
    # 添加一个空命令记录（边界情况）
    record = CommandRecord(
        command_id="test_empty",
        raw_command="",
        command_name="",
        arguments=[],
        options={},
        working_dir="/tmp/test"
    )
    temp_storage.add_command(record)
    
    # 尝试补全，应该不会崩溃
    completions = completion_engine.get_completions("", limit=5)
    # 空前缀应该返回空列表
    assert len(completions) == 0
    
    # 验证得分计算不会崩溃
    try:
        score = completion_engine._calculate_score(record, prefix_length=2)
        # 空命令得分应该是有效的数值
        assert isinstance(score, float)
        assert score >= 0
    except ZeroDivisionError:
        pytest.fail("ZeroDivisionError occurred in _calculate_score with empty command")

def test_vector_store_basic(temp_vector_dir):
    """测试向量存储基本功能"""
    vector_store = VectorStore(persist_directory=temp_vector_dir)
    
    # 添加测试命令
    record = CommandRecord(
        command_id="test123",
        raw_command="git push origin main",
        command_name="git",
        arguments=["push", "origin", "main"],
        options={},
        working_dir="/tmp/test",
        executed_at=datetime.now(),
        last_used_at=datetime.now()
    )
    
    vec_id = vector_store.add_command(record)
    assert vec_id == "test123"
    assert vector_store.command_count() == 1  # 唯一命令数为1
    assert vector_store.count() >= 1  # 文档数>=1（包含细粒度文档）

    # 测试搜索
    results = vector_store.search_commands("git push", limit=5)
    assert len(results) > 0
    assert results[0]["command_id"] == "test123"

    # 测试删除（删除命令及所有细粒度文档）
    vector_store.delete_command("test123")
    assert vector_store.command_count() == 0

def test_task_queue_basic(task_queue):
    """测试异步任务队列基本功能"""
    # 入队任务
    task_id = task_queue.enqueue("test_task", {"data": "test"}, priority=1)
    assert task_id is not None

    # 认领任务
    task = task_queue.claim_next()
    assert task is not None
    assert task.task_id == task_id
    assert task.status == TaskStatus.PROCESSING
    assert task.payload["data"] == "test"

    # 确认任务完成
    success = task_queue.confirm(task_id)
    assert success is True

    # 检查任务状态
    task = task_queue.get_task(task_id)
    assert task.status == TaskStatus.COMPLETED

def test_task_queue_retry(task_queue):
    """测试任务重试机制"""
    # 入队任务，最大重试2次
    task_id = task_queue.enqueue("test_task", {"data": "test"}, max_retries=2)

    # 第一次认领并标记失败
    task = task_queue.claim_next()
    assert task.retries == 0
    task_queue.mark_failed(task_id, "test error")

    # 应该可以再次认领
    task = task_queue.claim_next()
    assert task is not None
    assert task.retries == 1
    task_queue.mark_failed(task_id, "test error 2")

    # 第二次重试会达到max_retries，进入死信队列
    task = task_queue.get_task(task_id)
    assert task.status == TaskStatus.DEAD

def test_wal_basic(temp_wal_dir, temp_storage, temp_vector_dir):
    """测试WAL基本功能"""
    wal = WriteAheadLog(wal_path=temp_wal_dir)
    
    # 写入WAL条目
    record = CommandRecord(
        command_id="",
        raw_command="git pull origin main",
        command_name="git",
        arguments=["pull", "origin", "main"],
        options={},
        working_dir="/tmp/test",
        executed_at=datetime.now(),
        last_used_at=datetime.now()
    )
    
    entry_id = wal.append({
        "type": "add_command",
        "record": {
            **record.__dict__,
            "executed_at": record.executed_at.isoformat(),
            "last_used_at": record.last_used_at.isoformat()
        }
    })
    assert entry_id is not None
    
    # 检查未完成条目
    incomplete = wal.get_incomplete_entries()
    assert len(incomplete) == 1
    assert incomplete[0]["entry_id"] == entry_id
    
    # 标记为已完成
    wal.mark_complete(entry_id)
    incomplete = wal.get_incomplete_entries()
    assert len(incomplete) == 0

def test_wal_recovery(temp_wal_dir, temp_storage, temp_vector_dir):
    """测试WAL故障恢复"""
    wal = WriteAheadLog(wal_path=temp_wal_dir)
    vector_store = VectorStore(persist_directory=temp_vector_dir)
    
    # 添加一个WAL条目，不标记完成（模拟崩溃）
    record = CommandRecord(
        command_id="test_restore",
        raw_command="git push origin main",
        command_name="git",
        arguments=["push", "origin", "main"],
        options={},
        working_dir="/tmp/test",
        executed_at=datetime.now(),
        last_used_at=datetime.now()
    )
    
    wal.append({
        "type": "add_command",
        "record": {
            **record.__dict__,
            "executed_at": record.executed_at.isoformat(),
            "last_used_at": record.last_used_at.isoformat()
        }
    })
    
    # 此时数据库中还没有数据
    records = temp_storage.get_recent_commands()
    assert len(records) == 0
    
    # 执行重放恢复
    recovered = wal.replay_incomplete(temp_storage, vector_store)
    assert recovered == 1
    
    # 数据已恢复
    records = temp_storage.get_recent_commands()
    assert len(records) == 1
    assert records[0].raw_command == "git push origin main"

def test_storage_add_and_search(temp_storage):
    """测试存储和搜索功能"""
    # 添加测试命令
    record = CommandRecord(
        command_id="",
        raw_command="npm run build:prod",
        command_name="npm",
        arguments=["run", "build:prod"],
        options={},
        working_dir="/home/user/project",
        project_id="test_project",
        environment="prod"
    )
    
    command_id = temp_storage.add_command(record)
    assert command_id is not None
    
    # 搜索命令
    results = temp_storage.search_commands("build")
    assert len(results) == 1
    assert results[0].raw_command == "npm run build:prod"
    
    # 前缀匹配
    prefix_results = temp_storage.get_prefix_matches("npm run")
    assert len(prefix_results) == 1
    assert prefix_results[0].raw_command == "npm run build:prod"

def test_completion(completion_engine, temp_storage):
    """测试补全功能"""
    # 添加测试命令
    records = [
        CommandRecord(
            command_id="",
            raw_command="git pull origin main",
            command_name="git",
            arguments=["pull", "origin", "main"],
            options={},
            working_dir="/home/user/project",
            usage_count=5
        ),
        CommandRecord(
            command_id="", 
            raw_command="git push origin main",
            command_name="git",
            arguments=["push", "origin", "main"],
            options={},
            working_dir="/home/user/project",
            usage_count=3
        ),
        CommandRecord(
            command_id="",
            raw_command="npm run build",
            command_name="npm", 
            arguments=["run", "build"],
            options={},
            working_dir="/home/user/project"
        )
    ]
    
    for record in records:
        temp_storage.add_command(record)
    
    # 测试补全
    completions = completion_engine.get_completions("git ")
    assert len(completions) == 2
    assert completions[0].command == "git pull origin main"  # 使用次数更高，排前面
    assert completions[1].command == "git push origin main"
    
    # 测试前缀匹配
    completions = completion_engine.get_completions("npm run")
    assert len(completions) == 1
    assert completions[0].command == "npm run build"

def test_duplicate_command(temp_storage):
    """测试重复命令自动增加使用次数"""
    record = CommandRecord(
        command_id="",
        raw_command="git status",
        command_name="git",
        arguments=["status"],
        options={},
        working_dir="/home/user/project"
    )
    
    # 第一次添加
    id1 = temp_storage.add_command(record)
    record1 = temp_storage.get_recent_commands(limit=1)[0]
    assert record1.usage_count == 1
    
    # 第二次添加相同命令
    id2 = temp_storage.add_command(record)
    record2 = temp_storage.get_recent_commands(limit=1)[0]
    assert id2 == id1  # 相同命令返回相同ID
    assert record2.usage_count == 2  # 使用次数增加
