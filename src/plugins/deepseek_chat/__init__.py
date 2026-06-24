"""小青团 Agent 接入层（NoneBot 降级为接入层，架构 5.1 / 10.1）。

把每条文本消息标准化为 :class:`InboundMessage`，交给 LangGraph Agent Core 处理，
再把标准化回复发回渠道。QQ（OneBot V11）与微信（WxClaw）共用同一套 Agent。
"""

from nonebot import get_driver, on_message
from nonebot.adapters import Bot, Event, Message
from nonebot.params import EventMessage
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

__plugin_meta__ = PluginMetadata(
    name="小青团 Agent",
    description="任务型 LLM Agent：多轮对话、工具调用、长期记忆、人工确认。",
    usage="直接发送文本对话。发送『关闭插件』停用、『开启插件』恢复。高风险操作会请求确认。",
)

DISABLE_WORDS = {"关闭插件", "关闭大模型", "停用插件", "停用小青团"}
ENABLE_WORDS = {"开启插件", "开启大模型", "启用插件", "启用小青团"}
CONTROL_WORDS = DISABLE_WORDS | ENABLE_WORDS

driver = get_driver()
enabled = True


def _normalize(text: str) -> str:
    return text.strip().lstrip("/").strip()


def _extract_text(message: Message) -> str:
    return message.extract_plain_text().strip()


async def _is_control(message: Message = EventMessage()) -> bool:
    return _normalize(_extract_text(message)) in CONTROL_WORDS


async def _should_handle(message: Message = EventMessage()) -> bool:
    text = _extract_text(message)
    return enabled and bool(text) and _normalize(text) not in CONTROL_WORDS


control = on_message(rule=Rule(_is_control), priority=5, block=True)
agent = on_message(rule=Rule(_should_handle), priority=50, block=True)


@control.handle()
async def handle_control(message: Message = EventMessage()) -> None:
    global enabled
    text = _normalize(_extract_text(message))
    if text in DISABLE_WORDS:
        enabled = False
        await control.finish("已停用小青团自动回复。发送『开启插件』恢复。")
    if text in ENABLE_WORDS:
        enabled = True
        await control.finish("小青团已上线。")


def _standardize(bot: Bot, event: Event):
    from src.channels import qq, wechat

    bot_type = (getattr(bot, "type", "") or "").lower()
    if "wxclaw" in bot_type or "wechat" in bot_type or "微信" in bot_type:
        return wechat.standardize(bot, event)
    return qq.standardize(bot, event)


@agent.handle()
async def handle_agent(bot: Bot, event: Event) -> None:
    from src.agent.run import handle_message

    inbound = _standardize(bot, event)
    if not inbound.text:
        return

    try:
        out = await handle_message(inbound)
    except Exception as exc:  # noqa: BLE001
        await agent.finish(f"小青团处理消息时出错：{exc}")

    reply = out.reply_text or "（没有可回复的内容）"
    await agent.finish(reply)
