from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata


__plugin_meta__ = PluginMetadata(
    name="LLM Memory Starter",
    description="LLM and memory system plugin scaffold.",
    usage="/llm <message>",
)

llm = on_command("llm", aliases={"ask", "聊天"}, priority=10, block=True)


@llm.handle()
async def handle_llm(args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    if not text:
        await llm.finish("LLM 插件骨架已加载。用 /llm <内容> 测试入口。")

    await llm.finish(f"收到：{text}\n后续可以在这里接入 LLM 和记忆系统。")
