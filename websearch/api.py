"""对外 API 层 —— Agent / 其他程序调用的入口

用法 1（Python 直接导入，推荐给 Agent 用）:
    from websearch.api import search
    result = search("YOLO 车辆检测", num=5)
    # result 是纯 dict，可直接 json.dumps

用法 2（子进程调用 CLI）:
    python -m websearch "关键词" --json
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .engines import DEFAULT_TIMEOUT, BASE_HEADERS, BaseEngine, SearchResult, create_engines
from .extractor import extract_main_text, fetch_page
from .logging_setup import get_logger
from .merger import merge_results


# 未解析成功的跳转链接 / 纯 SERP 页面（百度/搜狗/360 主域名只用作 SERP/跳转，不会承载真实内容）
_REDIRECT_HOSTS = ("www.baidu.com", "www.sogou.com", "www.so.com")
_SERP_PATH_MARKERS = (
    # 任何 baidu/sogou 子域下的搜索结果页都是 SERP，没有正文价值
    "baidu.com/s?",
    "baidu.com/s/",
    "baidu.com/sf/vsearch",
    "image.baidu.com/search/",
    "sogou.com/web?",
    "sogou.com/web/",
    "wenku.so.com/s?",
    "news.so.com/ns?",
)
# 验证码/登录墙/反爬拦截页：跳转解析落到这些 host 上说明目标站拒绝了抓取
_BLOCK_HOST_PREFIXES = ("wappass.", "passport.", "verify.", "antispider.")
# 搜索引擎自有垂直频道（图片/视频/AI 问答），不是目标网页内容
_VERTICAL_HOSTS = (
    "image.so.com", "video.so.com", "tv.360kan.com", "ai.so.com",
    "image.baidu.com", "v.baidu.com", "tv.sohu.com",
)


def _is_valid_entry(item: dict) -> bool:
    """过滤：标题为空 / URL 仍是引擎跳转 / 验证码页 / 垂直频道 / 没有命中任何引擎 → 脏数据"""
    if not item.get("title") or not item["title"].strip():
        return False
    if not item.get("engines"):
        return False
    from urllib.parse import urlsplit
    try:
        host = urlsplit(item.get("url", "")).netloc.lower()
    except ValueError:
        return False
    if host in _REDIRECT_HOSTS:
        return False
    # 验证码/登录墙（如百度 wappass 验证码页曾作为结果泄漏进输出）
    if any(host.startswith(p) for p in _BLOCK_HOST_PREFIXES):
        return False
    # 引擎自有垂直频道（图片/视频/AI 问答）
    if host in _VERTICAL_HOSTS:
        return False
    # 百度/搜狗子域上的搜索结果页（SERP）也无正文价值
    full_url = item.get("url", "")
    if any(m in full_url for m in _SERP_PATH_MARKERS):
        return False
    return True


def search(
    query: str,
    num: int = 5,
    engines: tuple[str, ...] | list[str] = ("bing", "baidu", "sogou", "so"),
    fetch_content: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    max_content_chars: int = 3000,
    fetch_workers: int = 4,
) -> dict:
    """执行一次聚合搜索。

    参数:
        query: 搜索关键词（Agent 直接传入即可，无需预处理）
        num: 返回结果条数
        engines: 参与聚合的引擎列表
        fetch_content: 是否抓取并提取网页正文
        timeout: 单请求超时（秒）
        max_content_chars: 正文最大保留字符数
        fetch_workers: 抓取正文/解析跳转的并发数

    返回 dict（JSON 安全）:
        {
          "query": ...,
          "engines_used": [...],
          "engine_errors": {...},      # 各引擎报错信息（如有）
          "num_results": N,
          "elapsed_seconds": x.x,
          "results": [
             {
               "title": ...,
               "url": ...,
               "engines": ["bing", "baidu"],   # 命中的引擎（质量信号）
               "score": 21.5,
               "snippet": ...,
               "content": "...",               # 正文（可能为空）
               "content_method": "trafilatura" # 提取方式
             }, ...
          ]
        }
    """
    if not query or not query.strip():
        return {"query": query, "error": "关键词不能为空", "results": []}

    query = query.strip()
    t0 = time.time()
    log = get_logger()
    log.info("search start | query=%r | num=%d | engines=%s | fetch_content=%s",
             query, num, list(engines), fetch_content)

    engine_objs: list[BaseEngine] = create_engines(list(engines), timeout)

    # 1. 并发搜索各引擎（每引擎多取候选，给相关性重排留足池子）
    all_results: list[SearchResult] = []
    with ThreadPoolExecutor(max_workers=max(1, len(engine_objs))) as pool:
        futures = {pool.submit(e.search, query, min(max(num * 3, 12), 20)): e for e in engine_objs}
        for fut in as_completed(futures):
            rs = fut.result()
            all_results.extend(rs)
            log.info("engine result | engine=%s | count=%d | error=%s",
                     futures[fut].name, len(rs), futures[fut].last_error)

    engine_errors = {e.name: e.last_error for e in engine_objs if e.last_error}

    # 2. 解析跳转链接（百度/搜狗），得到真实 URL
    with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        future_map = {}
        for r in all_results:
            eng = _engine_of(r, engine_objs)
            resolver = getattr(eng, "resolve_redirect", None)
            if resolver:
                future_map[pool.submit(resolver, r.url)] = r
        for fut in as_completed(future_map):
            future_map[fut].url = fut.result()
    log.debug("redirects resolved | total=%d", len(future_map))

    # 3. 合并去重 + 排序（带 query 相关性重排：覆盖度加分 + 词典页惩罚）
    merged = merge_results(all_results, query=query)
    merged_filtered = [m for m in merged if _is_valid_entry(m)]
    dropped = len(merged) - len(merged_filtered)
    log.info("merge | raw=%d | after_filter=%d | dropped=%d",
             len(merged), len(merged_filtered), dropped)
    log.debug("merge detail | %s",
              " | ".join(f"{m['title'][:30]}={m['engines']}" for m in merged))

    # 多取一些候选（按 num 的 2 倍），过滤后通常仍然够
    top = merged_filtered[:num]

    # 4. 抓取并提取正文
    if fetch_content and top:
        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            future_map = {pool.submit(_fetch_one, m, timeout): m for m in top}
            for fut in as_completed(future_map):
                fut.result()  # 结果已写回 dict，这里只收集异常
        for m in top:
            content = m.pop("_raw_content", "")
            method = m.pop("_raw_method", "none")
            if len(content) > max_content_chars:
                content = content[:max_content_chars] + " …[截断]"
            m["content"] = content
            m["content_method"] = method
    else:
        for m in top:
            m["content"] = ""
            m["content_method"] = "skipped"

    # 5. 整理输出结构
    results = [
        {
            "title": m["title"],
            "url": m["url"],
            "engines": m["engines"],
            "score": m["score"],
            "coverage": m.get("coverage", 0.0),
            "snippet": m["snippet"],
            "content": m["content"],
            "content_method": m["content_method"],
        }
        for m in top
    ]

    elapsed = round(time.time() - t0, 2)
    log.info("search done | query=%r | num_results=%d | elapsed=%ss",
             query, len(results), elapsed)
    for i, r in enumerate(results, 1):
        log.info("  [%d] %s | engines=%s | score=%.2f | cov=%.2f | method=%s | len=%d | %s",
                 i, r["title"][:60], r["engines"], r["score"], r["coverage"],
                 r["content_method"], len(r["content"]), r["url"])

    return {
        "query": query,
        "engines_used": list(engines),
        "engine_errors": engine_errors,
        "num_results": len(results),
        "elapsed_seconds": elapsed,
        "results": results,
    }


def _engine_of(result: SearchResult, engine_objs: list[BaseEngine]) -> BaseEngine:
    for e in engine_objs:
        if e.name == result.engine:
            return e
    return engine_objs[0]


def _fetch_one(item: dict, timeout: int) -> None:
    """抓取并提取单条结果的正文，写回临时字段"""
    session = requests.Session()
    try:
        session.headers.update(BASE_HEADERS)
        html = fetch_page(session, item["url"], timeout)
        if html is None:
            item["_raw_content"], item["_raw_method"] = "", "fetch_failed"
            return
        text, method = extract_main_text(html, item["url"])
        # 正文提取失败时降级为搜索结果摘要
        if not text:
            text, method = item["snippet"], "snippet_fallback"
        item["_raw_content"], item["_raw_method"] = text, method
    finally:
        session.close()
