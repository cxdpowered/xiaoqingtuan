"""Wiki → chunk 切片（架构 7.4）。

按"标题 + 段（当前/历史）"切片，每段一个 chunk。chunk_id 稳定，便于先删后插。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from src.memory.wiki import Bullet, WikiDoc


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    path: str
    title: str
    section: str  # "当前" | "历史"
    content: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _bullets_to_text(bullets: list[Bullet], *, history: bool) -> str:
    parts = []
    for b in bullets:
        if history and (b.valid_from or b.valid_to):
            vf = b.valid_from or ""
            vt = b.valid_to or ""
            rng = f"{vf}~{vt}：" if vf else (f"{vt}：" if vt else "")
            parts.append(f"{rng}{b.value}")
        else:
            parts.append(b.value)
    return "。".join(p.rstrip("。") for p in parts if p)


def chunk_doc(doc: WikiDoc) -> list[Chunk]:
    chunks: list[Chunk] = []

    def make(section_label: str, key: str, bullets: list[Bullet], history: bool) -> None:
        content = _bullets_to_text(bullets, history=history)
        if not content.strip():
            return
        chunk_id = f"{doc.doc_id}#{key}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id=doc.doc_id,
                path=doc.path,
                title=doc.title,
                section=section_label,
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                metadata={"type": doc.memory_type},
            )
        )

    make("当前", "current", doc.current, history=False)
    make("历史", "history", doc.history, history=True)
    return chunks
