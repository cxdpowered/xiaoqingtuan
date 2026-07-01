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

MAX_ITERATIONS = 6


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
    prompt = _confirmation_prompt(high["name"], args, state.get("person_id"))
    return {
        "pending_tool": {"name": high["name"], "args": args},
        "requires_confirmation": True,
        "reply": prompt,
    }


def _confirmation_prompt(tool_name: str, args: dict[str, Any],
                         person_id: str | None = None) -> str:
    if tool_name == "library_reserve":
        return _reserve_confirmation_prompt(args)
    if tool_name == "library_create_booking_plan":
        return _plan_confirmation_prompt(args)
    if tool_name == "library_cancel_booking_plan":
        return _cancel_plan_confirmation_prompt(args, person_id)
    return f"即将执行高风险操作 {tool_name}，参数：{json.dumps(args, ensure_ascii=False)}。\n回复『确认』执行，『取消』放弃。"


def _cancel_plan_confirmation_prompt(args: dict[str, Any], person_id: str | None) -> str:
    """取消连续预约的确认话术：把要拆掉的计划说清楚（未开始的座位也会一并撤销）。"""
    from src.storage.db import get_db
    from src.tools.ccnu_library import repository as repo

    db = get_db()
    if args.get("plan_id"):
        p = repo.get_plan(db, args["plan_id"])
        plans = [p] if p else []
    else:
        plans = [p for p in (repo.list_plans(db, person_id) if person_id else [])
                 if p["status"] in ("active", "paused")]
    if not plans:
        return "没找到进行中的连续预约计划。回复『取消』结束。"
    if len(plans) > 1 and not args.get("all_plans"):
        listing = "；".join(
            f"{(p.get('title') or '计划')} {p['date_start']}~{p['date_end']}" for p in plans)
        return (f"你有 {len(plans)} 个进行中的计划：{listing}。\n"
                "确认后我会让你选具体取消哪个。回复『确认』继续，『取消』放弃。")
    lines = ["即将取消以下连续预约计划，并撤掉其待抢任务与尚未开始的座位预约："]
    for p in plans:
        lines.append(f"· {(p.get('title') or '计划')} {p['date_start']}~{p['date_end']}")
    lines.append("是否确认？（回复『确认』执行，『取消』放弃）")
    return "\n".join(lines)


def _plan_confirmation_prompt(args: dict[str, Any]) -> str:
    """多天预约计划的确认话术：一次覆盖整段日期，确认后不再逐天打扰。"""
    span = f"{args.get('date_start')}~{args.get('date_end')}"
    wds = args.get("weekdays")
    if not wds:
        wd_txt = "每天"
    elif set(wds) == {1, 2, 3, 4, 5}:
        wd_txt = "工作日"
    else:
        names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
        wd_txt = "周" + "、".join(names.get(w, str(w)) for w in wds)
    slots = args.get("time_slots") or []
    slot_txt = "、".join(str(s) for s in slots) if slots else "默认时段"
    pref = args.get("preferred_locations")
    where = ""
    if pref:
        where = f"，区域 {pref[0] if isinstance(pref, list) else pref}"
    return (f"即将创建预约计划：{span}，{wd_txt}，每天 {slot_txt}{where}。\n"
            f"生效后每天按放票时间自动抢座，遇下午闭馆自动改约上午、整天闭馆自动跳过。\n"
            f"是否确认？（回复『确认』执行，『取消』放弃）")


def _reserve_confirmation_prompt(args: dict[str, Any]) -> str:
    """预约确认话术：把缺省的日期/时段补齐后展示，区域优先展示自然语言描述。"""
    from src.tools.ccnu_library import service

    base = service.default_booking_window()
    given_all = bool(args.get("date") and args.get("start_time") and args.get("end_time"))
    date = args.get("date") or base["date"]
    start = args.get("start_time") or base["start_time"]
    end = args.get("end_time") or base["end_time"]
    span = f"{date} {start}-{end}"

    if args.get("seat_id"):
        where = f"指定座位 {args['seat_id']}"
    elif args.get("location_query"):
        where = f"{args['location_query']}（自动选可用座位）"
    elif args.get("location_id"):
        where = f"区域 {args['location_id']}（自动选可用座位）"
    else:
        where = "你的默认区域"
    assumed = "" if given_all else "（时间为默认，可改）"
    return (f"即将预约：{span}{assumed}，{where}。\n"
            f"是否确认提交？（回复『确认』执行，『取消』放弃）")


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
