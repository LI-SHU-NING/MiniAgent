"""LLM 客户端：OpenAI 兼容接口 + 双模式工具调用 + 流式 + 视觉消息

- native 模式：使用 /v1/chat/completions 的 tools/tool_calls（qwen3 等支持）
- json 模式：把工具 schema 注入 system prompt，解析模型输出的 JSON action
  （适配 deepseek-r1 等不原生支持 function calling 的模型）
- 自动兼容 qwen3 系列的 <think>...</think> 思考输出
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from openai import OpenAI

# ---------------------------------------------------------------- 数据结构


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict  # 已解析为 dict


@dataclass
class LLMResponse:
    content: str = ""
    thinking: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCall]
    raw_content: str = ""  # 未剥离 think 的原始文本


DeltaCallback = Callable[[str, str], None]  # (kind, text) kind in {"thinking", "content"}

THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def split_think(text: str) -> tuple[str, str]:
    """从模型输出中剥离 <think> 块，返回 (thinking, content)。
    处理三种情况：完整成对标签、只有开标签（流式中断）、无标签。"""
    thinking_parts = THINK_RE.findall(text)
    content = THINK_RE.sub("", text)
    # 只有 <think> 开标签没有闭合（qwen3 在某些端点直接把思考混进 content）
    if "<think>" in content:
        idx = content.index("<think>")
        thinking_parts.append(content[idx + len("<think>"):])
        content = content[:idx]
    return "\n".join(p.strip() for p in thinking_parts if p.strip()), content.strip()


# ---------------------------------------------------------------- 客户端


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float = 0.3,
        timeout: int = 300,
        no_think: bool = False,
        max_tokens: int | None = None,
        api_key: str = "not-needed",  # 自建服务可不校验 key；云端 API 传真实 key
    ):
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=1,  # SDK 层连接级重试（不含读超时后的逻辑重试）
        )
        self.model = model
        self.temperature = temperature
        self.no_think = no_think  # qwen3 系列 soft switch，远程慢链路建议开启
        self.max_tokens = max_tokens

    # -------- 消息构造辅助 --------

    @staticmethod
    def user_text(text: str) -> dict:
        return {"role": "user", "content": text}

    @staticmethod
    def user_multimodal(text: str, images: list[bytes]) -> dict:
        """构造带图片的用户消息（PNG bytes 列表），用于视觉模型。"""
        parts: list = []
        if text:
            parts.append({"type": "text", "text": text})
        for img in images:
            import base64

            b64 = base64.b64encode(img).decode()
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            )
        return {"role": "user", "content": parts}

    # -------- 核心调用 --------

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_mode: Literal["native", "json"] = "native",
        json_mode: bool = False,
        stream_cb: Optional[DeltaCallback] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """统一入口。tool_mode=json 时会先调用 build_json_tool_prompt 注入文档。"""
        if tool_mode == "json" and tools:
            messages = inject_json_tool_doc(messages, tools)
            tools = None

        messages = self._apply_no_think(messages)

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        use_stream = stream_cb is not None
        if use_stream:
            return self._chat_stream(kwargs, stream_cb)
        return self._chat_once(kwargs)

    def _apply_no_think(self, messages: list[dict]) -> list[dict]:
        """在最后一条 user 消息尾部追加 /no_think（qwen3 思考关闭开关）。
        只影响发出的请求，不污染本地保存的消息历史。"""
        if not self.no_think:
            return messages
        out = [dict(m) for m in messages]
        for m in reversed(out):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    m["content"] = c + "\n/no_think"
                elif isinstance(c, list):
                    for part in c:
                        if part.get("type") == "text":
                            part["text"] = part["text"] + "\n/no_think"
                            break
                break
        return out

    def _chat_once(self, kwargs: dict) -> LLMResponse:
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        extra = msg.model_extra or {}
        # 部分兼容层把思考放在 reasoning 字段；其他供应商常见 reasoning_content
        field_thinking = (
            getattr(msg, "reasoning_content", None) or extra.get("reasoning") or ""
        )
        raw = msg.content or ""
        tag_thinking, content = split_think(raw)
        thinking = "\n".join(x for x in (field_thinking.strip(), tag_thinking) if x.strip())
        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return LLMResponse(
            content=content, thinking=thinking, tool_calls=tool_calls, raw_content=raw
        )

    def _chat_stream(self, kwargs: dict, cb: DeltaCallback) -> LLMResponse:
        kwargs["stream"] = True
        stream = self.client.chat.completions.create(**kwargs)
        raw_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_parts: list = []  # 流式 tool_calls 增量拼装 [{id,name,arguments}]
        in_think = False
        _t0 = time.time()
        _STREAM_HARD_MAX = 180.0  # 流式总时长看门狗：防止 DeepSeek 思考/生成阶段无限挂起
        for chunk in stream:
            if time.time() - _t0 > _STREAM_HARD_MAX:
                raise TimeoutError(f"stream exceeded {_STREAM_HARD_MAX:.0f}s watchdog")
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            # 部分兼容层的思考字段（增量）
            piece_think = (
                getattr(delta, "reasoning_content", None)
                or (delta.model_extra or {}).get("reasoning")
                or ""
            )
            if piece_think:
                thinking_parts.append(piece_think)
                cb("thinking", piece_think)
            # 工具调用增量（id/name 首片带，arguments 分片到达需累加）
            for tc in getattr(delta, "tool_calls", None) or []:
                idx = tc.index
                while len(tool_parts) <= idx:
                    tool_parts.append({"id": "", "name": "", "arguments": ""})
                tid = getattr(tc, "id", None)
                if tid:
                    tool_parts[idx]["id"] = tid
                fn = getattr(tc, "function", None)
                if fn:
                    if getattr(fn, "name", None):
                        tool_parts[idx]["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        tool_parts[idx]["arguments"] += fn.arguments
            piece = delta.content or ""
            if not piece:
                continue
            raw_parts.append(piece)
            # 逐块区分 <think> / 正文输出
            while piece:
                if in_think:
                    end = piece.find("</think>")
                    if end == -1:
                        cb("thinking", piece)
                        piece = ""
                    else:
                        cb("thinking", piece[:end])
                        piece = piece[end + len("</think>"):]
                        in_think = False
                else:
                    start = piece.find("<think>")
                    if start == -1:
                        cb("content", piece)
                        piece = ""
                    else:
                        if start > 0:
                            cb("content", piece[:start])
                        piece = piece[start + len("<think>"):]
                        in_think = True
        raw = "".join(raw_parts)
        tag_thinking, content = split_think(raw)
        thinking = "\n".join(x for x in ("".join(thinking_parts).strip(), tag_thinking) if x.strip())
        tool_calls: list[ToolCall] = []
        for tp in tool_parts:
            if not tp["id"] or not tp["name"]:
                continue
            try:
                args = json.loads(tp["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tp["arguments"]}
            tool_calls.append(ToolCall(id=tp["id"], name=tp["name"], arguments=args))
        return LLMResponse(content=content, thinking=thinking, tool_calls=tool_calls, raw_content=raw)


# ---------------------------------------------------------------- JSON 降级模式

JSON_DOC_TEMPLATE = """

# 工具调用协议（重要）
你可以调用下列工具。**每次回复必须是且仅是一个 JSON 对象**，格式：
{{"action": "<工具名>", "arguments": {{<参数对象>}}}}
工具执行后会以 user 消息返回结果。不需要工具、任务完成要给最终答复时：
{{"action": "final", "answer": "<给用户的完整答复>"}}

可用工具：
{tool_docs}
"""


def build_tool_docs(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        params = fn.get("parameters", {}).get("properties", {})
        req = fn.get("parameters", {}).get("required", [])
        pstr = ", ".join(
            f"{k}:{v.get('type', 'any')}{'' if k in req else ' 可选'}" for k, v in params.items()
        )
        lines.append(f'- {fn["name"]}({pstr}): {fn.get("description", "")}')
    return "\n".join(lines)


def inject_json_tool_doc(messages: list[dict], tools: list[dict]) -> list[dict]:
    doc = JSON_DOC_TEMPLATE.format(tool_docs=build_tool_docs(tools))
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        out[0]["content"] = out[0].get("content", "") + doc
    else:
        out.insert(0, {"role": "system", "content": doc.strip()})
    return out


def parse_json_action(text: str) -> Optional[dict]:
    """从模型输出解析 {"action": ..., "arguments"/"answer": ...}。
    容忍 markdown 代码块、前后杂文、多个 JSON（取最后一个含 action 的）。"""
    if not text:
        return None
    # 先剥 think
    _, content = split_think(text)

    candidates: list[str] = []
    # ```json ... ``` 代码块
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL):
        candidates.append(m.group(1))
    # 裸 {...}（贪心平衡括号）
    depth = 0
    start = -1
    for i, ch in enumerate(content):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(content[start : i + 1])
    if not candidates:
        return None

    # 从后往前找第一个合法且含 action 的
    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            if "arguments" not in obj and obj.get("action") != "final":
                obj["arguments"] = {k: v for k, v in obj.items() if k != "action"}
            return obj
    return None
