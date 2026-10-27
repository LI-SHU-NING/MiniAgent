"""冒烟测试：需要可用的 OpenAI 兼容 API（在 config.yaml 的 api 段配置）
运行: pytest tests/test_smoke.py -s
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402

cfg, _ = load_config()


def api_ready() -> bool:
    return bool(cfg.api.api_key and cfg.api.base_url)


pytestmark = pytest.mark.skipif(
    not api_ready(), reason="未配置 OpenAI 兼容 API（api.api_key 为空）"
)


def _llm() -> LLMClient:
    return LLMClient(cfg.api.base_url, cfg.api.executor_model, api_key=cfg.api.api_key)


def test_llm_basic():
    llm = _llm()
    resp = llm.chat([{"role": "user", "content": "回答一个字：1+1=? 只输出数字"}], temperature=0)
    assert "2" in resp.content


def test_llm_native_tools():
    llm = _llm()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    resp = llm.chat(
        [{"role": "user", "content": "查一下北京的天气"}],
        tools=tools,
    )
    assert resp.tool_calls, "模型应发起工具调用"
    assert resp.tool_calls[0].name == "get_weather"
    assert "北京" in str(resp.tool_calls[0].arguments)


def test_agent_end_to_end(tmp_path, monkeypatch):
    """完整链路：规划 -> 写文件工具 -> finish"""
    import agent.tools.file_tools as ft

    ft.set_workspace(str(tmp_path))
    monkeypatch.setattr(cfg.security, "workspace", str(tmp_path))
    from agent.executor import Agent

    agent = Agent(cfg, on_event=None, quiet=True)
    answer = agent.run("在工作目录创建文件 smoke_test.txt，内容为 ok123，然后告诉我文件名")
    agent.close()
    assert (tmp_path / "smoke_test.txt").exists()
    # 模型可能用 write_file（utf-8）或 PowerShell（utf-16）落盘，做编码兼容读取
    raw = (tmp_path / "smoke_test.txt").read_bytes()
    for enc in ("utf-8", "utf-16", "gbk"):
        try:
            assert "ok123" in raw.decode(enc)
            break
        except (UnicodeDecodeError, AssertionError):
            continue
    else:
        pytest.fail(f"文件内容验证失败: {raw[:50]!r}")
