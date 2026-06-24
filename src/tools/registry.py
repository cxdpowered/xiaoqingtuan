"""工具注册表。

每个工具带:
- ``name`` / ``description``
- ``args_model``：pydantic 模型，用于校验参数 + 生成 JSON Schema
- ``func``：``async def (args, ctx) -> dict`` 执行体
- ``high_risk``：高风险标记，由 agent 的 confirm_gate 拦截（架构 5.3）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel

from src.storage.db import Database


@dataclass
class ToolContext:
    """工具执行上下文。"""

    db: Database
    session_id: str
    user_id: str
    event_id: Optional[str] = None
    person_id: Optional[str] = None  # 多用户记忆分区标识（见 src.identity）


ToolFunc = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    name: str
    description: str
    func: ToolFunc
    # 本地工具用 pydantic 模型校验参数 + 出 schema；
    # 远程（MCP）工具没有 pydantic 模型，改用对方给的原始 JSON Schema（``raw_schema``）。
    args_model: Optional[type[BaseModel]] = None
    raw_schema: Optional[dict[str, Any]] = None
    high_risk: bool = False
    # 工具来源：``builtin`` 本地内置；``mcp:<server>`` 来自某个 MCP server（便于排查/路由）。
    source: str = "builtin"

    def validate_args(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.args_model is not None:
            return self.args_model.model_validate(raw or {}).model_dump()
        # 远程工具：本地不强校验，交由 MCP server 端校验。
        return dict(raw or {})

    def parameters_schema(self) -> dict[str, Any]:
        if self.raw_schema is not None:
            return self.raw_schema
        if self.args_model is not None:
            schema = self.args_model.model_json_schema()
            schema.pop("title", None)
            return schema
        return {"type": "object", "properties": {}}

    def openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI / DeepSeek function-calling 工具格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """进程级单例注册表，首次访问时加载内置工具。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _load_builtin_tools(_registry)
    return _registry


def register(tool: Tool) -> Tool:
    return get_registry().register(tool)


def _load_builtin_tools(reg: ToolRegistry) -> None:
    # 延迟导入，避免循环依赖。
    from src.tools import library_seat, note_write, reservation_history, web_search, wiki_search

    for mod in (wiki_search, note_write, web_search, library_seat, reservation_history):
        for tool in mod.TOOLS:
            reg.register(tool)
