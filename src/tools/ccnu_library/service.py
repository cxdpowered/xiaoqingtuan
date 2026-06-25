"""预约相关的共享业务逻辑：手动 wrapper（tools.py）与定时 job（scheduler.py）都复用。

放在这里避免 tools ↔ scheduler 互相 import 形成环。本模块只依赖 client / repo / config。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from src import config
from src.storage.db import Database, now_iso
from src.tools.ccnu_library import repository as repo


def default_strategy(db: Database, person_id: Optional[str]) -> str:
    if person_id:
        s = repo.get_settings(db, person_id)
        if s and s.get("default_strategy"):
            return str(s["default_strategy"])
    return "favorite_first"


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def finalize_reservation(
    db: Database, *, person_id: str, user_key: str, result: dict[str, Any],
    request: dict[str, Any], session_id: Optional[str], channel: Optional[str],
    account_id: Optional[str], plan_id: Optional[str] = None, job_id: Optional[str] = None,
    auto_cancel: bool = False, auto_end: bool = False,
) -> str:
    """预约成功后落库 + 建 watch + 排签到提醒。返回 watch_id。

    ``result`` 为远程 reserve_seat 的返回，字段名做防御性读取。
    """
    seat_no = result.get("seat_no") or result.get("seat") or request.get("seat_id")
    location_path = result.get("location_path") or result.get("area") or request.get("location_id")
    external_id = result.get("reservation_id") or result.get("external_reservation_id")
    start_at = result.get("start_at")
    sign_in_deadline = result.get("sign_in_deadline")

    # 落一条 reservations 流水（复用既有表）。
    db.add_reservation(
        date=request.get("date", ""), start_time=request.get("start_time", ""),
        end_time=request.get("end_time", ""), status="success",
        library=result.get("library"), area=str(location_path) if location_path else None,
        seat=str(seat_no) if seat_no else None,
        metadata={"source": "ccnu_library", "external_reservation_id": external_id,
                  "plan_id": plan_id, "job_id": job_id},
    )

    # 若 MCP 未给签到截止，用宽限分钟数从 start_at 推算。
    if not sign_in_deadline and start_at:
        sdt = parse_dt(start_at)
        if sdt:
            grace = getattr(config, "LIBRARY_SIGNIN_GRACE_MINUTES", 20)
            sign_in_deadline = _iso(sdt + timedelta(minutes=grace))

    watch_id = repo.create_watch(
        db, person_id=person_id, user_key=user_key,
        fields={
            "plan_id": plan_id, "job_id": job_id, "session_id": session_id,
            "channel": channel, "account_id": account_id,
            "external_reservation_id": external_id, "seat_no": seat_no,
            "location_path": location_path, "date": request.get("date"),
            "start_time": request.get("start_time"), "end_time": request.get("end_time"),
            "start_at": start_at, "end_at": result.get("end_at"),
            "sign_in_deadline": sign_in_deadline, "status": result.get("status") or "reserved",
            "raw_status": result.get("status"), "raw_payload": result,
            "auto_cancel_if_cannot_signin": auto_cancel, "auto_end_if_away_timeout": auto_end,
            "next_check_at": now_iso(),
        },
    )
    schedule_signin_reminders(
        db, person_id=person_id, user_key=user_key, watch_id=watch_id,
        session_id=session_id, channel=channel, account_id=account_id,
        seat_no=seat_no, start_at=start_at, sign_in_deadline=sign_in_deadline,
    )
    return watch_id


# 签到提醒的相对偏移（分钟）：start 前 30/10 分钟、签到截止前 5/1 分钟（§10.2）。
_SIGNIN_OFFSETS_BEFORE_START = (30, 10)
_SIGNIN_OFFSETS_BEFORE_DEADLINE = (5, 1)
_AWAY_OFFSETS_BEFORE_DEADLINE = (10, 5, 1)


def _maybe_notify(
    db: Database, *, person_id: str, user_key: str, watch_id: str,
    session_id: Optional[str], channel: Optional[str], account_id: Optional[str],
    kind: str, message: str, when: Optional[datetime],
) -> None:
    if when is None or when <= datetime.now().astimezone():
        return
    repo.create_notification(
        db, person_id=person_id, user_key=user_key, kind=kind, message=message,
        due_at=_iso(when), session_id=session_id, channel=channel,
        account_id=account_id, watch_id=watch_id,
    )


def schedule_signin_reminders(
    db: Database, *, person_id: str, user_key: str, watch_id: str,
    session_id: Optional[str], channel: Optional[str], account_id: Optional[str],
    seat_no: Any, start_at: Optional[str], sign_in_deadline: Optional[str],
) -> None:
    seat = f"座位 {seat_no} " if seat_no else ""
    start_dt = parse_dt(start_at)
    for mins in _SIGNIN_OFFSETS_BEFORE_START:
        if start_dt:
            _maybe_notify(
                db, person_id=person_id, user_key=user_key, watch_id=watch_id,
                session_id=session_id, channel=channel, account_id=account_id,
                kind="signin_reminder",
                message=f"图书馆预约还有 {mins} 分钟开始（{seat}），记得按时签到。",
                when=start_dt - timedelta(minutes=mins),
            )
    deadline_dt = parse_dt(sign_in_deadline)
    for mins in _SIGNIN_OFFSETS_BEFORE_DEADLINE:
        if deadline_dt:
            _maybe_notify(
                db, person_id=person_id, user_key=user_key, watch_id=watch_id,
                session_id=session_id, channel=channel, account_id=account_id,
                kind="signin_reminder",
                message=f"签到将在 {mins} 分钟后截止（{seat}），请尽快签到，否则可能被自动取消。",
                when=deadline_dt - timedelta(minutes=mins),
            )


def schedule_away_reminders(
    db: Database, *, person_id: str, user_key: str, watch_id: str,
    session_id: Optional[str], channel: Optional[str], account_id: Optional[str],
    away_deadline: Optional[str],
) -> None:
    deadline_dt = parse_dt(away_deadline)
    for mins in _AWAY_OFFSETS_BEFORE_DEADLINE:
        if deadline_dt:
            _maybe_notify(
                db, person_id=person_id, user_key=user_key, watch_id=watch_id,
                session_id=session_id, channel=channel, account_id=account_id,
                kind="away_reminder",
                message=f"暂离将在 {mins} 分钟后超时，请及时回座，否则可能被自动退座。",
                when=deadline_dt - timedelta(minutes=mins),
            )
