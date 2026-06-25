"""CCNU 图书馆 MCP 功能组（小青团侧）。

复杂 MCP 功能组：有本地状态表、定时任务、challenge、计划级授权，且需要把
``person_id`` 注入为远程 MCP 的 ``user_key``。按需求文档 §15.2，作为 ``src/tools/``
下的子包存在，复用既有 ``_BUILTIN_TOOL_MODULES`` 装载约定（不另起注册机制）。

模块划分：

- :mod:`.models`        —— 状态枚举/常量/wrapper 参数 schema。
- :mod:`.errors`        —— 远程错误码 → 中文 + 重试策略。
- :mod:`.client`        —— 调远程 MCP 原始工具，统一注入 ``user_key``。
- :mod:`.repository`    —— 本组独立 SQLite 表读写。
- :mod:`.challenge`     —— 图形验证码 challenge 拦截、落盘、跨轮续跑。
- :mod:`.tools`         —— 暴露给 LLM 的本地 ``library_*`` wrapper 工具（导出 ``TOOLS``）。
- :mod:`.scheduler`     —— 复用 APScheduler 扫描 job/watch/notification。
"""
