"""LangChain 工具注册表。

本地 function 统一用 :class:`langchain_core.tools.StructuredTool` 表达，让
``bind_tools``、Pydantic 入参校验、OpenAI/DeepSeek schema 转换都交给 LangChain。
本模块只保留项目需要的薄封装：ToolContext 注入、高风险标记、MCP 动态工具注册。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Awaitable, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
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
    channel: Optional[str] = None  # qq | wechat | cli，供功能组做渠道相关编排/主动通知
    account_id: Optional[str] = None  # 平台原始账号号（主动通知投递目标）


ContextToolFunc = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

_CONFIG_CONTEXT_KEY = "tool_context"
_META_HIGH_RISK = "high_risk"
_META_SOURCE = "source"


def context_tool(
    *,
    name: str,
    description: str,
    args_schema: type[BaseModel] | dict[str, Any],
    func: ContextToolFunc,
    high_risk: bool = False,
    source: str = "builtin",
) -> StructuredTool:
    """创建带项目上下文的 LangChain StructuredTool。

    工具业务函数仍接收 ``(args, ctx)``，但模型绑定、参数 schema 和运行时调用都走
    LangChain 原生工具对象。执行时调用方需把 ``ToolContext`` 放进
    ``configurable.tool_context``。
    """

    async def _run(config: RunnableConfig, **kwargs: Any) -> dict[str, Any]:
        ctx = (config or {}).get("configurable", {}).get(_CONFIG_CONTEXT_KEY)
        if not isinstance(ctx, ToolContext):
            raise RuntimeError("工具执行缺少 ToolContext。")
        return await func(dict(kwargs), ctx)

    return StructuredTool.from_function(
        coroutine=_run,
        name=name,
        description=description,
        args_schema=args_schema,
        metadata={_META_HIGH_RISK: high_risk, _META_SOURCE: source},
    )


def high_risk(tool: BaseTool) -> bool:
    return bool((tool.metadata or {}).get(_META_HIGH_RISK, False))


def source(tool: BaseTool) -> str:
    return str((tool.metadata or {}).get(_META_SOURCE, "builtin"))


def _validate_args(tool: BaseTool, raw: dict[str, Any]) -> dict[str, Any]:
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(raw or {}).model_dump()
    # MCP / 远程工具可能只有 JSON Schema dict，本地不重复实现 JSON Schema 校验。
    return dict(raw or {})


def tool_config(ctx: ToolContext) -> RunnableConfig:
    """把项目上下文装进 LangChain RunnableConfig。"""
    return {"configurable": {_CONFIG_CONTEXT_KEY: ctx}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def bindable(self) -> list[BaseTool]:
        """返回可直接传给 ``llm.bind_tools(...)`` 的 LangChain 工具对象。"""
        return self.all()

    def validate_args(self, name: str, raw: dict[str, Any]) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            return dict(raw or {})
        return _validate_args(tool, raw)

    async def ainvoke(
        self, name: str, raw: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            return {"error": f"未知工具 {name}"}
        result = await tool.ainvoke(self.validate_args(name, raw), config=tool_config(ctx))
        if isinstance(result, dict):
            return result
        return {"result": result}


_registry: Optional[ToolRegistry] = None


# 内置 function 按领域拆模块，每个模块暴露 ``TOOLS``。
# 后续新增本地工具：新建/复用领域模块，并把模块路径加入这里。
_BUILTIN_TOOL_MODULES: tuple[str, ...] = (
    "src.tools.system",  # 当前时间等低风险系统信息
    "src.tools.wiki_search",  # 长期记忆 / Wiki 检索
    "src.tools.note_write",  # 长期记忆写入
    "src.tools.web_search",  # 联网搜索
    "src.tools.library_seat",  # 图书馆座位查询 / 预约（mock provider，本地直连）
    "src.tools.reservation_history",  # 历史预约查询
    "src.tools.ccnu_library.tools",  # CCNU 图书馆 MCP 功能组（user_key 注入 + 计划/调度/challenge）
)


def get_registry() -> ToolRegistry:
    """进程级单例注册表，首次访问时加载内置工具。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _load_builtin_tools(_registry)
    return _registry


def register(tool: BaseTool) -> BaseTool:
    return get_registry().register(tool)


def _load_builtin_tools(reg: ToolRegistry) -> None:
    # 延迟导入，避免循环依赖；注册顺序即 prompt 工具清单顺序。
    for module_path in _BUILTIN_TOOL_MODULES:
        mod = import_module(module_path)
        for tool in getattr(mod, "TOOLS", ()):
            reg.register(tool)
