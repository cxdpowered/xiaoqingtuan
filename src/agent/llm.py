"""DeepSeek LLM 客户端（LangChain ChatDeepSeek 封装）。"""

from __future__ import annotations

from typing import Optional

from src import config

_llm = None
_llm_with_tools = None


def get_llm():
    global _llm
    if _llm is None:
        from langchain_deepseek import ChatDeepSeek

        _llm = ChatDeepSeek(
            model=config.DEEPSEEK_MODEL,
            api_key=config.DEEPSEEK_API_KEY,
            api_base=config.DEEPSEEK_API_BASE,
            temperature=0.3,
            max_tokens=1024,
            timeout=60,
        )
    return _llm


def get_llm_with_tools():
    """绑定工具 schema 的 LLM（用于 function calling）。"""
    global _llm_with_tools
    if _llm_with_tools is None:
        from src.tools.registry import get_registry

        _llm_with_tools = get_llm().bind_tools(get_registry().bindable())
    return _llm_with_tools


def reset_tools_cache() -> None:
    """注册表变更后（如接入 MCP 工具）使已绑定工具的 LLM 失效，下次重新绑定。"""
    global _llm_with_tools
    _llm_with_tools = None
