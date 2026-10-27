"""Python 代码执行工具：独立子进程，超时保护"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from .registry import tool

_WORKSPACE: str = os.getcwd()


def configure(workspace: str):
    global _WORKSPACE
    _WORKSPACE = workspace


_RUNNER = '''
import sys, traceback
try:
    code = sys.stdin.read()
    g = {"__name__": "__main__"}
    exec(compile(code, "<agent_code>", "exec"), g)
except Exception:
    traceback.print_exc()
    sys.exit(1)
'''


@tool(description="执行 Python 代码片段（通过 stdin 传入独立进程），返回 stdout/stderr。")
def run_code(code: str, timeout: int = 120) -> str:
    """运行 Python 代码。适合数据处理、计算、调用第三方库。"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(_RUNNER)
        runner_path = f.name
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.run(
                [sys.executable, runner_path],
                input=code.encode("utf-8"),
                cwd=_WORKSPACE,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return f"[超时] 代码 {timeout}s 未完成被终止。"
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        result = f"exit_code={proc.returncode}"
        if out:
            result += f"\n--- stdout ---\n{out[:8000]}"
        if err:
            result += f"\n--- stderr ---\n{err[:4000]}"
        return result
    finally:
        try:
            os.unlink(runner_path)
        except OSError:
            pass
