"""微信（WxClaw）接入层 → 标准化消息。"""

from __future__ import annotations

from typing import Any

from src.channels.base import InboundMessage


def standardize(bot: Any, event: Any) -> InboundMessage:
    """把 WxClaw 事件转成 :class:`InboundMessage`。"""
    try:
        session_id = event.get_session_id()
    except Exception:
        session_id = str(getattr(event, "user_id", "unknown"))
    try:
        text = event.get_message().extract_plain_text().strip()
    except Exception:
        text = str(getattr(event, "get_plaintext", lambda: "")()).strip()

    wx_id = str(getattr(event, "user_id", "unknown"))
    return InboundMessage(
        channel="wechat",
        session_id=f"wechat:{session_id}",
        user_id=f"user:{wx_id}",
        account_id=wx_id,
        text=text,
        message_id=str(getattr(event, "message_id", "")),
        raw=(bot, event),
    )
