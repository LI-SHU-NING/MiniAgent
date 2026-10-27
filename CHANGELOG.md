# OpenAgent 工作过程更改日志

> 记录项目迭代过程中遇到的问题现象、根因、解决方案，以及下次构建同类项目可直接复用的提示词。
> 目的：避免重复踩坑——同类问题出现时，先查本日志，再按附录提示词让模型处理。

---

## 变更日志

### 2026-10-14 会话：前端可读性 / 过程持久化 / 并行约束

#### 1. 工具调用与思考内容刷新后消失

- **现象**：任务执行时能看到思考与工具调用的过程，但退出网页重新打开或切换对话再返回，过程内容全部消失，只剩"用户问 + 最终答"。
- **根因**：中间过程事件（`thought` / `tool_call` / `tool_result`）只通过 SSE 实时推给前端展示，落盘时只持久化了 `user` 消息和 `final` 回答（`server.py` 旧逻辑仅两条 `append`），切换会话时自然还原不出来。
- **解决**：
  - `server.py`：任务结束时按序把中间事件持久化为带 `kind` 字段的消息（`thought`/`tool_call`/`tool_result`/`progress`/`error`），每条截断 400-600 字符防止 `sessions.json` 膨胀。
  - `server.py get_agent`：模型历史回放时跳过带 `kind` 的过程事件，避免污染模型上下文（过程事件不是真实对话）。
  - `index.html switchSession`：按 `kind` 还原为折叠条或正文气泡。
- **相关文件**：`server.py`（`/api/chat` worker、`get_agent`、`_write_conversation_md`）、`web/static/index.html`（`switchSession`）。
- **注意**：该持久化只对改动后新跑的任务生效，旧会话刷新后仍只有"问+答"。

#### 2. 删除对话时页面直接"关闭"，只剩左上角一个 Details

- **现象**：在 Trae 内置浏览器（WebView）里删除会话后，主界面整页重置，只剩左上角一个 `Details` 文本；Edge 浏览器无此问题。服务端日志无 traceback、进程未崩溃。
- **根因**（两层）：
  1. 删除会话用的是原生 `confirm()` 弹窗。内嵌 WebView（Trae 内置浏览器）对原生 `confirm()/alert()` 处理不稳，确认后会重置页面框架——左上角 `Details` 是 WebView 自己的错误/详情面板，不是应用内容（`index.html` 里根本没有 `details` 元素）。
  2. `delete_session` 里调用 `ag.close()`，而 `close()` 会关闭**全局共享**的 Playwright 浏览器（`headless: false` 时是可见窗口），删任意一个会话会误关其他会话正在用的浏览器。
- **解决**：
  - 删除确认改为页面内自绘确认框 `uiConfirm()`（遮罩 + 删除/取消按钮），不再使用原生 `confirm()`。
  - `delete_session` 不再调用 `ag.close()`——浏览器是全局懒加载共享资源，删除单会话不应关闭它。
- **相关文件**：`web/static/index.html`（`uiConfirm`、`deleteSession`）、`server.py`（`/api/sessions/{sid}` DELETE）。

#### 3. deepseek 不主动并行调用工具

- **现象**：任务中工具内容大幅超过回答，步数多、页面被工具折叠条霸屏。deepseek 默认倾向一轮只发一个 `tool_call`（并非完全不会并行，只是很少主动批量）。
- **根因**：原生 FC 模式的执行链路（线程池并发、浏览器工具串行、finish_task 分流）早已就绪，但系统提示里没有引导模型一轮多调用，模型习惯"一步一工具"。
- **解决**：`executor.py` 新增 `PARALLEL_RULE`，native 模式注入系统提示，要求互不依赖的操作（多关键词搜索、多链接抓取、多文件读取）在一轮内并行发出全部 `tool_calls`，仅严格依赖时才顺序执行。
- **效果**：实测"今日要闻"任务 step 1 一次并行 4 个 `web_search`、step 4 并行 3 个调用，总步数从 IELTS 任务的 30 步降到约 14 步。
- **限制**：软约束，能显著提高并行频率但不能 100% 保证；且只治"步数多"，治不了"对反爬 412 反复重试"类无效操作（那是重复调用拦截/搜索僵局破除的范畴）。
- **相关文件**：`agent/executor.py`（`PARALLEL_RULE`、system 拼装处）。

#### 4. 增加过程总展开栏 + 任务结束自动收起

- **需求**：对全部思考与工具调用提供"总展开/总收起"控制，任务执行完毕时自动收起。
- **解决**：
  - `index.html` 聊天区顶部新增 sticky 工具栏按钮"全部展开/全部收起"（`.chat-tools`），`setAllEvents()` 统一切换所有 `.event` 折叠条的 `open` 状态。
  - `clearChat()` 清空内容后重建按钮（`chat.innerHTML = ''` 会连带清掉按钮）。
  - `final` 事件到达及 `send()` 收尾时自动 `setAllEvents(false)`。
- **相关文件**：`web/static/index.html`（`setAllEvents`、`clearChat`、`handleEvent`、`send`）。

#### 5. 执行过程中的正文汇报（progress 事件）

- **需求**：当前工具调用/思考是非正文式的折叠条，希望阶段太长、任务繁琐或执行较久时，模型用正文输出当前阶段或遇到的问题。
- **解决**：
  - `executor.py`：新增 `progress` 事件类型。native 模式下，每 4 步或距上次正文输出超过 40 秒时，向本次请求临时注入提示（不进持久上下文），要求模型在回复的 `content` 字段用 1~3 句中文播报进展/问题后再继续调工具。
  - 模型输出 `content`（非 thinking）时 emit `progress` 事件；JSON 模式仍走 `thought` 折叠条（content 是 JSON 动作）。
  - `index.html`：`progress` 以正文气泡显示（蓝色描边，区别于绿色 final），切换会话时按 `kind=progress` 还原。
- **相关文件**：`agent/executor.py`（`EVENT_TYPES`、循环内提示注入与事件分发）、`web/static/index.html`（`handleEvent`、CSS `.msg.agent.progress`）。

#### 6. 设备蓝屏排查（AMD 显卡驱动）

- **现象**：设备崩溃重启，期间有 AMD Radeon 界面弹出；怀疑与本项目运行相关。
- **诊断**：Windows 事件日志（System/Event 1001 WER）有 bugcheck 记录 `0x00000133 DPC_WATCHDOG_VIOLATION`，转储 `C:\Windows\Minidump\*.dmp`。`0x133` 属驱动级 DPC 看门狗超时，高度指向 AMD 显卡驱动（`amdkmdag.sys`）。
- **结论**：非应用代码缺陷，但硬件加速 Chromium（`headless: false` + 大量 `browser_navigate`）会增加触发概率。
- **处理**：建议 DDU 卸载重装/更新 AMD 驱动；可用 `Get-WinEvent` 查 `Kernel-Power`/`amdkmdag`/WHEA 事件定位；如确认是浏览器渲染压力，可将 `config.yaml` 的 `browser.headless` 临时改 `true` 缓解。

### 2026-09-25~26 会话：执行效率

- **问题**：deepseek-flash 思考模式输出冗余、单任务数十分钟无结束迹象、无中间进度。
- **方案**（已落地部分）：关闭思考（`no_think`）、恢复步数软上限（`max_steps_per_task: 35` + 接近上限注入剩余步数提示引导主动收尾）、`max_replans: 3`；后续补流式进度。

### 2026-09-15 会话：模型切换与工具调用

- 规划/执行模型切到 deepseek-r1:32b 后走 JSON 模式；`_probe_tool_mode` 自动探测 native/json。
- `_vision_describe` 视觉旁路：浏览器截图交 qwen3-vl:8b 转文本注入执行器。
- final 前置校验：实时性任务必须先调工具，禁止凭记忆直答。
- 工作目录切换弹文件管理器；会话与工作目录绑定（`.openagent/`）。

### 2026-09-12~14 会话：搜索与文档

- 自研 `websearch` 包集成（多引擎聚合 + trafilatura 正文提取 + 跨引擎排序）；修复响应头无 charset 导致中文乱码（`apparent_encoding`）。
- 搜索结果向量重排（nomic-embed 语义相似度 top_k）；连续 3 次 `web_search` 注入换策略提示（搜索僵局破除）。
- 新增 `read_document` 工具（docx/pdf/xlsx 解析），上传通道支持拖拽/粘贴。
- ReAct 历史滚动压缩：长工具结果仅保留最近 2 条全文，更早截为摘要。

### 2026-09-10~11 会话：基建与模型适配

- Ollama OpenAI 兼容层：思考内容在 `reasoning` 字段（model_extra）；`max_tokens` 含思考消耗（思考型模型 ≥2048）。
- 远程模型必须加请求超时 + 重试（单请求实测最长 300s+）。
- 浏览器元素定位用 JS 注入 `data-agent-ref` 编号，比坐标稳定。
- 工具注册防静默失效：`register_function` 缺装饰器直接 raise；`_coerce_args` 把字符串参数转 int/float/bool（注意 `from __future__ import annotations` 使注解变字符串）。
- 关键不变量要有防回归测试（`test_builtin_tools_registered` 校验 19 个工具齐全）。

---

## 附录：构建此类项目的可复用提示词

> 下次构建同类 Agent 项目（LLM 规划 + ReAct 执行 + 工具调用 + Web UI）遇到以下问题时，直接把这些提示词贴给模型。

### P1. 让模型主动并行调用工具

```
当前模型使用原生 Function Calling，但倾向一轮只调用一个工具，导致步数多、耗时长。
请在系统提示中加入并行约束：当一条回复里存在多个互不依赖的操作时
（如同时用多个关键词搜索、同时抓取多个链接、同时读取多个文件），
必须在本轮一次性并行发出全部 tool_calls，不要拆成多轮逐个执行；
仅当下一步严格依赖上一步结果时才顺序执行。执行链路需要支持：
一轮多 tool_calls 用线程池并发（共享浏览器会话的工具必须串行，
finish_task 等收尾工具最后单独处理），并保证 tool_calls 与 role=tool 消息严格配对。
```

### P2. 内嵌 WebView 中页面被重置 / confirm 弹窗异常

```
Web UI 运行在 IDE 内置浏览器（WebView）中，与标准浏览器行为不同：
原生 confirm()/alert() 会导致页面框架被重置（页面只剩左上角一个 Details，
这是 WebView 的错误面板，不是应用内容），标准浏览器无法复现。
请避免使用任何原生弹窗（confirm/alert/prompt），改用页面内自绘的
确认弹窗（遮罩 div + 按钮，用 Promise 包装异步确认）。同时在 CSS 优先级上
注意内联 style 高于类选择器，显示/隐藏弹窗应直接操作 style.display。
```

### P3. 执行过程信息刷新后丢失

```
当前任务执行过程的思考/工具调用只通过 SSE 实时推送给前端，没有落盘，
导致刷新页面或切换会话后过程内容全部消失，只剩"用户问+最终答"。
请：1) 服务端在任务结束时把中间事件（thought/tool_call/tool_result/正文）
按序持久化到会话记录，每条带 kind 字段标识类型并截断长度防膨胀；
2) 模型历史回放（把历史消息喂回 LLM）时跳过这些带 kind 的过程事件，
避免污染上下文；3) 前端切换会话时按 kind 还原为折叠条或正文气泡。
```

### P4. 执行过程的正文阶段汇报

```
任务执行中用户只能看到一排工具折叠条，看不到阶段进展。
请新增一个 progress 正文事件：在执行循环里每 N 步（如 4 步）或距上次
正文输出超过 T 秒（如 40s）时，向本次模型请求临时注入一条 user 提示
（只影响本次回复、不进持久历史），要求模型在回复的 content 字段用 1~3 句
中文播报当前进展、已完成事项或遇到的问题，然后继续调用工具。
native Function Calling 模式下模型返回的 content 即作为 progress 正文
在前端以普通正文气泡展示（区别于 final 的绿色样式）。
```

### P5. 删除单会话误关共享资源

```
删除某个会话时，如果调用了 agent.close()，而 close() 会关闭全局共享的
浏览器单例（headless=false 时为可见窗口），会导致其他会话的浏览器被误关。
删除单个会话应只清理会话数据与对话文件，不应关闭全局共享的懒加载资源；
共享资源按引用计数或会话数管理生命周期。
```

### P6. 蓝屏/驱动崩溃排查（AMD）

```
Windows 设备随机蓝屏重启，先查事件日志：Get-WinEvent -FilterHashtable
@{LogName='System'; StartTime=(Get-Date).AddHours(-12)} 过滤 Kernel-Power /
WHEA / amdkmdag / Display，重点找 Event 1001（bugcheck 记录）与 Event 41
（内核电源）。常见码 0x00000133 DPC_WATCHDOG_VIOLATION 是驱动级
DPC 看门狗超时，AMD 显卡驱动是高频来源；结合 Radeon 弹窗现象定位。
应用侧缓解：headless 浏览器会叠加 GPU 渲染压力，可临时改 headless 降低触发概率；
根治需 DDU 卸载重装显卡驱动。分析转储需 windbg（`!analyze -v`）。
```

### P7. 通用 Agent 项目提示词清单（一次贴全）

```
你正在构建一个 LLM Agent 项目（规划器 + ReAct 执行器 + 工具注册 + Web UI），
请遵守以下约束：
1. 模型可能是远程 API，必须给每次请求加超时与重试；思考型模型的 max_tokens
   要包含思考消耗（建议 ≥2048），否则 content 可能为空。
2. 工具调用模式要先探测：原生 Function Calling 可用则用，否则降级为
   JSON 模式（每步回复必须是 {"action","arguments"} 或 {"action":"final"}）。
3. 并行工具：互不依赖的调用一轮并行执行；共享浏览器会话的工具串行；
   assistant 消息的 tool_calls 必须紧跟同 id 的 role=tool 消息，
   否则 OpenAI 兼容接口返回 400。
4. 历史上下文要有滚动压缩（长工具结果只保留最近几条全文，更早截摘要）。
5. 重复工具调用要拦截（同工具+同参数连续调用返回提示，要求换方法）。
6. 搜索结果要防僵局：连续 N 次 web_search 后注入换策略提示。
7. 过程事件（思考/工具调用/正文）要能持久化并在刷新后回放，且不污染
   喂给模型的对话历史。
8. 实时性问题禁止凭记忆直答：未调用工具不允许输出 final。
9. 所有工具参数要做类型矫正（字符串 "120" 转 int），并注意
   `from __future__ import annotations` 会把注解变成字符串。
10. 前端避免原生 confirm/alert（WebView 会重置页面）；事件流用折叠条 +
    正文气泡两级展示，并提供总展开/收起控制。
```
