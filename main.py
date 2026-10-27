"""通用智能体 CLI 入口

用法:
  python main.py                 # 交互模式
  python main.py "把百度首页标题抄下来保存到 notes.txt"   # 直接执行目标
命令:
  /new     新会话（清空上下文）
  /tools   查看可用工具
  /memory  查看长期记忆条数
  /exit    退出
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from agent.config import load_config
from agent.executor import Agent, AgentEvent
from agent.tools.registry import ToolResult

console = Console()

BANNER = """[bold cyan]MiniAgent[/] - 开源通用智能体
[dim]Plan-and-Execute · ReAct · Tool Calling · Browser[/dim]
[dim]模型: OpenAI 兼容 API · MIT License[/dim]"""


def render_event(ev: AgentEvent):
    t = ev.type
    p = ev.payload
    if t == "log":
        console.print(f"[dim]· {p.get('text', '')}[/]")
    elif t == "plan_created":
        table = Table(
            title=("重规划后的新计划" if p.get("replan") else "执行计划"),
            show_header=False,
            box=None,
            padding=(0, 1),
        )
        table.add_column(style="bold")
        table.add_column()
        for task in p["tasks"]:
            table.add_row(f"#{task['idx']+1}", task["title"])
        console.print(Panel(table, border_style="cyan"))
    elif t == "task_start":
        console.print(f"\n[bold cyan]▶ 任务 #{p['idx']+1}[/] [bold]{p['title']}[/]")
    elif t == "task_done":
        if p["success"]:
            console.print(f"[bold green]✔ 任务 #{p['idx']+1} 完成[/] [dim]{p['summary'][:120]}[/]")
        else:
            console.print(f"[bold red]✘ 任务 #{p['idx']+1} 失败[/] [dim]{p['summary'][:120]}[/]")
    elif t == "replan":
        console.print(f"[yellow]⟳ 触发重规划[/] [dim]{p.get('reason', '')[:150]}[/]")
    elif t == "thought":
        prefix = "思考" if p.get("thinking") else "说"
        console.print(f"[dim italic]  [{prefix}] {p['text'][:300]}[/]")
    elif t == "tool_call":
        args = ", ".join(f"{k}={v}" for k, v in p.get("arguments", {}).items())
        console.print(f"  [yellow]→ {p['tool']}([/][dim]{args[:200]}[/][yellow])[/]")
    elif t == "tool_result":
        style = "red" if p.get("error") else "dim green"
        text = p["text"][:400].replace("\n", " ⏎ ")
        console.print(f"  [{style}]← {text}[/]")
    elif t == "error":
        console.print(f"[bold red]错误:[/] {p.get('text', '')}")
    elif t == "final":
        console.print(Panel(Markdown(p["text"]), title="执行结果", border_style="green"))


def run_goal_streamed(agent: Agent, goal: str) -> str:
    """在后台线程执行目标，主线程显示 spinner，事件实时渲染。"""
    done = threading.Event()
    result_box: dict = {}

    def worker():
        try:
            result_box["answer"] = agent.run(goal)
        except Exception as e:  # noqa: BLE001
            result_box["answer"] = f"执行异常: {e}"
            console.print_exception()
        finally:
            done.set()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    with console.status("[cyan]思考与执行中...", spinner="dots"):
        # 事件由 agent 的 on_event 回调同步打印（来自工作线程，rich 线程安全够用）
        th.join()
    return result_box.get("answer", "")


def main():
    cfg, root = load_config()
    console.print(Panel(BANNER, border_style="cyan"))

    arg_goal = " ".join(sys.argv[1:]).strip()
    agent = Agent(cfg, on_event=render_event, quiet=False)

    try:
        if arg_goal:
            run_goal_streamed(agent, arg_goal)
            return
        _interactive(agent)
    finally:
        agent.close()


def _interactive(agent: Agent):
    console.print("[dim]输入目标开始；/new /tools /memory /exit[/]")
    while True:
        try:
            goal = console.input("\n[bold cyan]目标>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not goal:
            continue
        if goal in ("/exit", "/quit", "exit", "quit"):
            break
        if goal == "/new":
            agent.history.clear()
            agent.last_session_summary = ""
            console.print("[green]已开启新会话[/]")
            continue
        if goal == "/tools":
            for name in agent.registry.names():
                t = agent.registry.get(name)
                mark = " [red](危险)[/]" if t.dangerous else ""
                console.print(f"  [yellow]{name}[/]{mark} - {t.description}")
            continue
        if goal == "/memory":
            n = len(agent.memory.entries) if agent.memory else 0
            console.print(f"[dim]长期记忆 {n} 条[/]")
            continue
        run_goal_streamed(agent, goal)


if __name__ == "__main__":
    main()
