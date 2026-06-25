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


# ---- 执行预约 job ----------------------------------------------------------
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

    # 2) 选定 location（优先 job.location_id；否则查分布取第一个）
    location_id = job.get("location_id")
    fallback = repo._loads(plan.get("fallback_locations"), []) if plan else []
    candidates = [location_id] if location_id else []
    candidates += [loc for loc in fallback if loc not in candidates]
    if not candidates:
        dist = await client.call(
            ctx, "get_availability_distribution",
            {"date": job["target_date"], "start_time": job["start_time"], "end_time": job["end_time"],
             "area_filter": job.get("area_filter")}, user_key=job["user_key"])
        if dist.get("ok"):
            areas = dist.get("distribution") or dist.get("areas") or []
            candidates = [a.get("location_id") for a in areas if isinstance(a, dict) and a.get("location_id")]

    # 3) 逐个候选尝试预约
    last: dict[str, Any] = {"ok": False, "code": "NO_SEAT"}
    for loc in candidates or [None]:
        payload = {"date": job["target_date"], "start_time": job["start_time"],
                   "end_time": job["end_time"], "location_id": loc, "strategy": job["strategy"]}
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
            _notify(db, plan or job, kind="booking_result",
                    message=f"定时预约成功 ✅ {job['target_date']} {job['start_time']}-{job['end_time']}"
                            f" 座位 {res.get('seat_no') or res.get('seat') or ''}。")
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


def _near(deadline: Optional[str], minutes: int = 2) -> bool:
    dt = service.parse_dt(deadline)
    if dt is None:
        return False
    return dt - datetime.now().astimezone() <= timedelta(minutes=minutes)


async def _sync_watch(db: Database, watch: dict[str, Any]) -> None:
    ctx = _ctx_from_row(db, watch)
    res = await client.call(ctx, "get_current_reservation", user_key=watch["user_key"])
    if not res.get("ok"):
        repo.update_watch(db, watch["id"], last_checked_at=now_iso(),
                          next_check_at=(datetime.now().astimezone() + timedelta(minutes=5)).isoformat(timespec="seconds"))
        return

    status = str(res.get("status") or "unknown")
    away_deadline = res.get("away_deadline") or watch.get("away_deadline")
    sign_in_deadline = res.get("sign_in_deadline") or watch.get("sign_in_deadline")

    # 冲突处理（§12）：网站为准。
    if status in ("none", "ended", "cancelled"):
        mapped = "ended" if status == "ended" else ("cancelled" if status == "cancelled" else "none")
        repo.update_watch(db, watch["id"], status=mapped, raw_status=status, last_checked_at=now_iso())
        repo.cancel_notifications_for_watch(db, watch["id"], ["signin_reminder", "away_reminder"])
        return

    # 自动取消未签到（§10.3）
    if (status in ("reserved", "waiting_sign_in") and watch["auto_cancel_if_cannot_signin"]
            and _near(sign_in_deadline)):
        cancel = await client.call(ctx, "cancel_reservation",
                                   {"reservation_id": watch.get("external_reservation_id")},
                                   user_key=watch["user_key"])
        if cancel.get("ok"):
            repo.update_watch(db, watch["id"], status="cancelled", raw_status=status, last_checked_at=now_iso())
            repo.cancel_notifications_for_watch(db, watch["id"], ["signin_reminder"])
            _notify(db, watch, kind="auto_cancel_result", watch_id=watch["id"],
                    message="临近签到截止且未签到，已按你的授权自动取消该预约。")
            return

    # 自动退座（§11.3）
    if status == "away" and watch["auto_end_if_away_timeout"] and _near(away_deadline):
        end = await client.call(ctx, "end_reservation_early",
                                {"reservation_id": watch.get("external_reservation_id")},
                                user_key=watch["user_key"])
        if end.get("ok"):
            repo.update_watch(db, watch["id"], status="ended", raw_status=status, last_checked_at=now_iso())
            repo.cancel_notifications_for_watch(db, watch["id"], ["away_reminder"])
            _notify(db, watch, kind="auto_end_result", watch_id=watch["id"],
                    message="暂离即将超时，已按你的授权自动提前结束/退座。")
            return

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
    counts = {"jobs": 0, "watches": 0, "notifications": 0}

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
