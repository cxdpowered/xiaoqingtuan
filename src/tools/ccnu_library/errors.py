"""远程错误码 → 中文解释 + 重试策略（需求文档 §9.4）。

远程 MCP 失败时返回 ``ok=false`` + ``code`` + ``message``。本模块把 ``code`` 翻译成
对用户友好的中文，并给调度器一个统一的重试决策。
"""

from __future__ import annotations

from typing import Literal

# 重试动作：
#   none      —— 直接失败，不重试
#   fallback  —— 尝试 fallback 区域 / 时段
#   login     —— 转登录/challenge 流程
#   challenge —— 挂起等待用户输入验证码
#   retry     —— 有限次重试（连接类抖动）
RetryAction = Literal["none", "fallback", "login", "challenge", "retry"]

_ERROR_ZH: dict[str, str] = {
    "OK": "成功",
    "NEED_LOGIN": "登录态失效，需要重新登录",
    "NEED_CHALLENGE": "需要验证码，请按提示输入",
    "CHALLENGE_PENDING": "有待输入的验证码，请先回复之前发送的验证码图片中的字符",
    "NO_SEAT": "该区域/时段已无可用座位",
    "BAD_TIME": "时间参数不合法（需 30 分钟步长，且开始早于结束）",
    "RESERVE_FAILED": "预约提交失败",
    "ALREADY_RESERVED": "已有进行中的预约，无法重复预约",
    "NOT_FOUND": "未找到对应的预约",
    "ACCOUNT_INVALID": "账号或密码无效",
    "RATE_LIMITED": "操作过于频繁，请稍后再试",
    "MCP_UNAVAILABLE": "图书馆服务暂时不可达",
    "UNKNOWN": "未知错误",
}

_RETRY: dict[str, RetryAction] = {
    "NEED_LOGIN": "login",
    "NEED_CHALLENGE": "challenge",
    "NO_SEAT": "fallback",
    "BAD_TIME": "none",
    "RESERVE_FAILED": "retry",
    "ALREADY_RESERVED": "none",
    "ACCOUNT_INVALID": "none",
    "NOT_FOUND": "none",
    "RATE_LIMITED": "retry",
    "MCP_UNAVAILABLE": "retry",
}

# RESERVE_FAILED / 连接类的有限重试上限。
MAX_RETRY_ATTEMPTS = 3


def explain(code: str | None, message: str | None = None) -> str:
    """把错误码翻译成中文；带上 server 原始 message 便于排查。"""
    code = (code or "UNKNOWN").upper()
    zh = _ERROR_ZH.get(code, _ERROR_ZH["UNKNOWN"])
    if message and message not in zh:
        return f"{zh}（{message}）"
    return zh


def retry_action(code: str | None) -> RetryAction:
    return _RETRY.get((code or "").upper(), "none")
