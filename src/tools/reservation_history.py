"""reservation_history 工具：查询历史图书馆预约（架构 5.3）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.tools.registry import Tool, ToolContext


class ReservationHistoryArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限")


async def reservation_history(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    limit = args.get("limit", 10)
    rows = ctx.db.list_reservations(limit=limit)
    items = [
        {
            "id": r["id"],
            "library": r["library"],
            "area": r["area"],
            "seat": r["seat"],
            "date": r["date"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"reservations": items, "count": len(items)}


TOOLS = [
    Tool(
        name="reservation_history",
        description="查询本人历史图书馆预约记录及状态。",
        args_model=ReservationHistoryArgs,
        func=reservation_history,
        high_risk=False,
    )
]
