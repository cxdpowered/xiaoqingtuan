"""Hybrid 检索（架构 7.6）：结构化 memories + FTS5 BM25 + LanceDB 向量，融合去重。

``status=expired`` 默认不进检索，仅显式查历史时使用。
"""

from __future__ import annotations

from typing import Any, Optional

from src.memory import fts, vector
from src.storage.db import Database, get_db

# 不同召回源的融合权重。
_WEIGHTS = {"vector": 1.0, "fts": 0.8, "memory": 0.6}


def _chunk_meta(db: Database, chunk_id: str) -> dict[str, Any]:
    row = db.query_one(
        "SELECT path, title, section FROM wiki_chunks WHERE chunk_id = ?", (chunk_id,)
    )
    if not row:
        return {"path": "", "title": "", "section": ""}
    return {"path": row["path"], "title": row["title"], "section": row["section"]}


def hybrid_search(
    query: str,
    *,
    top_k: int = 5,
    include_history: bool = False,
    db: Optional[Database] = None,
    person_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Hybrid 检索。带 ``person_id`` 时只返回该用户可见的片段
    （自己的 ``users/<pid>/`` + 全员共享路径），实现多用户记忆隔离。"""
    from src.identity.store import in_scope

    db = db or get_db()
    fused: dict[str, dict[str, Any]] = {}

    def add(hit: dict[str, Any], retriever: str) -> None:
        cid = hit.get("chunk_id")
        if not cid:
            return
        meta = _chunk_meta(db, cid)
        if not in_scope(meta.get("path", ""), person_id):
            return  # 不属于该用户、也非共享 → 跳过
        section = hit.get("section") or meta.get("section") or ""
        if not include_history and section == "历史":
            return
        score = hit.get("score", 0.0) * _WEIGHTS.get(retriever, 0.5)
        if cid in fused:
            fused[cid]["score"] += score
            fused[cid]["retrievers"].append(retriever)
            return
        fused[cid] = {
            "chunk_id": cid,
            "path": meta.get("path", ""),
            "title": hit.get("title") or meta.get("title"),
            "section": section,
            "content": hit.get("content", ""),
            "score": score,
            "retrievers": [retriever],
        }

    # 1) FTS5 关键词召回
    for hit in fts.search_bm25(db, query, top_k=top_k * 2):
        add(hit, "fts")

    # 2) 向量语义召回
    for hit in vector.search(query, top_k=top_k * 2):
        add(hit, "vector")

    results = sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    for r in results:
        r["source"] = f"{r['path']}#{r['section']}"
        r["retriever"] = "+".join(sorted(set(r.pop("retrievers"))))
    return results


def query_memories(
    db: Optional[Database] = None,
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    status: str = "active",
    limit: int = 20,
    person_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """结构化查询 memories（架构 7.6 第一路）。带 ``person_id`` 时按用户分区过滤。"""
    db = db or get_db()
    sql = "SELECT * FROM memories WHERE status = ?"
    params: list[Any] = [status]
    if subject:
        sql += " AND subject = ?"
        params.append(subject)
    if predicate:
        sql += " AND predicate = ?"
        params.append(predicate)
    if person_id:
        # 用户分区 OR 共享（非 users/）。
        sql += " AND (path LIKE ? OR path IS NULL OR path NOT LIKE 'users/%')"
        params.append(f"users/{person_id}/%")
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.query(sql, params)]
