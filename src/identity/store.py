"""身份解析与跨渠道关联的存储/逻辑层。

核心概念
--------
- **person**：稳定的「人」标识（``p`` + 10 位 hex）。长期记忆按 person 分区。
- **account**：``(channel, account_id)`` → person。首次见到某账号会自动建 person。
- **bind_code**：跨渠道绑定的一次性验证码，证明同一人同时掌控两个账号后再合并。

记忆分区约定（与 :mod:`src.memory` 配合）
----------------------------------------
- 个人记忆写到 ``wiki/users/<person_id>/...``。
- 任何不在 ``users/`` 下的 wiki 路径都是全员共享知识（项目文档、通用规则等）。
- 检索时某 person 可见：自己的 ``users/<pid>/`` + 全部共享路径。
"""

from __future__ import annotations

import secrets
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from src import config
from src.storage.db import Database, get_db, now_iso

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混字符 I/O/0/1
_CODE_LEN = 6
_BIND_TTL_MINUTES = 10

_CHANNEL_ALIASES = {
    "qq": "qq",
    "微信": "wechat",
    "wechat": "wechat",
    "weixin": "wechat",
    "cli": "cli",
}

_CHANNEL_LABELS = {"qq": "QQ", "wechat": "微信", "cli": "CLI"}


def channel_label(channel: str) -> str:
    return _CHANNEL_LABELS.get(channel, channel)


def normalize_channel(name: str) -> Optional[str]:
    return _CHANNEL_ALIASES.get((name or "").strip().lower())


def _new_person_id() -> str:
    return "p" + uuid.uuid4().hex[:10]


# ---- person / account 解析 ------------------------------------------------
def resolve(
    db: Optional[Database] = None,
    *,
    channel: str,
    account_id: str,
    display_name: Optional[str] = None,
) -> str:
    """把 (channel, account_id) 解析为 person_id；不存在则自动建。"""
    db = db or get_db()
    account_id = str(account_id)
    row = db.query_one(
        "SELECT person_id FROM accounts WHERE channel = ? AND account_id = ?",
        (channel, account_id),
    )
    if row:
        if display_name:
            db.execute(
                "UPDATE accounts SET display_name = ? WHERE channel = ? AND account_id = ?",
                (display_name, channel, account_id),
            )
        return row["person_id"]

    pid = _new_person_id()
    now = now_iso()
    db.execute(
        "INSERT INTO persons (id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (pid, display_name, now, now),
    )
    db.execute(
        "INSERT INTO accounts (channel, account_id, person_id, display_name, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (channel, account_id, pid, display_name, now),
    )
    return pid


def list_accounts(db: Database, person_id: str) -> list[dict]:
    rows = db.query(
        "SELECT channel, account_id, display_name FROM accounts WHERE person_id = ?"
        " ORDER BY created_at",
        (person_id,),
    )
    return [dict(r) for r in rows]


# ---- 记忆分区 scope -------------------------------------------------------
def person_dir_prefix(person_id: str) -> str:
    """个人记忆的 wiki 路径前缀。"""
    return f"users/{person_id}/"


def in_scope(path: str, person_id: Optional[str]) -> bool:
    """某 wiki 路径对该 person 是否可见。

    可见 = 自己的 ``users/<pid>/`` 路径，或任何非 ``users/`` 的共享路径。
    ``person_id`` 为 None 时（无身份上下文）只看共享路径。
    """
    path = (path or "").lstrip("/")
    if not path.startswith("users/"):
        return True  # 共享知识，全员可见
    if not person_id:
        return False
    return path.startswith(person_dir_prefix(person_id))


def scope_prefixes(person_id: Optional[str]) -> list[str]:
    """该 person 的可见前缀列表（用于检索过滤的说明性返回）。"""
    if person_id:
        return [person_dir_prefix(person_id), ""]  # "" 代表共享（非 users/）
    return [""]


# ---- 跨渠道绑定（验证码） -------------------------------------------------
def _gen_code(db: Database) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        if not db.query_one("SELECT code FROM bind_codes WHERE code = ?", (code,)):
            return code
    return uuid.uuid4().hex[:_CODE_LEN].upper()


def create_bind_code(
    db: Database,
    *,
    person_id: str,
    channel: str,
    target_channel: str,
    target_account_id: str,
) -> tuple[str, str]:
    """发起绑定：生成一次性验证码。返回 (code, expires_at_iso)。"""
    code = _gen_code(db)
    created = datetime.now(timezone.utc).astimezone()
    expires = created + timedelta(minutes=_BIND_TTL_MINUTES)
    db.execute(
        "INSERT INTO bind_codes (code, person_id, channel, target_channel,"
        " target_account_id, created_at, expires_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            code,
            person_id,
            channel,
            target_channel,
            str(target_account_id),
            created.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
            ),
    )
    return code, expires.isoformat(timespec="seconds")


def confirm_bind(
    db: Database,
    *,
    code: str,
    confirming_channel: str,
    confirming_account_id: str,
) -> dict:
    """在对方渠道用验证码确认绑定。成功则合并两个 person。"""
    row = db.query_one(
        "SELECT * FROM bind_codes WHERE code = ?", (code.strip().upper(),)
    )
    if not row or row["status"] != "pending":
        return {"ok": False, "reason": "code_not_found"}

    now = datetime.now(timezone.utc).astimezone()
    try:
        expired = datetime.fromisoformat(row["expires_at"]) < now
    except Exception:
        expired = False
    if expired:
        db.execute("UPDATE bind_codes SET status = 'expired' WHERE code = ?", (row["code"],))
        return {"ok": False, "reason": "expired"}

    # 确认者必须正是发起方声称要绑定的那个账号。
    if (
        confirming_channel != row["target_channel"]
        or str(confirming_account_id) != str(row["target_account_id"])
    ):
        return {
            "ok": False,
            "reason": "account_mismatch",
            "expected_channel": row["target_channel"],
            "expected_account": row["target_account_id"],
        }

    initiator_person = row["person_id"]
    confirming_person = resolve(
        db, channel=confirming_channel, account_id=confirming_account_id
    )

    db.execute("UPDATE bind_codes SET status = 'used' WHERE code = ?", (row["code"],))

    if initiator_person == confirming_person:
        return {"ok": True, "person_id": initiator_person, "already": True}

    canonical = link_persons(db, canonical=initiator_person, other=confirming_person)
    return {"ok": True, "person_id": canonical, "already": False}


def link_persons(db: Database, *, canonical: str, other: str) -> str:
    """把 ``other`` 合并进 ``canonical``：迁移记忆、改账号归属、删空 person。"""
    if canonical == other:
        return canonical

    _merge_wiki_dirs(db, canonical=canonical, other=other)

    db.execute("UPDATE accounts SET person_id = ? WHERE person_id = ?", (canonical, other))
    db.execute("UPDATE persons SET updated_at = ? WHERE id = ?", (now_iso(), canonical))
    db.execute("DELETE FROM persons WHERE id = ?", (other,))

    # 路径整体搬迁后，全量重建派生索引最稳妥（关联是低频操作）。
    from src.memory.reindex import reindex_all

    reindex_all(db)
    return canonical


def _merge_wiki_dirs(db: Database, *, canonical: str, other: str) -> None:
    """把 wiki/users/<other>/ 合并进 wiki/users/<canonical>/。"""
    from src.memory.wiki import read_doc, rel_path

    other_dir = config.WIKI_DIR / "users" / other
    canon_dir = config.WIKI_DIR / "users" / canonical
    if not other_dir.exists():
        return
    canon_dir.mkdir(parents=True, exist_ok=True)

    for src_file in sorted(other_dir.rglob("*.md")):
        sub = src_file.relative_to(other_dir)
        dst_file = canon_dir / sub
        if not dst_file.exists():
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_file), str(dst_file))
            continue
        # 目标已存在：把 other 的「当前」逐条作为候选并入 canonical（复用写入器冲突逻辑）。
        try:
            doc = read_doc(rel_path(src_file))
        except Exception:
            doc = None
        if doc:
            from src.memory.writer import apply_candidate

            for b in doc.current:
                apply_candidate(
                    {
                        "type": b.memory_type or doc.memory_type,
                        "subject": b.subject or doc.title,
                        "predicate": b.predicate,
                        "value": b.value,
                        "confidence": b.confidence or doc.confidence,
                    },
                    db=db,
                    person_id=canonical,
                )
        src_file.unlink(missing_ok=True)

    # 清理 other 的空目录。
    if other_dir.exists():
        shutil.rmtree(other_dir, ignore_errors=True)
