"""LanceDB 向量索引层（架构 6/7.4/7.6）。

对 chunk 的 ``content`` 用本地 ``bge-small-zh-v1.5``（512 维）生成向量入库。
embedding 依赖（sentence-transformers/torch）较重，做成惰性加载 + 优雅降级：
缺失时向量层整体跳过，检索仅靠 FTS5（索引损坏可重建，符合架构容错原则）。
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from src import config
from src.memory.chunker import Chunk

_TABLE = "wiki_vectors"
# bge 中文检索推荐给 query 加指令前缀（passage 不加）。
_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

_model = None
_model_lock = threading.Lock()
_model_failed = False
_db_conn = None


def embeddings_available() -> bool:
    return _load_model() is not None


def _load_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _model_lock:
        if _model is not None or _model_failed:
            return _model
        try:
            config.ensure_dirs()
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(
                config.EMBEDDING_MODEL, cache_folder=str(config.HF_CACHE_DIR)
            )
        except Exception:
            _model_failed = True
            _model = None
    return _model


def _embed(texts: list[str], *, is_query: bool) -> Optional[list[list[float]]]:
    model = _load_model()
    if model is None:
        return None
    payload = [(_QUERY_PREFIX + t) for t in texts] if is_query else texts
    vecs = model.encode(payload, normalize_embeddings=True, convert_to_numpy=True)
    return [v.tolist() for v in vecs]


def _connect():
    global _db_conn
    if _db_conn is None:
        import lancedb

        config.ensure_dirs()
        _db_conn = lancedb.connect(str(config.LANCEDB_DIR))
    return _db_conn


def _open_or_create_table(rows: Optional[list[dict[str, Any]]] = None):
    conn = _connect()
    names = conn.table_names()
    if _TABLE in names:
        return conn.open_table(_TABLE)
    # 首次创建需要数据来推断 schema。
    if not rows:
        import pyarrow as pa

        schema = pa.schema(
            [
                pa.field("chunk_id", pa.string()),
                pa.field("path", pa.string()),
                pa.field("title", pa.string()),
                pa.field("section", pa.string()),
                pa.field("content", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), config.EMBEDDING_DIM)),
            ]
        )
        return conn.create_table(_TABLE, schema=schema)
    return conn.create_table(_TABLE, data=rows)


def reindex_path(db: Any, path: str, chunks: list[Chunk]) -> None:
    """按 path 先删后插（幂等）。db 参数仅为与其它派生层签名一致，未使用。"""
    if not chunks:
        delete_path(None, path)
        return
    vectors = _embed([c.content for c in chunks], is_query=False)
    if vectors is None:
        return  # embedding 不可用，跳过向量层
    rows = [
        {
            "chunk_id": c.chunk_id,
            "path": c.path,
            "title": c.title or "",
            "section": c.section or "",
            "content": c.content,
            "vector": vec,
        }
        for c, vec in zip(chunks, vectors)
    ]
    table = _open_or_create_table(rows if _TABLE not in _connect().table_names() else None)
    safe = path.replace("'", "''")
    try:
        table.delete(f"path = '{safe}'")
    except Exception:
        pass
    table.add(rows)


def delete_path(db: Any, path: str) -> None:
    if not embeddings_available():
        return
    try:
        conn = _connect()
        if _TABLE not in conn.table_names():
            return
        table = conn.open_table(_TABLE)
        safe = path.replace("'", "''")
        table.delete(f"path = '{safe}'")
    except Exception:
        pass


def search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    qvecs = _embed([query], is_query=True)
    if qvecs is None:
        return []
    try:
        conn = _connect()
        if _TABLE not in conn.table_names():
            return []
        table = conn.open_table(_TABLE)
        hits = table.search(qvecs[0]).limit(top_k).to_list()
    except Exception:
        return []
    results = []
    for h in hits:
        dist = h.get("_distance", 1.0)
        results.append(
            {
                "chunk_id": h.get("chunk_id"),
                "title": h.get("title"),
                "section": h.get("section"),
                "content": h.get("content"),
                "score": 1.0 / (1.0 + float(dist)),
                "retriever": "vector",
            }
        )
    return results


def drop_all() -> None:
    """重建索引时清空向量表。"""
    try:
        conn = _connect()
        if _TABLE in conn.table_names():
            conn.drop_table(_TABLE)
    except Exception:
        pass
