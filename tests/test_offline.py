"""工具注册器与 JSON 解析的离线测试（不依赖模型服务）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import parse_json_action, split_think  # noqa: E402
from agent.planner import _parse_plan  # noqa: E402
from agent.tools.registry import ToolRegistry, tool  # noqa: E402


def test_split_think():
    thinking, content = split_think("<think>让我想想</think>答案是 42")
    assert thinking == "让我想想"
    assert content == "答案是 42"

    thinking2, content2 = split_think("<think>未闭合")
    assert thinking2 == "未闭合"
    assert content2 == ""


def test_parse_json_action():
    assert parse_json_action('{"action": "read_file", "arguments": {"path": "a.txt"}}')["action"] == "read_file"
    # markdown 包裹
    out = parse_json_action('```json\n{"action": "final", "answer": "done"}\n```')
    assert out["action"] == "final" and out["answer"] == "done"
    # 前后杂文
    out2 = parse_json_action('好的，我来读取文件\n{"action": "list_dir", "arguments": {"path": "."}} 以上')
    assert out2["action"] == "list_dir"
    # 思考混杂
    out3 = parse_json_action('<think>先列目录</think>{"action": "list_dir"}')
    assert out3["action"] == "list_dir"
    assert parse_json_action("没有任何 JSON") is None


def test_tool_registry():
    reg = ToolRegistry()

    @tool(description="加法")
    def add(a: int, b: int = 1) -> str:
        """加法工具"""
        return str(a + b)

    reg.register_function(add)

    schema = reg.to_openai_tools()[0]
    assert schema["function"]["name"] == "add"
    assert set(schema["function"]["parameters"]["required"]) == {"a"}
    assert schema["function"]["parameters"]["properties"]["b"]["type"] == "integer"

    result = reg.call("add", {"a": 2, "b": 3})
    assert result.text == "5"
    # 缺参数 -> 友好提示（不再抛 TypeError）
    result2 = reg.call("add", {})
    assert result2.error and "参数缺失" in result2.text and "a" in result2.text
    # 未知工具 -> 给最接近的名字建议（需注册真实工具 list_dir 才能被 difflib 匹配）
    @tool(description="列出目录")
    def list_dir(path: str = ".") -> str:
        """列出目录内容"""
        return ""

    reg.register_function(list_dir)
    result3 = reg.call("list_files", {})  # 发明的名字，真实为 list_dir
    assert result3.error and "list_dir" in result3.text


def test_tool_type_coercion():
    """模型把数字/布尔传成字符串时应自动矫正"""
    reg = ToolRegistry()

    @tool(description="类型测试")
    def probe(timeout: int = 120, ratio: float = 0.5, flag: bool = False, name: str = "x") -> str:
        """返回参数的实际类型"""
        return f"{type(timeout).__name__}/{type(ratio).__name__}/{type(flag).__name__}/{type(name).__name__}"

    reg.register_function(probe)
    r = reg.call("probe", {"timeout": "60", "ratio": "0.8", "flag": "true", "name": 123})
    assert r.text == "int/float/bool/str"
    # int("3.5") 场景走 float 过渡
    r2 = reg.call("probe", {"timeout": "3.5"})
    assert r2.text.startswith("int/")
    # bool 不被 int(float()) 分支误转
    r3 = reg.call("probe", {"timeout": True})
    assert r3.text.startswith("bool/")


def test_builtin_tools_registered():
    """防回归：所有内置工具必须完整注册（曾因编辑回退丢失 web_tools）"""
    from agent.config import load_config
    from agent.executor import Agent

    cfg, _ = load_config()
    agent = Agent(cfg, on_event=None, quiet=True)
    try:
        names = set(agent.registry.names())
        expected = [
            "read_file", "write_file", "edit_file", "list_dir",
            "run_command", "run_code",
            "fetch_url", "web_search",
            "browser_navigate", "browser_click", "browser_type", "browser_select",
            "browser_press_key", "browser_scroll", "browser_screenshot", "browser_extract_text",
            "read_document",
            "finish_task", "save_memory", "search_memory",
        ]
        missing = [n for n in expected if n not in names]
        assert not missing, f"缺少工具: {missing}"
        # 类型矫正对字符串注解（__future__ annotations 模块）也生效
        t = agent.registry.get("browser_extract_text")
        assert t._coerce_args({"max_chars": "10000"})["max_chars"] == 10000
    finally:
        agent.close()


def test_parse_plan_direct_answer():
    """规划器支持无需工具直答分支"""
    tasks, direct = _parse_plan('{"need_tools": false, "answer": "你好！有什么可以帮你？"}')
    assert tasks is None and direct == "你好！有什么可以帮你？"
    # 只给 answer 没有 tasks 也算直答
    tasks2, direct2 = _parse_plan('{"answer": "42"}')
    assert tasks2 is None and direct2 == "42"
    # 正常任务清单
    tasks3, direct3 = _parse_plan(
        '{"need_tools": true, "tasks": [{"title": "写文件", "detail": "写入 ok"}]}'
    )
    assert direct3 is None and tasks3[0]["title"] == "写文件"
    # think 包裹
    tasks4, direct4 = _parse_plan('<think>闲聊</think>{"need_tools": false, "answer": "hi"}')
    assert tasks4 is None and direct4 == "hi"
    # 空输出
    assert _parse_plan("没有 JSON") == (None, None)


def test_file_tools_sandbox(tmp_path):
    import agent.tools.file_tools as ft
    from agent.tools.registry import ToolRegistry

    ft.set_workspace(str(tmp_path))
    assert "已写入" in ft.write_file("hello.txt", "你好")
    assert "你好" in ft.read_file("hello.txt")
    assert "hello.txt" in ft.list_dir(".")
    # 越界访问：通过 registry 调用应被捕获为错误文本而非抛异常
    reg = ToolRegistry()
    reg.register_function(ft.read_file)
    result = reg.call("read_file", {"path": "C:/Windows/system32/drivers/etc/hosts"})
    assert result.error and "越界" in result.text
    result2 = reg.call("read_file", {"path": "../../outside.txt"})
    assert result2.error and "越界" in result2.text
