"""Web 工具：URL 抓取（正文抽取）+ 联网搜索（多引擎聚合）

web_search 基于 websearch 包（多引擎聚合：Bing 中国版/百度/搜狗/360），
自带正文提取（trafilatura）、跨引擎质量排序、跳转链接解析与脏数据过滤。
"""
from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from .registry import tool

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


@tool(description="请求网页 URL，返回正文文本（自动去除脚本/样式）。也可用于调用 JSON API。")
def fetch_url(url: str, max_chars: int = 6000) -> str:
    """抓取网页内容。"""
    with httpx.Client(timeout=30, follow_redirects=True, headers=_HEADERS) as client:
        resp = client.get(url)
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        body = resp.text
    elif "html" in ctype or "text" in ctype or not ctype:
        body = _extract_text(resp.text)
    else:
        return f"content-type={ctype}，非文本内容未解析，HTTP {resp.status_code}"
    meta = f"URL: {resp.url}\nHTTP {resp.status_code}\n"
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n...[截断，正文共 {len(body)} 字符]"
    return meta + body


@tool(
    description=(
        "联网搜索关键词（多引擎聚合：必应/百度/搜狗/360），返回带网页正文的搜索结果。"
        "当前网络环境下这是首选搜索方式。"
    )
)
def web_search(query: str, max_results: int = 4, fetch_content: bool = True) -> str:
    """聚合搜索并返回带正文的结果列表。fetch_content=false 时更快但只有摘要。"""
    try:
        from websearch.api import search
    except ImportError as e:
        return f"搜索组件未安装: {e}"

    try:
        data = search(query, num=max_results + 2, fetch_content=fetch_content)
    except Exception as e:  # noqa: BLE001
        return (
            f"搜索失败: {type(e).__name__}: {e}。"
            "可稍后重试，或用 fetch_url 直接访问已知网站，或用浏览器打开 bing.com 手动搜索。"
        )

    results = data.get("results") or []
    if not results:
        return (
            "搜索无结果。建议：换更具体/更口语化的关键词重试；"
            "或用浏览器直接访问知名站点；或用 fetch_url 直接访问目标网站。"
        )

    out = [f"搜索 '{data.get('query', query)}' 共 {len(results)} 条（引擎: {','.join(data.get('engines_used', []))}）"]
    for i, r in enumerate(results[:max_results], 1):
        title = " ".join(str(r.get("title", "")).split())
        url = r.get("url", "")
        content = " ".join(str(r.get("content") or r.get("snippet") or "").split())
        method = r.get("content_method", "")
        engines = ",".join(r.get("engines", []))
        if len(content) > 1500:
            content = content[:1500] + "...[正文截断]"
        out.append(
            f"{i}. {title}\n"
            f"   链接: {url}\n"
            f"   命中引擎: {engines}\n"
            f"   正文: {content}"
            + (f"（提取方式: {method}，正文抓取可能被拦截，可信度略降）" if method in ("snippet_fallback", "fetch_failed") else "")
        )
    out.append(
        "提示: 结果已含网页正文；如需某条更多细节可用 fetch_url 抓取其链接全文。"
        "引用来源时标注编号与链接。"
    )
    return "\n\n".join(out)
