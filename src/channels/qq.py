"""QQ（OneBot V11）接入层 → 标准化消息。"""

from __future__ import annotations

from typing import Any

from src.channels.base import InboundMessage


def standardize(bot: Any, event: Any) -> InboundMessage:
    """把 OneBot V11 事件转成 :class:`InboundMessage`。"""
    try:
        session_id = event.get_session_id()
    except Exception:
        session_id = str(getattr(event, "user_id", "unknown"))
    try:
        text = event.get_message().extract_plain_text().strip()
    except Exception:
        text = str(getattr(event, "raw_message", "")).strip()

    qq_number = str(getattr(event, "user_id", "unknown"))
    return InboundMessage(
        channel="qq",
        session_id=f"qq:{session_id}",
        user_id=f"user:{qq_number}",
        account_id=qq_number,
        text=text,
        message_id=str(getattr(event, "message_id", "")),
        raw=(bot, event),
    )
