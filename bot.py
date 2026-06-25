import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.wxclaw import Adapter as WxClawAdapter


def main() -> None:
    nonebot.init()

    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    driver.register_adapter(WxClawAdapter)

    nonebot.load_plugin("nonebot_plugin_status")
    nonebot.load_plugin("nonebot_plugin_apscheduler")  # 复用其共享 APScheduler
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

    @driver.on_startup
    async def _start_library_scheduler() -> None:
        # CCNU 图书馆功能组：把定时扫描挂到 nonebot 的共享 APScheduler 上（不自建轮询）。
        try:
            from nonebot_plugin_apscheduler import scheduler

            from src.tools.ccnu_library import scheduler as lib_scheduler

            lib_scheduler.setup(scheduler)
            nonebot.logger.info("已启用图书馆定时调度（APScheduler）。")
        except Exception as exc:  # noqa: BLE001
            nonebot.logger.warning(f"图书馆调度启动失败（已跳过）：{exc}")

    @driver.on_startup
    async def _start_qq_offline_alert() -> None:
        # QQ 掉线邮件告警：带外通道，QQ 掉线超阈值时发邮件（见 src/health/qq_offline_alert.py）。
        try:
            from nonebot_plugin_apscheduler import scheduler

            from src.health import qq_offline_alert

            qq_offline_alert.setup(scheduler)
        except Exception as exc:  # noqa: BLE001
            nonebot.logger.warning(f"QQ 掉线告警启动失败（已跳过）：{exc}")

    @driver.on_shutdown
    async def _close_mcp() -> None:
        from src.tools.mcp_bridge import shutdown_mcp

        await shutdown_mcp()

    nonebot.run()


if __name__ == "__main__":
    main()
