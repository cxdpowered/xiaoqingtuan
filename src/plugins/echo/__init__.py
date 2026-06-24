from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata


__plugin_meta__ = PluginMetadata(
    name="Echo",
    description="Echo command for checking whether the bot can receive messages.",
    usage="/echo <message>",
)

echo = on_command("echo", aliases={"复读"}, priority=5, block=True)


@echo.handle()
async def handle_echo(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    if not text:
        await echo.finish("收到 echo 命令。用 /echo <内容> 测试复读。")

    await echo.finish(text)
