"""文件工具：工作目录沙箱内的读写改查"""
from __future__ import annotations

import os
from pathlib import Path

from .registry import ToolResult, tool

_WORKSPACE: str = os.getcwd()


def set_workspace(path: str):
    global _WORKSPACE
    _WORKSPACE = str(Path(path).resolve())


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_WORKSPACE) / p
    p = p.resolve()
    ws = Path(_WORKSPACE).resolve()
    # 沙箱检查：workspace 之外直接拒绝
    if not str(p).startswith(str(ws)):
        raise PermissionError(f"路径越界: {p}（工作目录沙箱为 {ws}）")
    return p


@tool(description="读取文本文件内容。path 可为相对工作目录的路径。")
def read_file(path: str, max_chars: int = 8000) -> str:
    """读取文件内容，超长时截断。"""
    p = _resolve(path)
    if not p.exists():
        return f"文件不存在: {p}"
    if p.is_dir():
        return f"这是一个目录，请用 list_dir: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[截断，文件共 {len(text)} 字符]"
    return text


@tool(description="写入文本文件（覆盖）。目录不存在会自动创建。")
def write_file(path: str, content: str) -> str:
    """写入文件并返回确认。"""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p}（{len(content)} 字符）"


@tool(description="编辑文本文件：将 old_text 精确替换为 new_text（替换全部匹配）。")
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """精确字符串替换式编辑，old_text 必须在文件中存在。"""
    p = _resolve(path)
    if not p.exists():
        return f"文件不存在: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if old_text not in text:
        return f"未找到待替换文本。文件前 500 字符预览:\n{text[:500]}"
    count = text.count(old_text)
    p.write_text(text.replace(old_text, new_text), encoding="utf-8")
    return f"已替换 {count} 处并保存 {p}"


@tool(description="列出目录内容（含子目录名与文件大小）。")
def list_dir(path: str = ".") -> str:
    """列出目录。"""
    p = _resolve(path)
    if not p.exists():
        return f"目录不存在: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    lines = []
    for e in entries[:200]:
        if e.is_dir():
            lines.append(f"[目录] {e.name}/")
        else:
            lines.append(f"        {e.name}  ({e.stat().st_size} B)")
    if len(entries) > 200:
        lines.append(f"...共 {len(entries)} 项，仅显示前 200")
    return f"{p}\n" + "\n".join(lines)
