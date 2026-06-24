"""Wiki Markdown 解析 / 序列化（架构 7.3）。

严格模板：frontmatter + ``# 标题`` + ``## 当前`` + ``## 历史``。

agent 写入的 bullet 末尾带一个隐藏的结构化标记，便于派生 memories 与冲突检测，
人工书写的 bullet 没有标记也能正常解析（只是结构信息靠文本推断）::

    ## 当前
    - 优先二楼安静区  <!--mem:{"s":"图书馆偏好","p":"座位区域","t":"preference","c":0.9}-->

    ## 历史
    - 2026-06-20~2026-06-23：曾偏好三楼靠窗  <!--mem:{...,"vf":"2026-06-20","vt":"2026-06-23"}-->
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from src import config

CURRENT_HEADING = "## 当前"
HISTORY_HEADING = "## 历史"

_MEM_MARKER_RE = re.compile(r"\s*<!--mem:(?P<json>\{.*?\})-->\s*$")
_HISTORY_RANGE_RE = re.compile(r"^(?P<vf>\d{4}-\d{2}-\d{2})~(?P<vt>\d{4}-\d{2}-\d{2})[:：]\s*(?P<rest>.*)$")


def _slugify(text: str) -> str:
    text = re.sub(r"[\s/\\]+", "_", text.strip())
    text = re.sub(r"[^\w一-鿿_-]", "", text)
    return text or "untitled"


@dataclass
class Bullet:
    """一条 bullet 记忆。"""

    value: str  # 展示文本（不含标记 / 不含历史日期前缀）
    subject: Optional[str] = None
    predicate: Optional[str] = None
    memory_type: Optional[str] = None
    confidence: Optional[float] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None

    def marker(self) -> dict[str, Any]:
        m: dict[str, Any] = {}
        if self.subject:
            m["s"] = self.subject
        if self.predicate:
            m["p"] = self.predicate
        if self.memory_type:
            m["t"] = self.memory_type
        if self.confidence is not None:
            m["c"] = self.confidence
        if self.valid_from:
            m["vf"] = str(self.valid_from)
        if self.valid_to:
            m["vt"] = str(self.valid_to)
        return m

    def effective_predicate(self) -> str:
        """没有显式 predicate 时退化为文本指纹，用于去重/冲突判断。"""
        if self.predicate:
            return self.predicate
        return "_text:" + hashlib.sha1(self.value.encode("utf-8")).hexdigest()[:10]

    def render_current(self) -> str:
        m = self.marker()
        suffix = f"  <!--mem:{json.dumps(m, ensure_ascii=False, separators=(',', ':'))}-->" if m else ""
        return f"- {self.value}{suffix}"

    def render_history(self) -> str:
        m = self.marker()
        vf = self.valid_from or ""
        vt = self.valid_to or date.today().isoformat()
        prefix = f"{vf}~{vt}：" if vf else f"{vt}："
        suffix = f"  <!--mem:{json.dumps(m, ensure_ascii=False, separators=(',', ':'))}-->" if m else ""
        return f"- {prefix}{self.value}{suffix}"


def _parse_bullet(line: str, *, is_history: bool) -> Optional[Bullet]:
    line = line.rstrip()
    if not line.startswith("- "):
        return None
    body = line[2:]

    meta: dict[str, Any] = {}
    mo = _MEM_MARKER_RE.search(body)
    if mo:
        try:
            meta = json.loads(mo.group("json"))
        except Exception:
            meta = {}
        body = body[: mo.start()].rstrip()

    valid_from = meta.get("vf")
    valid_to = meta.get("vt")

    if is_history:
        rm = _HISTORY_RANGE_RE.match(body)
        if rm:
            valid_from = valid_from or rm.group("vf")
            valid_to = valid_to or rm.group("vt")
            body = rm.group("rest").strip()

    return Bullet(
        value=body.strip(),
        subject=meta.get("s"),
        predicate=meta.get("p"),
        memory_type=meta.get("t"),
        confidence=meta.get("c"),
        valid_from=valid_from,
        valid_to=valid_to,
    )


@dataclass
class WikiDoc:
    path: str  # 相对 wiki/ 的 posix 路径，如 "preferences/library.md"
    frontmatter: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    preamble: str = ""  # 标题与 ## 当前 之间的自由文本
    current: list[Bullet] = field(default_factory=list)
    history: list[Bullet] = field(default_factory=list)
    raw_text: str = ""

    @property
    def doc_id(self) -> str:
        fid = self.frontmatter.get("id")
        if fid:
            return str(fid)
        return _slugify(Path(self.path).with_suffix("").as_posix().replace("/", "."))

    @property
    def memory_type(self) -> str:
        return str(self.frontmatter.get("type", "semantic"))

    @property
    def confidence(self) -> float:
        try:
            return float(self.frontmatter.get("confidence", 0.8))
        except (TypeError, ValueError):
            return 0.8

    def serialize(self) -> str:
        fm = dict(self.frontmatter)
        fm["updated_at"] = date.today().isoformat()
        fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()

        parts = [f"---\n{fm_text}\n---", "", f"# {self.title}".rstrip(), ""]
        if self.preamble.strip():
            parts.append(self.preamble.strip())
            parts.append("")
        parts.append(CURRENT_HEADING)
        parts.extend(b.render_current() for b in self.current)
        parts.append("")
        parts.append(HISTORY_HEADING)
        parts.extend(b.render_history() for b in self.history)
        parts.append("")
        return "\n".join(parts)


def parse_text(text: str, path: str) -> WikiDoc:
    frontmatter: dict[str, Any] = {}
    body = text

    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_block = text[3:end].strip()
            try:
                frontmatter = yaml.safe_load(fm_block) or {}
            except Exception:
                frontmatter = {}
            body = text[end + 4 :]

    lines = body.splitlines()
    title = ""
    preamble_lines: list[str] = []
    current: list[Bullet] = []
    history: list[Bullet] = []

    section: Optional[str] = None  # None | "preamble" | "current" | "history"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not title and section is None:
            title = stripped[2:].strip()
            section = "preamble"
            continue
        if stripped == CURRENT_HEADING:
            section = "current"
            continue
        if stripped == HISTORY_HEADING:
            section = "history"
            continue
        if stripped.startswith("## "):
            # 其它二级标题，归回 preamble 收尾，不再解析为 bullet。
            section = "other"
            continue

        if section == "current":
            b = _parse_bullet(line, is_history=False)
            if b:
                current.append(b)
        elif section == "history":
            b = _parse_bullet(line, is_history=True)
            if b:
                history.append(b)
        elif section == "preamble":
            preamble_lines.append(line)

    return WikiDoc(
        path=path,
        frontmatter=frontmatter,
        title=title or str(frontmatter.get("title", path)),
        preamble="\n".join(preamble_lines).strip(),
        current=current,
        history=history,
        raw_text=text,
    )


def abs_path(rel_path: str) -> Path:
    return config.WIKI_DIR / rel_path


def rel_path(p: Path) -> str:
    return p.resolve().relative_to(config.WIKI_DIR).as_posix()


def read_doc(rel_path_str: str) -> Optional[WikiDoc]:
    p = abs_path(rel_path_str)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    return parse_text(text, rel_path_str)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(rel_path_str: str, text: str) -> None:
    """原子写回（临时文件 + rename），保证不会写出半截文件。"""
    p = abs_path(rel_path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def iter_wiki_files() -> list[str]:
    """列出 wiki/ 下所有 .md 的相对路径。"""
    root = config.WIKI_DIR
    if not root.exists():
        return []
    return sorted(rel_path(p) for p in root.rglob("*.md"))
