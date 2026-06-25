"""Agent 图状态（架构 5.2）。"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # 输入
    session_id: str
    user_id: str
    person_id: str  # 多用户记忆分区标识（见 src.identity）
    channel: str  # qq | wechat | cli
    account_id: str  # 平台原始账号号
    text: str
    event_id: str  # 本轮 user_message 的 event id

    # 上下文
    history: list[dict[str, str]]  # 最近若干轮 {role, content}

    # LLM 工具调用循环
    llm_messages: list[Any]  # langchain message 对象
    iterations: int

    # 确认
    pending_tool: Optional[dict[str, Any]]  # 高风险工具待确认 {name, args}
    requires_confirmation: bool
    confirmation_id: Optional[str]

    # 输出
    reply: str
