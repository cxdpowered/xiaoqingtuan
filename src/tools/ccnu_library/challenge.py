"""图形验证码 challenge 的落盘、回传与跨轮续跑（需求文档 §5、§8.2）。

设计：复用 SQLite 表 ``library_challenge_sessions`` 作为「跨轮」通道，不另造内存状态机。

- wrapper 工具拿到 ``NEED_CHALLENGE`` → :func:`record_challenge` 落盘图片 + 建会话
  （status=``pending``）。
- 本轮回复前，run.py 调 :func:`deliver_pending` 把图片塞进 ``OutboundMessage.attachments``，
  并把会话推进到 ``awaiting_answer``。
- 用户下一条消息进入 LLM **之前**，run.py 调 :func:`intercept`：把文本当作答案，
  确定性调用远程 ``submit_challenge`` 续跑。

只有 ``captcha`` 能完整续跑；``sms`` / ``confirm_send_sms`` / ``manual_login`` 为预留能力，
按文本提示用户并说明可能无法续跑。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Optional

from src import config
from src.channels.base import OutboundMessage
from src.storage.db import Database
from src.tools.ccnu_library import client, models
from src.tools.ccnu_library import repository as repo
from src.tools.registry import ToolContext


def _extract_challenge(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从远程结果里取出 challenge 字段（兼容顶层或嵌套在 'challenge' 下）。"""
    if str(result.get("code", "")).upper() != "NEED_CHALLENGE":
        return None
    ch = result.get("challenge")
    if isinstance(ch, dict):
        return ch
    # 顶层平铺
    keys = ("challenge_id", "challenge_type", "prompt", "image_base64", "expires_at", "phone_hint")
    if any(k in result for k in keys):
        return {k: result.get(k) for k in keys}
    return None


def _save_image(challenge_id: str, image_base64: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """把 data URL / 裸 base64 落盘，返回 (path, mime, sha256)。不长期把 base64 存 SQLite。"""
    if not image_base64:
        return None, None, None
    mime = "image/png"
    data = image_base64
    if image_base64.startswith("data:"):
        header, _, payload = image_base64.partition(",")
        data = payload
        if ";" in header and ":" in header:
            mime = header[header.index(":") + 1 : header.index(";")] or mime
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError):
        return None, None, None
    config.ensure_dirs()
    ext = "png" if "png" in mime else ("jpg" if "jpeg" in mime or "jpg" in mime else "img")
    path = config.LIBRARY_CHALLENGE_DIR / f"{challenge_id}.{ext}"
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    return str(path), mime, sha


def record_challenge(
    ctx: ToolContext, result: dict[str, Any], *,
    pending_action: Optional[str] = None, pending_context: Optional[Any] = None,
) -> Optional[str]:
    """收到 NEED_CHALLENGE 时建会话并落盘。返回给用户/LLM 的简短提示文本。

    返回 None 表示这不是一个 challenge 结果。
    """
    ch = _extract_challenge(result)
    if ch is None:
        return None

    ctype = str(ch.get("challenge_type") or models.CHALLENGE_CAPTCHA)
    image_path = image_mime = image_sha = None
    if ctype == models.CHALLENGE_CAPTCHA:
        image_path, image_mime, image_sha = _save_image(
            str(ch.get("challenge_id") or "challenge"), str(ch.get("image_base64") or "")
        )

    repo.create_challenge(
        ctx.db, person_id=ctx.person_id or "default",
        user_key=client.resolve_user_key(ctx), session_id=ctx.session_id,
        challenge_id=str(ch.get("challenge_id") or ""), challenge_type=ctype,
        prompt=ch.get("prompt"), image_path=image_path, image_mime=image_mime,
        image_sha256=image_sha, phone_hint=ch.get("phone_hint"),
        expires_at=ch.get("expires_at"), pending_action=pending_action,
        pending_context=pending_context, status="pending",
    )

    if ctype == models.CHALLENGE_CAPTCHA:
        return "需要图形验证码。我已把验证码图片发给你，请直接回复图中的字符。"
    if ctype in (models.CHALLENGE_SMS, models.CHALLENGE_CONFIRM_SMS):
        hint = f"（尾号 {ch.get('phone_hint')}）" if ch.get("phone_hint") else ""
        return (
            f"需要手机短信验证码{hint}。请回复收到的验证码。"
            "注意：短信验证码流程目前仍是预留能力，可能无法续跑成功。"
        )
    if ctype == models.CHALLENGE_MANUAL:
        return "需要人工登录。请在图书馆网站完成登录后回复『已登录』。"
    return f"需要完成验证（{ctype}）。请按提示回复。"


def _attachment_from_path(image_path: str, image_mime: Optional[str], challenge_id: str) -> Optional[dict[str, Any]]:
    try:
        raw = open(image_path, "rb").read()
    except OSError:
        return None
    mime = image_mime or "image/png"
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    ext = image_path.rsplit(".", 1)[-1]
    return {"type": "image", "mime": mime, "data_url": data_url,
            "name": f"ccnu_captcha_{challenge_id}.{ext}"}


def deliver_pending(db: Database, session_id: str) -> Optional[OutboundMessage]:
    """把本会话尚未送达的验证码图片打包成 OutboundMessage，并推进到 awaiting_answer。

    没有待送达 challenge 时返回 None。
    """
    pendings = repo.pending_to_deliver(db, session_id)
    if not pendings:
        return None
    attachments: list[dict[str, Any]] = []
    prompts: list[str] = []
    for ch in pendings:
        if ch.get("image_path"):
            att = _attachment_from_path(ch["image_path"], ch.get("image_mime"), ch["challenge_id"])
            if att:
                attachments.append(att)
        prompts.append(ch.get("prompt") or "请输入图形验证码。")
        repo.set_challenge_status(db, ch["id"], "awaiting_answer")
    text = "\n".join(dict.fromkeys(prompts)) or "请输入验证码。"
    return OutboundMessage(reply_text=text, attachments=attachments)


async def intercept(ctx: ToolContext, text: str) -> Optional[OutboundMessage]:
    """进入 LLM 前拦截：若本会话有等待答案的 challenge，把文本当答案确定性续跑。

    返回 OutboundMessage 表示已处理本轮（不再进 LLM）；返回 None 表示无 pending，正常往下走。
    """
    ch = repo.awaiting_answer(ctx.db, ctx.session_id)
    if ch is None:
        return None

    answer = (text or "").strip()
    if answer in {"取消", "放弃", "算了", "不验证了"}:
        repo.set_challenge_status(ctx.db, ch["id"], "failed")
        return OutboundMessage(reply_text="已取消本次验证。需要时再发起登录/预约即可。")

    # 仅图形验证码可确定性续跑；其余类型如实告知能力边界。
    if ch["challenge_type"] != models.CHALLENGE_CAPTCHA:
        result = await client.call(
            ctx, "submit_challenge",
            {"challenge_id": ch["challenge_id"], "answer": answer},
        )
        if result.get("ok"):
            repo.set_challenge_status(ctx.db, ch["id"], "resolved")
            return OutboundMessage(reply_text="验证已提交并通过。请重发刚才的请求继续。")
        repo.set_challenge_status(ctx.db, ch["id"], "failed")
        from src.tools.ccnu_library.errors import explain
        return OutboundMessage(
            reply_text=f"验证未通过：{explain(result.get('code'), result.get('message'))}。"
            "（短信/人工验证当前为预留能力，可能无法续跑。）"
        )

    result = await client.call(
        ctx, "submit_challenge",
        {"challenge_id": ch["challenge_id"], "answer": answer},
    )
    if result.get("ok"):
        repo.set_challenge_status(ctx.db, ch["id"], "resolved")
        action = ch.get("pending_action")
        tail = f"现在可以继续『{action}』了，请重发该请求。" if action else "请重发刚才的请求继续。"
        return OutboundMessage(reply_text=f"验证码正确，已登录 ✅ {tail}")

    from src.tools.ccnu_library.errors import explain
    code = str(result.get("code") or "").upper()
    if code == "NEED_CHALLENGE":
        # 远程换发了新验证码：记录新 challenge 并提示。
        msg = record_challenge(ctx, result, pending_action=ch.get("pending_action"))
        repo.set_challenge_status(ctx.db, ch["id"], "failed")
        out = deliver_pending(ctx.db, ctx.session_id)
        if out is not None:
            out.reply_text = "验证码不正确，换了一张新的：\n" + out.reply_text
            return out
        return OutboundMessage(reply_text=msg or "验证码不正确，请重试。")
    # 其它失败：保持 awaiting，让用户再试一次。
    return OutboundMessage(
        reply_text=f"验证码提交失败：{explain(result.get('code'), result.get('message'))}。请再回复一次验证码，或回复『取消』。"
    )
