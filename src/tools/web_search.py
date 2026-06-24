"""web_search 工具（架构第 9 节）。

本期 stub：若配置了 ``TAVILY_API_KEY`` 则走 Tavily，否则返回 mock 结果。
DeepSeek 无内置搜索，用 function calling 接第三方。回复尽量带来源链接。
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from src import config
from src.tools.registry import Tool, ToolContext


class WebSearchArgs(BaseModel):
    query: str = Field(description="搜索关键词或问题")
    max_results: int = Field(default=5, ge=1, le=10, description="返回结果条数上限")


async def _tavily_search(query: str, max_results: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": config.TAVILY_API_KEY,
                "query": query,
                "max_results": max_results,
                "include_answer": False,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "published_at": item.get("published_date", ""),
            }
        )
    return results


def _mock_results(query: str, max_results: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"[mock] 关于「{query}」的结果",
            "url": "https://example.com/mock",
            "snippet": "未配置 TAVILY_API_KEY，这是占位结果。配置后将返回真实联网搜索。",
            "published_at": "",
        }
    ][:max_results]


async def web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args["query"]
    max_results = args.get("max_results", 5)
    if config.TAVILY_API_KEY:
        try:
            results = await _tavily_search(query, max_results)
        except Exception as exc:  # noqa: BLE001
            return {"query": query, "results": [], "error": f"Tavily 调用失败：{exc}"}
    else:
        results = _mock_results(query, max_results)
    return {"query": query, "results": results}


TOOLS = [
    Tool(
        name="web_search",
        description="联网搜索网页信息（新闻、开放时间、实时资料等），返回标题/链接/摘要。",
        args_model=WebSearchArgs,
        func=web_search,
        high_risk=False,
    )
]
