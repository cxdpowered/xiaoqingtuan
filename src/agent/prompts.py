"""系统提示词构建。

工具清单不再硬编码，而是运行时从 :class:`~src.tools.registry.ToolRegistry`
动态渲染——这样新增内置工具、接入 MCP server 或把图书馆拆成独立服务后，
prompt 会自动跟随注册表变化，不会与实际可用工具脱节。

记忆已改为「按需」：不再每轮预取注入，改由 LLM 主动调用 ``wiki_search`` 工具，
故本模块只描述工具与原则，不再拼接记忆上下文。
"""

from __future__ import annotations

SYSTEM_ROLE = """你是「小青团」，一个面向 QQ / 微信聊天场景的个人任务型助手。
默认用简洁中文回答，先给结论。"""

SYSTEM_PRINCIPLES = """原则：
- 回答涉及用户偏好 / 个人历史 / 过往约定时，先调用 wiki_search 查证再回答，不要凭空编造；查不到就如实说没有记录。
- 需要实时信息（新闻、天气、最新数据等）时用 web_search 联网。
- 用户明确要你长期记住某偏好 / 事项时，用 note_write 记录到长期记忆。
- 提交预约等高风险操作前，系统会拦截并请用户二次确认，你只需正常发起调用即可。
- 涉及图书馆系统的验证码 / 登录失效一律转人工，不绕过、不抢座。
- 引用检索到的记忆时尽量带来源（文件路径）。"""


def _render_tools() -> str:
    """从注册表渲染当前可用工具清单（含 MCP / 远程工具）。"""
    from src.tools.registry import get_registry, high_risk

    lines: list[str] = []
    for tool in get_registry().all():
        # 高风险标记：description 已说明则不重复（兼容内置工具与 MCP 远程工具）。
        mark = "（高风险，提交前会请用户确认）" if high_risk(tool) and "高风险" not in tool.description else ""
        lines.append(f"- {tool.name}：{tool.description}{mark}")
    if not lines:
        return ""
    return "你可以调用以下工具完成任务：\n" + "\n".join(lines)


def build_system_prompt() -> str:
    parts = [SYSTEM_ROLE]
    tools = _render_tools()
    if tools:
        parts.append(tools)
    parts.append(SYSTEM_PRINCIPLES)
    return "\n\n".join(parts)
