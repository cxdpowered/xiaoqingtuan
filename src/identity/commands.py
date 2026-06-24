"""聊天内身份命令：绑定 / 确认 / 查看身份。

这些命令在 :func:`src.agent.run.handle_message` 进入 LangGraph 之前被确定性拦截，
不经过 LLM，保证解析稳定。匹配则返回回复文本，否则返回 ``None`` 走正常对话。

用法（用户直接在聊天里发）::

    绑定QQ 123456          # 在微信侧发起，把当前微信账号与 QQ 123456 关联
    确认绑定 AB12CD         # 在 QQ 123456 上发送验证码完成绑定
    我的身份 / whoami       # 查看当前 person 及其已关联账号
"""

from __future__ import annotations

import re
from typing import Optional

from src.identity import store
from src.storage.db import Database

_BIND_RE = re.compile(
    r"^\s*(?:绑定|关联)\s*(qq|QQ|微信|wechat|weixin)\s*[:：]?\s*([0-9A-Za-z@._\-]+)\s*$"
)
_CONFIRM_RE = re.compile(r"^\s*(?:确认绑定|绑定确认)\s+([0-9A-Za-z]{4,12})\s*$")
_WHOAMI_RE = re.compile(r"^\s*(?:我的身份|我的账号|我的记忆空间|whoami)\s*$", re.IGNORECASE)
_HELP_RE = re.compile(r"^\s*(?:绑定帮助|如何绑定|怎么绑定)\s*$")

_HELP_TEXT = (
    "🔗 跨渠道绑定（让 QQ 与微信共用同一份记忆）：\n"
    "1) 在任一渠道发送『绑定QQ <对方QQ号>』或『绑定微信 <对方微信号>』；\n"
    "2) 我会给出一个验证码；\n"
    "3) 到对方账号上发送『确认绑定 <验证码>』即可完成。\n"
    "随时发『我的身份』查看已关联的账号。"
)


def try_handle(
    db: Database,
    *,
    channel: str,
    account_id: str,
    person_id: str,
    text: str,
) -> Optional[str]:
    """命中身份命令则处理并返回回复；否则返回 None。"""
    if _HELP_RE.match(text):
        return _HELP_TEXT

    if _WHOAMI_RE.match(text):
        return _render_whoami(db, person_id)

    m = _CONFIRM_RE.match(text)
    if m:
        return _handle_confirm(db, channel, account_id, m.group(1))

    m = _BIND_RE.match(text)
    if m:
        return _handle_bind(db, channel, account_id, person_id, m.group(1), m.group(2))

    return None


def _render_whoami(db: Database, person_id: str) -> str:
    accounts = store.list_accounts(db, person_id)
    lines = [f"🪪 你的记忆身份：{person_id}", "已关联账号："]
    for a in accounts:
        label = store.channel_label(a["channel"])
        name = f"（{a['display_name']}）" if a.get("display_name") else ""
        lines.append(f"  · {label}：{a['account_id']}{name}")
    if len(accounts) <= 1:
        lines.append("（尚未跨渠道关联。发『绑定帮助』了解如何把 QQ 与微信合并记忆。）")
    return "\n".join(lines)


def _handle_bind(
    db: Database,
    channel: str,
    account_id: str,
    person_id: str,
    raw_channel: str,
    target_account_id: str,
) -> str:
    target_channel = store.normalize_channel(raw_channel)
    if not target_channel:
        return "暂不支持该渠道的绑定，目前支持 QQ 与微信。"
    if target_channel == channel and str(target_account_id) == str(account_id):
        return "不能绑定你当前正在使用的这个账号。"

    code, expires = store.create_bind_code(
        db,
        person_id=person_id,
        channel=channel,
        target_channel=target_channel,
        target_account_id=target_account_id,
    )
    label = store.channel_label(target_channel)
    return (
        f"已发起绑定：{label} {target_account_id}。\n"
        f"请到该 {label} 账号上向我发送：\n"
        f"  确认绑定 {code}\n"
        f"验证码 10 分钟内有效。完成后两个账号将共用同一份长期记忆。"
    )


def _handle_confirm(db: Database, channel: str, account_id: str, code: str) -> str:
    res = store.confirm_bind(
        db, code=code, confirming_channel=channel, confirming_account_id=account_id
    )
    if res.get("ok"):
        if res.get("already"):
            return "这两个账号本来就已经是同一份记忆了。"
        return f"✅ 绑定成功！现在你的 QQ 与微信共用同一份记忆（身份 {res['person_id']}）。"

    reason = res.get("reason")
    if reason == "code_not_found":
        return "验证码无效或已被使用，请重新发起绑定。"
    if reason == "expired":
        return "验证码已过期（超过 10 分钟），请重新发起绑定。"
    if reason == "account_mismatch":
        exp_label = store.channel_label(res.get("expected_channel", ""))
        return (
            "这个验证码不是发给当前账号的。\n"
            f"它应当在 {exp_label} {res.get('expected_account')} 上确认。"
        )
    return "绑定失败，请稍后重试或重新发起。"
