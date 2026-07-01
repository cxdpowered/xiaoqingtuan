"""远程 MCP 原始工具调用层：统一注入 ``user_key``，归一化结果。

职责（需求文档 §15.2）：

- 解析当前 ``person_id`` → ``user_key``（按 ``XQT_LIBRARY_USER_KEY_MODE``）。
- 经 :func:`src.tools.mcp_bridge.call_mcp_tool` 调远程原始工具，自动补 ``user_key``。
- 把结果收敛成统一形态 ``{"ok": bool, "code": str, "message": str, ...}``。

**不**让 LLM 决定 ``user_key``：它永远来自身份层解析出的 ``person_id``，绝不从用户文本提取。
"""

from __future__ import annotations

from typing import Any, Optional

from src import config
from src.tools.ccnu_library import repository as repo
from src.tools.registry import ToolContext


def resolve_user_key(ctx: ToolContext) -> str:
    """按配置模式把当前 person 映射为远程 MCP 的 ``user_key``。

    - ``person_id``（默认）：优先用 settings 里记录的 user_key，否则用 ``ctx.person_id``。
    - ``default``：所有人共享 MCP ``default`` 账号。
    - ``fixed``：固定为 ``XQT_LIBRARY_FIXED_USER_KEY``。
    """
    mode = getattr(config, "LIBRARY_USER_KEY_MODE", "person_id")
    if mode == "default":
        return "default"
    if mode == "fixed":
        return getattr(config, "LIBRARY_FIXED_USER_KEY", "default") or "default"

    # person_id 模式
    if ctx.person_id:
        settings = repo.get_settings(ctx.db, ctx.person_id)
        if settings and settings.get("user_key"):
            return str(settings["user_key"])
        return ctx.person_id
    return "default"


def _classify_error_message(message: str) -> str:
    lower = message.lower()
    if "permission denied" in lower or "errno 13" in lower:
        return "MCP_PERMISSION_DENIED"
    return "MCP_UNAVAILABLE"


def _coerce_result(raw: dict[str, Any]) -> dict[str, Any]:
    """把 mcp_bridge 归一化后的 dict 进一步统一为 {ok, code, message, ...}。"""
    # 传输层/isError：mcp_bridge 会塞 {"error": ...}。
    if "error" in raw and "ok" not in raw:
        err = raw["error"]
        if isinstance(err, dict):
            out = dict(err)
            out["ok"] = False
            message = str(out.get("message") or out.get("error") or err)
            out.setdefault("message", message)
            out.setdefault("code", _classify_error_message(message))
            return out
        message = str(err)
        return {"ok": False, "code": _classify_error_message(message), "message": message}
    # 远程已返回规范结构。
    out = dict(raw)
    out.setdefault("ok", True)
    if not out["ok"]:
        out.setdefault("code", "UNKNOWN")
        out.setdefault("message", "")
    return out


async def call(
    ctx: ToolContext, remote_tool: str, args: Optional[dict[str, Any]] = None,
    *, user_key: Optional[str] = None,
) -> dict[str, Any]:
    """调一个远程原始工具，自动注入 ``user_key`` 并归一化结果。

    远程 server 未接入时返回 ``ok=false, code=MCP_UNAVAILABLE``，由 wrapper 决定如何
    向用户降级提示（而不是抛栈）。
    """
    from src.tools import mcp_bridge

    payload = dict(args or {})
    payload["user_key"] = user_key or resolve_user_key(ctx)
    server = getattr(config, "LIBRARY_MCP_NAME", "ccnu_library")
    try:
        raw = await mcp_bridge.call_mcp_tool(server, remote_tool, payload)
    except KeyError as exc:
        return {"ok": False, "code": "MCP_UNAVAILABLE", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "MCP_UNAVAILABLE", "message": str(exc)}
    return _coerce_result(raw)
