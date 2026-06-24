"""派生层重建编排（架构 7.4）。

某 .md 变更后按文件重建 wiki_chunks / memories / relations / FTS5 / LanceDB，
幂等：按 path 先删后插。
"""

from __future__ import annotations

import json
from typing import Optional

from src.memory import fts, vector
from src.memory.chunker import Chunk, chunk_doc
from src.memory.wiki import Bullet, WikiDoc, read_doc
from src.storage.db import Database, new_id, now_iso


def _upsert_chunks(db: Database, path: str, chunks: list[Chunk]) -> None:
    db.execute("DELETE FROM wiki_chunks WHERE path = ?", (path,))
    for c in chunks:
        db.execute(
            "INSERT INTO wiki_chunks (chunk_id, doc_id, path, title, section, content,"
            " content_hash, updated_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.chunk_id,
                c.doc_id,
                c.path,
                c.title,
                c.section,
                c.content,
                c.content_hash,
                now_iso(),
                json.dumps(c.metadata, ensure_ascii=False),
            ),
        )


def _insert_memory(db: Database, doc: WikiDoc, b: Bullet, status: str) -> None:
    db.execute(
        "INSERT INTO memories (id, subject, predicate, value, memory_type, confidence, status,"
        " source_event_id, valid_from, valid_to, updated_at, path)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("mem_"),
            b.subject or doc.title,
            b.effective_predicate(),
            b.value,
            b.memory_type or doc.memory_type,
            float(b.confidence) if b.confidence is not None else doc.confidence,
            status,
            None,
            b.valid_from,
            b.valid_to,
            now_iso(),
            doc.path,
        ),
    )


def _upsert_memories(db: Database, doc: WikiDoc) -> None:
    db.execute("DELETE FROM memories WHERE path = ?", (doc.path,))
    for b in doc.current:
        _insert_memory(db, doc, b, status="active")
    for b in doc.history:
        _insert_memory(db, doc, b, status="expired")


def _upsert_relations(db: Database, doc: WikiDoc) -> None:
    # v1 仅留接口：清掉旧 path 的 relations，不抽取（架构 7.4 / 6.4）。
    db.execute("DELETE FROM relations WHERE path = ?", (doc.path,))


def rebuild_path(db: Database, rel_path: str) -> Optional[WikiDoc]:
    """重建单个 wiki 文件的全部派生索引。文件不存在则清掉其派生行。"""
    doc = read_doc(rel_path)
    if doc is None:
        delete_path(db, rel_path)
        return None
    chunks = chunk_doc(doc)
    _upsert_chunks(db, rel_path, chunks)
    _upsert_memories(db, doc)
    _upsert_relations(db, doc)
    fts.reindex_path(db, rel_path, chunks)
    vector.reindex_path(db, rel_path, chunks)
    return doc


def delete_path(db: Database, rel_path: str) -> None:
    fts.delete_path(db, rel_path)
    vector.delete_path(db, rel_path)
    db.execute("DELETE FROM wiki_chunks WHERE path = ?", (rel_path,))
    db.execute("DELETE FROM memories WHERE path = ?", (rel_path,))
    db.execute("DELETE FROM relations WHERE path = ?", (rel_path,))
