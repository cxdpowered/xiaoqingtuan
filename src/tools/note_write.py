"""note_write 工具：记录用户偏好 / 事项 / 笔记（架构 5.3）。

写入走 Markdown 写入器（last-write-wins + hash 防撞车），随后重建该文件的派生索引。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from src.tools.registry import ToolContext, context_tool


class NoteWriteArgs(BaseModel):
    content: str = Field(description="要记录的事实/偏好/笔记内容（一句话）")
    memory_type: str = Field(
        default="preference",
        description="记忆类型：preference | semantic | procedural | profile | record",
    )
    subject: Optional[str] = Field(
        default=None, description="主题（决定写入哪个 wiki 文件），如『图书馆偏好』"
    )


async def note_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from src.memory.writer import record_note

    result = record_note(
        content=args["content"],
        memory_type=args.get("memory_type", "preference"),
        subject=args.get("subject"),
        source_event_id=ctx.event_id,
        db=ctx.db,
        person_id=ctx.person_id,
    )
    return result


TOOLS = [
    context_tool(
        name="note_write",
        description="把用户明确表达的偏好、事项或笔记记入长期记忆（Wiki）。",
        args_schema=NoteWriteArgs,
        func=note_write,
        high_risk=False,
    )
]
