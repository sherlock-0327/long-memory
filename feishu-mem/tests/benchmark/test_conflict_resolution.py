"""
矛盾更新测试：验证系统能否根据时序正确处理冲突指令
测试场景符合项目要求：
1. 初始注入记忆v1
2. 更新注入记忆v2
3. 查询返回最新版本v2
4. 历史版本完整可追溯
"""
import pytest
from datetime import datetime
from pathlib import Path
import tempfile

from feishu_mem.core.storage import Storage, CommandRecord
from feishu_mem.core.version_manager import VersionManager, get_version_manager


@pytest.fixture
def test_storage():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    storage = Storage(db_path=db_path)
    yield storage

    import gc
    gc.collect()
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


@pytest.fixture
def version_manager(test_storage):
    # 重置全局实例
    global _version_manager
    _version_manager = VersionManager(test_storage.db_path)
    return _version_manager


def test_conflict_resolution(test_storage, version_manager):
    """冲突解决测试主流程"""
    project_id = "test-project-123"
    environment = "production"
    
    # ========== 阶段1：初始记忆 v1 ==========
    v1_command = "send-weekly-report --to zhangsan@company.com --subject 周报表"
    v1_record = CommandRecord(
        command_id="",
        raw_command=v1_command,
        command_name="send-weekly-report",
        arguments=["--to", "zhangsan@company.com", "--subject", "周报表"],
        options={},
        working_dir="/home/user/project",
        project_id=project_id,
        environment=environment,
        is_explicit=True,
        is_successful=True,
        executed_at=datetime(2026, 4, 15),
        last_used_at=datetime(2026, 4, 15)
    )
    
    v1_id = test_storage.add_command(v1_record)
    assert v1_id is not None
    
    # 创建版本记录
    v1_version_id = version_manager.create_version(v1_record, change_reason="initial setup", created_by="user")
    assert v1_version_id != ""
    print(f"✅ 初始记忆v1创建成功: 接收人=张三")
    print(f"   命令: {v1_command}")
    print(f"   版本ID: {v1_version_id}")
    
    # ========== 阶段2：更新记忆 v2（冲突版本） ==========
    v2_command = "send-weekly-report --to lisi@company.com --subject 周报表"
    v2_record = CommandRecord(
        command_id="",
        raw_command=v2_command,
        command_name="send-weekly-report",
        arguments=["--to", "lisi@company.com", "--subject", "周报表"],
        options={},
        working_dir="/home/user/project",
        project_id=project_id,
        environment=environment,
        is_explicit=True,
        is_successful=True,
        executed_at=datetime(2026, 4, 22),  # 比v1晚7天
        last_used_at=datetime(2026, 4, 22)
    )
    
    # 模拟冲突解决流程
    # 先查找是否有相同上下文的记忆
    existing_records = test_storage.get_prefix_matches(
        "send-weekly-report",
        project_id=project_id,
        environment=environment
    )
    
    assert len(existing_records) == 1
    existing_record = existing_records[0]
    
    # 执行冲突解决
    winner, reason = version_manager.resolve_conflict(existing_record, v2_record)
    
    # 验证v2胜出
    assert winner.raw_command == v2_command
    assert "时间戳更新" in reason
    print(f"\n✅ 冲突解决完成，v2胜出")
    print(f"   原因: {reason}")
    print(f"   新命令: {v2_command}")
    
    # 更新存储
    v2_id = test_storage.add_command(v2_record)
    assert v2_id is not None
    v2_version_id = version_manager.create_version(v2_record, change_reason="update receiver", created_by="user")
    assert v2_version_id != ""
    
    # ========== 阶段3：查询最新版本 ==========
    current_records = test_storage.get_prefix_matches(
        "send-weekly-report",
        project_id=project_id,
        environment=environment
    )
    
    # 验证返回最新版本（李四）
    assert len(current_records) > 0
    latest = current_records[0]
    assert "lisi@company.com" in latest.raw_command
    assert "zhangsan@company.com" not in latest.raw_command
    print("\n✅ 最新版本查询正确，返回李四")
    
    # ========== 阶段4：验证版本历史完整 ==========
    # 查询v1的历史版本
    v1_history = version_manager.get_version_history(v1_id)
    v2_history = version_manager.get_version_history(v2_id)
    
    assert len(v1_history) >= 1
    assert len(v2_history) >= 1
    
    # 合并查看所有相关版本
    all_versions = v1_history + v2_history
    all_versions.sort(key=lambda x: x.created_at)
    
    assert len(all_versions) == 2
    assert "zhangsan@company.com" in all_versions[0].content["raw_command"]
    assert "lisi@company.com" in all_versions[1].content["raw_command"]
    assert all_versions[0].created_at < all_versions[1].created_at
    
    print("\n✅ 版本历史完整")
    for i, ver in enumerate(all_versions, 1):
        cmd = ver.content["raw_command"]
        receiver = cmd.split("--to ")[1].split(" ")[0]
        print(f"   v{i}: {receiver} | {ver.created_at.strftime('%Y-%m-%d')} | {ver.change_reason}")
    
    # ========== 阶段5：测试版本回滚 ==========
    rollback_success = version_manager.rollback_to_version(v2_id, v1_version_id)
    assert rollback_success
    
    # 验证回滚后返回张三
    rolled_back = test_storage.get_commands_by_ids([v2_id])[0]
    assert "zhangsan@company.com" in rolled_back.raw_command
    
    # 验证回滚产生新版本
    new_history = version_manager.get_version_history(v2_id)
    assert len(new_history) >= 2
    assert any("rollback to version" in v.change_reason for v in new_history)
    
    print("\n✅ 版本回滚功能正常")
    print(f"   回滚后命令: {rolled_back.raw_command}")
    print(f"   新版本历史长度: {len(new_history)}")
    
    # ========== 综合验证 ==========
    print("\n🎉 矛盾更新测试全部通过！")
    print("   ✓ 时序优先冲突解决策略生效")
    print("   ✓ 最新版本返回正确")
    print("   ✓ 历史版本完整可追溯")
    print("   ✓ 版本回滚功能正常")
    print("   ✓ 修改记录可审计")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
