"""暴露给 LLM 的本地 ``library_*`` wrapper 工具（需求文档 §7）。

每个 wrapper 的统一职责：注入 ``user_key``（client 层）、统一错误码转中文、衔接
``NEED_LOGIN`` → ``start_login``、拦截 ``NEED_CHALLENGE`` 建会话、把远程结果规范化到本地表。
高风险动作标 ``high_risk=True``，仍走既有 ``confirm_gate`` 二次确认。
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta
from typing import Any, Optional

from src.storage.db import now_iso
from src.tools.ccnu_library import client, models, service
from src.tools.ccnu_library import challenge as challenge_mod
from src.tools.ccnu_library import repository as repo
from src.tools.ccnu_library.errors import explain
from src.tools.registry import ToolContext, context_tool

_ROLLING_WINDOW_DAYS = 7  # 计划创建时一次最多滚动生成多少天的 job


# ---- 公共小工具 ------------------------------------------------------------
def _is_code(res: dict[str, Any], code: str) -> bool:
    return not res.get("ok") and str(res.get("code") or "").upper() == code.upper()


def _fail(res: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "code": res.get("code"),
            "error": explain(res.get("code"), res.get("message"))}


def _challenge_pending(message: str) -> dict[str, Any]:
    return {"ok": False, "code": "NEED_CHALLENGE", "needs_challenge": True, "message": message}


async def _do_login(ctx: ToolContext, action_label: str) -> dict[str, Any]:
    """确保登录态：已登录直接返回；否则 start_login，必要时挂起 challenge。"""
    status = await client.call(ctx, "get_login_status")
    if status.get("ok") and status.get("logged_in"):
        return {"ok": True, "already": True}
    login = await client.call(ctx, "start_login")
    if _is_code(login, "NEED_CHALLENGE"):
        challenge_mod.record_challenge(ctx, login, pending_action=action_label)
        return {"ok": False, "code": "NEED_CHALLENGE", "_challenge": True}
    return login


async def _ensure_and_call(
    ctx: ToolContext, remote: str, args: dict[str, Any], *, action_label: str
) -> dict[str, Any]:
    """调远程工具；遇 NEED_LOGIN 自动登录后重试，遇 NEED_CHALLENGE 挂起验证码。"""
    res = await client.call(ctx, remote, args)
    if _is_code(res, "NEED_LOGIN"):
        login = await _do_login(ctx, action_label)
        if login.get("_challenge"):
            return res  # challenge 已记录，交由 run.py 投递图片
        if not login.get("ok"):
            return login
        res = await client.call(ctx, remote, args)
    if _is_code(res, "NEED_CHALLENGE"):
        challenge_mod.record_challenge(ctx, res, pending_action=action_label)
    return res


# ---- 账号 / 登录 -----------------------------------------------------------
async def library_save_account(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    payload = {k: args.get(k) for k in ("username", "password", "phone_hint", "login_now")
               if args.get(k) is not None}
    res = await client.call(ctx, "save_account", payload)
    if _is_code(res, "NEED_CHALLENGE"):
        challenge_mod.record_challenge(ctx, res, pending_action="保存图书馆账号")
        return _challenge_pending("账号已保存，登录需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    if ctx.person_id:
        repo.upsert_settings(ctx.db, ctx.person_id, user_key=client.resolve_user_key(ctx))
    return {"ok": True, "message": "图书馆账号已保存" + ("并登录成功。" if res.get("logged_in") else "。")}


async def library_login(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    res = await _do_login(ctx, "登录图书馆")
    if res.get("_challenge"):
        return _challenge_pending("登录需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    return {"ok": True, "message": "图书馆已登录，可用。" if res.get("already") else "登录成功。"}


# ---- 查询 ------------------------------------------------------------------
async def library_query_availability(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.QueryAvailabilityArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "get_availability_distribution",
        {"date": q.date, "start_time": q.start_time, "end_time": q.end_time,
         "library": q.library, "area_filter": q.area_filter},
        action_label="查询座位分布",
    )
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    return {"ok": True, "date": q.date, "start_time": q.start_time, "end_time": q.end_time,
            "distribution": res.get("distribution") or res.get("areas") or res.get("result")}


async def library_list_seats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ListSeatsArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "list_available_seats",
        {"date": q.date, "start_time": q.start_time, "end_time": q.end_time,
         "location_id": q.location_id, "area_filter": q.area_filter, "limit": q.limit},
        action_label="查询具体座位",
    )
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    return {"ok": True, "seats": res.get("seats") or res.get("result")}


async def library_current(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    res = await _ensure_and_call(ctx, "get_current_reservation", {}, action_label="查询当前预约")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    return {"ok": True, "status": res.get("status"), "reservation": res.get("reservation") or res}


# ---- 预约 / 在馆操作（高风险）---------------------------------------------
async def library_reserve(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReserveArgs.model_validate(args)
    strategy = q.strategy or service.default_strategy(ctx.db, ctx.person_id)
    if strategy not in models.STRATEGIES:
        return {"ok": False, "error": f"未知选座策略：{strategy}"}
    if strategy == "exact_seat" and not q.seat_id:
        return {"ok": False, "error": "exact_seat 策略需要提供 seat_id。"}
    if strategy != "exact_seat" and not q.location_id:
        return {"ok": False, "error": f"{strategy} 策略需要提供 location_id。"}

    payload = {"date": q.date, "start_time": q.start_time, "end_time": q.end_time,
               "seat_id": q.seat_id, "location_id": q.location_id, "strategy": strategy}
    res = await _ensure_and_call(ctx, "reserve_seat", payload, action_label="提交预约")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("预约前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)

    watch_id = service.finalize_reservation(
        ctx.db, person_id=ctx.person_id or "default", user_key=client.resolve_user_key(ctx),
        result=res, request=payload, session_id=ctx.session_id, channel=ctx.channel,
        account_id=ctx.account_id,
    )
    return {"ok": True, "watch_id": watch_id, "seat_no": res.get("seat_no") or res.get("seat"),
            "message": f"预约成功 ✅ 座位 {res.get('seat_no') or res.get('seat') or ''}，已为你建立签到提醒。"}


async def library_cancel(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReservationIdArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "cancel_reservation",
        {"reservation_id": q.reservation_id}, action_label="取消预约")
    if not res.get("ok"):
        return _fail(res)
    _close_active_watch(ctx, "cancelled", q.reservation_id)
    return {"ok": True, "message": "预约已取消。"}


async def library_start_leave(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReservationIdArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "start_temporary_leave",
        {"reservation_id": q.reservation_id}, action_label="暂离")
    if not res.get("ok"):
        return _fail(res)
    away_deadline = res.get("away_deadline")
    watch = _active_watch(ctx)
    if watch:
        repo.update_watch(ctx.db, watch["id"], status="away", away_deadline=away_deadline)
        service.schedule_away_reminders(
            ctx.db, person_id=watch["person_id"], user_key=watch["user_key"],
            watch_id=watch["id"], session_id=watch.get("session_id"),
            channel=watch.get("channel"), account_id=watch.get("account_id"),
            away_deadline=away_deadline,
        )
    return {"ok": True, "away_deadline": away_deadline,
            "message": "已暂离，到点前会提醒你回座。" + (f"（截止 {away_deadline}）" if away_deadline else "")}


async def library_return(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReservationIdArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "return_from_temporary_leave",
        {"reservation_id": q.reservation_id}, action_label="回座")
    if not res.get("ok"):
        return _fail(res)
    watch = _active_watch(ctx)
    if watch:
        repo.update_watch(ctx.db, watch["id"], status="in_use", away_deadline=None)
        repo.cancel_notifications_for_watch(ctx.db, watch["id"], ["away_reminder"])
    return {"ok": True, "message": "已回座，暂离提醒已关闭。"}


async def library_end_early(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReservationIdArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "end_reservation_early",
        {"reservation_id": q.reservation_id}, action_label="提前结束/退座")
    if not res.get("ok"):
        return _fail(res)
    _close_active_watch(ctx, "ended", q.reservation_id)
    return {"ok": True, "message": "已提前结束/退座。"}


def _active_watch(ctx: ToolContext) -> Optional[dict[str, Any]]:
    if not ctx.person_id:
        return None
    for w in repo.active_watches(ctx.db):
        if w["person_id"] == ctx.person_id:
            return w
    return None


def _close_active_watch(ctx: ToolContext, status: str, reservation_id: Optional[str]) -> None:
    watch = _active_watch(ctx)
    if watch:
        repo.update_watch(ctx.db, watch["id"], status=status)
        repo.cancel_notifications_for_watch(
            ctx.db, watch["id"], ["signin_reminder", "away_reminder"])


# ---- 预约计划 --------------------------------------------------------------
def _plan_dates(date_start: str, date_end: str, weekdays: Optional[list[int]]) -> list[str]:
    try:
        start = date_cls.fromisoformat(date_start)
        end = date_cls.fromisoformat(date_end)
    except ValueError:
        return []
    today = date_cls.today()
    start = max(start, today)
    end = min(end, today + timedelta(days=_ROLLING_WINDOW_DAYS))
    out: list[str] = []
    d = start
    while d <= end:
        if not weekdays or (d.isoweekday() in weekdays):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _generate_jobs(ctx: ToolContext, plan_id: str, plan: dict[str, Any]) -> int:
    """为计划滚动生成未来若干天的 job（幂等，跳过已存在）。返回新建数量。"""
    weekdays = repo._loads(plan.get("weekdays"), None)
    time_slots = repo._loads(plan.get("time_slots"), [])
    preferred = repo._loads(plan.get("preferred_locations"), []) or [None]
    count = 0
    for d in _plan_dates(plan["date_start"], plan["date_end"], weekdays):
        for slot in time_slots:
            start_time, _, end_time = str(slot).partition("-")
            if repo.job_exists(ctx.db, plan_id=plan_id, target_date=d, start_time=start_time.strip()):
                continue
            repo.create_job(
                ctx.db, plan_id=plan_id, person_id=plan["person_id"], user_key=plan["user_key"],
                target_date=d, start_time=start_time.strip(), end_time=end_time.strip(),
                location_id=(preferred[0] if preferred else None),
                area_filter=plan.get("default_area_filter"), strategy=plan["strategy"],
                run_after=now_iso(),
            )
            count += 1
    return count


async def library_create_booking_plan(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.CreateBookingPlanArgs.model_validate(args)
    if not q.time_slots:
        return {"ok": False, "error": "至少需要一个时段（如 08:00-12:00）。"}
    strategy = q.strategy or service.default_strategy(ctx.db, ctx.person_id)
    fields = {
        "session_id": ctx.session_id, "channel": ctx.channel, "account_id": ctx.account_id,
        "title": q.title, "date_start": q.date_start, "date_end": q.date_end,
        "weekdays": q.weekdays, "time_slots": q.time_slots,
        "preferred_locations": q.preferred_locations, "fallback_locations": q.fallback_locations,
        "fallback_time_slots": q.fallback_time_slots, "strategy": strategy,
        "auto_cancel_if_cannot_signin": q.auto_cancel_if_cannot_signin,
        "auto_end_if_away_timeout": q.auto_end_if_away_timeout,
        "require_confirmation_each_booking": q.require_confirmation_each_booking,
        "status": "active",
    }
    plan_id = repo.create_plan(
        ctx.db, person_id=ctx.person_id or "default",
        user_key=client.resolve_user_key(ctx), fields=fields)
    plan = repo.get_plan(ctx.db, plan_id)
    jobs = _generate_jobs(ctx, plan_id, plan) if plan else 0
    return {"ok": True, "plan_id": plan_id, "generated_jobs": jobs,
            "message": f"预约计划已启用（{q.date_start}~{q.date_end}），已排入 {jobs} 个待执行预约任务。"}


async def library_list_booking_plans(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    plans = repo.list_plans(ctx.db, ctx.person_id) if ctx.person_id else []
    return {"ok": True, "plans": [
        {"plan_id": p["id"], "title": p.get("title"), "date_start": p["date_start"],
         "date_end": p["date_end"], "time_slots": repo._loads(p.get("time_slots"), []),
         "status": p["status"]} for p in plans]}


def _set_plan_status(ctx: ToolContext, plan_id: str, status: str, verb: str) -> dict[str, Any]:
    plan = repo.get_plan(ctx.db, plan_id)
    if not plan or (ctx.person_id and plan["person_id"] != ctx.person_id):
        return {"ok": False, "error": "未找到该预约计划。"}
    repo.set_plan_status(ctx.db, plan_id, status)
    return {"ok": True, "message": f"预约计划已{verb}。"}


async def library_pause_booking_plan(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _set_plan_status(ctx, models.PlanIdArgs.model_validate(args).plan_id, "paused", "暂停")


async def library_resume_booking_plan(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _set_plan_status(ctx, models.PlanIdArgs.model_validate(args).plan_id, "active", "恢复")


async def library_cancel_booking_plan(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _set_plan_status(ctx, models.PlanIdArgs.model_validate(args).plan_id, "cancelled", "取消")


# ---- 注册 ------------------------------------------------------------------
TOOLS = [
    context_tool(name="library_save_account",
                 description="保存华师图书馆账号密码并尝试登录（高风险，提交前会请用户确认）。",
                 args_schema=models.SaveAccountArgs, func=library_save_account, high_risk=True),
    context_tool(name="library_login",
                 description="登录华师图书馆；未登录时发起登录，可能需要图形验证码。",
                 args_schema=models.LoginArgs, func=library_login),
    context_tool(name="library_query_availability",
                 description="查询某天某时段图书馆各区域的可用座位分布（按空闲降序）。",
                 args_schema=models.QueryAvailabilityArgs, func=library_query_availability),
    context_tool(name="library_list_seats",
                 description="查询某区域某时段的具体可用座位。",
                 args_schema=models.ListSeatsArgs, func=library_list_seats),
    context_tool(name="library_current",
                 description="查询当前图书馆预约状态（含暂离详情）。",
                 args_schema=models.CurrentArgs, func=library_current),
    context_tool(name="library_reserve",
                 description="提交图书馆座位预约（高风险，提交前会请用户确认）。",
                 args_schema=models.ReserveArgs, func=library_reserve, high_risk=True),
    context_tool(name="library_cancel",
                 description="取消未开始的图书馆预约（高风险，提交前会请用户确认）。",
                 args_schema=models.ReservationIdArgs, func=library_cancel, high_risk=True),
    context_tool(name="library_start_leave",
                 description="对当前预约发起暂离（高风险，提交前会请用户确认）。",
                 args_schema=models.ReservationIdArgs, func=library_start_leave, high_risk=True),
    context_tool(name="library_return",
                 description="暂离后回座（高风险，提交前会请用户确认）。",
                 args_schema=models.ReservationIdArgs, func=library_return, high_risk=True),
    context_tool(name="library_end_early",
                 description="提前结束/退座，不同于取消未开始的预约（高风险，提交前会请用户确认）。",
                 args_schema=models.ReservationIdArgs, func=library_end_early, high_risk=True),
    context_tool(name="library_create_booking_plan",
                 description="创建多天定时预约计划并启用（高风险/计划级授权，确认后在日期范围内自动预约）。",
                 args_schema=models.CreateBookingPlanArgs, func=library_create_booking_plan,
                 high_risk=True),
    context_tool(name="library_list_booking_plans",
                 description="列出我的图书馆预约计划及状态。",
                 args_schema=models.ListPlansArgs, func=library_list_booking_plans),
    context_tool(name="library_pause_booking_plan",
                 description="暂停某个预约计划。",
                 args_schema=models.PlanIdArgs, func=library_pause_booking_plan),
    context_tool(name="library_resume_booking_plan",
                 description="恢复某个已暂停的预约计划。",
                 args_schema=models.PlanIdArgs, func=library_resume_booking_plan),
    context_tool(name="library_cancel_booking_plan",
                 description="取消某个预约计划。",
                 args_schema=models.PlanIdArgs, func=library_cancel_booking_plan),
]
