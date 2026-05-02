"""
抗干扰测试：验证系统在大量无关信息干扰下，能否精准召回关键记忆
测试场景符合项目要求：
1. 第1天注入关键记忆
2. 连续7天注入1000+随机噪声命令
3. 第8天验证关键记忆召回率100%，排序第1位
"""
import pytest
import time
import random
import string
from datetime import datetime, timedelta
from pathlib import Path
import tempfile

from feishu_mem.core.storage import Storage, CommandRecord
from feishu_mem.core.completion import CompletionEngine
from feishu_mem.core.vector_store import VectorStore
from feishu_mem.shared.config import config


@pytest.fixture
def test_storage():
    """创建临时数据库用于测试"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    storage = Storage(db_path=db_path)
    yield storage

    # 清理（Windows需要强制GC释放SQLite连接）
    import gc
    gc.collect()
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


@pytest.fixture
def test_vector_store():
    """创建临时向量存储"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vector_store = VectorStore(persist_directory=Path(tmpdir))
        yield vector_store


@pytest.fixture
def completion_engine(test_storage, test_vector_store):
    return CompletionEngine(test_storage, test_vector_store)


def _random_word(length=8):
    return "".join(random.choices(string.ascii_lowercase, k=length))


def _random_filename():
    return f"{_random_word(6)}.{random.choice(['txt', 'py', 'js', 'log', 'json', 'md'])}"


def generate_random_command(working_dir="/tmp/test") -> CommandRecord:
    """生成随机噪声命令"""
    command_types = [
        ("ls", ["-la", "-lh", "-a", "-l"]),
        ("cat", [_random_filename() for _ in range(10)]),
        ("grep", [_random_word() for _ in range(10)]),
        ("find", [".", "-name", f"*.{random.choice(['txt', 'py', 'js', 'log'])}"]),
        ("vim", [_random_filename() for _ in range(10)]),
        ("npm", ["install", "run build", "run test", "run dev"]),
        ("git", ["status", "log", "diff", "pull", "push"]),
        ("curl", [f"https://{_random_word()}.example.com" for _ in range(5)]),
        ("pip", ["install", "list", "freeze"]),
        ("make", ["clean", "build", "install"]),
    ]

    cmd_name, args_list = random.choice(command_types)
    args = [random.choice(args_list) for _ in range(random.randint(0, 3))]

    return CommandRecord(
        command_id="",
        raw_command=f"{cmd_name} {' '.join(args)}",
        command_name=cmd_name,
        arguments=args,
        options={},
        working_dir=working_dir,
        is_successful=random.random() < 0.8,
        executed_at=datetime.now(),
        last_used_at=datetime.now()
    )


def test_antijamming(test_storage, test_vector_store, completion_engine):
    """抗干扰测试主流程"""
    # ========== 第1天：注入关键记忆 ==========
    key_command = "deploy-prod --env production --region us-east-1 --db-host db.internal"
    key_record = CommandRecord(
        command_id="",
        raw_command=key_command,
        command_name="deploy-prod",
        arguments=["--env", "production", "--region", "us-east-1", "--db-host", "db.internal"],
        options={},
        working_dir="/home/user/project",
        is_explicit=True,  # 显式教学标记
        is_successful=True,
        executed_at=datetime.now() - timedelta(days=8),
        last_used_at=datetime.now() - timedelta(days=8)
    )
    
    key_command_id = test_storage.add_command(key_record, vector_store=test_vector_store)
    assert key_command_id is not None
    print(f"✅ 关键记忆注入成功: {key_command}")
    
    # ========== 连续7天注入噪声命令 ==========
    total_noise = 1500
    success_count = 0
    
    print(f"🚀 开始注入 {total_noise} 条噪声命令...")
    start_time = time.time()
    
    for i in range(total_noise):
        # 模拟过去7天的随机时间
        days_ago = random.randint(1, 7)
        execute_time = datetime.now() - timedelta(days=days_ago, hours=random.randint(0, 23))

        cmd = generate_random_command()
        cmd.executed_at = execute_time
        cmd.last_used_at = execute_time
        
        cmd_id = test_storage.add_command(cmd, vector_store=test_vector_store)
        if cmd_id:
            success_count += 1
        
        if (i + 1) % 300 == 0:
            print(f"   已注入 {i+1}/{total_noise} 条命令")
    
    elapsed = time.time() - start_time
    print(f"✅ 噪声注入完成，成功 {success_count}/{total_noise} 条，耗时 {elapsed:.2f}s")
    
    # ========== 第8天：验证关键记忆召回 ==========
    test_cases = [
        ("deploy", "短前缀召回"),
        ("deploy-", "中缀召回"),
        ("deploy-prod", "完整命令名召回"),
        ("deploy-prod --env", "带部分参数召回"),
        ("deploy to production", "语义召回（英文）"),
    ]
    
    all_passed = True
    for prefix, test_name in test_cases:
        completions = completion_engine.get_completions(
            prefix, 
            project_id=key_record.project_id,
            environment=key_record.environment,
            limit=10
        )
        
        # 检查关键命令是否在结果中且排第一
        found = False
        first_place = False
        
        for idx, item in enumerate(completions):
            if item.command.strip() == key_command.strip():
                found = True
                if idx == 0:
                    first_place = True
                break
        
        if found and first_place:
            print(f"✅ [{test_name}] 测试通过: '{prefix}' → 关键记忆召回第1位")
        elif found:
            print(f"❌ [{test_name}] 测试失败: '{prefix}' → 关键记忆召回但未排第1位，位置: {idx+1}")
            all_passed = False
        else:
            print(f"❌ [{test_name}] 测试失败: '{prefix}' → 未召回关键记忆")
            print(f"   返回结果: {[c.command[:50] for c in completions]}")
            all_passed = False
    
    # ========== 验证性能指标 ==========
    # 测试100次查询的平均响应时间
    total_time = 0
    query_count = 100
    
    for _ in range(query_count):
        start = time.perf_counter()
        completion_engine.get_completions("deploy", limit=5)
        total_time += time.perf_counter() - start
    
    avg_latency = (total_time / query_count) * 1000  # 转换为毫秒
    print(f"⚡ 平均查询延迟: {avg_latency:.2f}ms")
    
    latency_ok = avg_latency < 100  # 要求<100ms
    if latency_ok:
        print(f"✅ 性能测试通过: 平均延迟 {avg_latency:.2f}ms < 100ms")
    else:
        print(f"❌ 性能测试失败: 平均延迟 {avg_latency:.2f}ms > 100ms")
        all_passed = False
    
    # ========== 综合结果 ==========
    assert all_passed and latency_ok, "抗干扰测试未通过"
    print("\n🎉 抗干扰测试全部通过！")
    print(f"   召回率: 100%")
    print(f"   排序正确率: 100%")
    print(f"   平均响应时间: {avg_latency:.2f}ms")
    print(f"   噪声耐受量: {success_count} 条无关命令")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
