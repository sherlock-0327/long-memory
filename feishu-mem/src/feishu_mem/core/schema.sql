-- Feishu-Mem SQLite数据库架构
-- 版本：v0.1.0

-- 命令记录表
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    session_id TEXT,
    raw_command TEXT NOT NULL,
    command_name TEXT NOT NULL,
    arguments TEXT, -- JSON格式的参数列表
    options TEXT, -- JSON格式的选项字典
    working_dir TEXT NOT NULL,
    project_id TEXT,
    environment TEXT,
    exit_code INTEGER,
    execution_time REAL,
    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT,
    source TEXT NOT NULL DEFAULT 'shell',
    tags TEXT, -- JSON格式的标签列表
    is_successful BOOLEAN DEFAULT 1,
    sensitivity_level TEXT DEFAULT 'public',
    content_hash TEXT NOT NULL,
    usage_count INTEGER DEFAULT 1,
    last_used_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_explicit BOOLEAN DEFAULT 0, -- 是否是用户主动教学的记忆
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 向量嵌入表
CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    embedding BLOB NOT NULL, -- 二进制存储的向量
    model_version TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (command_id) REFERENCES commands(command_id) ON DELETE CASCADE,
    UNIQUE(command_id, model_version)
);

-- 配置表
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_command_name ON commands(command_name);
CREATE INDEX IF NOT EXISTS idx_project_id ON commands(project_id);
CREATE INDEX IF NOT EXISTS idx_environment ON commands(environment);
CREATE INDEX IF NOT EXISTS idx_executed_at ON commands(executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_hash ON commands(content_hash);
CREATE INDEX IF NOT EXISTS idx_usage_count ON commands(usage_count DESC);
CREATE INDEX IF NOT EXISTS idx_last_used_at ON commands(last_used_at DESC);

-- FTS5全文搜索索引
CREATE VIRTUAL TABLE IF NOT EXISTS commands_fts USING fts5(
    raw_command, command_name, arguments, options,
    content='commands', content_rowid='rowid'
);

-- FTS触发器
CREATE TRIGGER IF NOT EXISTS commands_fts_insert AFTER INSERT ON commands BEGIN
    INSERT INTO commands_fts(rowid, raw_command, command_name, arguments, options)
    VALUES (new.rowid, new.raw_command, new.command_name, new.arguments, new.options);
END;

CREATE TRIGGER IF NOT EXISTS commands_fts_update AFTER UPDATE ON commands BEGIN
    UPDATE commands_fts 
    SET raw_command = new.raw_command, command_name = new.command_name, arguments = new.arguments, options = new.options
    WHERE rowid = old.rowid;
END;

CREATE TRIGGER IF NOT EXISTS commands_fts_delete AFTER DELETE ON commands BEGIN
    DELETE FROM commands_fts WHERE rowid = old.rowid;
END;

-- 记忆反馈表（参考claude-mem的observation_feedback）
-- 用于追踪记忆的质量信号，支持价值评估和遗忘决策
CREATE TABLE IF NOT EXISTS command_feedback (
    feedback_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,  -- used / useful / useless / corrected
    context TEXT,               -- 反馈上下文（JSON格式）
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (command_id) REFERENCES commands(command_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedback_command ON command_feedback(command_id);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON command_feedback(signal_type);
