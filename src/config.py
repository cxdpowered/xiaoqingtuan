"""集中管理路径与运行时配置。

所有派生数据（SQLite、LanceDB、HF 缓存）都放在 ``data/`` 下，
``wiki/`` 是长期知识唯一权威源（见 AGENT_ARCHITECTURE.md 第 7 节）。
可用环境变量覆盖默认路径，便于容器内挂 volume。
"""

from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    # src/config.py -> 项目根目录
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


DATA_DIR = _path_env("XQT_DATA_DIR", PROJECT_ROOT / "data")
WIKI_DIR = _path_env("XQT_WIKI_DIR", PROJECT_ROOT / "wiki")
SQLITE_PATH = _path_env("XQT_SQLITE_PATH", DATA_DIR / "xiaoqingtuan.db")
LANCEDB_DIR = _path_env("XQT_LANCEDB_DIR", DATA_DIR / "lancedb")
HF_CACHE_DIR = _path_env("XQT_HF_CACHE_DIR", DATA_DIR / "hf_cache")

# Embedding 模型（本地 bge-small-zh，512 维）。
EMBEDDING_MODEL = os.environ.get("XQT_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DIM = int(os.environ.get("XQT_EMBEDDING_DIM", "512"))

# DeepSeek（与现有 deepseek_chat 插件保持一致的环境变量）。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")

# web_search 提供方（本期 stub）。
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# 图书馆后端选择：mock（默认）| mcp（独立 MCP 服务）| playwright（阶段三）。
LIBRARY_PROVIDER = os.environ.get("XQT_LIBRARY_PROVIDER", "mock")
LIBRARY_MCP_SERVER = os.environ.get("XQT_LIBRARY_MCP_SERVER", "library")

# MCP 客户端：指向声明外部 MCP server 列表的 JSON 配置（可选；见 src/tools/mcp_bridge.py）。
MCP_CONFIG = os.environ.get("XQT_MCP_CONFIG", "")


def ensure_dirs() -> None:
    """创建运行所需的数据目录（幂等）。"""
    for d in (DATA_DIR, LANCEDB_DIR, HF_CACHE_DIR, WIKI_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # 让 sentence-transformers / huggingface 使用项目内缓存目录。
    os.environ.setdefault("HF_HOME", str(HF_CACHE_DIR))
