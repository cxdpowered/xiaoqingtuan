"""幂等全量重建命令（架构 7.8）。

    python -m src.memory.reindex

扫描 wiki/**/*.md → 重建 wiki_chunks / memories / relations / FTS5 / LanceDB。
用于索引损坏恢复、换 embedding 模型、git 拉取他处编辑后同步。
"""

from __future__ import annotations

from src.memory import store, vector
from src.memory.wiki import iter_wiki_files
from src.storage.db import Database, get_db


def reindex_all(db: Database | None = None, *, drop_vectors: bool = True) -> dict[str, int]:
    db = db or get_db()
    if drop_vectors:
        vector.drop_all()
    # 清空派生表（按全表，比逐 path 更彻底）。
    for table in ("wiki_chunks", "memories", "relations"):
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM wiki_fts")

    files = iter_wiki_files()
    for rel in files:
        store.rebuild_path(db, rel)

    chunk_count = db.query_one("SELECT COUNT(*) AS c FROM wiki_chunks")["c"]
    mem_count = db.query_one("SELECT COUNT(*) AS c FROM memories")["c"]
    return {"files": len(files), "chunks": chunk_count, "memories": mem_count}


def main() -> None:
    stats = reindex_all()
    has_vec = vector.embeddings_available()
    print(
        f"重建完成：{stats['files']} 个文件 / {stats['chunks']} chunks / {stats['memories']} memories。"
        f" 向量层：{'已索引 (bge-small)' if has_vec else '跳过 (embedding 不可用)'}"
    )


if __name__ == "__main__":
    main()
