"""工具系统：@tool 装饰器 + 注册器 + 自动 JSON Schema 生成

工具函数可以是：
- 返回 str：纯文本结果
- 返回 ToolResult：带图片等多媒体结果
- 抛异常：自动捕获转为错误文本（不中断 Agent 循环）
"""
from __future__ import annotations

import inspect
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolResult:
    text: str = ""
    images: list = field(default_factory=list)  # list[bytes] PNG
    error: bool = False

    def truncated_view(self, max_chars: int) -> str:
        view = self.text
        if len(view) > max_chars:
            half = max_chars // 2
            view = (
                view[:half]
                + f"\n...[结果过长，已截断 {len(self.text) - max_chars} 字符]...\n"
                + view[-half:]
            )
        if self.images:
            view += f"\n[附图 {len(self.images)} 张，已作为图片消息发给模型分析]"
        return view


class Tool:
    def __init__(
        self,
        name: str,
        func: Callable,
        description: str,
        parameters: dict,
        dangerous: bool = False,
    ):
        self.name = name
        self.func = func
        self.description = description.strip()
        self.parameters = parameters
        self.dangerous = dangerous
        self._signature = inspect.signature(func)

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def _coerce_args(self, kwargs: dict) -> dict:
        """按函数类型注解矫正参数。小模型常把 int/float/bool 传成字符串，
        直接透传会在工具内部炸出隐晦的 TypeError。转换失败则保留原值。
        注意：模块启用 `from __future__ import annotations` 时注解是字符串，需先映射。"""
        out = {}
        for name, param in self._signature.parameters.items():
            if name not in kwargs:
                continue
            val = kwargs[name]
            ann = param.annotation
            if isinstance(ann, str):  # 延迟求值的注解（'int'/'bool' 等字符串）
                ann = {"int": int, "float": float, "bool": bool, "str": str}.get(ann)
            try:
                if ann is int and not isinstance(val, bool):
                    out[name] = int(float(val))
                elif ann is float and not isinstance(val, bool):
                    out[name] = float(val)
                elif ann is bool:
                    if isinstance(val, str):
                        out[name] = val.strip().lower() in ("true", "1", "yes", "on")
                    else:
                        out[name] = bool(val)
                elif ann is str and not isinstance(val, str):
                    out[name] = str(val)
                else:
                    out[name] = val
            except (TypeError, ValueError):
                out[name] = val
        return out

    def call(self, **kwargs) -> ToolResult:
        # 缺参校验：给模型可行动的提示（Python TypeError 对模型不可读）
        missing = [
            p for p in self.parameters.get("required", [])
            if p not in kwargs or kwargs[p] in (None, "")
        ]
        if missing:
            usage = ", ".join(
                f"{k}=<{'必填' if k in missing else '可选'}>" for k in self.parameters.get("properties", {})
            )
            return ToolResult(
                text=(
                    f"[参数缺失] 工具 {self.name} 缺少必填参数: {', '.join(missing)}。"
                    f"正确调用格式: {self.name}({usage})。请补全参数后重试。"
                ),
                error=True,
            )
        try:
            result = self.func(**self._coerce_args(kwargs))
        except Exception as e:  # noqa: BLE001 工具异常不应杀死 Agent
            tb = traceback.format_exc(limit=3)
            return ToolResult(text=f"工具 {self.name} 执行出错: {type(e).__name__}: {e}\n{tb}", error=True)
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, (dict, list)):
            return ToolResult(text=json.dumps(result, ensure_ascii=False, indent=2))
        return ToolResult(text=str(result))


def _pytype_to_json(hint: Any) -> str:
    origin = getattr(hint, "__origin__", None)
    if hint in (str,):
        return "string"
    if hint is int:
        return "integer"
    if hint is float:
        return "number"
    if hint is bool:
        return "boolean"
    if origin in (list, tuple, set) or hint in (list, tuple, set):
        return "array"
    if origin is dict or hint is dict:
        return "object"
    return "string"


def tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    dangerous: bool = False,
) -> Callable:
    """装饰器：注册工具。描述取 docstring；参数 schema 取类型注解。"""

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        doc = inspect.getdoc(func) or tool_name
        # docstring 第一段作为描述
        desc = description or doc.split("\n\n")[0]

        sig = inspect.signature(func)
        props: dict = {}
        required: list = []
        for pname, param in sig.parameters.items():
            if param.annotation is inspect.Parameter.empty:
                jtype = "string"
            elif param.default is inspect.Parameter.empty:
                jtype = _pytype_to_json(param.annotation)
                required.append(pname)
            else:
                jtype = _pytype_to_json(param.annotation)
            props[pname] = {"type": jtype}
            # 支持 "param: type, 描述文字" 不做强制；参数说明从 docstring 略过（小模型够用）

        parameters = {"type": "object", "properties": props, "required": required}

        t = Tool(tool_name, func, desc, parameters, dangerous=dangerous)
        func._agent_tool = t  # 挂载标记，供 registry 收集
        return func

    return decorator


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, t: Tool):
        if t.name in self._tools:
            raise ValueError(f"工具重名: {t.name}")
        self._tools[t.name] = t

    def register_function(self, func: Callable):
        t = getattr(func, "_agent_tool", None)
        if t is None:
            # 静默跳过会让"工具消失"这类问题极难排查，直接炸出来
            raise ValueError(f"函数 {getattr(func, '__name__', func)} 未用 @tool 装饰，无法注册")
        self.register(t)

    def register_module(self, module):
        """批量注册一个模块里所有 @tool 函数。"""
        for attr in vars(module).values():
            if callable(attr) and hasattr(attr, "_agent_tool"):
                self.register(attr._agent_tool)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def to_openai_tools(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def call(self, name: str, arguments: dict) -> ToolResult:
        t = self._tools.get(name)
        if t is None:
            # 给出最接近的真实工具名，帮助模型自我纠正（r1 常发明 list_files 等不存在的名字）
            import difflib

            close = difflib.get_close_matches(name, self._tools, n=2, cutoff=0.6)
            hint = f"你是想调用 {close[0]} 吗？" if close else ""
            return ToolResult(
                text=f"未知工具: {name}。{hint}可用工具: {', '.join(self._tools)}",
                error=True,
            )
        if not isinstance(arguments, dict):
            arguments = {}
        return t.call(**arguments)
