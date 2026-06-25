"""本地 CLI 渠道：在终端里直接和 Agent 对话，便于本机开发调试。

用法::

    python -m src.channels.cli
    python -m src.channels.cli --session cli:dev --user user:me
    # 模拟某 QQ 用户（用于测试多用户记忆隔离 / 跨渠道绑定）：
    python -m src.channels.cli --channel qq --account 123456
"""

from __future__ import annotations

import argparse
import asyncio

from src.channels.base import InboundMessage


async def _repl(session_id: str, user_id: str, channel: str, account_id: str) -> None:
    # 延迟导入，避免在仅做消息标准化时拉起整个 Agent 依赖。
    from src.agent.run import handle_message
    from src.tools.mcp_bridge import init_mcp_from_config, shutdown_mcp

    # 若配置了 XQT_MCP_CONFIG，则接入外部 MCP server 的工具（可选）。
    try:
        names = await init_mcp_from_config()
        if names:
            print(f"[MCP] 已接入工具：{names}")
    except Exception as exc:  # noqa: BLE001
        print(f"[MCP] 初始化失败（已跳过，不影响内置工具）：{exc}")

    # 图书馆定时调度（复用 APScheduler；CLI 下自建一个 AsyncIOScheduler）。
    try:
        from src.tools.ccnu_library import scheduler as lib_scheduler

        lib_scheduler.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[调度] 启动失败（已跳过）：{exc}")

    print("小青团 CLI 已启动。输入消息开始对话，'exit' / 'quit' 退出。")
    print("（高风险操作会请求确认，回复 '确认' 执行、'取消' 放弃。）\n")

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                text = await loop.run_in_executor(None, input, "你 > ")
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return

            text = text.strip()
            if not text:
                continue
            if text.lower() in {"exit", "quit"}:
                print("再见。")
                return

            inbound = InboundMessage(
                channel=channel,
                session_id=session_id,
                user_id=user_id,
                account_id=account_id,
                text=text,
            )
            out = await handle_message(inbound)
            _save_attachments(out.attachments)
            prefix = "小青团 ⚠ > " if out.requires_confirmation else "小青团 > "
            print(f"{prefix}{out.reply_text}\n")
    finally:
        await shutdown_mcp()


def _save_attachments(attachments: list) -> None:
    """CLI 不能直接显示图片：把附件（如图书馆验证码）落盘并打印路径。"""
    if not attachments:
        return
    import base64
    from pathlib import Path

    from src import config

    config.ensure_dirs()
    for att in attachments:
        if att.get("type") != "image":
            continue
        data_url = att.get("data_url") or ""
        payload = data_url.partition(",")[2] if data_url.startswith("data:") else data_url
        name = att.get("name") or "attachment.png"
        try:
            raw = base64.b64decode(payload)
        except (ValueError, TypeError):
            continue
        path = Path(config.LIBRARY_CHALLENGE_DIR) / name
        path.write_bytes(raw)
        print(f"[附件] 已保存图片：{path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="小青团本地 CLI")
    parser.add_argument("--session", default="cli:dev", help="会话 ID")
    parser.add_argument("--user", default="user:cli", help="用户 ID（展示用）")
    parser.add_argument("--channel", default="cli", help="渠道：cli | qq | wechat（用于测试身份）")
    parser.add_argument("--account", default="cli-local", help="平台账号号（身份解析用）")
    args = parser.parse_args()
    asyncio.run(_repl(args.session, args.user, args.channel, args.account))


if __name__ == "__main__":
    main()
