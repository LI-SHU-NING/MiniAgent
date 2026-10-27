"""Shell 工具：PowerShell 命令执行，带超时与危险命令拦截"""
from __future__ import annotations

import os
import subprocess

from .registry import ToolResult, tool

_WORKSPACE: str = os.getcwd()
_DANGEROUS: list[str] = []
_CONFIRM: bool = True
_CONFIRMER = None  # callable(pattern, command) -> bool  True=允许执行


def configure(workspace: str, dangerous_patterns: list, confirm: bool, confirmer=None):
    global _WORKSPACE, _DANGEROUS, _CONFIRM, _CONFIRMER
    _WORKSPACE = workspace
    _DANGEROUS = [p.lower() for p in dangerous_patterns]
    _CONFIRM = confirm
    _CONFIRMER = confirmer


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def is_dangerous(command: str) -> str | None:
    low = command.lower()
    for pat in _DANGEROUS:
        if pat in low:
            return pat
    return None


@tool(
    description="在工作目录执行 PowerShell 命令并返回 stdout/stderr。适合 pip/git/系统操作等。",
    dangerous=True,
)
def run_command(command: str, timeout: int = 120) -> str:
    """执行 shell 命令。危险命令需确认或会被拒绝。"""
    pat = is_dangerous(command)
    if pat is not None:
        if _CONFIRMER is not None:
            allowed = _CONFIRMER(pat, command)
        elif _CONFIRM:
            allowed = False  # 无确认器时默认拒绝，安全第一
        else:
            allowed = False
        if not allowed:
            return f"[安全拦截] 命令包含危险模式 '{pat}'，已被拒绝执行。请改用其他方案。"

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=_WORKSPACE,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[超时] 命令 {timeout}s 未完成被终止。可尝试拆分任务或加大 timeout 参数。"

    out = _decode(proc.stdout).strip()
    err = _decode(proc.stderr).strip()
    result = f"exit_code={proc.returncode}"
    if out:
        result += f"\n--- stdout ---\n{out[:8000]}"
    if err:
        result += f"\n--- stderr ---\n{err[:4000]}"
    return result
