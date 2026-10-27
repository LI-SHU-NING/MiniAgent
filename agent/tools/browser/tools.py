"""浏览器工具集：把 BrowserSession 包装为可注册的 @tool 函数"""
from __future__ import annotations

import importlib
from pathlib import Path

from ..registry import ToolResult, tool
from .browser import BrowserSession

_session: "BrowserSession | None" = None
_cfg: dict = {}
_browser_py = Path(__file__).with_name("browser.py")
_browser_mtime: float = 0.0


def _hot_reload() -> None:
    """开发期热重载：检测到 browser.py 变更时重载模块并重建会话，免重启 server。

    首次调用仅记录基线 mtime；只有文件确实变化才重载。正常运行开销 = 一次 stat()。
    """
    global BrowserSession, _browser_mtime, _session
    try:
        mtime = _browser_py.stat().st_mtime
    except OSError:
        return
    if _browser_mtime == 0.0:
        _browser_mtime = mtime
        return
    if mtime == _browser_mtime:
        return
    _browser_mtime = mtime
    if _session is not None:  # 旧类实例先释放，避免句柄泄漏
        try:
            _session.close()
        except Exception:  # noqa: BLE001
            pass
        _session = None
    mod = importlib.reload(importlib.import_module("agent.tools.browser.browser"))
    BrowserSession = mod.BrowserSession


def configure(headless: bool = True, timeout: int = 30, viewport=None, user_agent=None):
    global _cfg
    _cfg = {
        "headless": headless,
        "timeout": timeout,
        "viewport": viewport or [1280, 800],
        "user_agent": user_agent,
    }


def get_session() -> BrowserSession:
    global _session
    _hot_reload()
    if _session is None:
        _session = BrowserSession(**_cfg)
    return _session


def close_session():
    global _session
    if _session is not None:
        _session.close()
        _session = None


@tool(description="用浏览器打开 URL，返回页面可交互元素清单（带 ref 编号）。")
def browser_navigate(url: str) -> str:
    """打开网页。后续用 ref 编号操作页面元素。"""
    _hot_reload()
    return get_session().navigate(url)


@tool(description="点击页面元素。ref 来自最近一次快照中的 [ref=N] 编号。")
def browser_click(ref: int) -> str:
    """点击后自动返回最新快照。"""
    _hot_reload()
    return get_session().click(ref)


@tool(description="向输入框输入文本。ref 为输入框编号；press_enter=true 时输入后回车（适合搜索框）。")
def browser_type(ref: int, text: str, press_enter: bool = False) -> str:
    """输入文本（默认先清空原有内容）。"""
    _hot_reload()
    return get_session().type(ref, text, press_enter=press_enter)


@tool(description="在下拉框中选择选项（按 value 或可见文本）。")
def browser_select(ref: int, value: str) -> str:
    _hot_reload()
    return get_session().select(ref, value)


@tool(description="按下键盘按键（如 Enter/Escape/ArrowDown/PageDown）。")
def browser_press_key(key: str) -> str:
    _hot_reload()
    return get_session().press_key(key)


@tool(description="滚动页面（direction: down/up，amount 为格数）。")
def browser_scroll(direction: str = "down", amount: int = 3) -> str:
    _hot_reload()
    return get_session().scroll(direction, amount)


@tool(description="截取当前页面屏幕截图并交给视觉模型分析。用于无 ref 的复杂元素（Canvas/图表/验证码判断）。")
def browser_screenshot() -> ToolResult:
    _hot_reload()
    png_desc, png = get_session().screenshot()
    return ToolResult(
        text=f"截图完成。{png_desc}\n请根据图片内容决定下一步操作。",
        images=[png],
    )


@tool(description="提取当前页面全部正文文本（比快照更长，适合阅读文章内容）。")
def browser_extract_text(max_chars: int = 6000) -> str:
    _hot_reload()
    return get_session().extract_text(max_chars)


ALL = [
    browser_navigate,
    browser_click,
    browser_type,
    browser_select,
    browser_press_key,
    browser_scroll,
    browser_screenshot,
    browser_extract_text,
]
