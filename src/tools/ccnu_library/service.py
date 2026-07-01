"""预约相关的共享业务逻辑：手动 wrapper（tools.py）与定时 job（scheduler.py）都复用。

放在这里避免 tools ↔ scheduler 互相 import 形成环。本模块只依赖 client / repo / config。
"""

from __future__ import annotations

import re
from datetime import date as date_cls, datetime, time as time_cls, timedelta
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


# ---- 时段缺省推算（防止过度询问）------------------------------------------
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(value: str) -> time_cls:
    m = _TIME_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"时间格式非法：{value}")
    return time_cls(int(m.group(1)), int(m.group(2)))


def _ceil_to_step(t: time_cls, step_min: int = 30) -> time_cls:
    total = t.hour * 60 + t.minute
    rounded = ((total + step_min - 1) // step_min) * step_min
    rounded = min(rounded, 23 * 60 + 30)
    return time_cls(rounded // 60, rounded % 60)


def default_booking_window(now: Optional[datetime] = None) -> dict[str, str]:
    """用户没给日期/时段时，推算一个合理的默认预约窗口。

    规则：今天还没闭馆就约「此刻(向上取整到 30 分钟，且不早于开馆) → 闭馆」；
    已过闭馆则约「明天开馆 → 闭馆」。返回 {date,start_time,end_time,assumed:bool,note}。
    """
    now = now or datetime.now().astimezone()
    open_t = _parse_hhmm(getattr(config, "LIBRARY_OPEN_TIME", "08:00"))
    close_t = _parse_hhmm(getattr(config, "LIBRARY_CLOSE_TIME", "22:00"))

    start = _ceil_to_step(now.time())
    if now.time() < open_t:
        target, start = now.date(), open_t
    elif start >= close_t:  # 今天已闭馆或临近闭馆 → 顺延到明天开馆
        target, start = now.date() + timedelta(days=1), open_t
    else:
        target = now.date()

    return {
        "date": target.isoformat(),
        "start_time": start.strftime("%H:%M"),
        "end_time": close_t.strftime("%H:%M"),
        "assumed": True,
        "note": f"未指定时间，默认按 {target.isoformat()} {start.strftime('%H:%M')}-"
                f"{close_t.strftime('%H:%M')} 处理（如需调整请直接说时间）。",
    }


def validate_window(date: str, start_time: str, end_time: str,
                    now: Optional[datetime] = None) -> Optional[str]:
    """校验预约窗口；合法返回 None，否则返回面向用户的中文原因。"""
    now = now or datetime.now().astimezone()
    try:
        d = date_cls.fromisoformat(date)
        st, et = _parse_hhmm(start_time), _parse_hhmm(end_time)
    except ValueError:
        return "日期或时间格式不对（日期 YYYY-MM-DD，时间 HH:MM，30 分钟步长）。"
    if st.minute % 30 != 0 or et.minute % 30 != 0:
        return "时间需按 30 分钟为步长（如 08:00、14:30）。"
    if et <= st:
        return "结束时间要晚于开始时间。"
    if d < now.date():
        return "不能预约过去的日期。"
    if d == now.date() and datetime.combine(d, st).astimezone() <= now:
        return "开始时间已经过去了，请选择更晚的时段或改约明天。"
    return None


# ---- 区域名 → location_id 的确定性解析（自然语言选座核心）------------------
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_FLOOR_RE = re.compile(r"([0-9]+|[一二两三四五六七八九十])\s*(?:楼|层|[fF])")
# 已知馆区/属性词库：命中即视为强信号（按需扩充，不求穷尽）。
_BUILDING_WORDS = ("南湖", "逸夫", "恒信", "中心馆", "老馆", "本部", "东区", "西区",
                   "佑铭", "元宝山", "综合楼", "文科", "理科")
_ATTR_WORDS = ("中庭", "开敞", "安静", "静音", "研读", "自习", "阅览", "咨询",
              "靠窗", "电源", "沙发", "讨论")


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _extract_floor(text: str) -> Optional[int]:
    m = _FLOOR_RE.search(text)
    if not m:
        if "首层" in text or "底楼" in text:
            return 1
        return None
    tok = m.group(1)
    return int(tok) if tok.isdigit() else _CN_NUM.get(tok)


def area_name(area: dict[str, Any]) -> str:
    """从分布返回的一个区域条目里尽量取出人类可读的区域名（字段名做防御性兜底）。"""
    for key in ("name", "area_name", "area", "title", "label", "location_name", "path_text"):
        v = area.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    path = area.get("path")
    if isinstance(path, list):
        joined = " / ".join(str(p) for p in path if p)
        if joined:
            return joined
    return str(area.get("location_id") or "未知区域")


def area_free(area: dict[str, Any]) -> Optional[int]:
    """区域剩余可用座位数（字段名兜底）；取不到返回 None。"""
    for key in ("free", "available", "free_count", "available_count", "remain",
                "remaining", "seats_available", "count"):
        v = area.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v)
    return None


def _score_area(query: str, name: str) -> int:
    """区域名与自然语言查询的匹配分（越大越匹配；楼层冲突给负分）。"""
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0
    score = 0
    for w in _BUILDING_WORDS:
        if w in q and w in n:
            score += 3
    qf, nf = _extract_floor(q), _extract_floor(n)
    if qf is not None and nf is not None:
        score += 4 if qf == nf else -4
    for w in _ATTR_WORDS:
        if w in q and w in n:
            score += 1
    if q in n or n in q:
        score += 2
    # 共享的 CJK 二元组（兜底相似度）。
    qb = {q[i:i + 2] for i in range(len(q) - 1)}
    nb = {n[i:i + 2] for i in range(len(n) - 1)}
    score += len(qb & nb)
    return score


def match_areas(areas: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """给 distribution 的区域列表按匹配度排序，返回带 score/name/free 的候选（纯函数，可测）。"""
    scored = []
    for a in areas:
        if not isinstance(a, dict):
            continue
        name = area_name(a)
        scored.append({"location_id": a.get("location_id"), "name": name,
                       "free": area_free(a), "score": _score_area(query, name), "raw": a})
    scored.sort(key=lambda x: (x["score"], (x["free"] or 0)), reverse=True)
    return scored


def _distribution_areas(res: dict[str, Any]) -> list[dict[str, Any]]:
    areas = res.get("distribution") or res.get("areas") or res.get("result") or []
    return [a for a in areas if isinstance(a, dict)]


def resolve_from_distribution(
    res: dict[str, Any], query: str, *, prefer_free: bool = True
) -> dict[str, Any]:
    """在一次分布查询结果里把自然语言区域名解析为唯一 location_id。

    返回 status ∈ {resolved, ambiguous, none_free, not_found}，附带 candidates（面向用户）。
    """
    areas = _distribution_areas(res)
    if not areas:
        return {"status": "not_found", "candidates": []}
    ranked = match_areas(areas, query)
    positive = [c for c in ranked if c["score"] > 0]
    if not positive:
        # 没有命中：把有空位的区域给用户挑（按空位降序）。
        alts = sorted(ranked, key=lambda x: (x["free"] or 0), reverse=True)
        return {"status": "not_found", "candidates": _slim(alts[:6])}

    top = positive[0]
    # 与次优分差 <=1 视为歧义（同馆同层多个细分区域）。
    rivals = [c for c in positive[1:] if top["score"] - c["score"] <= 1]
    if rivals:
        return {"status": "ambiguous", "candidates": _slim([top, *rivals][:6])}

    if prefer_free and (top["free"] == 0):
        free_alts = [c for c in ranked if (c["free"] or 0) > 0]
        return {"status": "none_free", "matched": _slim([top])[0],
                "candidates": _slim(free_alts[:6])}

    return {"status": "resolved", "location_id": top["location_id"],
            "area_name": top["name"], "free": top["free"]}


def _slim(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"location_id": c["location_id"], "name": c["name"], "free": c["free"]}
            for c in cands]


def slot_has_areas(dist_res: dict[str, Any]) -> bool:
    """该日该时段是否「有可约区域」（哪怕满员）。空 = 该时段不开放（闭馆/未放票）。"""
    return bool(dist_res.get("ok")) and bool(_distribution_areas(dist_res))


# 探测「下午是否开放」的固定采样时段（30 分钟小窗，比长时段更能反映是否闭馆）。
_AFTERNOON_PROBE = ("14:00", "14:30")


def afternoon_probe() -> tuple[str, str]:
    return _AFTERNOON_PROBE


def morning_probe() -> tuple[str, str]:
    """开馆后 30 分钟的小窗，用来判断上午是否开放。"""
    o = _parse_hhmm(getattr(config, "LIBRARY_OPEN_TIME", "08:00"))
    end = (datetime.combine(date_cls.today(), o) + timedelta(minutes=30)).time()
    return o.strftime("%H:%M"), end.strftime("%H:%M")


def clip_to_morning(start_time: str, end_time: str) -> Optional[tuple[str, str]]:
    """把时段裁到「上午」（开馆~半天闭馆点）。整段在下午则返回 None。"""
    try:
        s, e = _parse_hhmm(start_time), _parse_hhmm(end_time)
        open_t = _parse_hhmm(getattr(config, "LIBRARY_OPEN_TIME", "08:00"))
        close_t = _parse_hhmm(getattr(config, "LIBRARY_HALF_DAY_CLOSE_TIME", "12:00"))
    except ValueError:
        return None
    ns, ne = max(s, open_t), min(e, close_t)
    return (ns.strftime("%H:%M"), ne.strftime("%H:%M")) if ns < ne else None


# ---- 预约计划：放票时刻 + job 生成（库级，不依赖 ToolContext）----------------
def compute_run_after(target_date: str, now: Optional[datetime] = None) -> str:
    """把「目标日」换算成 job 该发起的时刻（ISO）。

    次日票 18:00 放、我们 18:02 抢（让位手动抢座）→ run_after = (目标日 - 放票提前天数) 的放票时刻；
    当天/已过放票时刻的直接取 now（当天随时可约）。
    """
    now = now or datetime.now().astimezone()
    try:
        d = date_cls.fromisoformat(target_date)
    except ValueError:
        return _iso(now)
    lead = getattr(config, "LIBRARY_RELEASE_LEAD_DAYS", 1)
    rel = _parse_hhmm(getattr(config, "LIBRARY_RELEASE_TIME", "18:02"))
    release = datetime.combine(d - timedelta(days=lead), rel).astimezone()
    return _iso(max(now, release))


def plan_dates(date_start: str, date_end: str, weekdays: Optional[list[int]],
               *, today: Optional[date_cls] = None) -> list[str]:
    """展开计划日期（按 weekdays 过滤），并 clamp 到 [今天, 今天+HORIZON]（配合滚动补齐）。"""
    try:
        start = date_cls.fromisoformat(date_start)
        end = date_cls.fromisoformat(date_end)
    except ValueError:
        return []
    today = today or date_cls.today()
    horizon = getattr(config, "LIBRARY_PLAN_HORIZON_DAYS", 62)
    start = max(start, today)
    end = min(end, today + timedelta(days=horizon))
    out: list[str] = []
    d = start
    while d <= end:
        if not weekdays or (d.isoweekday() in weekdays):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def generate_plan_jobs(db: Database, plan_id: str, plan: dict[str, Any]) -> int:
    """为计划生成 job（幂等，跳过已存在）。run_after 按放票规则错峰。返回新建数量。

    休馆/寒暑假不在这里判定——那要到放票时刻查「实际可预约时段」才知道（见 scheduler 执行期处理）。
    """
    weekdays = repo._loads(plan.get("weekdays"), None)
    time_slots = repo._loads(plan.get("time_slots"), [])
    preferred = repo._loads(plan.get("preferred_locations"), []) or [None]
    count = 0
    for d in plan_dates(plan["date_start"], plan["date_end"], weekdays):
        for slot in time_slots:
            start_time, _, end_time = str(slot).partition("-")
            start_time, end_time = start_time.strip(), end_time.strip()
            if repo.job_exists(db, plan_id=plan_id, target_date=d, start_time=start_time):
                continue
            repo.create_job(
                db, plan_id=plan_id, person_id=plan["person_id"], user_key=plan["user_key"],
                target_date=d, start_time=start_time, end_time=end_time,
                location_id=(preferred[0] if preferred else None),
                area_filter=plan.get("default_area_filter"), strategy=plan["strategy"],
                run_after=compute_run_after(d),
            )
            count += 1
    return count


def regenerate_active_plans(db: Database, *, today: Optional[date_cls] = None) -> dict[str, int]:
    """滚动补齐所有 active 计划的 job；已过 date_end 的计划标记 completed。供调度器周期调用。"""
    today = today or date_cls.today()
    generated = completed = 0
    for plan in repo.active_plans(db):
        try:
            end = date_cls.fromisoformat(plan["date_end"])
        except (ValueError, TypeError):
            end = today
        if end < today:
            repo.set_plan_status(db, plan["id"], "completed")
            completed += 1
            continue
        generated += generate_plan_jobs(db, plan["id"], plan)
    return {"generated": generated, "completed": completed}


def in_building_from_door_log(res: dict[str, Any]) -> Optional[bool]:
    """从 ``get_door_log`` 结果推断当前是否在馆。

    优先用服务端给的 ``in_building``；缺省时按首条（最新）闸机事件方向推断：
    ``in``/raw_direction 0 = 在馆，``out``/raw_direction 1 = 已离馆。
    无法判断时返回 ``None``（未知，调用方不应据此改状态）。
    """
    if not isinstance(res, dict):
        return None
    if isinstance(res.get("in_building"), bool):
        return res["in_building"]
    events = res.get("events") or []
    if not events or not isinstance(events[0], dict):
        return None
    first = events[0]
    direction = str(first.get("direction") or "").lower()
    if direction == "in":
        return True
    if direction == "out":
        return False
    raw = first.get("raw_direction")
    if raw == 0:
        return True
    if raw == 1:
        return False
    return None


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


# ---- 预约回执 --------------------------------------------------------------
def _fmt_time(iso: Optional[str]) -> Optional[str]:
    dt = parse_dt(iso)
    return dt.strftime("%H:%M") if dt else None


def reservation_receipt(
    *, seat_no: Any = None, location_path: Any = None, date: Optional[str] = None,
    start_time: Optional[str] = None, end_time: Optional[str] = None,
    sign_in_deadline: Optional[str] = None, seat_name: Optional[str] = None,
    prefix: str = "预约成功 ✅",
) -> str:
    """统一的预约回执：区域、座位（图）编号、日期时段、签到截止。手动/定时预约共用。"""
    where = location_path
    if isinstance(where, list):
        where = " / ".join(str(p) for p in where if p)
    lines = [prefix]
    loc = f"📍 {where}" if where else ""
    if seat_no:
        loc += (f"  座位 {seat_no}" if loc else f"📍 座位 {seat_no}")
    if seat_name and str(seat_name) != str(seat_no):
        loc += f"（{seat_name}）"
    if loc:
        lines.append(loc)
    if date:
        span = f"{start_time or ''}-{end_time or ''}".strip("-")
        lines.append(f"🗓 {date} {span}".rstrip())
    dl = _fmt_time(sign_in_deadline)
    if dl:
        lines.append(f"⏰ 签到截止 {dl}（记得按时签到）")
    return "\n".join(lines)


# ---- 爽约/暂离保护：时机判断 + 回复归类 ------------------------------------
def within_ask_lead(target_iso: Optional[str], now: Optional[datetime] = None) -> bool:
    """是否已进入「该问用户了」的时间窗（距截止 <= ASK_LEAD 分钟）。"""
    now = now or datetime.now().astimezone()
    dt = parse_dt(target_iso)
    if dt is None:
        return False
    lead = getattr(config, "LIBRARY_PROTECT_ASK_LEAD_MINUTES", 15)
    return now >= dt - timedelta(minutes=lead)


def protect_safety_deadline(target_iso: Optional[str]) -> Optional[str]:
    """无回复时自动执行保护动作的时刻（截止前留 SAFETY 分钟操作余量）。"""
    dt = parse_dt(target_iso)
    if dt is None:
        return None
    safety = getattr(config, "LIBRARY_PROTECT_SAFETY_MINUTES", 3)
    return _iso(dt - timedelta(minutes=safety))


_PROTECT_KEEP_NEG = ("别取消", "不要取消", "不用取消", "先别取消", "不取消", "别退", "不要退")
_PROTECT_RELEASE = ("取消", "不去了", "不去", "退了", "退座", "算了", "放弃", "不要了",
                    "别留", "走了", "不来了", "来不了", "去不了", "不用了")
_PROTECT_KEEP = ("保留", "继续", "我会去", "马上到", "在路上", "去的", "要去", "会去",
                 "去呀", "这就去", "留着", "签到", "我去")


def classify_protection_reply(text: str) -> str:
    """把用户对保护提示的回复判为 keep（保留）/ release（提前取消）/ unclear。"""
    t = (text or "").strip()
    if not t:
        return "unclear"
    for w in _PROTECT_KEEP_NEG:  # 「不要取消」这类否定优先，避免被 release 命中
        if w in t:
            return "keep"
    for w in _PROTECT_RELEASE:
        if w in t:
            return "release"
    for w in _PROTECT_KEEP:
        if w in t:
            return "keep"
    if t in ("去", "在", "到", "嗯", "好", "好的"):
        return "keep"
    return "unclear"


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
