import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.wxclaw import Adapter as WxClawAdapter


def main() -> None:
    nonebot.init()

    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    driver.register_adapter(WxClawAdapter)

    nonebot.load_plugin("nonebot_plugin_status")
    nonebot.load_plugins("src/plugins")

    @driver.on_startup
    async def _init_mcp() -> None:
        # 若配置了 XQT_MCP_CONFIG，则接入外部 MCP server 的工具（可选）。
        from src.tools.mcp_bridge import init_mcp_from_config

        try:
            names = await init_mcp_from_config()
            if names:
                nonebot.logger.info(f"已接入 MCP 工具：{names}")
        except Exception as exc:  # noqa: BLE001
            nonebot.logger.warning(f"MCP 初始化失败（已跳过，不影响内置工具）：{exc}")

    @driver.on_shutdown
    async def _close_mcp() -> None:
        from src.tools.mcp_bridge import shutdown_mcp

        await shutdown_mcp()

    nonebot.run()


if __name__ == "__main__":
    main()
