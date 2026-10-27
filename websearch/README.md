# websearch

多引擎聚合搜索 + 网页正文提取的纯搜索 CLI 工具（**不引入大模型**），聚焦搜索质量。

## 功能特性

- **多引擎聚合**：同时查询 Bing 中国版、百度、搜狗、360 搜索，国内可直连，无需 API Key
- **正文提取**：不只给链接，还会抓取网页并用 trafilatura 提取正文（提取失败自动降级为摘要）
- **质量优先排序**：跨引擎命中 + query 词覆盖度双重信号，不依赖大模型也能提升相关性
- **跳转解析**：自动解析百度/搜狗/360 的跳转链接，拿到真实 URL
- **双调用方式**：命令行交互 + Python API，天然适合作为 Agent 的搜索工具

## 安装

```bash
cd D:\websearch
pip install -r requirements.txt
# 全局 site-packages 无权限时：
pip install -r requirements.txt --user
```

## 命令行使用

```bash
# 基本用法（返回 5 条，含正文）
python -m websearch "YOLO 车辆检测 CPU部署"

# 指定条数、不抓正文（更快）
python -m websearch "python 异步编程" -n 3 --no-content

# JSON 输出（Agent 子进程调用推荐）
python -m websearch "关键词" --json

# 只用部分引擎
python -m websearch "关键词" --engine bing,baidu

# 其他参数：--timeout 15（超时秒） / --max-chars 3000（正文截断长度）
```

## 作为 Agent 工具调用

### 方式一：Python 直接导入（推荐）

```python
from websearch.api import search

result = search("搜索关键词", num=5)   # 返回纯 dict，可直接 json.dumps
for r in result["results"]:
    print(r["title"], r["url"], r["content"][:200])
```

Agent 只需把 LLM 生成的搜索关键词传入 `query` 参数即可，无任何交互依赖。

### 方式二：子进程调用 CLI

```bash
python -m websearch "关键词" --json
```

输出稳定的 UTF-8 JSON，结构如下：

```json
{
  "query": "...",
  "engines_used": ["bing", "baidu", "sogou"],
  "engine_errors": {},
  "num_results": 5,
  "elapsed_seconds": 9.8,
  "results": [
    {
      "title": "...",
      "url": "真实URL（已解析跳转）",
      "engines": ["bing", "baidu"],
      "score": 21.0,
      "snippet": "...",
      "content": "网页正文（最多 max-chars 字符）",
      "content_method": "trafilatura | fallback | snippet_fallback | fetch_failed | skipped"
    }
  ]
}
```

## 项目结构

```
websearch/
├── websearch/
│   ├── engines.py        # 引擎适配层（bing/baidu/sogou/so，新增引擎在 ENGINES 注册）
│   ├── merger.py         # URL 规范化去重 + 标题相似合并 + 跨引擎加权 + query覆盖度重排
│   ├── extractor.py      # trafilatura 正文提取 + BeautifulSoup 兜底
│   ├── logging_setup.py  # 文件日志（按大小轮转，logs/websearch.log）
│   ├── api.py            # search() 对外 API（Agent 入口，含脏数据过滤）
│   ├── cli.py            # 命令行入口
│   └── __main__.py
├── logs/                 # 运行后自动创建，存放 websearch.log
├── requirements.txt
└── README.md
```

## 排序规则

```
score = 命中引擎数 × 10 + Σ(1 / 各引擎内原始排名) + query覆盖度 × 8 - 词典页惩罚(6)
```

- 多个引擎都返回的同一结果排在最前 —— 跨引擎一致是最强质量信号
- query 覆盖度 = 标题+摘要覆盖 query 语义单元（中文二元组/英文整词）的比例，
  用于压制"只命中第一个词"的宽泛结果（如搜"如何缓解颈椎酸痛"返回"如何_百度百科"）
- 标题形如"词条（汉语词语）_XX百科"的词典消歧页直接扣分，长 query 下几乎必然跑题

## 日志

每次搜索都会写入 `D:\websearch\logs\websearch.log`（按 5MB 轮转，保留 3 份）：
- search start：query / num / engines / fetch_content
- 各引擎返回条数和错误
- merge 阶段：raw=29 → after_filter=26
- search done + 每条结果的 engines/score/提取方式/正文长度/URL

方便你事后回看每次搜索的效果。

## 脏数据过滤

合并后会丢弃以下条目（避免出现"一长串 URL 没标题没正文"的退化结果）：
- 标题为空
- URL 仍是百度/搜狗跳转（`www.baidu.com` / `www.sogou.com` 主域名）→ 跳转解析失败
- 没有命中任何引擎

## Debug 模式

排查问题时启用，会记录 HTTP 请求耗时/状态、引擎耗时、跳转解析尝试次数、抓取字节数、正文提取方式等细节到独立日志：

```powershell
$env:WEBSEARCH_DEBUG = "1"
python -m websearch "关键词" -n 5
```

- 主日志 `logs/websearch.log`：INFO 级别（每次搜索的结构化摘要）
- Debug 日志 `logs/debug.log`：DEBUG 级别（每步细节，按 10MB 轮转）

## LLM 自动评估（可选）

搜索质量评估可接入任意 OpenAI 兼容模型（在 `config.yaml` 的 `api` 段配置 `base_url` / `api_key` / 模型名），对 websearch 返回结果按 relevance / content_quality / usefulness 维度打分，便于批量观察质量走势。

## 已知限制

- 百度百科等强反爬站点可能拒绝正文抓取，此时自动降级为搜索摘要
- 百度/搜狗频繁请求可能触发验证码（正常单次使用请求量很小，风险低）
- B 站等 JS 渲染页面提取到的正文质量一般（后续版本可考虑引入播放页专用解析）

## 迭代计划

- [x] v0.1 多引擎聚合 + 正文提取（当前版本）
- [ ] v0.2 SQLite 搜索缓存、结果持久化
- [ ] v0.3 更多引擎适配（360、头条等）、Agent 工具 Schema（MCP/Function Calling 格式）
