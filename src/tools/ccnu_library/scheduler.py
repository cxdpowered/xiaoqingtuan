"""定时扫描：执行预约 job、推进 watch、投递到期通知（需求文档 §9-§12）。

**不自建轮询循环**：复用 APScheduler（NoneBot 生态的 `nonebot-plugin-apscheduler`）。
bot 进程用其共享 scheduler；CLI/独立进程用 :func:`start` 兜底起一个 AsyncIOScheduler。

扫描函数 :func:`scan_once` 是幂等的：只处理「到期且可推进」的行，状态全在 SQLite，
进程重启后从库里恢复，不依赖内存计时器。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from src import config
from src.storage.db import Database, get_db, now_iso
from src.tools.ccnu_library import client, service
from src.tools.ccnu_library import repository as repo
from src.tools.ccnu_library.errors import MAX_RETRY_ATTEMPTS, explain, retry_action
from src.tools.registry import ToolContext

_log = logging.getLogger(__name__)
_started = False


def _ctx_from_row(db: Database, row: dict[str, Any]) -> ToolContext:
    return ToolContext(
        db=db, session_id=row.get("session_id") or "", user_id="",
        person_id=row.get("person_id"), channel=row.get("channel"),
        account_id=row.get("account_id"),
    )


def _notify(db: Database, row: dict[str, Any], *, kind: str, message: str,
            watch_id: Optional[str] = None) -> None:
    repo.create_notification(
        db, person_id=row["person_id"], user_key=row["user_key"], kind=kind, message=message,
        due_at=now_iso(), session_id=row.get("session_id"), channel=row.get("channel"),
        account_id=row.get("account_id"), watch_id=watch_id,
    )


# ---- 爽约 / 暂离保护（先问后保护）-----------------------------------------
async def _maybe_open_protection(
    db: Database, watch: dict[str, Any], *, kind: str, action: str, target_deadline: Optional[str]
) -> None:
    """临近截止时开一个「保护提示」并私信问用户；已开过或未到询问窗则跳过。"""
    if not getattr(config, "LIBRARY_PROTECTION_ENABLED", True):
        return
    if not target_deadline or not service.within_ask_lead(target_deadline):
        return
    if repo.has_open_protection(db, watch["id"], kind):
        return
    safety = service.protect_safety_deadline(target_deadline)
    if not safety:
        return
    dt = service.parse_dt(target_deadline)
    tlabel = dt.strftime("%H:%M") if dt else ""
    seat_txt = f"（{watch.get('seat_no')}）" if watch.get("seat_no") else ""
    if kind == "signin":
        prompt = (f"⏰ 你预约的座位{seat_txt}签到快截止了（{tlabel}），还去吗？\n"
                  "回复『去』我就保留；回复『取消』或不回复，我会在到点前帮你取消，避免留下爽约违约记录。")
    else:
        prompt = (f"🚶 检测到你已离馆，暂离快超时了（{tlabel}），还回座吗？\n"
                  "回复『回来』我就保留；回复『取消』或不回复，我会在超时前帮你退座，避免违约。")
    repo.create_protection_prompt(
        db, person_id=watch["person_id"], user_key=watch["user_key"], watch_id=watch["id"],
        kind=kind, action=action, deadline=safety,
        reservation_id=watch.get("external_reservation_id"), session_id=watch.get("session_id"),
        channel=watch.get("channel"), account_id=watch.get("account_id"), prompt=prompt)
    _notify(db, watch, kind="sync_warning", watch_id=watch["id"], message=prompt)


async def _execute_protection(db: Database, pr: dict[str, Any]) -> None:
    """到安全线仍未收到回复：执行保护动作（取消/退座），避免违约。"""
    ctx = _ctx_from_row(db, pr)
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
        repo.set_protection_status(db, pr["id"], "executed")
        repo.update_watch(db, pr["watch_id"], status=wstatus)
        repo.cancel_notifications_for_watch(db, pr["watch_id"], ["signin_reminder", "away_reminder"])
        _notify(db, pr, kind="auto_cancel_result" if pr["action"] == "cancel" else "auto_end_result",
                watch_id=pr["watch_id"],
                message=f"没收到你的回复，已按保护策略帮你提前{verb}，这次不会计违约 ✅")
    else:
        repo.set_protection_status(db, pr["id"], "failed")
        _notify(db, pr, kind="sync_warning", watch_id=pr["watch_id"],
                message=f"想帮你提前{verb}避免违约，但操作失败（{explain(res.get('code'), res.get('message'))}），"
                        "请尽快手动处理。")


# ---- 执行预约 job ----------------------------------------------------------
async def _day_open_mode(ctx: ToolContext, user_key: str, date: str) -> str:
    """探测某日开放情况：full（下午也开）| morning（仅上午）| closed（闭馆）| unknown（查询失败）。

    用固定小窗采样：下午有区域=full；下午无、上午有=morning；都无=closed。
    查询本身失败返回 unknown，让调用方按原时段照常尝试，避免因抖动误跳过。
    """
    async def _has_areas(window: tuple[str, str]) -> Optional[bool]:
        res = await client.call(
            ctx, "get_availability_distribution",
            {"date": date, "start_time": window[0], "end_time": window[1]}, user_key=user_key)
        if not res.get("ok"):
            return None
        return service.slot_has_areas(res)

    aft = await _has_areas(service.afternoon_probe())
    if aft is None:
        return "unknown"
    if aft:
        return "full"
    morn = await _has_areas(service.morning_probe())
    if morn is None:
        return "unknown"
    return "morning" if morn else "closed"


async def _run_job(db: Database, job: dict[str, Any]) -> None:
    plan = repo.get_plan(db, job["plan_id"]) if job.get("plan_id") else None
    if plan and plan["status"] != "active":
        repo.update_job(db, job["id"], status="skipped", last_error_message="计划非 active")
        return

    repo.update_job(db, job["id"], status="running")
    ctx = _ctx_from_row(db, plan or job)

    # 1) 确保登录
    status = await client.call(ctx, "get_login_status", user_key=job["user_key"])
    if status.get("ok") and not status.get("logged_in"):
        login = await client.call(ctx, "start_login", user_key=job["user_key"])
        if not login.get("ok"):
            code = str(login.get("code") or "").upper()
            if code == "NEED_CHALLENGE":
                repo.update_job(db, job["id"], status="waiting_challenge",
                                last_error_code=code, last_error_message="需要验证码")
                _notify(db, plan or job, kind="sync_warning",
                        message="定时预约需要登录/验证码，请发『登录图书馆』完成验证后我会继续。")
                return

    # 1.5) 休馆/寒暑假自适应：放票时刻探测该日「实际可预约时段」，据此决定有效时段。
    #      下午无区域 = 半天闭馆 → 只约上午；全天无区域 = 闭馆 → 跳过（不写死星期几）。
    slot_start, slot_end = job["start_time"], job["end_time"]
    mode = await _day_open_mode(ctx, job["user_key"], job["target_date"])
    if mode == "closed":
        repo.update_job(db, job["id"], status="skipped", last_error_code="CLOSED",
                        last_error_message="该日无可预约时段（疑似闭馆/假期）")
        _notify(db, plan or job, kind="booking_result",
                message=f"{job['target_date']} 图书馆疑似闭馆（查无可约时段），已跳过当日预约。")
        return
    if mode == "morning":
        clipped = service.clip_to_morning(slot_start, slot_end)
        if clipped is None:
            repo.update_job(db, job["id"], status="skipped", last_error_code="HALF_DAY",
                            last_error_message="该日下午闭馆，原时段落在下午")
            _notify(db, plan or job, kind="booking_result",
                    message=f"{job['target_date']} 下午闭馆，原定 {slot_start}-{slot_end} 在下午，已跳过。")
            return
        slot_start, slot_end = clipped

    # 2) 选定 location（优先 job.location_id；否则查分布取第一个）
    location_id = job.get("location_id")
    fallback = repo._loads(plan.get("fallback_locations"), []) if plan else []
    candidates = [location_id] if location_id else []
    candidates += [loc for loc in fallback if loc not in candidates]
    if not candidates:
        dist = await client.call(
            ctx, "get_availability_distribution",
            {"date": job["target_date"], "start_time": slot_start, "end_time": slot_end,
             "area_filter": job.get("area_filter")}, user_key=job["user_key"])
        if dist.get("ok"):
            areas = dist.get("distribution") or dist.get("areas") or []
            candidates = [a.get("location_id") for a in areas if isinstance(a, dict) and a.get("location_id")]

    # 3) 逐个候选尝试预约
    last: dict[str, Any] = {"ok": False, "code": "NO_SEAT"}
    for loc in candidates or [None]:
        payload = {"date": job["target_date"], "start_time": slot_start,
                   "end_time": slot_end, "location_id": loc, "strategy": job["strategy"]}
        res = await client.call(ctx, "reserve_seat", payload, user_key=job["user_key"])
        last = res
        if res.get("ok"):
            service.finalize_reservation(
                db, person_id=job["person_id"], user_key=job["user_key"], result=res,
                request=payload, session_id=(plan or job).get("session_id"),
                channel=(plan or job).get("channel"), account_id=(plan or job).get("account_id"),
                plan_id=job.get("plan_id"), job_id=job["id"],
                auto_cancel=bool(plan and plan["auto_cancel_if_cannot_signin"]),
                auto_end=bool(plan and plan["auto_end_if_away_timeout"]),
            )
            repo.update_job(db, job["id"], status="success", mcp_result=res)
            receipt = service.reservation_receipt(
                prefix="定时预约成功 ✅", seat_no=res.get("seat_no") or res.get("seat"),
                seat_name=res.get("seat_name"),
                location_path=res.get("location_path") or res.get("area") or loc,
                date=job["target_date"], start_time=slot_start, end_time=slot_end,
                sign_in_deadline=res.get("sign_in_deadline"))
            _notify(db, plan or job, kind="booking_result", watch_id=None, message=receipt)
            return
        if str(res.get("code") or "").upper() not in ("NO_SEAT",):
            break  # 非「无座」错误不再换区域

    # 4) 失败处置
    code = str(last.get("code") or "NO_SEAT").upper()
    action = retry_action(code)
    if action == "challenge":
        repo.update_job(db, job["id"], status="waiting_challenge", last_error_code=code)
        _notify(db, plan or job, kind="sync_warning",
                message="定时预约需要验证码，请发『登录图书馆』完成验证。")
        return
    if action == "retry" and job["attempts"] + 1 < MAX_RETRY_ATTEMPTS:
        repo.update_job(db, job["id"], status="pending", attempts=job["attempts"] + 1,
                        last_error_code=code, last_error_message=last.get("message"),
                        run_after=(datetime.now().astimezone() + timedelta(minutes=2)).isoformat(timespec="seconds"))
        return
    repo.update_job(db, job["id"], status="failed", last_error_code=code,
                    last_error_message=last.get("message"), mcp_result=last)
    _notify(db, plan or job, kind="booking_result",
            message=f"定时预约失败：{job['target_date']} {job['start_time']}-{job['end_time']}"
                    f"（{explain(code, last.get('message'))}）。")


# ---- 推进 watch（状态同步 + 自动处置）-------------------------------------
def _next_check_delay(status: str, start_at: Optional[str]) -> int:
    """按 §12 的节奏返回下次检查的间隔（秒）。"""
    if status in ("waiting_sign_in", "away"):
        return 60
    start_dt = service.parse_dt(start_at)
    if start_dt is not None:
        mins_to_start = (start_dt - datetime.now().astimezone()).total_seconds() / 60
        if 0 <= mins_to_start <= 30:
            return 4 * 60
    if status == "in_use":
        return 12 * 60
    return 12 * 60




async def _physical_away(db: Database, ctx: ToolContext, watch: dict[str, Any]) -> dict[str, Any]:
    """使用中座位的「物理暂离」检测（修复 3）。

    物理暂离/回座是过闸机自动触发的，**不会实时改预约状态**（仍是 in_use），所以这里用
    :func:`get_door_log` 的 ``in_building`` 判断，而不是看预约状态。副作用（排回座提醒、发
    通知、到点自动退座）在此完成；watch 主行的 ``away_deadline`` 由调用方统一落库。

    返回 ``{"ended": bool, "away_deadline": Optional[str]}``。
    """
    prev = watch.get("away_deadline")
    unchanged = {"ended": False, "away_deadline": prev}
    today = datetime.now().astimezone().date().isoformat()
    log = await client.call(ctx, "get_door_log", {"date": today}, user_key=watch["user_key"])
    if not log.get("ok"):
        return unchanged
    in_building = service.in_building_from_door_log(log)
    if in_building is None:
        return unchanged

    if in_building:
        # 已回馆：清掉暂离追踪与回座提醒。
        if prev:
            repo.cancel_notifications_for_watch(db, watch["id"], ["away_reminder"])
        return {"ended": False, "away_deadline": None}

    # 已离馆。首次检测到 → 记录回座截止、排回座提醒、提示一次。
    if not prev:
        grace = getattr(config, "LIBRARY_AWAY_GRACE_MINUTES", 60)
        deadline = (datetime.now().astimezone() + timedelta(minutes=grace)).isoformat(timespec="seconds")
        service.schedule_away_reminders(
            db, person_id=watch["person_id"], user_key=watch["user_key"], watch_id=watch["id"],
            session_id=watch.get("session_id"), channel=watch.get("channel"),
            account_id=watch.get("account_id"), away_deadline=deadline)
        _notify(db, watch, kind="away_reminder", watch_id=watch["id"],
                message=f"检测到你已离馆（暂离），请在约 {grace} 分钟内回座，否则暂离超时会被释放并计违约。")
        return {"ended": False, "away_deadline": deadline}

    # 持续离馆：暂离超时的处置改由「暂离保护」（先问后退座）统一负责，见 _sync_watch。
    return unchanged


async def _sync_watch(db: Database, watch: dict[str, Any]) -> None:
    ctx = _ctx_from_row(db, watch)
    res = await client.call(ctx, "get_current_reservation", user_key=watch["user_key"])
    if not res.get("ok"):
        repo.update_watch(db, watch["id"], last_checked_at=now_iso(),
                          next_check_at=(datetime.now().astimezone() + timedelta(minutes=5)).isoformat(timespec="seconds"))
        return

    # 判断预约状态只看 status（修复 1/2）；不再依赖 seat_no 是否存在。
    status = str(res.get("status") or "unknown")
    away_deadline = res.get("away_deadline") or watch.get("away_deadline")
    sign_in_deadline = res.get("sign_in_deadline") or watch.get("sign_in_deadline")

    # 冲突处理（§12）：网站为准。终态含 violation（已发生的违约，修复 2）。
    if status in ("none", "ended", "cancelled", "violation"):
        mapped = status if status in ("ended", "cancelled", "violation") else "none"
        repo.update_watch(db, watch["id"], status=mapped, raw_status=status, last_checked_at=now_iso())
        repo.cancel_notifications_for_watch(db, watch["id"], ["signin_reminder", "away_reminder"])
        if status == "violation":
            _notify(db, watch, kind="sync_warning", watch_id=watch["id"],
                    message="本次预约已违约释放（爽约或暂离超时）。注意：每月违约满 3 次会进黑名单一周，"
                            "可发『查违约记录』查看累计次数。")
        return

    # 使用中：用闸机记录检测「物理暂离」（修复 3），而不是看预约状态。
    if status == "in_use" and getattr(config, "LIBRARY_DOOR_LOG_AWAY_DETECTION", True):
        away = await _physical_away(db, ctx, watch)
        if away["ended"]:
            return
        away_deadline = away["away_deadline"]

    # 爽约保护 / 暂离保护：临近截止先私信问用户；用户回复取消或到安全线仍无回复，
    # 由「先问后保护」机制自动提前取消/退座，避免留下违约记录（见 run.py / _execute_protection）。
    if status in ("reserved", "waiting_sign_in"):
        await _maybe_open_protection(db, watch, kind="signin", action="cancel",
                                     target_deadline=sign_in_deadline)
    elif away_deadline and status in ("away", "in_use"):
        await _maybe_open_protection(db, watch, kind="away", action="end_early",
                                     target_deadline=away_deadline)

    delay = _next_check_delay(status, watch.get("start_at"))
    repo.update_watch(
        db, watch["id"], status=status, raw_status=status, raw_payload=res,
        away_deadline=away_deadline, sign_in_deadline=sign_in_deadline, last_checked_at=now_iso(),
        next_check_at=(datetime.now().astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds"),
    )


# ---- 投递通知 --------------------------------------------------------------
async def _deliver(db: Database, notif: dict[str, Any]) -> None:
    try:
        sent = await _send_message(notif)
    except Exception as exc:  # noqa: BLE001
        _log.warning("通知投递失败 %s：%s", notif["id"], exc)
        sent = False
    repo.set_notification_status(db, notif["id"], "sent" if sent else "failed")


async def _send_message(notif: dict[str, Any]) -> bool:
    """通过 NoneBot 主动给用户发消息。CLI/无 bot 环境下返回 False（标 failed，不重试）。"""
    channel = notif.get("channel")
    session_id = notif.get("session_id") or ""
    message = notif["message"]
    try:
        import nonebot
        bot = nonebot.get_bot()
    except Exception:
        return False

    if channel == "qq":
        target = session_id.split(":", 1)[-1]  # 形如 group_123_456 或 纯 QQ 号
        if "group" in target:
            digits = "".join(c for c in target if c.isdigit())
            await bot.send_msg(message_type="group", group_id=int(digits), message=message)
        else:
            uid = notif.get("account_id") or "".join(c for c in target if c.isdigit())
            await bot.send_msg(message_type="private", user_id=int(uid), message=message)
        return True
    # 其它渠道（微信/CLI）暂不支持主动推送：交由调用方降级。
    return False


# ---- 扫描入口 --------------------------------------------------------------
async def scan_once(db: Optional[Database] = None) -> dict[str, int]:
    """扫描一轮到期项。返回各类处理计数（便于测试/观测）。"""
    db = db or get_db()
    now = now_iso()
    counts = {"jobs": 0, "watches": 0, "notifications": 0, "plans": 0}

    # 0) 滚动补齐多天/跨月计划的 job（幂等），并把已过期计划标记完成。
    reg = service.regenerate_active_plans(db)
    counts["plans"] = reg["generated"] + reg["completed"]

    # 0.5) 到安全线仍无回复的爽约/暂离保护 → 兜底执行保护动作。
    counts["protections"] = 0
    for pr in repo.due_protection_prompts(db, now):
        try:
            await _execute_protection(db, pr)
        except Exception as exc:  # noqa: BLE001
            _log.warning("执行保护动作 %s 出错：%s", pr["id"], exc)
            repo.set_protection_status(db, pr["id"], "failed")
        counts["protections"] += 1

    for job in repo.due_jobs(db, now):
        try:
            await _run_job(db, job)
        except Exception as exc:  # noqa: BLE001
            _log.warning("执行预约 job %s 出错：%s", job["id"], exc)
            repo.update_job(db, job["id"], status="failed", last_error_message=str(exc))
        counts["jobs"] += 1

    for watch in repo.due_watches(db, now):
        try:
            await _sync_watch(db, watch)
        except Exception as exc:  # noqa: BLE001
            _log.warning("同步 watch %s 出错：%s", watch["id"], exc)
        counts["watches"] += 1

    for notif in repo.due_notifications(db, now):
        await _deliver(db, notif)
        counts["notifications"] += 1

    return counts


def setup(scheduler: Any) -> None:
    """把扫描任务注册到一个 APScheduler 实例（interval job）。幂等。"""
    interval = getattr(config, "LIBRARY_SCAN_INTERVAL_SECONDS", 30)
    if scheduler.get_job("ccnu_library_scan"):
        return
    scheduler.add_job(scan_once, "interval", seconds=interval, id="ccnu_library_scan",
                      max_instances=1, coalesce=True)


def start() -> None:
    """兜底启动：优先用 nonebot-plugin-apscheduler 的共享 scheduler，否则自建一个。

    bot 进程建议直接 :func:`setup` nonebot 的 scheduler；本函数供 CLI/独立进程使用。
    """
    global _started
    if _started:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler  # type: ignore
    except Exception:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore

        scheduler = AsyncIOScheduler()
        scheduler.start()
    setup(scheduler)
    _started = True
