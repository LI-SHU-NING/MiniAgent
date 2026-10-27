"""Planner：把用户目标分解为任务清单，失败时重规划

输出 JSON: {"tasks": [{"title": "...", "detail": "..."}]}
不依赖工具调用，纯 JSON 模式（对 planner_model 无 FC 要求）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .llm import LLMClient, split_think


@dataclass
class PlanTask:
    idx: int
    title: str
    detail: str = ""
    status: str = "pending"  # pending / done / failed / skipped
    result: str = ""


@dataclass
class Plan:
    goal: str
    tasks: list = field(default_factory=list)
    replans: int = 0
    direct_answer: Optional[str] = None  # 非空 = 无需工具，直接答复

    def summary_lines(self) -> str:
        marks = {"pending": " ", "done": "x", "failed": "!", "skipped": "-"}
        return "\n".join(
            f"[{marks.get(t.status, ' ')}] #{t.idx+1} {t.title}" for t in self.tasks
        )


PLAN_SYSTEM = """你是任务规划器。先判断用户目标是否需要调用工具（文件/命令/代码/搜索/浏览器等真实环境操作）：
- 闲聊问候、常识问答、观点咨询、纯文字创作（写诗/起名/文案等可直接在答复中给出的内容）→ 无需工具
- 需要读写文件、执行命令或代码、联网搜索、浏览器操作、获取实时或环境信息 → 需要工具
只输出 JSON，二选一：
无需工具: {"need_tools": false, "answer": "直接给用户的完整答复"}
需要工具: {"need_tools": true, "tasks": [{"title": "简短标题", "detail": "具体要做什么、验收标准"}]}
需要工具时任务数量控制在 1-6 个，简单问题只给 1 个任务，不要过度拆分。"""

REPLAN_SYSTEM = """你是任务重规划器。之前的执行计划出现了失败，请基于失败原因调整计划。
只输出 JSON，格式：
{"tasks": [{"title": "简短标题", "detail": "具体要做什么"}]}"""


def _parse_plan(text: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """解析规划输出，返回 (tasks, direct_answer)。两者互斥，均失败返回 (None, None)。"""
    _, content = split_think(text or "")
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None, None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    # 无需工具直答
    if obj.get("need_tools") is False or ("answer" in obj and "tasks" not in obj):
        ans = str(obj.get("answer", "")).strip()
        if ans:
            return None, ans
        return None, None
    tasks = obj.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None, None
    out = []
    for t in tasks[:8]:
        if isinstance(t, dict) and t.get("title"):
            out.append({"title": str(t["title"])[:100], "detail": str(t.get("detail", ""))[:500]})
    return (out or None), None


class Planner:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def plan(self, goal: str, context: str = "") -> Plan:
        from datetime import datetime

        now = datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        user = f"【当前日期】{now.strftime('%Y年%m月%d日')}（星期{wd}）\n目标: {goal}"
        if context:
            user += f"\n\n可用上下文:\n{context}"
        last_err = None
        for attempt in range(3):
            try:
                resp = self.llm.chat(
                    [
                        {"role": "system", "content": PLAN_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.2,
                )
                tasks, direct = _parse_plan(resp.raw_content)
                last_err = resp.raw_content
            except Exception as e:  # noqa: BLE001 模型超时/网络异常，重试
                tasks, direct = None, None
                last_err = f"{type(e).__name__}: {e}"
            if direct is not None:
                return Plan(goal=goal, direct_answer=direct)
            if tasks:
                plan = Plan(goal=goal)
                for i, t in enumerate(tasks):
                    plan.tasks.append(PlanTask(idx=i, title=t["title"], detail=t["detail"]))
                return plan
        # 兜底：规划失败就单任务直跑（宁可多执行也不误答）
        plan = Plan(goal=goal)
        plan.tasks.append(PlanTask(idx=0, title="直接完成目标", detail=goal))
        return plan

    def replan(self, plan: Plan, failed_task: PlanTask, reason: str) -> Plan:
        remaining = [
            {"title": t.title, "status": t.status}
            for t in plan.tasks
            if t.idx > failed_task.idx
        ]
        user = (
            f"总体目标: {plan.goal}\n"
            f"已失败任务: #{failed_task.idx+1} {failed_task.title}\n"
            f"失败原因: {reason[:1500]}\n"
            f"未执行的后续任务: {json.dumps(remaining, ensure_ascii=False)}\n"
            f"已尝试重规划次数: {plan.replans}"
        )
        for _ in range(2):
            try:
                resp = self.llm.chat(
                    [
                        {"role": "system", "content": REPLAN_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.3,
                )
                tasks, _ = _parse_plan(resp.raw_content)
            except Exception:  # noqa: BLE001 重规划模型异常，走兜底
                tasks = None
            if tasks:
                new = Plan(goal=plan.goal, replans=plan.replans + 1)
                for i, t in enumerate(tasks):
                    new.tasks.append(PlanTask(idx=i, title=t["title"], detail=t["detail"]))
                return new
        # 重规划失败：跳过失败任务继续原有计划
        failed_task.status = "skipped"
        plan.replans += 1
        return plan
