"""MCP 客户端桥接。

把外部 **MCP server** 暴露的 tools 适配成 LangChain ``StructuredTool``，
注册进 :class:`~src.tools.registry.ToolRegistry` 后，LangGraph 主循环（``agent`` →
``tool_executor``）即可像调用内置工具一样调用它们，**Agent 代码零改动**。

设计要点
--------
- 远程工具没有 pydantic 模型：直接用 MCP server 给的 ``inputSchema`` 作为
  ``StructuredTool.args_schema``（function-calling 参数 schema），参数校验交给 server 端。
- 高风险标记由本地配置决定（``high_risk_tools`` 白名单），保证 confirm_gate 仍能拦截
  ——不能信任远程 server 自报风险等级；注册后远程工具仍走 confirm_gate。
- ``mcp`` 不是核心依赖：本模块全部 **惰性导入**，未安装时给出清晰提示。
  安装：``uv sync --extra mcp``（见 pyproject 可选依赖）。

接入方式
--------
1. 写一份 JSON 配置（见 README「接入 MCP 服务」），用 ``XQT_MCP_CONFIG`` 指向它。
2. 进程启动时调用 :func:`init_mcp_from_config`（bot.py / CLI 已接好），
   把远程工具并入注册表；退出时调用 :func:`shutdown_mcp` 关闭连接。
3. 之后与内置工具完全一致：被 LLM 选择 → confirm_gate → tool_executor 执行。

会话生命周期
------------
每个 server 的连接（stdio 子进程 / sse / streamable-http）在首次使用时建立并**保活**，
由进程级 :class:`~contextlib.AsyncExitStack` 持有，:func:`shutdown_mcp` 统一关闭。
注意：MCP 客户端基于 anyio 任务作用域，连接的建立与关闭应在同一事件循环内完成
（本项目 LangGraph 全程跑在同一 loop，满足该约束）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import BaseTool

from src.tools.registry import ToolContext, ToolRegistry, context_tool


@dataclass
class MCPServerConfig:
    """一个 MCP server 的连接配置。

    transport:
        - ``stdio``：本地子进程，用 ``command`` + ``args`` 启动。
        - ``sse``：远程 Server-Sent Events 服务，用 ``url``（可带 ``headers`` 鉴权）。
        - ``http`` / ``streamable-http``：远程 Streamable HTTP 服务，用 ``url`` + ``headers``。
    """

    name: str
    transport: str = "stdio"  # stdio | sse | http | streamable-http
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    url: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    # 哪些工具视为高风险（必须经 confirm_gate 二次确认）。
    high_risk_tools: set[str] = field(default_factory=set)
    # 工具名过滤；为空表示全部导入。导入后名字加前缀避免与内置冲突（见 tool_prefix）。
    include: Optional[set[str]] = None
    exclude: set[str] = field(default_factory=set)
    tool_prefix: str = ""  # 例如 "lib_"，最终工具名 = prefix + 远程名


# ---- 工具适配 ------------------------------------------------------------
def _make_tool(config: MCPServerConfig, *, remote_name: str, description: str,
               input_schema: dict[str, Any]) -> BaseTool:
    """把一个远程 MCP 工具描述适配成 LangChain 工具。"""
    local_name = f"{config.tool_prefix}{remote_name}"

    async def _call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # 每次调用复用到该 server 的会话，转发调用并归一化结果。
        session = await _get_session(config)
        result = await session.call_tool(remote_name, args)
        return _normalize_result(result)

    return context_tool(
        name=local_name,
        description=description,
        func=_call,
        args_schema=input_schema or {"type": "object", "properties": {}},
        high_risk=remote_name in config.high_risk_tools,
        source=f"mcp:{config.name}",
    )


def _normalize_result(result: Any) -> dict[str, Any]:
    """把 MCP ``CallToolResult`` 收敛成本项目工具统一的 dict 结果。

    优先用结构化输出（``structuredContent``）；否则解析 content blocks，
    单个 JSON 文本会被解析为 dict，其余按文本拼接。``isError`` 映射为 ``error``。
    """
    if isinstance(result, dict):
        return result

    is_error = bool(getattr(result, "isError", False))

    structured = getattr(result, "structuredContent", None)
    if structured:
        if isinstance(structured, dict):
            return {"error": structured} if is_error else structured
        return {"error" if is_error else "result": structured}

    content = getattr(result, "content", None)
    if content is not None:
        texts: list[str] = []
        for c in content:
            text = getattr(c, "text", None)
            texts.append(text if text is not None else str(c))
        if len(texts) == 1:
            try:
                parsed = json.loads(texts[0])
                if isinstance(parsed, dict):
                    return {"error": parsed} if is_error else parsed
            except (ValueError, TypeError):
                pass
        joined = "\n".join(texts)
        return {"error": joined} if is_error else {"content": joined}

    return {"result": str(result)}


# ---- 会话管理（连接保活 + 统一关闭） --------------------------------------
_sessions: dict[str, Any] = {}
_exit_stack: Optional[AsyncExitStack] = None
_lock = asyncio.Lock()


def _ensure_mcp_installed() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "未安装 MCP 客户端依赖。请先 `uv sync --extra mcp` 再启用 MCP 工具。"
        ) from exc


async def _open_streams(config: MCPServerConfig, stack: AsyncExitStack):
    """按 transport 建立底层读写流，并登记到 ``stack`` 以便统一关闭。"""
    transport = (config.transport or "stdio").lower()

    if transport == "stdio":
        import os

        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not config.command:
            raise ValueError(f"MCP server {config.name!r} 的 stdio 传输缺少 command。")
        env = {**os.environ, **config.env} if config.env else None
        params = StdioServerParameters(command=config.command, args=config.args, env=env)
        read, write = await stack.enter_async_context(stdio_client(params))
        return read, write

    if transport == "sse":
        from mcp.client.sse import sse_client

        if not config.url:
            raise ValueError(f"MCP server {config.name!r} 的 sse 传输缺少 url。")
        read, write = await stack.enter_async_context(
            sse_client(config.url, headers=config.headers or None)
        )
        return read, write

    if transport in ("http", "streamable-http", "streamable_http"):
        from mcp.client.streamable_http import streamablehttp_client

        if not config.url:
            raise ValueError(f"MCP server {config.name!r} 的 http 传输缺少 url。")
        # streamable-http 返回 (read, write, get_session_id)，第三个回调本项目用不到。
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(config.url, headers=config.headers or None)
        )
        return read, write

    raise ValueError(f"未知 MCP transport: {config.transport!r}")


async def _get_session(config: MCPServerConfig) -> Any:
    """建立/复用到某 MCP server 的客户端会话（线程内串行，进程级保活）。"""
    global _exit_stack
    async with _lock:
        existing = _sessions.get(config.name)
        if existing is not None:
            return existing

        _ensure_mcp_installed()
        from mcp import ClientSession

        if _exit_stack is None:
            _exit_stack = AsyncExitStack()
        read, write = await _open_streams(config, _exit_stack)
        session = await _exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        _sessions[config.name] = session
        return session


async def call_mcp_tool(
    server_name: str, tool_name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """对外公共入口：调用某个**已接入** MCP server 的原始工具并归一化结果。

    供复杂功能组（如 ``src.tools.ccnu_library``）的本地 wrapper 使用，避免它们去碰
    私有的 :func:`_get_session`。要求该 server 此前已通过 :func:`init_mcp_from_config`
    建立会话（哪怕用 ``include: []`` 不向 LLM 暴露任何工具，会话仍会建立）。

    若 server 尚未接入则抛 ``KeyError``，由调用方决定如何降级提示用户。
    """
    session = _sessions.get(server_name)
    if session is None:
        raise KeyError(
            f"MCP server {server_name!r} 未接入。请确认 XQT_MCP_CONFIG 已声明并在启动时初始化。"
        )
    result = await session.call_tool(tool_name, args)
    return _normalize_result(result)


async def shutdown_mcp() -> None:
    """关闭所有 MCP 连接（进程退出时调用）。幂等。"""
    global _exit_stack
    async with _lock:
        _sessions.clear()
        if _exit_stack is not None:
            await _exit_stack.aclose()
            _exit_stack = None


# ---- 列举 / 注册 ---------------------------------------------------------
async def load_mcp_tools(config: MCPServerConfig) -> list[BaseTool]:
    """连接一个 MCP server，列出其 tools 并适配为 LangChain 工具列表。"""
    session = await _get_session(config)
    listed = await session.list_tools()
    tools: list[BaseTool] = []
    for t in getattr(listed, "tools", listed):
        remote_name = t.name
        if config.include is not None and remote_name not in config.include:
            continue
        if remote_name in config.exclude:
            continue
        tools.append(
            _make_tool(
                config,
                remote_name=remote_name,
                description=getattr(t, "description", None) or remote_name,
                input_schema=getattr(t, "inputSchema", None) or {},
            )
        )
    return tools


async def register_mcp_servers(
    configs: list[MCPServerConfig], registry: ToolRegistry
) -> list[str]:
    """把多个 MCP server 的工具并入注册表，返回成功注册的工具名列表。

    单个 server 连接/列举失败不影响其余 server（记录后跳过）。
    """
    registered: list[str] = []
    for cfg in configs:
        try:
            tools = await load_mcp_tools(cfg)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning(
                "接入 MCP server %r 失败，已跳过：%s", cfg.name, exc
            )
            continue
        for tool in tools:
            registry.register(tool)
            registered.append(tool.name)
    return registered


# ---- 配置加载（XQT_MCP_CONFIG → MCPServerConfig 列表） --------------------
def _config_from_dict(d: dict[str, Any]) -> MCPServerConfig:
    return MCPServerConfig(
        name=d["name"],
        transport=d.get("transport", "stdio"),
        command=d.get("command"),
        args=list(d.get("args", [])),
        url=d.get("url"),
        headers=dict(d.get("headers", {})),
        env=dict(d.get("env", {})),
        high_risk_tools=set(d.get("high_risk_tools", [])),
        include=set(d["include"]) if d.get("include") is not None else None,
        exclude=set(d.get("exclude", [])),
        tool_prefix=d.get("tool_prefix", ""),
    )


def load_mcp_configs(path: str) -> list[MCPServerConfig]:
    """从 JSON 文件解析 MCP server 列表。

    支持两种写法（二选一）：

    1. 类 Claude Desktop 的对象映射（key 即 server 名）::

        {"mcpServers": {"library": {"transport": "stdio", "command": "..."}}}

    2. 显式数组::

        {"servers": [{"name": "library", "transport": "stdio", "command": "..."}]}
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    servers = data.get("mcpServers")
    if servers is None:
        servers = data.get("servers", data)

    if isinstance(servers, dict):
        items: list[dict[str, Any]] = []
        for name, cfg in servers.items():
            cfg = dict(cfg)
            cfg.setdefault("name", name)
            items.append(cfg)
        servers = items

    return [_config_from_dict(c) for c in servers]


async def init_mcp_from_config(registry: Optional[ToolRegistry] = None) -> list[str]:
    """读取 ``XQT_MCP_CONFIG`` 指向的配置并把 MCP 工具并入注册表。

    未配置则直接返回空列表（不报错、不依赖 mcp 包）。注册后会让已绑定工具的
    LLM 缓存失效，使新工具在下一次对话即可用。
    """
    from src import config as app_config

    path = app_config.MCP_CONFIG
    if not path:
        return []

    if registry is None:
        from src.tools.registry import get_registry

        registry = get_registry()

    configs = load_mcp_configs(path)
    if not configs:
        return []

    _ensure_mcp_installed()
    registered = await register_mcp_servers(configs, registry)
    if registered:
        from src.agent import llm

        llm.reset_tools_cache()
    return registered
