"""网页正文提取：trafilatura 为主，BeautifulSoup 兜底"""
from __future__ import annotations

import re
import time

import requests
import trafilatura
from bs4 import BeautifulSoup

from .logging_setup import get_logger

MAX_PAGE_BYTES = 3_000_000  # 单页最大下载量，防止异常大文件

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def fetch_page(session: requests.Session, url: str, timeout: int) -> str | None:
    """下载页面 HTML，失败返回 None"""
    log = get_logger()
    t0 = time.time()
    try:
        resp = session.get(
            url, timeout=timeout, headers=UA_HEADERS, stream=True, allow_redirects=True
        )
        # 限制下载体积
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                log.debug("fetch truncated at %d bytes | %s", MAX_PAGE_BYTES, url[:80])
                break
        resp.close()
        # 编码检测：响应头未声明 charset 时 requests 会默认 ISO-8859-1，
        # 中文页面会乱码——改用页面内容探测（apparent_encoding）
        enc = resp.encoding
        if not enc or enc.lower() in ("iso-8859-1", "latin-1"):
            enc = resp.apparent_encoding or "utf-8"
        html = b"".join(chunks).decode(enc, errors="replace")
        log.debug(
            "fetch ok | status=%d bytes=%d %.2fs | %s",
            resp.status_code, total, time.time() - t0, url[:80],
        )
        return html if html.strip() else None
    except Exception as exc:  # noqa: BLE001 - 抓取失败不致命，降级即可
        log.debug("fetch failed | %s | %.2fs | %s", exc, time.time() - t0, url[:80])
        return None


def extract_main_text(html: str, url: str) -> tuple[str, str]:
    """提取正文，返回 (text, method)。method: trafilatura / fallback / none"""
    log = get_logger()
    # 主路径：trafilatura（正文提取质量最高的开源方案）
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        if text and len(text.strip()) >= 30:
            log.debug("extract ok via trafilatura | %d chars | %s",
                      len(text), url[:80])
            return text.strip(), "trafilatura"
    except Exception as exc:  # noqa: BLE001
        log.debug("trafilatura failed: %s | %s", exc, url[:80])

    # 兜底路径：BeautifulSoup 粗提取
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        root = soup.find("article") or soup.body or soup
        text = root.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text.strip()) >= 30:
            log.debug("extract ok via fallback | %d chars | %s",
                      len(text), url[:80])
            return text.strip(), "fallback"
    except Exception as exc:  # noqa: BLE001
        log.debug("fallback failed: %s | %s", exc, url[:80])

    log.debug("extract FAILED (no usable content) | %s", url[:80])
    return "", "none"
