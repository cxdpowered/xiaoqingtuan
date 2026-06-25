"""图书馆座位预约工具（架构第 8 节，本期只留接口 + mock）。

- ``LibraryProvider`` 抽象基类：``query_seats`` / ``create_reservation`` / ``cancel_reservation``。
- 两个工具：``library_seat_query``（低风险）、``library_reservation_create``（高风险，走 confirm_gate）。
- 实现体先返回 mock 数据，便于打通主流程；真实 provider（Playwright）留作阶段三。

失败处理原则：遇验证码/登录失效一律转人工，不绕过、不抢座。
"""

from __future__ import annotations

import abc
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.storage.db import Database
from src.tools.registry import ToolContext, context_tool


# ---- Pydantic 输入/输出模型 ---------------------------------------------
class SeatQueryArgs(BaseModel):
    date: str = Field(description="预约日期，格式 YYYY-MM-DD")
    start_time: str = Field(default="08:00", description="开始时间 HH:MM")
    end_time: str = Field(default="22:00", description="结束时间 HH:MM")
    library: Optional[str] = Field(default=None, description="馆名，如『总馆』")
    area: Optional[str] = Field(default=None, description="馆区/楼层偏好，如『二楼安静区』")


class ReservationCreateArgs(BaseModel):
    date: str = Field(description="预约日期 YYYY-MM-DD")
    start_time: str = Field(description="开始时间 HH:MM")
    end_time: str = Field(description="结束时间 HH:MM")
    seat: str = Field(description="座位号，如 312")
    library: Optional[str] = Field(default=None)
    area: Optional[str] = Field(default=None)


class Seat(BaseModel):
    library: str
    area: str
    seat: str
    available: bool = True


# ---- Provider 抽象 -------------------------------------------------------
class LibraryProvider(abc.ABC):
    """图书馆系统适配抽象。确认学校系统后实现具体子类（Playwright）。"""

    @abc.abstractmethod
    async def query_seats(self, args: SeatQueryArgs) -> list[Seat]:
        ...

    @abc.abstractmethod
    async def create_reservation(self, args: ReservationCreateArgs) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    async def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        ...


class MockLibraryProvider(LibraryProvider):
    """占位实现：返回固定 mock 座位，用于打通主流程。"""

    async def query_seats(self, args: SeatQueryArgs) -> list[Seat]:
        area = args.area or "二楼安静区"
        library = args.library or "总馆"
        return [
            Seat(library=library, area=area, seat="312"),
            Seat(library=library, area=area, seat="315"),
            Seat(library=library, area="三楼自习区", seat="508"),
        ]

    async def create_reservation(self, args: ReservationCreateArgs) -> dict[str, Any]:
        # 真实实现遇验证码/登录失效需转人工；这里直接 mock 成功。
        return {
            "ok": True,
            "confirmation_code": "MOCK-RES-0001",
            "detail": f"{args.date} {args.start_time}-{args.end_time} 座位 {args.seat}（mock）",
        }

    async def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        return {"ok": True, "reservation_id": reservation_id}


class MCPLibraryProvider(LibraryProvider):
    """把图书馆预约委托给一个**独立的 MCP server**（扩展点，尚未启用）。

    设计动机：图书馆系统与学校强耦合、含登录态/验证码/Playwright，体积大且脆弱，
    适合拆成独立进程/容器，通过 MCP 暴露 ``query_seats`` / ``create_reservation`` /
    ``cancel_reservation`` 三个工具。Agent 侧只需实现本 provider，主流程与
    ``LibraryProvider`` 抽象不变——确认/落库/审计仍由本仓库 ``library_reservation_create`` 负责。

    远程 server 约定（建议）：工具名与本类方法同名，入参/出参对齐
    :class:`SeatQueryArgs` / :class:`ReservationCreateArgs` / :class:`Seat` 的 JSON 形态。

    实现走 :mod:`src.tools.mcp_bridge` 的会话层；本期仅留骨架。
    """

    def __init__(self, server_name: str = "library") -> None:
        self.server_name = server_name

    async def _session(self) -> Any:
        from src.tools.mcp_bridge import MCPServerConfig, _get_session

        return await _get_session(MCPServerConfig(name=self.server_name))

    async def query_seats(self, args: SeatQueryArgs) -> list[Seat]:
        session = await self._session()
        res = await session.call_tool("query_seats", args.model_dump())  # type: ignore[attr-defined]
        return [Seat.model_validate(s) for s in (res or {}).get("seats", [])]

    async def create_reservation(self, args: ReservationCreateArgs) -> dict[str, Any]:
        session = await self._session()
        return await session.call_tool("create_reservation", args.model_dump())  # type: ignore[attr-defined]

    async def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        session = await self._session()
        return await session.call_tool("cancel_reservation", {"reservation_id": reservation_id})  # type: ignore[attr-defined]


def get_provider() -> LibraryProvider:
    """按配置选择图书馆后端：``mock``（默认）| ``mcp``（独立服务）| ``playwright``（阶段三）。

    用环境变量 ``XQT_LIBRARY_PROVIDER`` 切换，便于在不改代码的前提下接入真实后端。
    """
    from src import config as _cfg

    kind = getattr(_cfg, "LIBRARY_PROVIDER", "mock")
    if kind == "mcp":
        return MCPLibraryProvider(server_name=getattr(_cfg, "LIBRARY_MCP_SERVER", "library"))
    # "playwright" 子类留作阶段三；未知值回退 mock。
    return MockLibraryProvider()


_provider: LibraryProvider = get_provider()


def set_provider(provider: LibraryProvider) -> None:
    global _provider
    _provider = provider


# ---- 工具实现 ------------------------------------------------------------
async def library_seat_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = SeatQueryArgs.model_validate(args)
    seats = await _provider.query_seats(query)
    return {
        "date": query.date,
        "start_time": query.start_time,
        "end_time": query.end_time,
        "seats": [s.model_dump() for s in seats],
    }


async def library_reservation_create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    req = ReservationCreateArgs.model_validate(args)
    db: Database = ctx.db
    # 先落一条 planned 流水，再调用 provider。
    res_id = db.add_reservation(
        date=req.date,
        start_time=req.start_time,
        end_time=req.end_time,
        status="planned",
        library=req.library,
        area=req.area,
        seat=req.seat,
        source_event_id=ctx.event_id,
    )
    try:
        result = await _provider.create_reservation(req)
    except NotImplementedError:
        db.update_reservation_status(res_id, "failed")
        return {"ok": False, "reservation_id": res_id, "error": "真实图书馆 provider 未实现（阶段三）"}
    except Exception as exc:  # noqa: BLE001
        db.update_reservation_status(res_id, "failed")
        return {"ok": False, "reservation_id": res_id, "error": str(exc)}

    db.update_reservation_status(res_id, "success" if result.get("ok") else "failed")
    return {"reservation_id": res_id, **result}


TOOLS = [
    context_tool(
        name="library_seat_query",
        description="查询某天某时段图书馆的空闲座位（返回候选座位列表）。",
        args_schema=SeatQueryArgs,
        func=library_seat_query,
        high_risk=False,
    ),
    context_tool(
        name="library_reservation_create",
        description="提交图书馆座位预约（高风险操作，提交前必须经用户二次确认）。",
        args_schema=ReservationCreateArgs,
        func=library_reservation_create,
        high_risk=True,
    ),
]
