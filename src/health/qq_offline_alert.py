"""QQ（OneBot v11）掉线邮件告警。

**为什么走邮件**：现有 `library_notifications` 通知靠 QQ Bot 自己 ``send_msg`` 投递，
QQ 一掉线就发不出来。所以「QQ 掉线」这件事必须用**带外通道**通知——这里用 SMTP 发邮件。

**怎么判定掉线**：小青团容器即使 QQ 掉线也照常运行，用 NoneBot 的 ``get_bots()`` 即可
判断有没有 OneBot Bot 连着。连续掉线超过 :data:`config.ALERT_OFFLINE_THRESHOLD_SECONDS`
才发一封告警；恢复后再发一封「已恢复」。带去重，一次掉线只发一封，不刷屏。

**不自建轮询**：挂到 NoneBot 共享 APScheduler 上（见 ``bot.py`` 启动钩子）。
状态在内存即可——重启后从「未告警」开始重新计时，符合预期（重启本身就要重新登录）。
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Optional

import nonebot

from src import config

_log = logging.getLogger(__name__)

# 内存状态：掉线起始时刻 + 是否已就本次掉线发过告警。
_state: dict[str, Any] = {"down_since": None, "alerted": False}


# 只认 QQ（OneBot v11）适配器。本项目还挂了 WxClaw（微信）适配器，
# get_bots() 会把两个适配器的 Bot 都返回；若按「有任意 Bot」判断，微信在线时
# 就会把 QQ 掉线也算成在线，导致告警永远不触发（本 bug 的根因）。
_QQ_ADAPTER_NAME = "OneBot V11"


def _bot_online() -> bool:
    """是否有 QQ（OneBot v11）Bot 当前连着。只看 QQ，不把微信等其它适配器算进来。"""
    try:
        for bot in nonebot.get_bots().values():
            try:
                if bot.adapter.get_name() == _QQ_ADAPTER_NAME:
                    return True
            except Exception:  # noqa: BLE001 — 个别 Bot 取适配器失败不影响整体判断
                continue
        return False
    except Exception:  # noqa: BLE001 — 框架未就绪时按掉线处理
        return False


def _send_email(subject: str, body: str) -> None:
    """同步发送一封纯文本邮件（在线程里调用，避免阻塞事件循环）。"""
    msg = EmailMessage()
    msg["From"] = formataddr(("小青团告警", config.ALERT_SMTP_USER))
    msg["To"] = config.ALERT_MAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(config.ALERT_SMTP_HOST, config.ALERT_SMTP_PORT,
                          context=ctx, timeout=20) as smtp:
        smtp.login(config.ALERT_SMTP_USER, config.ALERT_SMTP_PASS)
        smtp.send_message(msg)


async def _email(subject: str, body: str) -> bool:
    try:
        await asyncio.to_thread(_send_email, subject, body)
        _log.info("已发送告警邮件：%s", subject)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("告警邮件发送失败：%s", exc)
        return False


def _fmt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "未知"
    # 转成北京时间展示，方便阅读。
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


async def check_once() -> None:
    """周期检查 QQ 是否在线；掉线超阈值发告警，恢复发恢复通知。幂等、可重入。"""
    now = datetime.now(timezone.utc)

    if _bot_online():
        if _state["alerted"]:
            down_since = _state["down_since"]
            mins = int((now - down_since).total_seconds() // 60) if down_since else 0
            await _email(
                "✅ 小青团 QQ 已恢复在线",
                f"QQ Bot 已重新连接，恢复时间：{_fmt(now)}。\n"
                f"本次掉线约 {mins} 分钟（约 {_fmt(down_since)} 起）。",
            )
        _state["down_since"] = None
        _state["alerted"] = False
        return

    # —— 掉线中 ——
    if _state["down_since"] is None:
        _state["down_since"] = now
        _log.info("检测到 QQ 掉线，开始计时（阈值 %ds）。", config.ALERT_OFFLINE_THRESHOLD_SECONDS)
        return

    elapsed = (now - _state["down_since"]).total_seconds()
    if not _state["alerted"] and elapsed >= config.ALERT_OFFLINE_THRESHOLD_SECONDS:
        mins = int(elapsed // 60)
        sent = await _email(
            "⚠️ 小青团 QQ 掉线了，请扫码重新登录",
            f"QQ Bot 已连续掉线约 {mins} 分钟（约 {_fmt(_state['down_since'])} 起），"
            f"期间无法收发 QQ 消息。\n\n"
            f"处理：打开 WebUI http://localhost:3080 用手机 QQ 扫码重新登录；\n"
            f"或查看 `docker logs xqt-llbot` 里的登录二维码。\n"
            f"登录后小青团会自动重连，恢复时你会再收到一封「已恢复」邮件。",
        )
        # 发送成功才置位，失败则下次检查继续重试。
        _state["alerted"] = sent


def setup(scheduler: Any) -> None:
    """把检查任务注册到 APScheduler（interval job）。幂等；未配置则不注册。"""
    if not config.alert_enabled():
        _log.info("未配置 SMTP 发件账号/授权码，QQ 掉线邮件告警未启用。")
        return
    if scheduler.get_job("qq_offline_alert"):
        return
    scheduler.add_job(
        check_once, "interval",
        seconds=config.ALERT_CHECK_INTERVAL_SECONDS,
        id="qq_offline_alert", max_instances=1, coalesce=True,
    )
    _log.info(
        "已启用 QQ 掉线邮件告警：每 %ds 检查，连续掉线 %ds 后发邮件至 %s。",
        config.ALERT_CHECK_INTERVAL_SECONDS,
        config.ALERT_OFFLINE_THRESHOLD_SECONDS,
        config.ALERT_MAIL_TO,
    )
