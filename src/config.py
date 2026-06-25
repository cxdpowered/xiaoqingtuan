"""集中管理路径与运行时配置。

所有派生数据（SQLite、LanceDB、HF 缓存）都放在 ``data/`` 下，
``wiki/`` 是长期知识唯一权威源（见 README「架构概览」）。
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

# ---- CCNU 图书馆功能组（ccnu_library）---------------------------------------
# 远程 MCP server 名称（须与 XQT_MCP_CONFIG 里声明的一致）。
LIBRARY_MCP_NAME = os.environ.get("XQT_LIBRARY_MCP_NAME", "ccnu_library")
# user_key 模式：person_id（默认，多用户隔离）| default（共享 MCP default 账号）| fixed。
LIBRARY_USER_KEY_MODE = os.environ.get("XQT_LIBRARY_USER_KEY_MODE", "person_id")
# fixed 模式下使用的固定 user_key。
LIBRARY_FIXED_USER_KEY = os.environ.get("XQT_LIBRARY_FIXED_USER_KEY", "default")
# MCP 未返回签到截止时间时，本地推算用的宽限分钟数。
LIBRARY_SIGNIN_GRACE_MINUTES = int(os.environ.get("XQT_LIBRARY_DEFAULT_SIGNIN_GRACE_MINUTES", "20"))
# 验证码图片落盘目录（不长期把 base64 写进 SQLite）。
LIBRARY_CHALLENGE_DIR = _path_env("XQT_LIBRARY_CHALLENGE_DIR", DATA_DIR / "challenges")
# 调度器扫描间隔（秒）。
LIBRARY_SCAN_INTERVAL_SECONDS = int(os.environ.get("XQT_LIBRARY_SCAN_INTERVAL_SECONDS", "30"))


# ---- QQ 掉线邮件告警（health/qq_offline_alert）------------------------------
# 带外通道：QQ（OneBot v11）掉线超过阈值时用 SMTP 发邮件提醒。掉线时 QQ 本身发不出
# 消息，故必须走邮件。仅当配置了发件账号与授权码时才启用。
ALERT_SMTP_HOST = os.environ.get("XQT_ALERT_SMTP_HOST", "smtp.qq.com")
ALERT_SMTP_PORT = int(os.environ.get("XQT_ALERT_SMTP_PORT", "465"))
ALERT_SMTP_USER = os.environ.get("XQT_ALERT_SMTP_USER", "")  # 发件 QQ 邮箱地址
ALERT_SMTP_PASS = os.environ.get("XQT_ALERT_SMTP_PASS", "")  # QQ 邮箱「授权码」（非登录密码）
ALERT_MAIL_TO = os.environ.get("XQT_ALERT_MAIL_TO", "") or ALERT_SMTP_USER  # 收件人，默认发回自己
# 连续掉线多久（秒）后才发告警，避免重启/扫码/网络抖动误报。
ALERT_OFFLINE_THRESHOLD_SECONDS = int(os.environ.get("XQT_ALERT_OFFLINE_THRESHOLD_SECONDS", "600"))
# 检查间隔（秒）。
ALERT_CHECK_INTERVAL_SECONDS = int(os.environ.get("XQT_ALERT_CHECK_INTERVAL_SECONDS", "60"))


def alert_enabled() -> bool:
    """配齐发件账号 + 授权码 + 收件人才启用 QQ 掉线邮件告警。"""
    return bool(ALERT_SMTP_USER and ALERT_SMTP_PASS and ALERT_MAIL_TO)


def ensure_dirs() -> None:
    """创建运行所需的数据目录（幂等）。"""
    for d in (DATA_DIR, LANCEDB_DIR, HF_CACHE_DIR, WIKI_DIR, LIBRARY_CHALLENGE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # 让 sentence-transformers / huggingface 使用项目内缓存目录。
    os.environ.setdefault("HF_HOME", str(HF_CACHE_DIR))
