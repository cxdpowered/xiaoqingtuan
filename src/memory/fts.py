"""FTS5（trigram/BM25）全文索引层（架构 6.7 / 7.4 / 7.6）。

中文用 trigram tokenizer；查询走 ``MATCH``，按 BM25 排序。
"""

from __future__ import annotations

import re
from typing import Any

from src.memory.chunker import Chunk
from src.storage.db import Database


def reindex_path(db: Database, path: str, chunks: list[Chunk]) -> None:
    """按 path 先删后插（幂等）。"""
    rows = db.query("SELECT chunk_id FROM wiki_chunks WHERE path = ?", (path,))
    old_ids = [r["chunk_id"] for r in rows]
    for cid in old_ids:
        db.execute("DELETE FROM wiki_fts WHERE chunk_id = ?", (cid,))
    for c in chunks:
        db.execute(
            "INSERT INTO wiki_fts (chunk_id, title, section, content) VALUES (?, ?, ?, ?)",
            (c.chunk_id, c.title or "", c.section or "", c.content),
        )


def delete_path(db: Database, path: str) -> None:
    rows = db.query("SELECT chunk_id FROM wiki_chunks WHERE path = ?", (path,))
    for r in rows:
        db.execute("DELETE FROM wiki_fts WHERE chunk_id = ?", (r["chunk_id"],))


def _fts_query(text: str) -> str:
    """把自然语言转成 trigram-friendly 的 FTS5 查询串。

    去掉特殊字符，按空白与中文切词，每个词加引号做 OR 匹配。
    trigram tokenizer 要求匹配片段 >= 3 字符，过短的词会被忽略。
    """
    text = re.sub(r"[\"'^*:()\-+]", " ", text)
    tokens = [t for t in re.split(r"\s+", text.strip()) if len(t) >= 3]
    # 中文连续串本身可作为一个 token；英文短词（<3）丢弃以免 trigram 报错。
    if not tokens:
        # 退化：取整串里前若干字符
        compact = re.sub(r"\s+", "", text)
        if len(compact) >= 3:
            tokens = [compact[:20]]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def search_bm25(db: Database, query: str, top_k: int = 5) -> list[dict[str, Any]]:
    match = _fts_query(query)
    if not match:
        return []
    try:
        rows = db.query(
            "SELECT chunk_id, title, section, content, bm25(wiki_fts) AS score "
            "FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY score LIMIT ?",
            (match, top_k),
        )
    except Exception:
        return []
    results = []
    for r in rows:
        # bm25 越小越相关；转成越大越相关的归一分。
        raw = r["score"]
        results.append(
            {
                "chunk_id": r["chunk_id"],
                "title": r["title"],
                "section": r["section"],
                "content": r["content"],
                "score": 1.0 / (1.0 + abs(raw)),
                "retriever": "fts",
            }
        )
    return results
