"""channel_gateway：统一 QQ / 微信 / CLI 入口的消息标准化（架构 5.1）。

Agent Core 只认 :class:`InboundMessage` / :class:`OutboundMessage`，
不依赖具体聊天框架；NoneBot 降级为接入层。
"""

from src.channels.base import InboundMessage, OutboundMessage

__all__ = ["InboundMessage", "OutboundMessage"]
