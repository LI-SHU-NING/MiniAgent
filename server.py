"""通用智能体 Web 服务入口

  python server.py            # 默认 http://127.0.0.1:8799
接口:
  GET    /                    聊天页面
  POST   /api/chat            SSE 流式事件（body: {"goal", "session_id"?}）
  GET    /api/tools           工具列表
  GET    /api/memory          长期记忆条数
  GET    /api/sessions        会话列表
  POST   /api/sessions        新建会话
  DELETE /api/sessions/{sid}  删除会话
  GET    /api/sessions/{sid}/messages   会话历史消息
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agent.config import load_config
from agent.executor import Agent, AgentEvent

app = FastAPI(title="OpenAgent")
cfg, root = load_config()
WEB_DIR = Path(__file__).parent / "web" / "static"
SESSIONS_FILE = root / "data" / "sessions.json"

_sessions: dict[str, dict] = {}  # id -> {id,title,created_at,messages:[{role,content}]}
_agents: dict[str, Agent] = {}  # id -> 惰性创建的 Agent 实例
_sessions_lock = threading.RLock()
_exec_lock = threading.Lock()  # 全局执行串行锁（浏览器/模型资源独占）
_running: set[str] = set()  # 正在执行任务的会话 id，防止同会话二次请求带旧上下文静默排队
_jobs: dict[str, dict] = {}  # sid -> {q, buf, seq, done, started, goal} 事件缓冲档案（断线重连用）

CONV_DIR = root / "conversations"  # 兼容旧版：默认主目录的对话目录

_ws_root = str(Path(cfg.security.workspace).resolve())  # 当前工作目录（可切换）


def _sessions_file() -> Path:
    """当前工作目录的会话存储（按工作目录隔离）。"""
    return Path(_ws_root) / ".openagent" / "sessions.json"


def _conv_dir() -> Path:
    return Path(_ws_root) / ".openagent" / "conversations"


def _load_sessions():
    sf = _sessions_file()
    if not sf.exists():
        # 兼容迁移：默认主目录首次使用时，把旧版 data/sessions.json 迁移过去
        legacy = root / "data" / "sessions.json"
        if Path(_ws_root).resolve() == (root / "data").resolve().parent and legacy.exists():
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                for s in data.get("sessions", []):
                    _sessions[s["id"]] = s
            except Exception:  # noqa: BLE001
                _sessions.clear()
        return
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        for s in data.get("sessions", []):
            _sessions[s["id"]] = s
    except Exception:  # noqa: BLE001
        _sessions.clear()


def _conv_path(sid: str) -> Path:
    return _conv_dir() / f"{sid}.md"


def _write_conversation_md(sid: str):
    """把会话写为当前工作目录 .openagent/conversations/ 下的 Markdown 文件。"""
    s = _sessions.get(sid)
    if not s:
        return
    _conv_dir().mkdir(parents=True, exist_ok=True)
    title = s["title"].strip()[:40] or "新对话"
    ts = datetime.fromtimestamp(s["created_at"]).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title}", f"> 会话 {sid} ｜ 创建于 {ts} ｜ OpenAgent", ""]
    for m in s["messages"]:
        role = "🧑 用户" if m["role"] == "user" else "🤖 智能体"
        kind = m.get("kind")
        if kind:
            tag = {"thought": "思考", "tool_call": "工具调用", "tool_result": "工具结果",
                   "progress": "过程", "error": "错误"}.get(kind, kind)
            tool = m.get("tool", "")
            lines.append(f"## {role}（{tag}{f'：{tool}' if tool else ''}）")
        else:
            lines.append(f"## {role}")
        lines.append("")
        lines.append(str(m["content"]))
        lines.append("")
    _conv_path(sid).write_text("\n".join(lines), encoding="utf-8")


def _save_sessions():
    _sessions_file().parent.mkdir(parents=True, exist_ok=True)
    payload = {"sessions": sorted(_sessions.values(), key=lambda s: s["created_at"])}
    _sessions_file().write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for sid in _sessions:
        _write_conversation_md(sid)


_load_sessions()
# 回填：把已有会话写到 conversations/ 独立目录
if _sessions:
    with _sessions_lock:
        _save_sessions()


def _new_session() -> dict:
    s = {
        "id": uuid.uuid4().hex[:10],
        "title": "新对话",
        "created_at": time.time(),
        "messages": [],
    }
    _sessions[s["id"]] = s
    _save_sessions()
    return s


def get_agent(session_id: str) -> Agent:
    """惰性创建会话专属 Agent，并用持久化历史恢复上下文。"""
    if session_id in _agents:
        return _agents[session_id]
    agent = Agent(cfg, on_event=None, quiet=True, session_id=session_id)  # Web 模式危险命令直接拒绝
    for m in _sessions.get(session_id, {}).get("messages", []):
        if m.get("kind"):  # 中间过程事件（思考/工具调用等）不参与模型历史回放
            continue
        agent.history.messages.append({"role": m["role"], "content": m["content"]})
    _agents[session_id] = agent
    return agent


class ChatRequest(BaseModel):
    goal: str
    session_id: str | None = None


@app.get("/")
def index():
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/tools")
def tools():
    ag = get_agent(next(iter(_sessions), "")) if _sessions else Agent(cfg, on_event=None, quiet=True)
    return {
        "tools": [
            {"name": n, "description": ag.registry.get(n).description}
            for n in ag.registry.names()
        ],
        "executor_model": cfg.api.executor_model,
        "planner_model": cfg.api.planner_model,
        "api_mode": bool(cfg.api.api_key),
    }


@app.get("/api/memory")
def memory():
    ag = get_agent(next(iter(_sessions), "")) if _sessions else Agent(cfg, on_event=None, quiet=True)
    return {"count": len(ag.memory.entries) if ag.memory else 0}


@app.get("/api/sessions")
def list_sessions():
    with _sessions_lock:
        return sorted(
            (
                {"id": s["id"], "title": s["title"], "created_at": s["created_at"]}
                for s in _sessions.values()
            ),
            key=lambda s: -s["created_at"],
        )


@app.post("/api/sessions")
def create_session():
    with _sessions_lock:
        s = _new_session()
        return {"id": s["id"], "title": s["title"]}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    with _sessions_lock:
        _sessions.pop(sid, None)
        _save_sessions()
    # 不调用 ag.close()：浏览器会话是全局共享的懒加载资源，删除单个会话不应关闭全局
    # 浏览器（否则会误关其它会话正在用的浏览器窗口，且部分内嵌 WebView 对删除时的
    # 进程/窗口回调敏感，可能整页重置）。
    _agents.pop(sid, None)
    cp = _conv_path(sid)
    if cp.exists():
        try:
            cp.unlink()
        except OSError:
            pass
    return {"ok": True}


@app.get("/api/sessions/{sid}/messages")
def session_messages(sid: str):
    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            return {"messages": []}
        return {"messages": s["messages"]}


@app.delete("/api/sessions/{sid}/messages/{idx}")
def delete_message(sid: str, idx: int):
    with _sessions_lock:
        s = _sessions.get(sid)
        if not s or idx < 0 or idx >= len(s["messages"]):
            return {"error": "消息不存在"}
        s["messages"].pop(idx)
        _save_sessions()
    return {"ok": True}


class RenameRequest(BaseModel):
    title: str


@app.get("/api/tasks/running")
def running_tasks():
    """当前正在后台执行任务的会话列表（前端刷新后据此恢复显示）。"""
    return {
        "running": [
            {"sid": sid, "started": j["started"], "goal": j["goal"][:60]}
            for sid, j in _jobs.items() if not j.get("done")
        ]
    }


@app.get("/api/chat/stream/{sid}")
def reconnect_stream(sid: str):
    """断线重连：先回放任务已产生的事件，再实时续播直到任务结束。"""
    job = _jobs.get(sid)
    if not job:
        raise HTTPException(status_code=404, detail="该会话没有可重连的任务")
    q = job["q"]
    buf = job["buf"]

    def sse():
        yield f"data: {json.dumps({'type': 'session', 'payload': {'id': sid}}, ensure_ascii=False)}\n\n"
        if not job["done"]:
            yield f"data: {json.dumps({'type': 'task_goal', 'payload': {'goal': job['goal']}}, ensure_ascii=False)}\n\n"
        cursor = 0  # 快照游标：回放后只续播序号更大的事件，避免重复
        for seq, ev in list(buf):
            cursor = seq
            yield f"data: {json.dumps({'type': ev.type, 'payload': ev.payload}, ensure_ascii=False)}\n\n"
        while True:
            seq, ev = q.get()
            if ev is None:
                yield "event: done\ndata: {}\n\n"
                break
            if seq <= cursor:
                continue
            yield f"data: {json.dumps({'type': ev.type, 'payload': ev.payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{sid}/rename")
def rename_session(sid: str, req: RenameRequest):
    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            raise HTTPException(status_code=404, detail="会话不存在")
        s["title"] = req.title.strip()[:40] or s["title"]
        _save_sessions()
        return {"ok": True, "title": s["title"]}


# ---------------- 工作文件夹 API（右侧文件显示器用） ----------------

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".idea", ".vscode"}


def set_workspace_dir(new_root: str):
    """全局切换工作目录：保存当前会话 -> 清空并加载新目录的会话 ->
    文件面板 + 文件工具沙箱 + Shell/Python 工作目录同步重建。"""
    global _ws_root
    # 1. 保存当前工作目录的会话
    with _sessions_lock:
        if _sessions:
            _save_sessions()
    _ws_root = str(Path(new_root).resolve())
    cfg.security.workspace = _ws_root
    # 2. 重建会话存储：清空后加载新目录的 .openagent/sessions.json
    with _sessions_lock:
        _sessions.clear()
        _load_sessions()
    import agent.tools.file_tools as _ft
    import agent.tools.shell_tools as _st
    import agent.tools.python_tools as _pt
    _ft.set_workspace(_ws_root)
    _st.configure(_ws_root, cfg.security.dangerous_patterns, cfg.security.confirm_shell, None)
    _pt.configure(_ws_root)
    # 3. 重建所有会话 Agent（工作目录已变，旧实例的浏览器/工具配置作废）
    for sid, ag in list(_agents.items()):
        ag.close()
        _agents.pop(sid, None)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "workspace.json").write_text(
        json.dumps({"workspace": _ws_root}, ensure_ascii=False), encoding="utf-8"
    )


def load_saved_workspace():
    ws_file = root / "data" / "workspace.json"
    if ws_file.exists():
        try:
            saved = json.loads(ws_file.read_text(encoding="utf-8")).get("workspace", "")
            if saved and Path(saved).is_dir():
                set_workspace_dir(saved)
        except Exception:  # noqa: BLE001
            pass


load_saved_workspace()
_TEXT_VIEW_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".csv",
    ".log", ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".bat", ".sh", ".ps1", ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".toml",
}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _safe_rel_path(rel: str) -> Path:
    """相对路径 -> 沙箱内绝对路径；越界抛 PermissionError。"""
    p = (Path(_ws_root) / rel).resolve()
    if not str(p).startswith(str(Path(_ws_root).resolve())):
        raise PermissionError("路径越界")
    return p


@app.get("/api/workspace")
def get_workspace():
    return {"workspace": _ws_root, "drives": _list_drives()}


def _list_drives() -> list[str]:
    import string

    drives = []
    for ch in string.ascii_uppercase:
        d = f"{ch}:\\"
        if Path(d).exists():
            drives.append(d)
    return drives


class WorkspaceRequest(BaseModel):
    path: str


@app.get("/api/fs/list")
def fs_list(path: str):
    """弹窗资源管理器用：列出任意绝对路径下的子目录（仅目录，只读浏览）。"""
    p = Path(path).resolve()
    if not p.exists() or not p.is_dir():
        return {"error": "目录不存在", "dirs": [], "current": path}
    dirs = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if e.is_dir() and not e.name.startswith(".") and e.name not in _SKIP_DIRS:
                dirs.append(e.name)
    except PermissionError:
        pass
    return {"current": str(p), "dirs": dirs, "parent": str(p.parent) if p.parent != p else None}


@app.post("/api/workspace")
def switch_workspace(req: WorkspaceRequest):
    """切换工作目录。支持绝对路径（如 D:/projects）或盘符根。目录不存在时返回错误。"""
    newp = Path(req.path).resolve()
    if not newp.exists() or not newp.is_dir():
        return {"error": f"目录不存在: {req.path}"}
    set_workspace_dir(str(newp))
    return {"ok": True, "workspace": _ws_root}


@app.get("/api/files")
def list_files(rel: str = "."):
    """列目录（浅层），供文件树懒加载展开。"""
    try:
        p = _safe_rel_path(rel)
    except PermissionError:
        return {"error": "路径越界"}
    if not p.exists() or not p.is_dir():
        return {"error": "目录不存在"}
    items = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    except PermissionError:
        return {"error": "无权限访问该目录"}
    for e in entries:
        if e.name in _SKIP_DIRS or e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                items.append({"name": e.name, "type": "dir", "path": str(e.relative_to(_ws_root))})
            else:
                items.append({
                    "name": e.name, "type": "file", "path": str(e.relative_to(_ws_root)),
                    "size": e.stat().st_size,
                })
        except OSError:  # 个别条目 stat 失败（权限/断链），跳过不炸
            continue
    return {"path": rel, "items": items}


@app.get("/api/file/view")
def view_file(rel: str):
    """读取文件内容用于预览。文本直接返回；图片返回 base64；不支持格式给提示。"""
    try:
        p = _safe_rel_path(rel)
    except PermissionError:
        return {"error": "路径越界"}
    if not p.exists() or p.is_dir():
        return {"error": "文件不存在"}
    ext = p.suffix.lower()
    if ext in _IMG_EXTS:
        import base64

        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "image/png")
        return {"type": "image", "mime": mime,
                "data": base64.b64encode(p.read_bytes()).decode()}
    if ext in _TEXT_VIEW_EXTS or ext == "":
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="gbk", errors="replace")
        truncated = len(text) > 200_000
        return {"type": "text", "name": p.name, "ext": ext,
                "content": text[:200_000], "truncated": truncated, "total_chars": len(text)}
    return {"type": "unsupported", "ext": ext,
            "hint": "该格式不支持预览（二进制/专有格式）。可让智能体用 read_document 工具解析 docx/pdf/xlsx。"}


@app.post("/api/upload")
async def upload_doc(file: UploadFile, session_id: str = ""):
    DOC_EXT = (".docx", ".pdf", ".xlsx")
    name = Path(file.filename or "").name  # 去路径，防目录穿越
    if not name.lower().endswith(DOC_EXT):
        return {"error": f"仅支持 {'/'.join(DOC_EXT)} 文档上传"}
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        return {"error": "文件超过 20MB 限制"}

    sid = session_id if session_id in _sessions else "common"
    updir = root / "data" / "uploads" / sid
    updir.mkdir(parents=True, exist_ok=True)
    # 重名加时间戳避免覆盖
    dest = updir / name
    if dest.exists():
        dest = updir / f"{int(time.time())}_{name}"
    dest.write_bytes(content)
    # 返回相对工作目录的路径，供模型 read_document 直接使用
    rel = dest.relative_to(root)
    return {"path": str(rel), "size": len(content)}


@app.post("/api/chat")
def chat(req: ChatRequest):
    with _sessions_lock:
        if req.session_id and req.session_id in _sessions:
            sid = req.session_id
        else:
            sid = _new_session()["id"]
        session = _sessions[sid]
        # 同会话任务运行中再次请求 -> 明确拒绝，而不是静默排队带旧上下文执行（防串台污染）
        if sid in _running:
            raise HTTPException(
                status_code=409,
                detail="该对话正在执行任务，请等待完成后再发送，或新建一个对话。",
            )
        _running.add(sid)
    ag = get_agent(sid)
    q: queue.Queue = queue.Queue()
    collected: list = []  # 本任务中间过程事件（按序持久化，供刷新后回看）
    # 任务档案：事件缓冲 + 序号，支持前端刷新后重连（回放 + 续播）
    job = {"q": q, "buf": [], "seq": 0, "done": False, "started": time.time(), "goal": req.goal}
    _jobs[sid] = job

    def on_event(ev: AgentEvent):
        job["seq"] += 1
        item = (job["seq"], ev)
        job["buf"].append(item)
        q.put(item)
        if ev.type in ("thought", "tool_call", "tool_result", "progress", "error"):
            collected.append(ev)

    def worker():
        old_cb = ag.on_event
        ag.on_event = on_event
        try:
            with _exec_lock:  # 全局串行：同一时间只跑一个目标（保护浏览器与模型链路）
                answer = ag.run(req.goal)
        except Exception as e:  # noqa: BLE001
            job["seq"] += 1
            q.put((job["seq"], AgentEvent("error", {"text": str(e)})))
            answer = f"执行异常: {e}"
        finally:
            with _sessions_lock:
                _running.discard(sid)
            job["done"] = True
            ag.on_event = old_cb
            q.put((0, None))  # 结束哨兵
        # 持久化对话记录：用户消息 + 中间过程事件（思考/工具调用/正文，刷新后仍可回看）+ 最终回答
        with _sessions_lock:
            session["messages"].append({"role": "user", "content": req.goal})
            for ev in collected:
                p = ev.payload
                if ev.type == "thought":
                    session["messages"].append({"role": "assistant", "kind": "thought",
                                                "thinking": bool(p.get("thinking")),
                                                "content": str(p.get("text", ""))[:600]})
                elif ev.type == "tool_call":
                    session["messages"].append({"role": "assistant", "kind": "tool_call",
                                                "tool": str(p.get("tool", "")),
                                                "content": json.dumps(p.get("arguments", {}), ensure_ascii=False)[:400]})
                elif ev.type == "tool_result":
                    session["messages"].append({"role": "assistant", "kind": "tool_result",
                                                "tool": str(p.get("tool", "")),
                                                "error": bool(p.get("error")),
                                                "content": str(p.get("text", ""))[:400]})
                elif ev.type == "progress":
                    if str(p.get("text", "")).strip():  # 空 progress 不持久化（空气泡会积累 flex 间距形成大空白）
                        session["messages"].append({"role": "assistant", "kind": "progress",
                                                    "content": str(p.get("text", ""))[:600]})
                elif ev.type == "error":
                    if str(p.get("text", "")).strip():
                        session["messages"].append({"role": "assistant", "kind": "error",
                                                    "content": str(p.get("text", ""))[:600]})
            session["messages"].append({"role": "assistant", "content": answer[:4000]})
            if len(session["messages"]) <= 2:  # 首条目标作为会话标题
                session["title"] = req.goal.strip()[:24] or "新对话"
            _save_sessions()

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        yield f"data: {json.dumps({'type': 'session', 'payload': {'id': sid}}, ensure_ascii=False)}\n\n"
        while True:
            _, ev = q.get()
            if ev is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield f"data: {json.dumps({'type': ev.type, 'payload': ev.payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    print(f"OpenAgent Web: http://{cfg.server.host}:{cfg.server.port}")
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
