"""CCNU 图书馆功能组的状态常量与 wrapper 工具参数 schema。

参数 schema 用 Pydantic（与项目其他工具一致，交给 LangChain 生成 function-calling
schema）。状态字符串集中在此，避免散落在各处拼错。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# ---- 预约策略（远程 reserve_seat.strategy）---------------------------------
STRATEGIES = ("exact_seat", "first_available", "random_available", "favorite_first")
DEFAULT_STRATEGY = "favorite_first"

# ---- 当前预约状态（远程 get_current_reservation.status）---------------------
# 完整枚举（mcp-ccnu-lib 更新后）：新增 in_use / ended / violation。
RESERVATION_STATUSES = (
    "none", "reserved", "waiting_sign_in", "in_use", "away",
    "ended", "cancelled", "violation", "violation_risk", "unknown",
)

# 「仍有活跃预约」的状态：判断「有没有当前预约」只看 status（不看 seat_no）。
# 注意 violation_risk 是进行中的违约风险，仍算活跃；violation 是已发生的违约（终态）。
ACTIVE_RESERVATION_STATUSES = frozenset(
    {"reserved", "waiting_sign_in", "in_use", "away", "violation_risk"}
)
# 终态 / 无活跃预约的状态。
INACTIVE_RESERVATION_STATUSES = frozenset(
    {"none", "ended", "cancelled", "violation", "unknown"}
)

# 状态 → 面向用户的中文标签与一句话说明。
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "none": ("无预约", "当前没有有效预约"),
    "reserved": ("已预约", "已预约但还没到签到时间"),
    "waiting_sign_in": ("待签到", "在签到窗口内，请尽快到馆签到，否则会被释放"),
    "in_use": ("使用中", "已签到，正在使用座位"),
    "away": ("暂离中", "已发起暂离，请在截止前回座"),
    "ended": ("已结束", "已正常用完 / 主动结束 / 提前退座"),
    "cancelled": ("已取消", "预约已取消"),
    "violation": ("已违约", "爽约未签到或暂离超时未归，本次预约已违约释放"),
    "violation_risk": ("违约风险", "存在进行中的违约风险，请及时处理"),
    "unknown": ("状态未知", "未能识别的预约状态"),
}


def describe_status(status: Optional[str]) -> tuple[str, str]:
    """状态字符串 → (中文标签, 一句话说明)。未知状态回落到 unknown。"""
    key = (status or "unknown").lower()
    return STATUS_LABELS.get(key, STATUS_LABELS["unknown"])


def is_active_status(status: Optional[str]) -> bool:
    """是否仍有活跃预约（判断只依据 status，绝不依据 seat_no 是否存在）。"""
    return (status or "").lower() in ACTIVE_RESERVATION_STATUSES

# ---- 本地表状态 ------------------------------------------------------------
PLAN_STATUSES = ("draft", "active", "paused", "completed", "cancelled")
JOB_STATUSES = (
    "pending", "running", "waiting_challenge", "waiting_confirmation",
    "success", "failed", "skipped", "cancelled",
)
CHALLENGE_STATUSES = ("pending", "awaiting_answer", "submitted", "resolved", "expired", "failed")
NOTIFICATION_STATUSES = ("pending", "sent", "cancelled", "failed")
NOTIFICATION_KINDS = (
    "booking_result", "signin_reminder", "away_reminder", "challenge",
    "auto_cancel_result", "auto_end_result", "sync_warning",
)

# 远程 challenge_type
CHALLENGE_CAPTCHA = "captcha"
CHALLENGE_SMS = "sms"
CHALLENGE_CONFIRM_SMS = "confirm_send_sms"
CHALLENGE_MANUAL = "manual_login"
# 当前仅图形验证码可完整续跑；其余为预留能力。
SUPPORTED_CHALLENGE_TYPES = (CHALLENGE_CAPTCHA,)


# ---- wrapper 工具参数 schema ----------------------------------------------
class SaveAccountArgs(BaseModel):
    username: Optional[str] = Field(default=None, description="图书馆/统一认证学号，缺省时回落 MCP 默认账号")
    password: Optional[str] = Field(default=None, description="密码，缺省时回落 MCP 默认账号")
    phone_hint: Optional[str] = Field(default=None, description="手机号尾号提示（短信验证码预留用）")
    login_now: bool = Field(default=True, description="保存后是否立即尝试登录")


class LoginArgs(BaseModel):
    pass


class QueryAvailabilityArgs(BaseModel):
    date: str = Field(description="日期 YYYY-MM-DD")
    start_time: str = Field(description="开始时间 HH:MM，步长 30 分钟，范围约 07:30-22:00")
    end_time: str = Field(description="结束时间 HH:MM，须晚于 start_time")
    library: Optional[str] = Field(default=None, description="馆名，可选")
    area_filter: Optional[str] = Field(default=None, description="区域过滤词，如『安静区』")


class ListSeatsArgs(BaseModel):
    date: str = Field(description="日期 YYYY-MM-DD")
    start_time: str = Field(description="开始时间 HH:MM")
    end_time: str = Field(description="结束时间 HH:MM")
    location_id: str = Field(description="区域 ID（来自可用分布查询）")
    area_filter: Optional[str] = Field(default=None, description="区域过滤词，可选")
    limit: Optional[int] = Field(default=None, description="最多返回多少个座位")


class ReserveArgs(BaseModel):
    date: Optional[str] = Field(default=None, description="日期 YYYY-MM-DD；缺省默认今天（已闭馆则明天）")
    start_time: Optional[str] = Field(default=None, description="开始时间 HH:MM；缺省默认此刻/开馆")
    end_time: Optional[str] = Field(default=None, description="结束时间 HH:MM；缺省默认闭馆时间")
    location_query: Optional[str] = Field(
        default=None,
        description="自然语言区域描述，如『南湖一楼』『南湖分馆一楼中庭』；有 location_id 时可不填，"
                    "否则用它自动解析到 location_id。",
    )
    seat_id: Optional[str] = Field(default=None, description="exact_seat 策略必填")
    location_id: Optional[str] = Field(
        default=None, description="非 exact_seat 策略必填的区域 ID（来自 library_find_area / 分布查询）")
    strategy: Optional[str] = Field(
        default=None,
        description="exact_seat | first_available | random_available | favorite_first；缺省用用户默认策略",
    )


class FindAreaArgs(BaseModel):
    location_query: str = Field(
        description="自然语言区域描述，如『南湖一楼』『南湖分馆一楼中庭开敞区』")
    date: Optional[str] = Field(default=None, description="日期 YYYY-MM-DD；缺省默认今天（已闭馆则明天）")
    start_time: Optional[str] = Field(default=None, description="开始时间 HH:MM；缺省默认此刻/开馆")
    end_time: Optional[str] = Field(default=None, description="结束时间 HH:MM；缺省默认闭馆时间")


class ReservationIdArgs(BaseModel):
    reservation_id: Optional[str] = Field(default=None, description="预约 ID，缺省时操作当前预约")


class CurrentArgs(BaseModel):
    pass


class CreateBookingPlanArgs(BaseModel):
    date_start: str = Field(description="计划起始日期 YYYY-MM-DD")
    date_end: str = Field(description="计划结束日期 YYYY-MM-DD")
    time_slots: list[str] = Field(
        description="每日时段，元素形如『08:00-12:00』，至少一个",
    )
    title: Optional[str] = Field(default=None, description="计划标题，可选")
    weekdays: Optional[list[int]] = Field(
        default=None, description="生效星期，1=周一…7=周日；缺省表示每天",
    )
    preferred_locations: Optional[list[str]] = Field(
        default=None, description="优先区域 location_id 列表",
    )
    fallback_locations: Optional[list[str]] = Field(
        default=None, description="备选区域 location_id 列表（优先区域无座时尝试）",
    )
    fallback_time_slots: Optional[list[str]] = Field(
        default=None, description="备选时段（优先时段无座时尝试）",
    )
    strategy: Optional[str] = Field(default=None, description="选座策略，缺省用用户默认策略")
    auto_cancel_if_cannot_signin: bool = Field(
        default=False, description="签到风险前自动取消（计划级授权）",
    )
    auto_end_if_away_timeout: bool = Field(
        default=False, description="暂离超时前自动退座（计划级授权）",
    )
    require_confirmation_each_booking: bool = Field(
        default=False, description="每次自动预约前仍要用户确认",
    )


class PlanIdArgs(BaseModel):
    plan_id: str = Field(description="预约计划 ID")


class CancelBookingPlanArgs(BaseModel):
    plan_id: Optional[str] = Field(
        default=None,
        description="要取消的预约计划 ID；缺省时若只有一个进行中的计划就取消它，多个会让你选。")
    all_plans: bool = Field(default=False, description="取消我全部进行中的连续预约计划")


class ListPlansArgs(BaseModel):
    pass


class SeatLayoutArgs(BaseModel):
    location_id: str = Field(description="区域 ID（来自可用分布查询的 location_id）")
    power_only: bool = Field(
        default=False, description="是否只返回带电源的座位（辅助选座用）",
    )
    limit: Optional[int] = Field(default=None, description="最多返回多少个座位，缺省全部")


class DoorLogArgs(BaseModel):
    date: Optional[str] = Field(
        default=None, description="日期 YYYY-MM-DD，缺省为今天",
    )


class PagedArgs(BaseModel):
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数")
