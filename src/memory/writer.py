"""Markdown 写入器（架构 7.5）。

把候选记忆 {type, subject, predicate, value, confidence} 落地到目标 .md：
- 新事实 append 到 ``## 当前``；
- 同 subject+predicate 的冲突事实走 **last-write-wins**：旧 bullet 移到 ``## 历史``
  并标 ``valid_to=今天``，新值进 ``## 当前``；
- 写回前 hash 校验，撞车（人工已改）则不覆盖，候选入 pending 队列。

随后重建该文件派生索引，并写一条 ``memory_update`` 事件。
冲突策略 v1：仅 last-write-wins + expire 保留历史（不做来源优先级仲裁）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

from src import config
from src.memory import store
from src.memory.wiki import (
    Bullet,
    WikiDoc,
    abs_path,
    atomic_write,
    content_hash,
    read_doc,
)
from src.storage.db import Database, get_db

_PENDING_FILE = config.DATA_DIR / "pending_memory.jsonl"

_PREF_KEYWORDS = {
    "preferences/library.md": ["图书馆", "座位", "馆", "自习", "library", "seat"],
    "preferences/response_style.md": ["回复", "风格", "语气", "称呼", "格式", "style", "tone"],
}


def _slug(text: str) -> str:
    import re

    text = re.sub(r"[\s/\\]+", "_", (text or "").strip())
    text = re.sub(r"[^\w一-鿿_-]", "", text)
    return text or "general"


def _base_path(memory_type: str, subject: str, value: str = "") -> str:
    """按 type/subject 映射到 wiki 内的相对子路径（不含 person 前缀，架构 7.3 目录）。"""
    blob = f"{subject} {value}"
    mt = (memory_type or "semantic").lower()

    if mt in {"profile"}:
        return "user_profile.md"
    if mt in {"preference"}:
        for path, kws in _PREF_KEYWORDS.items():
            if any(k in blob for k in kws):
                return path
        return "preferences/general.md"
    if mt in {"procedural", "rule"}:
        if any(k in blob for k in ["图书馆", "预约", "座位", "booking"]):
            return "rules/library_booking.md"
        return f"rules/{_slug(subject)}.md"
    if mt in {"record", "task_state", "episodic"}:
        if any(k in blob for k in ["图书馆", "预约", "座位"]):
            return "records/library_reservations.md"
        return f"records/{_slug(subject)}.md"
    # semantic / 其它
    return "knowledge.md"


def target_path(
    memory_type: str, subject: str, value: str = "", person_id: Optional[str] = None
) -> str:
    """目标 .md 路径。带 ``person_id`` 时落到 ``users/<pid>/`` 个人分区；
    否则写到共享根目录（全员可见知识）。"""
    base = _base_path(memory_type, subject, value)
    if person_id:
        return f"users/{person_id}/{base}"
    return base


def _new_doc(path: str, *, memory_type: str, subject: str, confidence: float) -> WikiDoc:
    doc_id = _slug(Path(path).with_suffix("").as_posix().replace("/", "."))
    return WikiDoc(
        path=path,
        frontmatter={
            "id": doc_id,
            "type": memory_type,
            "confidence": confidence,
            "source": f"conversation:{date.today().isoformat()}",
            "updated_at": date.today().isoformat(),
        },
        title=subject or doc_id,
        current=[],
        history=[],
    )


def _enqueue_pending(candidate: dict[str, Any], path: str, reason: str) -> None:
    config.ensure_dirs()
    rec = {"candidate": candidate, "path": path, "reason": reason, "at": date.today().isoformat()}
    with open(_PENDING_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def apply_candidate(
    candidate: dict[str, Any],
    *,
    db: Optional[Database] = None,
    source_event_id: Optional[str] = None,
    person_id: Optional[str] = None,
) -> dict[str, Any]:
    """把一条候选记忆落地。返回 {ok, path, action, ...}。

    ``person_id`` 决定写到哪个用户的记忆分区（``users/<pid>/``）；
    为 None 时写入共享知识区（根目录）。
    """
    db = db or get_db()
    memory_type = str(candidate.get("type") or candidate.get("memory_type") or "semantic")
    subject = str(candidate.get("subject") or "").strip()
    predicate = candidate.get("predicate")
    value = str(candidate.get("value") or "").strip()
    confidence = float(candidate.get("confidence", 0.8))
    if not value:
        return {"ok": False, "error": "empty value"}

    path = target_path(memory_type, subject, value, person_id)
    abs_p = abs_path(path)

    # 1) 读文件 + 计算 hash（不存在则视为空文件 hash）
    before_text = abs_p.read_text(encoding="utf-8") if abs_p.exists() else None
    before_hash = content_hash(before_text) if before_text is not None else None

    doc = read_doc(path) or _new_doc(
        path, memory_type=memory_type, subject=subject or "记忆", confidence=confidence
    )

    new_bullet = Bullet(
        value=value,
        subject=subject or doc.title,
        predicate=predicate,
        memory_type=memory_type,
        confidence=confidence,
    )

    # 2) 内存里改 当前/历史
    action = "appended"
    matched_idx = None
    for i, b in enumerate(doc.current):
        same_subject = (b.subject or doc.title) == new_bullet.subject
        same_pred = b.effective_predicate() == new_bullet.effective_predicate()
        if same_subject and same_pred:
            matched_idx = i
            break

    if matched_idx is not None:
        old = doc.current[matched_idx]
        if old.value.strip() == value:
            # 同值，只更新置信度，不动历史。
            old.confidence = confidence
            action = "noop"
        else:
            # last-write-wins：旧值进历史，新值进当前。
            prior = old.valid_from or doc.frontmatter.get("updated_at")
            old.valid_from = str(prior) if prior else date.today().isoformat()
            old.valid_to = date.today().isoformat()
            doc.history.insert(0, old)
            doc.current[matched_idx] = new_bullet
            action = "replaced"
    else:
        doc.current.append(new_bullet)

    if action == "noop":
        # 仍重建索引（置信度可能变），但不必动文件内容。
        new_text = doc.serialize()
    else:
        new_text = doc.serialize()

    # 3) 写回前再校验磁盘 hash 未变
    current_disk = abs_p.read_text(encoding="utf-8") if abs_p.exists() else None
    current_hash = content_hash(current_disk) if current_disk is not None else None
    if current_hash != before_hash:
        _enqueue_pending(candidate, path, "hash_changed")
        return {"ok": False, "path": path, "action": "pending", "reason": "文件已被人工修改，候选入 pending 队列"}

    atomic_write(path, new_text)

    # 4) 重建派生索引
    store.rebuild_path(db, path)

    # 5) events 记一条 memory_update
    db.add_event(
        session_id="memory",
        role="system",
        content=f"{action}: {subject}/{new_bullet.effective_predicate()} = {value}",
        event_type="memory_update",
        metadata={"path": path, "action": action, "confidence": confidence},
    )
    if source_event_id:
        pass  # 候选来源已在 metadata，保留扩展位

    return {"ok": True, "path": path, "action": action, "value": value}


def record_note(
    *,
    content: str,
    memory_type: str = "preference",
    subject: Optional[str] = None,
    source_event_id: Optional[str] = None,
    db: Optional[Database] = None,
    person_id: Optional[str] = None,
) -> dict[str, Any]:
    """note_write 工具入口：把一句话笔记/偏好落地到该用户的记忆分区。"""
    candidate = {
        "type": memory_type,
        "subject": subject or "用户笔记",
        "predicate": None,  # 无显式谓词 → 文本指纹，行为为 append
        "value": content,
        "confidence": 0.9,
    }
    return apply_candidate(
        candidate, db=db, source_event_id=source_event_id, person_id=person_id
    )
