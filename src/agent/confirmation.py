"""人工确认（架构 5.2 confirm_gate + 跨轮恢复）。

高风险工具被 confirm_gate 拦截后，把待执行调用挂到会话级 pending 区，
并向用户发出确认请求；用户下一条消息若为肯定/否定，则恢复执行或取消。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

from src.storage.db import get_db, new_id

_AFFIRM = {"确认", "确定", "是", "好", "好的", "可以", "yes", "y", "ok", "嗯", "对", "执行", "继续"}
_DENY = {"取消", "不", "不要", "否", "算了", "no", "n", "停", "放弃"}


@dataclass
class PendingConfirmation:
    confirmation_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    prompt: str


class ConfirmationManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingConfirmation] = {}  # session_id -> pending

    def create(
        self, session_id: str, tool_name: str, arguments: dict[str, Any], prompt: str
    ) -> PendingConfirmation:
        pc = PendingConfirmation(
            confirmation_id=new_id("confirm_"),
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
            prompt=prompt,
        )
        with self._lock:
            self._pending[session_id] = pc
        get_db().add_event(
            session_id=session_id,
            role="assistant",
            content=prompt,
            event_type="confirmation_request",
            metadata={"confirmation_id": pc.confirmation_id, "tool": tool_name, "arguments": arguments},
        )
        return pc

    def get(self, session_id: str) -> Optional[PendingConfirmation]:
        with self._lock:
            return self._pending.get(session_id)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._pending.pop(session_id, None)

    @staticmethod
    def classify(text: str) -> str:
        """把用户回复判定为 affirm / deny / unclear。"""
        t = text.strip().lower().lstrip("/").strip("。.! ！")
        if t in _AFFIRM:
            return "affirm"
        if t in _DENY:
            return "deny"
        return "unclear"


_manager: Optional[ConfirmationManager] = None


def get_manager() -> ConfirmationManager:
    global _manager
    if _manager is None:
        _manager = ConfirmationManager()
    return _manager
