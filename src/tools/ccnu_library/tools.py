"""暴露给 LLM 的本地 ``library_*`` wrapper 工具（需求文档 §7）。

每个 wrapper 的统一职责：注入 ``user_key``（client 层）、统一错误码转中文、衔接
``NEED_LOGIN`` → ``start_login``、拦截 ``NEED_CHALLENGE`` 建会话、把远程结果规范化到本地表。
高风险动作标 ``high_risk=True``，仍走既有 ``confirm_gate`` 二次确认。
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any, Optional

from src import config
from src.tools.ccnu_library import client, models, service
from src.tools.ccnu_library import challenge as challenge_mod
from src.tools.ccnu_library import repository as repo
from src.tools.ccnu_library.errors import explain
from src.tools.registry import ToolContext, context_tool


# ---- 公共小工具 ------------------------------------------------------------
def _is_code(res: dict[str, Any], code: str) -> bool:
    return not res.get("ok") and str(res.get("code") or "").upper() == code.upper()


def _fail(res: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "code": res.get("code"),
            "error": explain(res.get("code"), res.get("message"))}


def _challenge_pending(message: str) -> dict[str, Any]:
    return {"ok": False, "code": "NEED_CHALLENGE", "needs_challenge": True, "message": message}


def _fill_window(q: Any) -> dict[str, Any]:
    """把（可能残缺的）date/start_time/end_time 补成完整预约窗口。

    未给全时用 :func:`service.default_booking_window` 的默认值，并带一句说明（防止过度询问）。
    """
    given_all = bool(q.date and q.start_time and q.end_time)
    base = service.default_booking_window()
    date = q.date or base["date"]
    start = q.start_time or base["start_time"]
    end = q.end_time or base["end_time"]
    note = None if given_all else (
        f"已按 {date} {start}-{end} 处理（未给全时间时用的默认，需要改直接说时间）。")
    return {"date": date, "start_time": start, "end_time": end, "note": note}


def _find_area_message(query: str, window: dict[str, Any], resolution: dict[str, Any]) -> str:
    """把区域解析结果转成一句面向用户/供 LLM 转述的中文提示。"""
    span = f"{window['date']} {window['start_time']}-{window['end_time']}"
    status = resolution.get("status")
    cands = resolution.get("candidates") or []
    if status == "resolved":
        free = resolution.get("free")
        free_txt = f"，约 {free} 个空位" if isinstance(free, int) else ""
        return f"已定位到「{resolution.get('area_name')}」（{span}{free_txt}），可直接预约。"
    if status == "ambiguous":
        names = "、".join(c["name"] for c in cands)
        return f"「{query}」匹配到多个区域：{names}。你要约哪一个？"
    if status == "none_free":
        m = resolution.get("matched") or {}
        alts = "、".join(f"{c['name']}(空位{c['free']})" for c in cands) or "暂无其它空位区域"
        return f"「{m.get('name', query)}」在 {span} 已无空位。有空位的区域：{alts}。换一个吗？"
    # not_found
    if cands:
        alts = "、".join(f"{c['name']}(空位{c['free']})" for c in cands)
        return f"没找到与「{query}」匹配的区域。当前有空位：{alts}。你想约哪个？"
    return f"没找到与「{query}」匹配的区域，且当前查询不到可用区域，请稍后再试或换个时段。"


async def _query_distribution(ctx: ToolContext, window: dict[str, Any]) -> dict[str, Any]:
    return await _ensure_and_call(
        ctx, "get_availability_distribution",
        {"date": window["date"], "start_time": window["start_time"],
         "end_time": window["end_time"]},
        action_label="查询座位分布",
    )


async def _resolve_location_id(
    ctx: ToolContext, location_query: Optional[str], window: dict[str, Any]
) -> dict[str, Any]:
    """为非 exact_seat 预约确定 location_id。

    ok=True 时含 ``location_id``；否则返回带引导语/candidates 的 ``NEED_LOCATION``，交由助手追问。
    优先用自然语言区域解析，其次回落到用户记录的默认区域。
    """
    if location_query:
        res = await _query_distribution(ctx, window)
        if _is_code(res, "NEED_CHALLENGE"):
            return _challenge_pending("预约前需要验证码，已发送图片，请回复验证码。")
        if not res.get("ok"):
            return _fail(res)
        resolution = service.resolve_from_distribution(res, location_query)
        if resolution.get("status") == "resolved":
            return {"ok": True, "location_id": resolution["location_id"]}
        return {"ok": False, "code": "NEED_LOCATION", "resolution": resolution,
                "error": _find_area_message(location_query, window, resolution)}
    if ctx.person_id:
        s = repo.get_settings(ctx.db, ctx.person_id)
        if s and s.get("default_location_id"):
            return {"ok": True, "location_id": str(s["default_location_id"])}
    return {"ok": False, "code": "NEED_LOCATION",
            "error": "还不知道约哪个区域，告诉我区域（如『南湖一楼』）我来帮你定位。"}


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


async def library_find_area(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """把自然语言区域描述（如『南湖一楼』）解析为可预约的 location_id。

    自然语言预约的关键一步：先用它拿到唯一区域，再调 library_reserve。缺时间自动补默认，
    匹配到多个/无空位/找不到时返回 candidates 和一句引导语，供助手做**一次**聚焦追问。
    """
    q = models.FindAreaArgs.model_validate(args)
    window = _fill_window(q)
    err = service.validate_window(window["date"], window["start_time"], window["end_time"])
    if err:
        return {"ok": False, "error": err}
    res = await _query_distribution(ctx, window)
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    resolution = service.resolve_from_distribution(res, q.location_query)
    out: dict[str, Any] = {
        "ok": True, "date": window["date"], "start_time": window["start_time"],
        "end_time": window["end_time"], "query": q.location_query, **resolution,
        "message": _find_area_message(q.location_query, window, resolution),
    }
    if window["note"]:
        out["window_note"] = window["note"]
    return out


async def library_current(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    res = await _ensure_and_call(ctx, "get_current_reservation", {}, action_label="查询当前预约")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)

    # 判断「有没有当前预约」只看 status，不看 seat_no（远程无预约时返回干净的 none）。
    status = str(res.get("status") or "unknown")
    label, _desc = models.describe_status(status)
    if not models.is_active_status(status):
        return {"ok": True, "active": False, "status": status, "status_label": label,
                "message": res.get("message") or f"当前无有效预约（{label}）。"}

    seat_no = res.get("seat_no")
    path = res.get("path") if isinstance(res.get("path"), list) else []
    where = " / ".join(p for p in path if p)
    detail = {
        "reservation_id": res.get("reservation_id"), "seat_no": seat_no, "path": path,
        "date": res.get("date"), "start_time": res.get("start_time"),
        "end_time": res.get("end_time"), "receipt": res.get("receipt"),
        "raw_text": res.get("raw_text"), "away_deadline": res.get("away_deadline"),
    }
    parts = [f"当前预约：{label}"]
    if seat_no:
        parts.append(f"座位 {seat_no}")
    if where:
        parts.append(where)
    if res.get("date"):
        span = f"{res.get('start_time', '')}-{res.get('end_time', '')}".strip("-")
        parts.append(f"{res['date']} {span}".strip())
    msg = "｜".join(parts)
    if res.get("raw_text"):
        msg += f"\n{res['raw_text']}"
    return {"ok": True, "active": True, "status": status, "status_label": label,
            "reservation": detail, "message": msg}


async def library_seat_layout(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """座位平面图 + 辅助选座（静态表，不含实时空闲）。"""
    q = models.SeatLayoutArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "get_seat_layout", {"location_id": q.location_id}, action_label="查询座位平面图")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    all_seats = [s for s in (res.get("seats") or []) if isinstance(s, dict)]
    power_total = sum(1 for s in all_seats if s.get("power"))
    seats = [s for s in all_seats if s.get("power")] if q.power_only else all_seats
    # 只保留辅助选座需要的字段（丢掉 layout 画布大对象）。
    slim = [{"seat_id": s.get("seat_id"), "seat_no": s.get("seat_no"), "name": s.get("name"),
             "power": bool(s.get("power")), "direction": s.get("direction")} for s in seats]
    if q.limit:
        slim = slim[: q.limit]
    return {"ok": True, "location_id": res.get("location_id") or q.location_id,
            "seat_count": res.get("seat_count") or len(all_seats), "power_seat_count": power_total,
            "returned": len(slim), "seats": slim,
            "note": "静态座位表（name=几行几列，power=是否有电源），不含实时空闲；查空闲请用座位分布/具体座位查询。"}


async def library_door_log(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """闸机进出记录：判断在馆/离馆的唯一可靠途径。"""
    q = models.DoorLogArgs.model_validate(args)
    date = q.date or date_cls.today().isoformat()
    res = await _ensure_and_call(ctx, "get_door_log", {"date": date}, action_label="查询闸机记录")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    in_building = service.in_building_from_door_log(res)
    events = res.get("events") or []
    count = res.get("count", len(events))
    where = "在馆" if in_building else ("已离馆" if in_building is False else "无法确定在馆状态")
    parts = [f"{res.get('date') or date} 闸机进出 {count} 次，当前{where}。"]
    if events and isinstance(events[0], dict):
        parts.append(f"最近：{events[0].get('time', '')} {events[0].get('gate', '')}".rstrip())
    return {"ok": True, "date": res.get("date") or date, "in_building": in_building,
            "count": count, "events": events, "message": " ".join(parts)}


async def library_violation_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """违约记录 + 风控提醒（每月达上限进黑名单一周）。"""
    q = models.PagedArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "get_violation_records", {"page": q.page, "page_size": q.page_size},
        action_label="查询违约记录")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    total = int(res.get("total") or 0)
    limit = getattr(config, "LIBRARY_VIOLATION_MONTHLY_LIMIT", 3)
    warning = None
    if total >= limit:
        warning = f"⚠️ 违约累计 {total} 次，已达上限（{limit} 次/月），可能已被列入黑名单一周。"
    elif total == limit - 1:
        warning = f"⚠️ 违约已累计 {total} 次，再违约 1 次（满 {limit} 次/月）本月就会进黑名单一周，请注意。"
    return {"ok": True, "total": total, "count": res.get("count"),
            "page": res.get("page", q.page), "page_size": res.get("page_size", q.page_size),
            "records": res.get("records") or [], "warning": warning,
            "message": warning or f"共 {total} 条违约记录。"}


async def library_reservation_history(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """图书馆历史预约查询（分页）。"""
    q = models.PagedArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "get_reservation_history", {"page": q.page, "page_size": q.page_size},
        action_label="查询预约历史")
    if _is_code(res, "NEED_CHALLENGE"):
        return _challenge_pending("查询前需要验证码，已发送图片，请回复验证码。")
    if not res.get("ok"):
        return _fail(res)
    total = int(res.get("total") or 0)
    return {"ok": True, "total": total, "count": res.get("count"),
            "page": res.get("page", q.page), "page_size": res.get("page_size", q.page_size),
            "records": res.get("records") or [],
            "message": f"共 {total} 条历史预约（本页 {res.get('count', 0)} 条）。"}


# ---- 预约 / 在馆操作（高风险）---------------------------------------------
async def library_reserve(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReserveArgs.model_validate(args)
    strategy = q.strategy or service.default_strategy(ctx.db, ctx.person_id)
    if strategy not in models.STRATEGIES:
        return {"ok": False, "error": f"未知选座策略：{strategy}"}

    window = _fill_window(q)
    err = service.validate_window(window["date"], window["start_time"], window["end_time"])
    if err:
        return {"ok": False, "error": err}

    seat_id, location_id = q.seat_id, q.location_id
    if strategy == "exact_seat":
        if not seat_id:
            return {"ok": False, "error": "exact_seat 策略需要 seat_id（可用 library_find_area 或查具体座位获取）。"}
    elif not location_id:
        # 需要区域但没给 location_id：依次用 location_query 解析 → 用户默认区域兜底。
        resolved = await _resolve_location_id(ctx, q.location_query, window)
        if not resolved.get("ok"):
            return resolved  # 含 NEED_LOCATION / candidates / 引导语，交 LLM 追问
        location_id = resolved["location_id"]

    payload = {"date": window["date"], "start_time": window["start_time"],
               "end_time": window["end_time"], "seat_id": seat_id,
               "location_id": location_id, "strategy": strategy}
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
    seat_no = res.get("seat_no") or res.get("seat")
    receipt = service.reservation_receipt(
        seat_no=seat_no, seat_name=res.get("seat_name"),
        location_path=res.get("location_path") or res.get("area") or payload.get("location_id"),
        date=payload.get("date"), start_time=payload.get("start_time"),
        end_time=payload.get("end_time"), sign_in_deadline=res.get("sign_in_deadline"),
    )
    return {"ok": True, "watch_id": watch_id, "seat_no": seat_no,
            "message": receipt + "\n已为你建立签到提醒。"}


async def library_cancel(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.ReservationIdArgs.model_validate(args)
    res = await _ensure_and_call(
        ctx, "cancel_reservation",
        {"reservation_id": q.reservation_id}, action_label="取消预约")
    if not res.get("ok"):
        return _fail(res)
    _close_active_watch(ctx, "cancelled", q.reservation_id)
    return {"ok": True, "message": "预约已取消。"}


# 注意（修复 3）：暂离/回座真机上是过闸机自动触发的（走出馆=暂离、走回馆=回座），
# 物理暂离不会实时改预约状态。判断「此刻在不在馆」请用 library_door_log 看 in_building，
# 不要看预约状态。下面这两个工具只是网页「手动暂离/回座」按钮的镜像，正常流程用不到。
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
    jobs = service.generate_plan_jobs(ctx.db, plan_id, plan) if plan else 0
    weekday_note = ""
    if q.weekdays and set(q.weekdays) == {1, 2, 3, 4, 5}:
        weekday_note = "（仅工作日）"
    return {"ok": True, "plan_id": plan_id, "generated_jobs": jobs,
            "message": f"预约计划已启用（{q.date_start}~{q.date_end}{weekday_note}），已排入 {jobs} 个待执行任务；"
                       f"次日票会在放票后自动抢，遇下午闭馆会自动只约上午。"}


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


async def _teardown_plan(ctx: ToolContext, plan: dict[str, Any]) -> dict[str, Any]:
    """完整拆除一个计划：停计划 + 撤未完成 job + 撤未开始的座位预约（远程 + 本地 + 通知）。"""
    plan_id = plan["id"]
    repo.set_plan_status(ctx.db, plan_id, "cancelled")
    jobs_cancelled = repo.cancel_pending_jobs(ctx.db, plan_id)
    reservations_cancelled = kept_in_use = failed = 0
    for w in repo.active_watches_for_plan(ctx.db, plan_id):
        # 只撤「还没开始用」的预约；使用中/暂离中的不动，避免误退座。
        if str(w.get("status") or "") not in ("reserved", "waiting_sign_in"):
            kept_in_use += 1
            continue
        res = await _ensure_and_call(
            ctx, "cancel_reservation", {"reservation_id": w.get("external_reservation_id")},
            action_label="取消预约")
        if res.get("ok"):
            reservations_cancelled += 1
            repo.update_watch(ctx.db, w["id"], status="cancelled")
            repo.cancel_notifications_for_watch(
                ctx.db, w["id"], ["signin_reminder", "away_reminder"])
        else:
            failed += 1  # 保留 watch，让用户知道有残留、可重试
    return {"plan_id": plan_id, "title": plan.get("title"), "jobs_cancelled": jobs_cancelled,
            "reservations_cancelled": reservations_cancelled, "kept_in_use": kept_in_use,
            "failed": failed}


async def library_cancel_booking_plan(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    q = models.CancelBookingPlanArgs.model_validate(args)
    if q.plan_id:
        plan = repo.get_plan(ctx.db, q.plan_id)
        if not plan or (ctx.person_id and plan["person_id"] != ctx.person_id):
            return {"ok": False, "error": "未找到该预约计划。"}
        targets = [plan]
    else:
        plans = [p for p in (repo.list_plans(ctx.db, ctx.person_id) if ctx.person_id else [])
                 if p["status"] in ("active", "paused")]
        if not plans:
            return {"ok": True, "message": "你当前没有进行中的连续预约计划。"}
        if q.all_plans or len(plans) == 1:
            targets = plans
        else:
            listing = "；".join(
                f"{p.get('title') or '计划'} {p['date_start']}~{p['date_end']}（id={p['id']}）"
                for p in plans)
            return {"ok": False, "code": "NEED_PLAN_CHOICE",
                    "error": f"你有 {len(plans)} 个进行中的计划：{listing}。要取消哪个？"
                             "（把 plan_id 告诉我，或说『全部取消』）"}

    results = [await _teardown_plan(ctx, p) for p in targets]
    jobs = sum(r["jobs_cancelled"] for r in results)
    resv = sum(r["reservations_cancelled"] for r in results)
    in_use = sum(r["kept_in_use"] for r in results)
    failed = sum(r["failed"] for r in results)
    parts = [f"已取消 {len(results)} 个连续预约计划",
             f"停掉 {jobs} 个待抢任务", f"撤销 {resv} 个未开始的座位预约"]
    if in_use:
        parts.append(f"{in_use} 个使用中的座位保留未动（要退座请说『退座』）")
    msg = "，".join(parts) + "。"
    if failed:
        msg += f" ⚠️ 有 {failed} 个座位远程取消失败，请稍后说『查当前预约』确认或重试。"
    return {"ok": True, "message": msg, "details": results}


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
    context_tool(name="library_find_area",
                 description="把自然语言区域（如『南湖一楼』）解析为可预约的区域ID；预约前先用它定位区域。"
                             "缺时间自动补默认，匹配多个/无空位时返回候选供你追问。",
                 args_schema=models.FindAreaArgs, func=library_find_area),
    context_tool(name="library_current",
                 description="查询当前图书馆预约状态（有没有预约只看 status；含使用中/暂离/已违约等）。",
                 args_schema=models.CurrentArgs, func=library_current),
    context_tool(name="library_seat_layout",
                 description="按区域拿座位平面图辅助选座（name=几行几列、power=是否有电源；静态表不含实时空闲）。",
                 args_schema=models.SeatLayoutArgs, func=library_seat_layout),
    context_tool(name="library_door_log",
                 description="查询某天闸机进出记录，判断当前在馆/离馆（in_building）；暂离检测的唯一可靠途径。",
                 args_schema=models.DoorLogArgs, func=library_door_log),
    context_tool(name="library_violation_records",
                 description="查询违约记录与累计次数（接近每月上限会提醒有进黑名单风险）。",
                 args_schema=models.PagedArgs, func=library_violation_records),
    context_tool(name="library_reservation_history",
                 description="查询我最近约过哪些座（图书馆历史预约，分页）。",
                 args_schema=models.PagedArgs, func=library_reservation_history),
    context_tool(name="library_reserve",
                 description="提交图书馆座位预约（高风险，提交前会请用户确认）。支持传自然语言区域"
                             "（location_query，如『南湖一楼』）自动定位；缺日期/时段时自动补默认，不必逐项追问。",
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
                 description="取消连续/多天预约计划并一并撤掉其待抢任务和未开始的座位预约（高风险，提交前会请用户确认）。"
                             "缺 plan_id 时自动定位唯一进行中的计划，多个会让你选或『全部取消』。",
                 args_schema=models.CancelBookingPlanArgs, func=library_cancel_booking_plan,
                 high_risk=True),
]
