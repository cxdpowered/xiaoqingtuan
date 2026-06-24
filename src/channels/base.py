"""标准化的进出消息模型（架构 5.1）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class InboundMessage:
    """从任意渠道标准化后的入站消息。"""

    channel: str  # qq | wechat | cli
    session_id: str  # 例如 "group:123456" / "private:789"
    user_id: str  # 例如 "user:789"
    text: str
    account_id: str = ""  # 平台原始账号号（QQ 号 / wxid / CLI 用户），身份解析用
    message_id: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    raw: Any = None  # 原始平台对象，便于回复时定位

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "text": self.text,
            "attachments": self.attachments,
            "created_at": self.created_at,
        }


@dataclass
class OutboundMessage:
    """Agent 产出的标准化回复。"""

    reply_text: str
    requires_confirmation: bool = False
    confirmation_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply_text": self.reply_text,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_id": self.confirmation_id,
        }
