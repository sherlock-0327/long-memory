# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
cd feishu-mem
pip install -e .                    # Install in dev mode
pytest tests/ -v                    # Run all tests
pytest tests/test_basic.py::test_name -v  # Run a single test
black src/ tests/                   # Format code (line-length=120)
ruff check src/ tests/              # Lint
mypy src/                           # Type-check (strict mode)
```

## Architecture

This is **feishu-mem**, a CLI memory system that captures shell commands, provides intelligent auto-completion, discovers workflows, and manages memory lifecycle. The CLI entry point is `mem` (defined in `pyproject.toml`).

**Layer structure**: `cli/` → `core/` → `shared/`

### Core Engine (`src/feishu_mem/core/`)

- **`storage.py`** — SQLite database with FTS5 full-text search. All write paths go through WAL first, then SQLite. Uses content hashing (SHA-256) for deduplication — duplicate commands increment `usage_count` instead of creating new rows.
- **`collector.py`** — Parses raw shell commands via `shlex`, extracts context (git branch/project, env), and applies multi-layer sensitive info filtering (passwords, tokens, API keys, phones, emails). `SensitiveFilter.filter_raw_command()` runs before storage.
- **`completion.py`** — Three-layer retrieval: L1 cache → prefix match (<30ms) → semantic search via ChromaDB (<50ms) → scoring/ranking (<20ms). Scoring weights: frequency 40%, recency 30%, match precision 20%, explicit bonus 10%.
- **`vector_store.py`** — ChromaDB with `sentence-transformers` (all-MiniLM-L6-v2) for semantic search. Uses cosine similarity (`hnsw:space`).
- **`wal.py`** — Write-Ahead Log: all DB mutations append to WAL files first, marked complete via a checkpoint file after DB commit. `replay_incomplete()` recovers from crashes on startup.
- **`task_queue.py`** — SQLite-backed async task queue with CLAIM-CONFIRM pattern. Supports retries, dead-letter queue, stuck-task recovery, and worker heartbeats.
- **`forgetting_engine.py`** — Ebbinghaus curve-based memory scoring. Explicitly taught memories (`is_explicit=True`) are never auto-deleted.
- **`version_manager.py`** — Version history for memory records with conflict resolution (timestamp > confidence > user feedback > usage count).
- **`workflow.py`** — Discovers command sequences from history, executes multi-step workflows via `subprocess.run`.

### CLI (`src/feishu_mem/cli/`)

All commands in `main.py` using Click. Key commands: `search`, `teach`, `list`, `install`, `completion` (hidden), `hook` (hidden — called by shell preexec hooks), `stats`, `version`. Hidden commands must never crash — they exit 0 silently on failure.

### Shared (`src/feishu_mem/shared/`)

- **`config.py`** — `ConfigManager` singleton. Config priority: env vars (`FEISHU_MEM_*`) > `~/.feishu-mem/config.json` > dataclass defaults. Uses `platformdirs` for data/cache/log paths.
- **`logger.py`** — Singleton logger with rotating file handler (10MB × 5 backups). Console output only in debug mode.
- **`cache.py`** — LRU in-memory cache used as L1 cache for completion results (TTL: 60s for completions, 300s default).

### Worker (`worker.py`)

Background service (`mem-worker` entry point) that processes async tasks: embedding generation, pattern analysis, workflow discovery, memory cleanup, WAL cleanup. Supports daemon mode via double-fork.

## Key Design Rules

- **Graceful degradation**: All components initialize in try/except at CLI startup. If anything fails, `storage`, `vector_store`, `completion_engine`, etc. are set to `None` and downstream code handles null checks.
- **WAL-first writes**: `storage.add_command()` calls `wal.append()` before touching SQLite, then `wal.mark_complete()` after commit.
- **Hook never crashes**: The `hook` CLI command catches all exceptions and exits 0 — a failing hook must never block the user's terminal.
- **Explicit memory is permanent**: `is_explicit=True` records are never deleted by `cleanup_expired_memory()` or `ForgettingEngine`.
- **Sensitive filtering happens at collection time**, before any storage — `SensitiveFilter.filter_raw_command()` replaces matches with `***TYPE_HIDDEN***` placeholders (not redaction — the command shape is preserved for pattern matching).
