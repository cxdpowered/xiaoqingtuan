"""Agent 对外入口：把标准化消息跑完整个流程，返回标准化回复。

职责：写 events 流水、跨轮人工确认、调用 LangGraph、触发记忆抽取写入。
所有渠道（CLI / QQ / 微信）都调 :func:`handle_message`。
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.confirmation import get_manager
from src.agent.graph import get_graph
from src.channels.base import InboundMessage, OutboundMessage
from src.storage.db import get_db
from src.tools.registry import ToolContext, get_registry

_HISTORY_TURNS = 12


def _load_history(session_id: str) -> list[dict[str, str]]:
    db = get_db()
    rows = db.recent_events(
        session_id, limit=_HISTORY_TURNS, event_types=("user_message", "assistant_message")
    )
    history = []
    for r in rows:
        role = "user" if r["event_type"] == "user_message" else "assistant"
        history.append({"role": role, "content": r["content"]})
    return history


async def _execute_confirmed(pending, ctx: ToolContext) -> dict[str, Any]:
    reg = get_registry()
    tool = reg.get(pending.tool_name)
    if tool is None:
        return {"error": f"未知工具 {pending.tool_name}"}
    try:
        args = tool.validate_args(pending.arguments)
        result = await tool.func(args, ctx)
        status = "success"
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}
        status = "failed"
    ctx.db.add_tool_call(
        event_id=ctx.event_id or "",
        tool_name=pending.tool_name,
        arguments=pending.arguments,
        status=status,
        result=json.dumps(result, ensure_ascii=False) if status == "success" else None,
        error=json.dumps(result, ensure_ascii=False) if status == "failed" else None,
    )
    return result


def _summarize_result(tool_name: str, result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"操作失败：{result['error']}"
    if tool_name == "library_reservation_create":
        detail = result.get("detail") or ""
        code = result.get("confirmation_code", "")
        return f"已提交预约 ✅ {detail}（确认号 {code}）。"
    return f"已完成操作：{json.dumps(result, ensure_ascii=False)}"


async def _extract_and_store_memory(
    session_id: str, user_text: str, reply: str, event_id: str, person_id: str
) -> None:
    """response 之后跑 LangMem 抽取 → Markdown 写入器（架构 7.5）。失败不影响主流程。

    抽取出的记忆写入该用户（``person_id``）的记忆分区，实现多用户隔离。
    """
    try:
        from src.memory.langmem_adapter import extract_memories
        from src.memory.writer import apply_candidate

        candidates = await extract_memories(
            [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
        )
        db = get_db()
        for cand in candidates:
            if float(cand.get("confidence", 0)) < 0.6:
                continue
            apply_candidate(cand, db=db, source_event_id=event_id, person_id=person_id)
    except Exception:
        pass


def _resolve_person(inbound: InboundMessage) -> str:
    """把入站消息解析为稳定的 person_id（多用户记忆分区标识）。"""
    from src.identity import store as identity_store

    account_id = inbound.account_id or inbound.user_id.removeprefix("user:") or "unknown"
    return identity_store.resolve(
        get_db(), channel=inbound.channel, account_id=account_id
    )


async def handle_message(inbound: InboundMessage) -> OutboundMessage:
    db = get_db()
    session_id = inbound.session_id
    text = inbound.text

    # 解析身份（多用户）：得到稳定 person_id，记忆按它分区。
    person_id = _resolve_person(inbound)
    account_id = inbound.account_id or inbound.user_id.removeprefix("user:") or "unknown"

    # 身份命令（绑定 / 确认 / whoami）在进入对话前确定性拦截，不经过 LLM。
    from src.identity import commands as identity_commands

    identity_reply = identity_commands.try_handle(
        db, channel=inbound.channel, account_id=account_id, person_id=person_id, text=text
    )
    if identity_reply is not None:
        db.add_event(session_id=session_id, role="user", content=text,
                     event_type="user_message", user_id=inbound.user_id,
                     metadata={"person_id": person_id})
        db.add_event(session_id=session_id, role="assistant", content=identity_reply,
                     event_type="assistant_message", metadata={"person_id": person_id})
        return OutboundMessage(reply_text=identity_reply)

    # 历史要在记录本条之前取，避免把当前消息混进上下文。
    history = _load_history(session_id)
    event_id = db.add_event(
        session_id=session_id,
        role="user",
        content=text,
        event_type="user_message",
        user_id=inbound.user_id,
        metadata={"person_id": person_id},
    )
    ctx = ToolContext(db=db, session_id=session_id, user_id=inbound.user_id,
                      event_id=event_id, person_id=person_id)
    cm = get_manager()

    # 1) 跨轮确认：是否有等待确认的高风险操作
    pending = cm.get(session_id)
    if pending:
        decision = cm.classify(text)
        if decision == "affirm":
            cm.clear(session_id)
            result = await _execute_confirmed(pending, ctx)
            reply = _summarize_result(pending.tool_name, result)
            db.add_event(session_id=session_id, role="assistant", content=reply,
                         event_type="confirmation_result", metadata={"decision": "affirm", "result": result})
            db.add_event(session_id=session_id, role="assistant", content=reply,
                         event_type="assistant_message")
            return OutboundMessage(reply_text=reply)
        if decision == "deny":
            cm.clear(session_id)
            reply = "好的，已取消该操作。"
            db.add_event(session_id=session_id, role="assistant", content=reply,
                         event_type="confirmation_result", metadata={"decision": "deny"})
            db.add_event(session_id=session_id, role="assistant", content=reply,
                         event_type="assistant_message")
            return OutboundMessage(reply_text=reply)
        # 不明确：重述确认请求
        reply = pending.prompt + "\n（请回复『确认』或『取消』）"
        return OutboundMessage(reply_text=reply, requires_confirmation=True,
                               confirmation_id=pending.confirmation_id)

    # 2) 正常流程：跑图
    state: dict[str, Any] = {
        "session_id": session_id,
        "user_id": inbound.user_id,
        "person_id": person_id,
        "text": text,
        "event_id": event_id,
        "history": history,
    }
    result_state = await get_graph().ainvoke(state)

    # 3) 高风险确认拦截
    if result_state.get("requires_confirmation") and result_state.get("pending_tool"):
        pt = result_state["pending_tool"]
        prompt = result_state.get("reply") or "需要确认该操作。"
        pc = cm.create(session_id, pt["name"], pt["args"], prompt)
        return OutboundMessage(reply_text=prompt, requires_confirmation=True,
                               confirmation_id=pc.confirmation_id)

    reply = result_state.get("reply", "（没有可回复的内容）")
    db.add_event(session_id=session_id, role="assistant", content=reply,
                 event_type="assistant_message")

    # 4) 记忆抽取写入（post-reply），落到当前用户的记忆分区
    await _extract_and_store_memory(session_id, text, reply, event_id, person_id)

    return OutboundMessage(reply_text=reply)
