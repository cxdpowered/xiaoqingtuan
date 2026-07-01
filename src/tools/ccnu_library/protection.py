"""爽约 / 暂离保护的跨轮回复拦截。

调度器在临近签到/暂离截止时会私信问用户「还去吗」，并在 SQLite 落一条
``library_protection_prompts``（status=awaiting）。用户下一条消息进入 LLM **之前**，
run.py 调 :func:`intercept`：
- 明确「保留」（去/回来/别取消）→ 标 kept，不再自动取消。
- 明确「放弃」（取消/不去/退了）→ 立即提前取消/退座，避免违约。
- 含糊 → 返回 None 不劫持，交给 LLM 正常回复；到安全线仍无回复时由调度器兜底执行。
"""

from __future__ import annotations

from typing import Optional

from src.channels.base import OutboundMessage
from src.tools.ccnu_library import client, service
from src.tools.ccnu_library import repository as repo
from src.tools.registry import ToolContext


async def intercept(ctx: ToolContext, text: str) -> Optional[OutboundMessage]:
    pr = repo.awaiting_protection(ctx.db, ctx.session_id)
    if pr is None:
        return None

    decision = service.classify_protection_reply(text)
    if decision == "unclear":
        return None  # 不确定就不劫持，交给 LLM；兜底仍会在安全线保护

    if decision == "keep":
        repo.set_protection_status(ctx.db, pr["id"], "kept")
        tail = "记得按时签到 👌" if pr["kind"] == "signin" else "记得及时回座 👌"
        return OutboundMessage(reply_text=f"好，那我保留这个座位，不会自动取消。{tail}")

    # decision == "release" → 立即执行保护动作
    rid = pr.get("reservation_id")
    if pr["action"] == "cancel":
        res = await client.call(ctx, "cancel_reservation", {"reservation_id": rid},
                                user_key=pr["user_key"])
        verb, wstatus = "取消预约", "cancelled"
    else:
        res = await client.call(ctx, "end_reservation_early", {"reservation_id": rid},
                                user_key=pr["user_key"])
        verb, wstatus = "退座", "ended"

    if res.get("ok"):
        repo.set_protection_status(ctx.db, pr["id"], "released")
        repo.update_watch(ctx.db, pr["watch_id"], status=wstatus)
        repo.cancel_notifications_for_watch(
            ctx.db, pr["watch_id"], ["signin_reminder", "away_reminder"])
        return OutboundMessage(reply_text=f"好的，已帮你提前{verb}，这次不会计违约 ✅")

    from src.tools.ccnu_library.errors import explain
    repo.set_protection_status(ctx.db, pr["id"], "failed")
    return OutboundMessage(
        reply_text=f"想帮你{verb}，但没成功：{explain(res.get('code'), res.get('message'))}。"
                   "请尽快在图书馆系统手动处理，以免违约。")
