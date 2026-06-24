"""身份层：多用户 + 跨渠道（QQ / 微信）账号关联。

- 一个「人」(person) 可拥有多个 account（不同渠道、不同号）。
- 记忆按 person_id 分区到 ``wiki/users/<person_id>/``；非 ``users/`` 路径为全员共享。
- QQ↔微信 通过「聊天内绑定 + 验证码」关联（见 :mod:`src.identity.commands`）。
"""

from src.identity.store import (
    in_scope,
    person_dir_prefix,
    resolve,
    scope_prefixes,
)

__all__ = ["resolve", "person_dir_prefix", "in_scope", "scope_prefixes"]
