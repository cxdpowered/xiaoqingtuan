"""记忆抽取（架构 7.7：LangMem 仅做抽取）。

实现说明：LangMem 的定位是"对话 → 结构化候选"。为在本机稳健运行，这里用 DeepSeek
直接做结构化抽取（自包含、无额外依赖）；接口 ``extract_memories`` 与 LangMem 的抽取
职责一致，未来可无缝替换为 langmem 的 memory manager。

存储不走 LangMem，统一交给 :mod:`src.memory.writer`（架构 7.7：避免两套记忆系统打架）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from src import config

_SYSTEM = """你是记忆抽取器。从对话中抽取「值得长期记住」的用户事实/偏好/规则，忽略闲聊与一次性内容。
只在用户明确表达持久偏好、稳定事实、长期规则或个人画像时抽取。
输出 JSON：{"memories": [{"type": "...", "subject": "...", "predicate": "...", "value": "...", "confidence": 0.0-1.0}]}
type 取值：preference（偏好）| profile（画像）| procedural（规则/流程）| semantic（事实）| record（事项记录）。
predicate 是该主题下可被覆盖的属性键（如『座位区域』『确认要求』），用于同主题新值替换旧值。
没有可抽取内容时返回 {"memories": []}。只输出 JSON，不要解释。"""


def _format_conversation(messages: list[dict[str, str]]) -> str:
    lines = []
    for m in messages:
        role = {"user": "用户", "assistant": "助手"}.get(m.get("role", ""), m.get("role", ""))
        lines.append(f"{role}：{m.get('content', '')}")
    return "\n".join(lines)


async def extract_memories(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """从最近对话抽取候选记忆。失败/无内容时返回空列表。"""
    if not config.DEEPSEEK_API_KEY or not messages:
        return []

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _format_conversation(messages)},
        ],
        "stream": False,
        "max_tokens": 800,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                f"{config.DEEPSEEK_API_BASE.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for item in data.get("memories", []):
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        out.append(
            {
                "type": item.get("type", "semantic"),
                "subject": str(item.get("subject", "")).strip(),
                "predicate": item.get("predicate") or None,
                "value": value,
                "confidence": float(item.get("confidence", 0.8)),
            }
        )
    return out
