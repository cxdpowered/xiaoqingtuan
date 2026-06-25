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
RESERVATION_STATUSES = (
    "none", "reserved", "waiting_sign_in", "in_use", "away",
    "ended", "cancelled", "violation_risk", "unknown",
)

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
    date: str = Field(description="日期 YYYY-MM-DD")
    start_time: str = Field(description="开始时间 HH:MM")
    end_time: str = Field(description="结束时间 HH:MM")
    seat_id: Optional[str] = Field(default=None, description="exact_seat 策略必填")
    location_id: Optional[str] = Field(default=None, description="非 exact_seat 策略必填")
    strategy: Optional[str] = Field(
        default=None,
        description="exact_seat | first_available | random_available | favorite_first；缺省用用户默认策略",
    )


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


class ListPlansArgs(BaseModel):
    pass
