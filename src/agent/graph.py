"""LangGraph 主流程（架构 4 / 5.2）。

流程：prepare → agent(LLM+工具) ⇄ tool_executor，
高风险工具被 confirm_gate 拦截 → 产出确认请求并结束本轮（跨轮恢复见 run.py）。

意图判断与工具选择完全交给 LLM 的 function calling，不再有启发式路由层；
长期记忆也不再每轮强制预取，改由 LLM 按需调用 ``wiki_search`` 工具获取。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph

from src.agent.llm import get_llm_with_tools
from src.agent.prompts import build_system_prompt
from src.agent.state import AgentState
from src.storage.db import get_db
from src.tools.registry import ToolContext, get_registry, high_risk

MAX_ITERATIONS = 4


# ---- 节点 ----------------------------------------------------------------
async def prepare_node(state: AgentState) -> dict[str, Any]:
    """装配初始消息：system（含动态工具说明）+ 历史 + 当前输入。

    记忆不在此预取——若需要用户偏好 / 历史，LLM 会自行调用 ``wiki_search``（架构 7.6）。
    """
    text = state["text"]
    messages: list[Any] = [SystemMessage(content=build_system_prompt())]
    for h in state.get("history", []):
        role = h.get("role")
        content = h.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=text))
    return {"llm_messages": messages, "iterations": 0}


async def agent_node(state: AgentState) -> dict[str, Any]:
    """调用 LLM（带工具）。"""
    llm = get_llm_with_tools()
    ai: AIMessage = await llm.ainvoke(state["llm_messages"])
    msgs = state["llm_messages"] + [ai]
    return {"llm_messages": msgs, "iterations": state.get("iterations", 0) + 1}


def _last_ai(state: AgentState) -> AIMessage | None:
    for m in reversed(state["llm_messages"]):
        if isinstance(m, AIMessage):
            return m
    return None


def route_after_agent(state: AgentState) -> str:
    ai = _last_ai(state)
    if ai is None:
        return "compose"
    tool_calls = getattr(ai, "tool_calls", None) or []
    if not tool_calls:
        return "compose"
    if state.get("iterations", 0) >= MAX_ITERATIONS:
        return "compose"
    # 有高风险工具则进确认门
    reg = get_registry()
    for tc in tool_calls:
        tool = reg.get(tc["name"])
        if tool and high_risk(tool):
            return "confirm_gate"
    return "tool_executor"


def confirm_gate_node(state: AgentState) -> dict[str, Any]:
    """拦截高风险工具，产出确认请求（架构 5.3）。"""
    ai = _last_ai(state)
    reg = get_registry()
    high = None
    for tc in (getattr(ai, "tool_calls", None) or []):
        tool = reg.get(tc["name"])
        if tool and high_risk(tool):
            high = tc
            break
    if high is None:
        return {"requires_confirmation": False}

    try:
        args = reg.validate_args(high["name"], high.get("args", {}))
    except Exception:
        args = high.get("args", {})
    prompt = _confirmation_prompt(high["name"], args)
    return {
        "pending_tool": {"name": high["name"], "args": args},
        "requires_confirmation": True,
        "reply": prompt,
    }


def _confirmation_prompt(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "library_reservation_create":
        area = args.get("area") or args.get("library") or "图书馆"
        return (
            f"即将预约：{args.get('date')} {args.get('start_time')}-{args.get('end_time')}，"
            f"{area} 座位 {args.get('seat')}。\n是否确认提交？（回复『确认』执行，『取消』放弃）"
        )
    return f"即将执行高风险操作 {tool_name}，参数：{json.dumps(args, ensure_ascii=False)}。\n回复『确认』执行，『取消』放弃。"


async def tool_executor_node(state: AgentState) -> dict[str, Any]:
    """执行（非高风险）工具调用并记录（架构 5.2/6.2）。"""
    ai = _last_ai(state)
    reg = get_registry()
    db = get_db()
    ctx = ToolContext(
        db=db,
        session_id=state["session_id"],
        user_id=state.get("user_id", ""),
        event_id=state.get("event_id"),
        person_id=state.get("person_id"),
        channel=state.get("channel"),
        account_id=state.get("account_id"),
    )
    new_messages = list(state["llm_messages"])
    for tc in (getattr(ai, "tool_calls", None) or []):
        name = tc["name"]
        raw_args = tc.get("args", {})
        tool = reg.get(name)
        if tool is None:
            result_obj: dict[str, Any] = {"error": f"未知工具 {name}"}
            status = "failed"
        else:
            try:
                result_obj = await reg.ainvoke(name, raw_args, ctx)
                status = "success"
            except Exception as exc:  # noqa: BLE001
                result_obj = {"error": str(exc)}
                status = "failed"
        result_text = json.dumps(result_obj, ensure_ascii=False)
        db.add_tool_call(
            event_id=state.get("event_id") or "",
            tool_name=name,
            arguments=raw_args,
            status=status,
            result=result_text if status == "success" else None,
            error=None if status == "success" else result_text,
        )
        new_messages.append(ToolMessage(content=result_text, tool_call_id=tc.get("id", name)))
    return {"llm_messages": new_messages}


def compose_node(state: AgentState) -> dict[str, Any]:
    """response_composer：取 LLM 最终文本。"""
    if state.get("requires_confirmation"):
        return {}  # reply 已由 confirm_gate 写好
    ai = _last_ai(state)
    text = ""
    if ai is not None:
        text = ai.content if isinstance(ai.content, str) else str(ai.content)
    return {"reply": text.strip() or "（没有可回复的内容）"}


# ---- 构图 ----------------------------------------------------------------
_graph = None


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("prepare", prepare_node)
    g.add_node("agent", agent_node)
    g.add_node("confirm_gate", confirm_gate_node)
    g.add_node("tool_executor", tool_executor_node)
    g.add_node("compose", compose_node)

    g.set_entry_point("prepare")
    g.add_edge("prepare", "agent")
    g.add_conditional_edges(
        "agent",
        route_after_agent,
        {"compose": "compose", "confirm_gate": "confirm_gate", "tool_executor": "tool_executor"},
    )
    g.add_edge("tool_executor", "agent")
    g.add_edge("confirm_gate", "compose")
    g.add_edge("compose", END)
    return g.compile()


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
