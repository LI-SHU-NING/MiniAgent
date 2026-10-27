"""工具消息配对守护（Agent._ensure_tool_pairing）回归测试。

背景：OpenAI 兼容接口要求——带 tool_calls 的 assistant 消息后面，必须为每个
tool_call_id 紧跟一条 role=tool 的回复，否则整段对话在下次请求时返回 400
（"An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'"）。

本测试覆盖：补齐、清理孤立 tool_calls、移动游离 tool 消息、删除孤立 tool 消息、
以及"配对良好时不改动"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.executor import Agent  # noqa: E402

fix = Agent._ensure_tool_pairing


def _assert_paired(msgs):
    """断言消息列表中每个 tool_call_id 都有紧随其后（或本列表内）的 tool 回复。"""
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            following = []
            j = i + 1
            while j < len(msgs) and msgs[j].get("role") == "tool":
                following.append(msgs[j].get("tool_call_id"))
                j += 1
            for tid in ids:
                assert tid in following, f"tool_call_id {tid} 缺少紧随的 tool 回复"


def test_well_paired_unchanged():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "a1", "type": "function",
                                              "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "r1"},
        {"role": "assistant", "content": "done"},
    ]
    before = [dict(m) for m in msgs]
    assert fix(msgs) == 0
    assert msgs == before


def test_missing_tool_reply_is_filled():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "a1", "type": "function",
                                              "function": {"name": "x", "arguments": "{}"}},
                                             {"id": "a2", "type": "function",
                                              "function": {"name": "y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "r1"},
        # a2 的回复缺失
    ]
    assert fix(msgs) >= 1
    _assert_paired(msgs)
    ids = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
    assert "a2" in ids


def test_detached_tool_reply_is_moved_adjacent():
    msgs = [
        {"role": "assistant", "tool_calls": [{"id": "a1", "type": "function",
                                              "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "user", "content": "interleaved"},
        {"role": "tool", "tool_call_id": "a1", "content": "r1"},  # 游离在别处
    ]
    fix(msgs)
    _assert_paired(msgs)
    # a1 的回复必须紧跟 assistant
    assert msgs[1].get("role") == "tool" and msgs[1].get("tool_call_id") == "a1"


def test_illegal_tool_call_without_id_is_cleaned():
    msgs = [
        {"role": "assistant", "content": "thinking",
         "tool_calls": [{"type": "function", "function": {"name": "bad", "arguments": "{}"}}]},
    ]
    fix(msgs)
    _assert_paired(msgs)
    # 非法 tool_call 被清理；不应残留空的 tool_calls
    assert not any(m.get("tool_calls") for m in msgs)


def test_orphan_tool_message_is_dropped():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        {"role": "assistant", "content": "done"},
    ]
    fix(msgs)
    assert not any(m.get("role") == "tool" for m in msgs)


def test_finish_task_tool_call_gets_reply():
    """模拟 finish_task 场景：assistant 只带 finish_task 调用，修复后应有其 tool 回复。"""
    msgs = [
        {"role": "user", "content": "goal"},
        {"role": "assistant", "tool_calls": [{"id": "f1", "type": "function",
                                              "function": {"name": "finish_task", "arguments": "{}"}}]},
    ]
    assert fix(msgs) >= 1
    _assert_paired(msgs)


def test_duplicate_tool_reply_is_deduplicated():
    msgs = [
        {"role": "assistant", "tool_calls": [{"id": "a1", "type": "function",
                                              "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "r1"},
        {"role": "tool", "tool_call_id": "a1", "content": "r1-dup"},
    ]
    fix(msgs)
    _assert_paired(msgs)
    assert sum(1 for m in msgs if m.get("role") == "tool") == 1


# -------- 2026-10-14 新增：方案A「从源头修复」回归 --------
# 背景：_note_tool_call / _vision_describe 曾在 tool_calls 回填循环内直接向真实的
# messages 追加 user 消息，导致顺序变成 assistant(tool_calls) -> tool(tc1) -> user
# -> tool(tc2)，下一次模型调用 400。修复后改为注入到临时列表 post_tool_msgs，
# 待本步所有 tool 消息回填完毕再 messages.extend(post_tool_msgs)。
import inspect  # noqa: E402

from agent import executor as _exec_mod  # noqa: E402


class _NoteStub:
    def __init__(self):
        self._tool_trace = []


def test_note_tool_call_appends_hint_to_passed_list():
    """_note_tool_call 必须把僵局提示追加到“传入的列表”，而非固定写死 messages。

    这是延迟注入方案成立的前提：调用方传入临时列表，稍后再统一合并。
    """
    stub = _NoteStub()
    out = []
    c1 = Agent._note_tool_call(stub, "web_search", {}, 0, out)
    c2 = Agent._note_tool_call(stub, "web_search", {}, c1, out)
    c3 = Agent._note_tool_call(stub, "web_search", {}, c2, out)
    assert (c1, c2, c3) == (1, 2, 3)
    assert len(out) == 1 and out[0]["role"] == "user"
    assert out[0]["content"] == _exec_mod.SEARCH_STUCK_HINT


def test_tool_call_loop_defers_user_messages_after_tools():
    """源码级守护：native tool_calls 回填循环内不得再直接向 messages 注入 user 消息。"""
    src = inspect.getsource(_exec_mod)
    # 修复后的正确写法
    assert "messages.extend(post_tool_msgs)" in src
    assert "search_count, post_tool_msgs)" in src
    assert "_vision_describe(result, post_tool_msgs, tc.name)" in src
    # 回归保护：循环内旧的“直接注入 messages”写法不应再出现
    assert "_note_tool_call(tc.name, tc.arguments, search_count, messages)" not in src
    assert "_vision_describe(result, messages, tc.name)" not in src


def test_retry_repairs_pairing_before_next_attempt():
    """方案B：重试前应调用 _ensure_tool_pairing 修复历史，避免复用非法 messages 连续 400。"""
    src = inspect.getsource(_exec_mod)
    assert "重试前先修复消息配对" in src
    assert "self._ensure_tool_pairing(messages)" in src
