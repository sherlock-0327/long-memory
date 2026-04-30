# Feishu-Mem: 企业级长程协作Memory系统

企业级CLI高频命令与工作流记忆工具，帮助开发者减少重复输入，降低错误率，提升终端操作效率。

## ✨ 核心特性

### 🚀 智能补全
- **三层检索架构**：前缀匹配（<30ms）→ 语义搜索（<50ms）→ 智能排序（<20ms），总响应<100ms
- **上下文感知**：自动识别项目、环境、分支，提供场景化推荐
- **语义理解**：支持自然语言搜索相关命令，无需精确匹配
- **工作流推荐**：自动预测下一个常用命令，一键执行复杂序列

### 🔒 安全可靠
- **多层敏感信息过滤**：自动脱敏密码、Token、密钥、手机号、邮箱等8种敏感数据
- **本地数据存储**：所有数据保存在本地，完全可控，无数据泄露风险
- **写前日志（WAL）**：崩溃自动恢复，保证数据一致性
- **优雅降级**：所有组件故障时不影响用户正常终端使用

### 📊 知识沉淀
- **双模式记忆采集**：主动教学 + 隐式自动学习
- **工作流自动发现**：从历史命令中识别常用操作序列，一键执行
- **团队共享**：支持工作流发布到团队库，统一操作规范
- **记忆生命周期管理**：自动清理低价值记忆，保留重要命令

## 🛠️ 快速开始

### 安装
```bash
# 安装依赖
pip install -e .

# 安装Shell钩子
mem install
```

安装完成后重启终端或执行 `source ~/.zshrc`（或对应Shell配置文件）生效。

### 基础使用

#### 1. 主动记忆命令
```bash
# 记忆常用命令
mem teach "git push origin main --force-with-lease" "强制推送当前分支" --tags git,常用

# 后续输入git push前缀时会自动推荐
```

#### 2. 搜索历史命令
```bash
# 搜索包含"部署"的命令
mem search 部署

# 指定项目搜索
mem search build --project feishu-mem

# 指定环境搜索
mem search deploy --env prod
```

#### 3. 查看最近命令
```bash
# 显示最近20条命令
mem list --limit 20
```

#### 4. 工作流管理
```bash
# 从历史命令中发现工作流
mem workflow discover

# 执行工作流
mem workflow run <workflow_id>

# 列出所有可用工作流
mem workflow list
```

#### 5. 系统状态
```bash
# 查看版本
mem version

# 查看统计信息
mem stats
```

## 🏗️ 技术架构

### 六层架构设计
```
终端交互层 → 核心引擎层 → 飞书适配层 → 服务层 → 存储层
```

### 核心技术栈
- **存储引擎**：SQLite（结构化数据） + ChromaDB（向量嵌入）
- **检索架构**：前缀匹配 + 语义检索 + 智能排序
- **异步队列**：CLAIM-CONFIRM模式，支持重试、死信队列
- **可靠性**：写前日志（WAL）+ ACID事务 + 自动故障恢复

## 🔧 配置

### 环境变量
- `FEISHU_MEM_DEBUG`: 开启调试模式 (true/false)
- `FEISHU_MEM_LOG_LEVEL`: 日志级别 (DEBUG/INFO/WARNING/ERROR)
- `FEISHU_MEM_DB_PATH`: 自定义数据库路径
- `FEISHU_MEM_DATA_DIR`: 自定义数据目录

### 配置文件
创建 `~/.feishu-mem/config.json` 来自定义配置：
```json
{
  "completion_limit": 5,
  "min_prefix_length": 2,
  "vector_search_enabled": true,
  "embedding_model_name": "all-MiniLM-L6-v2"
}
```

## 📈 性能指标
- 补全响应时间：<100ms 99分位
- 命令采集性能：<10ms，异步执行不阻塞用户
- 存储容量：支持百万级命令记录
- 缓存命中率：>90%

## 🔐 安全说明
- 所有敏感信息在存储前自动脱敏，永不保存明文密码、Token等数据
- 所有数据存储在本地设备，无需联网即可使用
- 支持显式标记敏感命令，特殊保护

## 🤝 贡献
欢迎提交Issue和PR！

## 📄 许可证
MIT