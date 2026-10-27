"""Executor：Agent 主循环

流程 = 记忆召回 -> 规划 -> 逐任务 ReAct -> 失败重规划 -> 汇总
所有过程通过 AgentEvent 事件流输出，CLI / Web UI 消费渲染。
"""
from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .config import Config
from .llm import LLMResponse, LLMClient, parse_json_action
from .memory import ConversationManager, VectorMemory
from .planner import Plan, PlanTask, Planner
from .tools.browser import tools as browser_tools
from .tools.browser.browser import BrowserSession
from .tools import shell_tools, python_tools, file_tools, web_tools, doc_tools
from .tools.registry import ToolRegistry, ToolResult, tool

# ---------------------------------------------------------------- 事件

EVENT_TYPES = (
    "plan_created", "task_start", "task_done", "replan",
    "thought", "tool_call", "tool_result", "progress", "delta", "error", "final", "log",
)


@dataclass
class AgentEvent:
    type: str
    payload: dict = field(default_factory=dict)


EventCallback = Callable[[AgentEvent], None]

# ---------------------------------------------------------------- 系统 Prompt

EXECUTOR_SYSTEM = """你是通用智能体的任务执行者。你的职责是利用工具完成【当前任务】。

工作规则：
1. 每一步先简述思路，再决定是否调用工具；工具结果是真实环境观察，一切以观察为准
2. 文件操作只能在工作目录内；命令执行、代码执行结果可能报错，报错后分析原因修正重试
3. 不要重复执行完全相同的失败调用；连续失败应换思路
4. 任务完成后立即调用 finish_task 工具；确认无法完成时调用 finish_task(success=false, reason=原因)
5. 浏览器操作后系统会自动返回带 ref 编号的页面快照，用 ref 编号操作元素
6. 需要记住用户偏好/重要结论时调用 save_memory；需要历史经验时调用 search_memory
7. 过程性叙述保持简洁；但 finish_task 的 summary 是任务的最终产出——用户对输出有字数、
   格式或内容要求时（如"写500字总结"），必须在 summary 中完整交付全部内容，绝不能只写"已完成"
8. 当前网络为中国大陆环境：google.com、facebook.com 等站点无法访问，不要尝试；
   联网搜索优先使用 web_search 工具，浏览器搜索请访问 bing.com 或 baidu.com
9. 搜索结果只是摘要预览：回答需要具体细节/数据/正文时，用 fetch_url 抓取搜索结果里的链接读取全文，
   不要只凭摘要作答"""

SEARCH_STUCK_HINT = (
    "[系统] 你已连续多次使用 web_search。如果结果仍不理想，请改变策略，不要再用相同思路反复搜索：\n"
    "1) 用 browser_navigate 直接访问领域知名站点（数据类：worldometer.info / "
    "data.worldbank.org / population.un.org / stats.gov.cn 等）\n"
    "2) 用 fetch_url 直接抓取之前搜索结果中看似相关的链接全文\n"
    "3) 换更具体、更口语化的中文关键词"
)

JSON_MODE_RULE = """
（重要）你当前处于 JSON 工具模式：每次回复必须是且仅是一个 JSON 对象，
{"action": "<工具名>", "arguments": {...}}；完成答复时 {"action": "final", "answer": "..."}
禁止在未调用任何工具的情况下直接 final——涉及搜索/查询/最新信息的任务必须先调用
web_search 等工具获取真实信息，凭记忆回答实时性问题是被禁止的。"""

PARALLEL_RULE = """
（重要：并行工具调用）你的一条回复可以同时返回多个 tool_calls。当有多个互不依赖
的操作时——例如同时用多个不同关键词搜索、同时抓取多个链接正文、同时读取多个文件——
必须在本轮一次性并行发出全部 tool_calls，不要拆成多轮逐个执行。仅当下一步严格依赖
上一步结果时才顺序执行。并行能显著减少步数、缩短总耗时，并降低工具输出在对话中的
占比。"""


# ---------------------------------------------------------------- Agent


class Agent:
    def __init__(
        self,
        cfg: Config,
        on_event: Optional[EventCallback] = None,
        quiet: bool = False,
        session_id: str = "cli",
    ):
        self.cfg = cfg
        self.on_event = on_event
        self.quiet = quiet  # True: 危险命令直接拒绝（Web 模式）
        self.session_id = session_id

        # 模型统一走 api 段
        use_api = bool(cfg.api.api_key)
        exec_base = cfg.api.base_url
        exec_key = cfg.api.api_key or "not-needed"
        exec_model = cfg.api.executor_model
        plan_model = cfg.api.planner_model
        self.executor_llm = LLMClient(
            exec_base,
            exec_model,
            timeout=cfg.agent.request_timeout,
            no_think=False if use_api else cfg.agent.no_think,
            max_tokens=cfg.agent.executor_max_tokens,
            api_key=exec_key,
        )
        self.planner_llm = LLMClient(
            exec_base,
            plan_model,
            timeout=cfg.agent.request_timeout,
            no_think=False if use_api else cfg.agent.no_think,
            max_tokens=cfg.agent.planner_max_tokens,
            api_key=exec_key,
        )
        # 视觉旁路模型：工具返回图片时走它分析转文本，执行器模型无需视觉能力
        self.vision_llm = LLMClient(
            cfg.api.base_url,
            cfg.api.vision_model,
            timeout=cfg.agent.request_timeout,
            max_tokens=cfg.agent.executor_max_tokens,
            api_key=cfg.api.api_key,
        )
        self.planner = Planner(self.planner_llm)
        self.history = ConversationManager(self.planner_llm, cfg.agent.history_budget_chars)
        self.memory: Optional[VectorMemory] = None
        if cfg.memory.enabled:
            try:
                self.memory = VectorMemory(
                    cfg.api.base_url,
                    cfg.api.embed_model,
                    cfg.memory.path,
                    cfg.memory.top_k,
                    api_key=cfg.api.api_key,
                    embed_base_url=cfg.api.embed_base_url,
                    embed_api_key=cfg.api.embed_api_key,
                    allow_local_fallback=cfg.memory.allow_local_fallback,
                )
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    AgentEvent("log", {"text": f"向量记忆初始化失败，已禁用: {exc}"})
                )

        # 工具注册
        self.registry = ToolRegistry()
        self._register_tools()

        # 工具调用模式：运行时探测（native 失败自动降级 json）
        self.tool_mode: str = "native"
        self._mode_probed = False

        self.last_session_summary = ""  # 跨目标上下文

    # -------- 事件 --------

    def _emit(self, ev: AgentEvent):
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                pass
        self._log_event(ev)

    def _log_event(self, ev: AgentEvent):
        """完整执行日志落盘（JSONL）：思考/工具调用/结果/报错/重规划全程可追溯。"""
        if ev.type == "delta":
            return  # 流式增量不落日志，避免刷屏；完整 progress/final 已落
        log_path = self.cfg.logging.file
        if not log_path:
            return
        try:
            payload = dict(ev.payload)
            # 日志中进一步限制体积（tool_result 已在 payload 截断过一轮）
            for k, v in payload.items():
                s = str(v)
                if len(s) > 400:
                    payload[k] = s[:400] + "...[日志截断]"
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "sid": self.session_id, "type": ev.type, **payload}
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 日志失败不影响主流程
            pass

    # -------- 工具轨迹与搜索僵局 --------

    def _stream_delta(self, kind: str, piece: str):
        """模型流式回调：把正在生成的正文增量实时推给前端（打字机效果）。
        目前仅用于无工具调用（多任务最终汇总）；工具轮次保持非流式。"""
        if kind == "content" and piece.strip():
            self._emit(AgentEvent("delta", {"text": piece}))

    def _stream_answer(self, text: str, chunk: int = 6, delay: float = 0.012):
        """直答文本分片推送：planner 已完整生成，此处分片制造打字机效果。"""
        for i in range(0, len(text), chunk):
            self._emit(AgentEvent("delta", {"text": text[i:i + chunk]}))
            time.sleep(delay)

    def _note_tool_call(self, name: str, args: dict, search_count: int, messages: list) -> int:
        """记录真实工具轨迹；连续 3 次 web_search 后注入换策略提示（搜索僵局破除）。"""
        self._tool_trace.append(f"{name}({_safe_args(args)})")
        if name == "web_search":
            search_count += 1
            if search_count == 3:
                messages.append({"role": "user", "content": SEARCH_STUCK_HINT})
        return search_count

    @staticmethod
    def _compress_history(messages: list, keep: int = 2):
        """ReAct 历史滚动压缩：长工具结果只保留最近 keep 条全文，更早的截为摘要。
        防止长上下文稀释任务指令（8B 模型有效注意力远小于标称窗口）。"""
        long_idx = []
        for i, m in enumerate(messages):
            if i < 2:  # 跳过 system 与首条任务简报
                continue
            c = m.get("content")
            if not isinstance(c, str) or len(c) <= 600:
                continue
            if m.get("role") == "tool" or (
                m.get("role") == "user" and (c.startswith("[工具") or "[工具" in c[:40])
            ):
                long_idx.append(i)
        for i in long_idx[:-keep]:
            messages[i]["content"] = str(messages[i]["content"])[:200] + "\n...[历史工具结果已压缩，最新状态以最近观察为准]"

    @staticmethod
    def _ensure_tool_pairing(messages: list) -> int:
        """确保 assistant.tool_calls 与 role=tool 消息严格配对。

        修复"assistant 消息带 tool_calls、却缺少对应 tool 回复"的畸形历史——
        这是 OpenAI 兼容接口返回 400（An assistant message with 'tool_calls'
        must be followed by tool messages responding to each 'tool_call_id'）的根因。
        就地修复 messages：
          1) 每个 tool_call_id 必须紧跟一条同 id 的 tool 消息；若该消息存在于列表
             其它位置则移动到紧跟处，若确实不存在则补一条占位 tool 消息（"补齐"）。
          2) 无 id 的非法 tool_call，以及无对应 tool 消息且无法补齐的孤立
             tool_calls 条目，从 assistant 消息中移除（"清理"）。
          3) 没有前置 assistant.tool_calls 声明的孤立 tool 消息一并删除。
        返回本次修复的条目数（0 表示原本就配对良好）。
        """
        fixed = 0
        tool_idx: dict = {}
        for idx, m in enumerate(messages):
            if m.get("role") == "tool":
                tool_idx.setdefault(m.get("tool_call_id"), []).append(idx)

        consumed: set = set()
        out: list = []
        for idx, m in enumerate(messages):
            if idx in consumed:
                continue
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                original = list(m.get("tool_calls") or [])
                valid = [tc for tc in original if isinstance(tc, dict) and tc.get("id")]
                if len(valid) != len(original):
                    fixed += len(original) - len(valid)  # 非法 tool_call（无 id）无法配对，清理
                if not valid:
                    # 全部被清理：改写成普通文本 assistant，避免空 tool_calls 再次触发 400
                    m = {"role": "assistant",
                         "content": m.get("content") or "(已清理无配对的工具调用)"}
                elif len(valid) != len(original):
                    m = {**m, "tool_calls": valid}
                out.append(m)
                for tc in valid:
                    tid = tc["id"]
                    cand = [j for j in tool_idx.get(tid, []) if j not in consumed and j != idx]
                    if cand:
                        consumed.add(cand[0])
                        out.append(messages[cand[0]])
                    else:
                        out.append({"role": "tool", "tool_call_id": tid,
                                    "content": "[工具未返回结果，系统已自动补齐占位]"})
                        fixed += 1
                continue
            if role == "tool":
                # 未被任何 assistant.tool_calls 紧随消费：重复项或孤立 tool 消息，删除
                fixed += 1
                continue
            out.append(m)

        messages[:] = out
        return fixed

    # -------- 视觉旁路 --------

    def _vision_describe(self, result: "ToolResult", messages: list, tool_name: str):
        """工具返回图片时走视觉模型分析，转成文本描述注入执行器上下文。
        这样执行器模型（如 deepseek-r1 无视觉）也能"看"截图。
        视觉模型不可用时降级为文本说明，不阻塞执行。"""
        ctx = result.text[:1500] or "(无附加文本)"
        prompt = (
            f"工具 {tool_name} 返回了一张截图（可能含页面快照文本）。请用简洁中文描述图片内容："
            f"页面布局、可见的文字、可点击的按钮/链接、表格或图表数据、弹窗或错误提示等。"
            f"不要推测，只描述实际看到的内容。\n\n附带的文本快照:\n{ctx}"
        )
        try:
            resp = self.vision_llm.chat(
                [LLMClient.user_multimodal(prompt, result.images)],
                temperature=0.2,
            )
            desc = resp.content.strip() or "(视觉模型未返回有效描述)"
            self._emit(AgentEvent("thought", {"step": 0, "text": f"[视觉分析] {desc[:300]}", "thinking": True}))
        except Exception as e:  # noqa: BLE001 视觉模型失败不阻塞主流程
            desc = f"(视觉分析失败: {type(e).__name__}: {e}，请基于文本快照继续)"
            self._emit(AgentEvent("log", {"text": f"视觉旁路失败: {e}"}))
        messages.append(
            {"role": "user", "content": f"[视觉模型对 {tool_name} 截图的分析]\n{desc}"}
        )

    # -------- 工具装配 --------

    def _register_tools(self):
        reg = self.registry
        file_tools.set_workspace(self.cfg.security.workspace)
        shell_tools.configure(
            self.cfg.security.workspace,
            self.cfg.security.dangerous_patterns,
            self.cfg.security.confirm_shell,
            confirmer=self._confirm_dangerous,
        )
        python_tools.configure(self.cfg.security.workspace)
        browser_tools.configure(
            self.cfg.browser.headless,
            self.cfg.browser.timeout,
            self.cfg.browser.viewport,
            self.cfg.browser.user_agent,
        )

        reg.register_module(file_tools)
        reg.register_module(shell_tools)
        reg.register_module(python_tools)
        reg.register_module(web_tools)
        reg.register_module(doc_tools)
        for t in browser_tools.ALL:
            reg.register_function(t)

        # 动态工具：finish_task / save_memory / search_memory
        agent_ref = self

        @tool(
            description=(
                "宣告当前任务完成或失败。summary 将直接呈现给用户，是任务的最终产出："
                "内容要详尽完整（通常不少于 200 字），包含执行过程要点、结果细节"
                "（数据/列表/结论完整给出，不要省略）、产出文件路径；"
                "用户对输出有字数/格式/内容要求时（如\"写500字总结\"），必须在 summary 中"
                "完整交付全部内容，绝不能用一两句'已完成'代替。"
            )
        )
        def finish_task(success: bool = True, summary: str = "", reason: str = "") -> str:
            """宣告当前任务结果，结束本任务循环。"""
            return "ok"

        reg.register_function(finish_task)

        if agent_ref.memory is not None:
            @tool(description="把重要事实/用户偏好/关键结论存入长期记忆，跨会话可查。")
            def save_memory(text: str) -> str:
                return agent_ref.memory.add(text, kind="fact")

            @tool(description="按语义检索长期记忆中的历史经验与用户偏好。")
            def search_memory(query: str) -> str:
                hits = agent_ref.memory.search(query)
                return "\n".join(hits) if hits else "没有找到相关记忆。"

            reg.register_function(save_memory)
            reg.register_function(search_memory)

    def _confirm_dangerous(self, pattern: str, command: str) -> bool:
        if self.quiet:
            return False
        # CLI 交互确认
        try:
            from rich.console import Console
            from rich.prompt import Confirm

            console = Console()
            console.print(f"[bold yellow]危险命令检测 [{pattern}]:[/] {command}")
            return Confirm.ask("允许执行该命令吗?", default=False)
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        try:
            browser_tools.close_session()
        except Exception:  # noqa: BLE001
            pass

    # -------- 工具调用模式探测 --------

    def _probe_tool_mode(self):
        """测试模型是否支持原生 FC：发一个明确需要工具调用的 prompt，
        检查是否返回 tool_calls。r1 等非 FC 模型会接受 tools 参数但不返回调用。"""
        try:
            resp = self.executor_llm.chat(
                [
                    {"role": "user", "content": "请调用 get_weather 工具查询北京天气"},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "查询天气",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        },
                    }
                ],
            )
            # 有 tool_calls → 原生模式可用；无 → 降级 JSON
            self.tool_mode = "native" if resp.tool_calls else "json"
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "tool" in msg or "function" in msg or "400" in msg:
                self.tool_mode = "json"
            else:
                self.tool_mode = "native"
        self._mode_probed = True
        self._emit(AgentEvent("log", {"text": f"工具调用模式: {self.tool_mode}"}))

    # -------- 主入口 --------

    def run(self, goal: str) -> str:
        """执行一个目标，返回最终答复文本。"""
        if not self._mode_probed:
            self._probe_tool_mode()

        # 记忆召回 + 跨目标上下文
        context_parts = []
        if self.memory:
            try:
                rc = self.memory.recall_context(goal)
                if rc:
                    context_parts.append(rc)
            except Exception:  # noqa: BLE001
                pass
        if self.history.summary:
            context_parts.append(f"此前对话摘要:\n{self.history.summary}")
        if self.last_session_summary:
            context_parts.append(f"上一个目标的执行结论:\n{self.last_session_summary}")
        if self.history.messages:
            recent = self.history.messages[-6:]
            convo = "\n".join(
                f"[{m['role']}] {str(m.get('content', ''))[:300]}" for m in recent
            )
            context_parts.append(f"近期对话:\n{convo}")
        context = "\n\n".join(context_parts)

        # 1. 规划（含"无需工具直答"判定）
        plan = self.planner.plan(goal, context=context)
        if plan.direct_answer is not None:
            answer = plan.direct_answer
            self._stream_answer(answer)  # 分片推送，制造打字机效果
            self._emit(AgentEvent("final", {"text": answer}))
            self.history.add("user", goal)
            self.history.add("assistant", answer[:1500])
            self.last_session_summary = f"{goal} -> {answer[:500]}"
            return answer
        self._emit(
            AgentEvent(
                "plan_created",
                {
                    "goal": goal,
                    "tasks": [{"idx": t.idx, "title": t.title, "detail": t.detail} for t in plan.tasks],
                },
            )
        )

        final_answer = ""
        task_results: list[str] = []
        self._tool_trace: list[str] = []  # 真实工具调用轨迹（供汇报反幻觉对齐）

        # 2. 逐任务执行（含重规划循环）
        task_cursor = 0
        while task_cursor < len(plan.tasks):
            task = plan.tasks[task_cursor]
            task.status = "pending"
            self._emit(AgentEvent("task_start", {"idx": task.idx, "title": task.title, "detail": task.detail}))

            ok, summary = self._run_task_react(plan, task, context, task_results)
            if ok:
                task.status = "done"
                task.result = summary
                task_results.append(f"#{task.idx+1} {task.title}: {summary}")
                self._emit(AgentEvent("task_done", {"idx": task.idx, "title": task.title, "success": True, "summary": summary}))
                task_cursor += 1
            else:
                task.status = "failed"
                task.result = summary
                self._emit(
                    AgentEvent(
                        "task_done",
                        {"idx": task.idx, "title": task.title, "success": False, "summary": summary},
                    )
                )
                _max_replans = self.cfg.agent.max_replans or 3  # 0 视为未设上限，用 3 作安全默认，避免无限重规划
                if plan.replans >= _max_replans:
                    self._emit(AgentEvent("error", {"text": "已达最大重规划次数，终止执行"}))
                    final_answer = f"任务执行失败: {summary}"
                    break
                self._emit(AgentEvent("replan", {"reason": summary[:500]}))
                new_plan = self.planner.replan(plan, task, summary)
                plan = new_plan
                task_cursor = 0
                done_titles = []
                # replan 后从头执行新计划（旧任务结果已并入 context）
                self._emit(
                    AgentEvent(
                        "plan_created",
                        {
                            "goal": plan.goal,
                            "tasks": [{"idx": t.idx, "title": t.title, "detail": t.detail} for t in plan.tasks],
                            "replan": True,
                        },
                    )
                )
                context = (context + "\n已完成任务结论:\n" + "\n".join(task_results)).strip()
                continue
        else:
            # 3. 全部完成 -> 汇总
            final_answer = self._summarize(goal, task_results)

        if final_answer:
            self._emit(AgentEvent("final", {"text": final_answer}))
            self.history.add("user", goal)
            self.history.add("assistant", final_answer[:1500])
            self.last_session_summary = f"{goal} -> {final_answer[:500]}"
        return final_answer

    # -------- ReAct 单任务 --------

    def _run_task_react(
        self, plan: Plan, task: PlanTask, context: str, prev_results: list[str]
    ) -> tuple[bool, str]:
        # 0 = 不设硬上限；仍留一条宽松保险丝，防止模型不收敛时无限步进
        max_steps = self.cfg.agent.max_steps_per_task or 60
        tools = self.registry.to_openai_tools()
        max_chars = self.cfg.agent.tool_result_max_chars

        plan_view = plan.summary_lines()
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        date_line = f"【当前日期时间】{now.strftime('%Y年%m月%d日')}（星期{weekday}）{now.strftime('%H:%M')}——涉及\"今天/最新/当前\"的任务以此为准，不要臆测日期\n\n"
        task_brief = (
            f"{date_line}"
            f"总体目标: {plan.goal}\n\n任务清单:\n{plan_view}\n\n"
            f"已完成任务结论:\n" + ("\n".join(prev_results) if prev_results else "(无)") + "\n\n"
            f"【当前任务 #{task.idx+1}】{task.title}\n{task.detail}\n\n"
            + (f"参考上下文:\n{context[:2000]}\n" if context else "")
        )

        system = EXECUTOR_SYSTEM + (PARALLEL_RULE if self.tool_mode == "native" else JSON_MODE_RULE)
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task_brief},
        ]
        search_count = 0  # 本任务内 web_search 调用次数（僵局检测）
        stats_tools_used = 0  # 本任务内实际执行的工具总数（final 前置校验）
        last_call: tuple | None = None  # (工具名, 参数指纹) 用于重复调用拦截
        call_seen: dict = {}  # (工具名, 参数指纹) -> 本任务累计次数：全局重复调用护栏
        stagnant = 0  # 连续“无新信息”步数（本步全部调用都被重复拦截时累加）
        last_progress_ts = time.time()  # 过程正文（progress）上次输出的时间，用于定时汇报

        for step in range(1, max_steps + 1):
            self._compress_history(messages)  # 滚动压缩历史，防长上下文注意力稀释
            self._ensure_tool_pairing(messages)  # 保证 tool_calls/tool 消息严格配对，避免 400
            # 软收敛：接近步数预算时告知模型剩余步数，让它主动收尾，而非被硬截断
            hint = None
            if max_steps > 6 and step >= max_steps - 5:
                hint = {
                    "role": "user",
                    "content": (
                        f"[系统] 你已执行 {step} 步，本任务仅剩约 {max_steps - step} 步预算。"
                        f"请勿再展开新的调查分支，基于已有信息尽快调用 finish_task 输出最终结论。"
                    ),
                }
            msgs = messages + ([hint] if hint else [])
            # 过程正文：阶段较长/耗时较久时，要求模型在正文（content）里用中文播报进展或问题，
            # 让等待中的用户看到阶段汇报，而不是只有一排工具折叠条。
            if self.tool_mode == "native" and step > 1 and (
                step % 4 == 0 or time.time() - last_progress_ts > 40
            ):
                msgs = msgs + [
                    {
                        "role": "user",
                        "content": (
                            "[系统] 你已运行一段时间/多步。请在本次回复的正文（content 字段）中，"
                            "用 1~3 句简短中文向用户播报当前进展、已完成的关键事项，或当前遇到的问题与下一步打算；"
                            "播报后照常继续你的工具调用。若一切顺利且无特别可报，可忽略本提示。"
                        ),
                    }
                ]
            # 模型调用韧性：远程模型可能超时/挂起，重试一次，仍失败则任务失败上报重规划
            resp: LLMResponse | None = None
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    resp = self.executor_llm.chat(
                        msgs,
                        tools=tools if self.tool_mode == "native" else None,
                        tool_mode="json" if self.tool_mode == "json" else "native",
                        # 工具轮次保持非流式：DeepSeek 流式+工具在长上下文下会无限挂起（无 read 超时兜底）
                    )
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    self._emit(AgentEvent("log", {"text": f"模型调用异常(第{attempt+1}次): {type(e).__name__}: {str(e)[:150]}"}))
                    # 重试前先修复消息配对：避免原样复用非法历史（如 tool_calls 与 tool 消息错序）连续 400
                    try:
                        self._ensure_tool_pairing(messages)
                    except Exception:  # noqa: BLE001
                        pass
            if resp is None:
                return False, f"模型调用失败: {type(last_err).__name__}: {str(last_err)[:200]}"

            if resp.thinking:
                self._emit(AgentEvent("thought", {"step": step, "text": resp.thinking[:600], "thinking": True}))
            if resp.content and resp.content.strip():  # 纯空白正文不算有效输出，避免空气泡
                if self.tool_mode == "native":
                    # native 模式下 content 是过程正文：以正文消息（progress）呈现，而非折叠条
                    last_progress_ts = time.time()
                    self._emit(AgentEvent("progress", {"step": step, "text": resp.content[:600]}))
                else:
                    self._emit(AgentEvent("thought", {"step": step, "text": resp.content[:600]}))

            # --- JSON 降级模式 ---
            if self.tool_mode == "json":
                action = parse_json_action(resp.raw_content)
                if action is None:
                    messages.append({"role": "assistant", "content": resp.raw_content[:500] or "(空回复)"})
                    messages.append(
                        {"role": "user", "content": "[系统] 无法解析你的回复。请严格输出 JSON: {\"action\": \"工具名\", \"arguments\": {...}}，或 {\"action\": \"final\", \"answer\": \"...\"}"}
                    )
                    continue
                if action["action"] == "final":
                    # 硬约束：规划为需要工具的任务，至少调用过一次工具才允许 final
                    # （防止 r1 等模型跳过搜索、凭内部知识编造实时性答案）
                    if stats_tools_used == 0 and step <= max_steps - 2:
                        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:500]})
                        messages.append(
                            {"role": "user", "content": "[系统] 本任务需要真实信息，禁止在未调用任何工具的情况下直接回答。请先调用 web_search 等工具获取最新信息，再基于工具结果给出 final。"}
                        )
                        continue
                    return True, str(action.get("answer", "完成"))
                name = action["action"]
                args = action.get("arguments", {}) or {}
                args_key = json.dumps(args, sort_keys=True, ensure_ascii=False)[:200]
                seen = call_seen.get((name, args_key), 0)
                call_seen[(name, args_key)] = seen + 1
                if seen >= 2 and name != "finish_task":
                    result = ToolResult(
                        text=f"[重复调用拦截] 完全相同的调用已执行 {seen} 次且结果相同，严禁再重复。请换方法/换参数，或直接基于已有观察总结。",
                        error=True,
                    )
                else:
                    last_call = (name, args_key)
                    stats_tools_used += 1
                    result = self.registry.call(name, args)
                messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                self._emit(AgentEvent("tool_call", {"step": step, "tool": name, "arguments": _safe_args(args)}))
                self._emit(AgentEvent("tool_result", {"step": step, "tool": name, "text": "[界面预览，完整结果已提供给模型] " + result.text[:400], "error": result.error}))
                messages.append(
                    {"role": "user", "content": f"[工具 {name} 返回]\n{result.truncated_view(max_chars)}"}
                )
                search_count = self._note_tool_call(name, args, search_count, messages)
                if result.images:
                    self._vision_describe(result, messages, name)
                stagnant = stagnant + 1 if result.text.startswith("[重复调用拦截]") else 0
                if stagnant >= 3:
                    messages.append({"role": "user", "content": "[系统] 连续多步调用均被判定为重复/无效，任务已陷于空转。请立即改用不同方法，或调用 finish_task 结束本任务并说明已完成部分。"})
                continue

            # --- Native FC 模式（并行工具执行）---
            if resp.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": resp.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                            }
                            for tc in resp.tool_calls
                        ],
                    }
                )
                # 先做拦截判定与 finish_task 分流（同步、快），再并行执行可并发的工具
                # 并发安全分组：浏览器工具共享一个页面会话，必须串行；finish_task 最后单独处理
                browser_prefix = ("browser_",)
                serial_calls: list = []  # (tc, result) 浏览器工具按原顺序串行
                parallel_calls: list = []  # (tc, result占位) 可并行
                finish_calls: list = []
                for tc in resp.tool_calls:
                    self._emit(AgentEvent("tool_call", {"step": step, "tool": tc.name, "arguments": _safe_args(tc.arguments)}))
                    if tc.name == "finish_task":
                        finish_calls.append(tc)
                        continue
                    args_key = json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)[:200]
                    seen = call_seen.get((tc.name, args_key), 0)
                    call_seen[(tc.name, args_key)] = seen + 1
                    if seen >= 2 and tc.name != "finish_task":
                        result = ToolResult(
                            text=f"[重复调用拦截] 完全相同的调用已执行 {seen} 次且结果相同，严禁再重复。请换方法/换参数，或直接基于已有观察继续。",
                            error=True,
                        )
                        (serial_calls if tc.name.startswith(browser_prefix) else parallel_calls).append((tc, result))
                    elif tc.name.startswith(browser_prefix):
                        serial_calls.append((tc, None))
                    else:
                        parallel_calls.append((tc, None))
                    last_call = (tc.name, args_key)

                # 可并发的工具线程池执行（读写文件/搜索/代码等互不依赖）
                if parallel_calls:
                    with ThreadPoolExecutor(max_workers=min(4, len(parallel_calls))) as pool:
                        futures = [pool.submit(self.registry.call, tc.name, tc.arguments) for tc, _ in parallel_calls]
                        parallel_results = [(tc, f.result()) for (tc, _), f in zip(parallel_calls, futures)]
                else:
                    parallel_results = []

                # 浏览器工具串行执行（共享浏览器会话）
                serial_results = [(tc, self.registry.call(tc.name, tc.arguments) if r is None else r) for tc, r in serial_calls]
                if parallel_calls:
                    stats_tools_used += len(parallel_calls)
                if serial_calls:
                    stats_tools_used += sum(1 for _, r in serial_calls if r is None)

                # 汇总结果：先并行组，再串行组，按 tool_calls 顺序回填给模型
                by_id = {tc.id: (tc, r) for (tc, r) in parallel_results + serial_results}
                # 本步要追加的 user 消息（搜索僵局提示 / 视觉描述）先暂存到独立列表，
                # 待本步所有 tool 消息回填完毕后再统一追加；否则 user 消息会插在
                # assistant(tool_calls) 与紧跟其后的 tool 消息之间，破坏配对 → 下一次
                # 模型调用返回 400 "must be followed by tool messages"。
                post_tool_msgs: list = []
                for tc in resp.tool_calls:
                    if tc.name == "finish_task":
                        # finish_task 无独立工具结果，但仍回填一条 tool 消息以保持配对（防 400）
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": "[finish_task] 结束当前任务（框架内部处理，无工具输出）"})
                        continue
                    _, result = by_id[tc.id]
                    self._emit(AgentEvent("tool_result", {"step": step, "tool": tc.name, "text": "[界面预览，完整结果已提供给模型] " + result.text[:400], "error": result.error}))
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result.truncated_view(max_chars)})
                    search_count = self._note_tool_call(tc.name, tc.arguments, search_count, post_tool_msgs)
                    if result.images:
                        self._vision_describe(result, post_tool_msgs, tc.name)
                # 本步全部 tool 消息已回填，再追加延迟的 user 消息（顺序安全）
                messages.extend(post_tool_msgs)

                # finish_task：等本步其他工具都执行完再结束（结论可包含本轮产出）
                for tc in finish_calls:
                    payload = tc.arguments
                    self._tool_trace.append(f"finish_task({_safe_args(payload)})")
                    ok = bool(payload.get("success", True))
                    text = str(payload.get("summary") or payload.get("reason") or "")
                    return ok, text
                # 停滞检测：本步全部调用都被重复拦截 -> 累加无进展步数并注入收敛提示
                total_n = len(serial_results) + len(parallel_results)
                blocked_n = sum(1 for (_, r) in (serial_results + parallel_results) if r.text.startswith("[重复调用拦截]"))
                stagnant = stagnant + 1 if (total_n > 0 and blocked_n == total_n) else 0
                if stagnant >= 3:
                    messages.append({"role": "user", "content": "[系统] 连续多步工具调用均被判定为重复/无效，任务已陷于空转。请立即改用不同方法，或调用 finish_task 结束本任务并说明已完成部分。"})
                continue

            # 无工具调用：qwen3 未经 finish 就直接说话 -> 引导收敛
            if resp.content:
                # 如果模型自己说完成了，接受为结果
                if any(kw in resp.content for kw in ("任务完成", "已完成", "任务已", "I have completed", "task is complete")) and step > 1:
                    return True, resp.content[:500]
                messages.append({"role": "assistant", "content": resp.content})
                messages.append(
                    {
                        "role": "user",
                        "content": "[系统] 请调用工具继续工作，或调用 finish_task 结束当前任务。",
                    }
                )

        return False, f"达到最大步数 {max_steps} 未完成任务"

    # -------- 汇总 --------

    SUMMARY_SYSTEM = """你是智能体汇报器。基于目标与任务执行记录，用中文写一份详细、有条理的完成汇报（300-600字），结构：
1. 目标回顾：一两句话说明要做什么
2. 执行过程：按顺序说明做了哪些事、用了什么方法或工具、遇到并解决了什么问题
3. 结果详情：具体数据、内容、结论——重点信息要完整呈现，不要一笔带过
4. 产出与位置：生成/修改了哪些文件（给出路径），或获取了哪些信息
5. 备注：未尽事项与建议（如无可省略）
重要：
- 用户对输出有字数/格式/内容要求时必须严格满足（如要求500字总结，汇报中就要包含完整的500字总结原文）
- 任务记录中已有的详细内容（总结、数据、文本）要完整保留融入汇报，禁止二次概括缩短
- 【执行过程】部分必须与"真实工具调用记录"严格一致：没有发生过的操作（如访问某网站、联系某人）绝对不能写；搜索失败就说搜索失败，禁止编造成功过程
- 只基于执行记录写，不要编造；语气自然，像同事间的完工汇报。"""

    def _summarize(self, goal: str, task_results: list[str]) -> str:
        detail = "\n".join(f"- {r}" for r in task_results) or "(无任务记录)"
        trace = self._tool_trace[:40]
        trace_view = "\n".join(f"{i+1}. {t}" for i, t in enumerate(trace)) or "(无工具调用)"
        # 单任务且任务产出已足够详尽（如用户要求的总结/文章）时直接采用：
        # 再过一遍 LLM 只会二次压缩缩短，还可能因思考挤占 token 被截断
        if len(task_results) == 1 and len(task_results[0]) >= 150:
            return task_results[0]
        try:
            resp = self.planner_llm.chat(
                [
                    {"role": "system", "content": self.SUMMARY_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"目标: {goal}\n\n任务执行记录:\n{detail}\n\n"
                            f"真实工具调用记录（汇报的执行过程必须与此一致）:\n{trace_view}"
                        ),
                    },
                ],
                temperature=0.4,
                stream_cb=self._stream_delta,  # 汇总正文流式推送
            )
            # 汇总被截断时退回原始记录，保真优先
            if len(resp.content.strip()) < 100 and len(detail) > 200:
                return detail
            return resp.content.strip() or detail
        except Exception:  # noqa: BLE001
            return detail


def _safe_args(args: dict) -> dict:
    """事件里展示的参数：长文本截断。"""
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[k] = s[:200] + "..." if len(s) > 200 else v
    return out
