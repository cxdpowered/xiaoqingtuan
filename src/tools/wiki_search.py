"""wiki_search 工具：查询 Wiki 与记忆索引（hybrid 检索，架构 7.6）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.tools.registry import Tool, ToolContext


class WikiSearchArgs(BaseModel):
    query: str = Field(description="要查询的问题或关键词，如『我一般喜欢坐哪里』")
    top_k: int = Field(default=5, ge=1, le=10, description="返回片段数上限")


async def wiki_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # 延迟导入，避免在不需要记忆系统时拉起 LanceDB/embedding。
    from src.memory.search import hybrid_search

    query = args["query"]
    top_k = args.get("top_k", 5)
    hits = hybrid_search(query, top_k=top_k, db=ctx.db, person_id=ctx.person_id)
    return {
        "query": query,
        "hits": [
            {
                "path": h["path"],
                "section": h["section"],
                "title": h.get("title"),
                "content": h["content"],
                "score": round(h.get("score", 0.0), 4),
                "source": h.get("source", ""),
            }
            for h in hits
        ],
    }


TOOLS = [
    Tool(
        name="wiki_search",
        description="查询长期记忆 / 个人 Wiki（偏好、画像、规则、历史记录），返回带来源的片段。",
        args_model=WikiSearchArgs,
        func=wiki_search,
        high_risk=False,
    )
]
