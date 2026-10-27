"""搜索引擎适配层

每个引擎实现 _search(query, num)，统一返回 SearchResult 列表。
任何引擎失败都不会抛出异常，只会记录到 last_error 并返回空列表，
保证聚合层可以安全并发调用。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .logging_setup import get_logger

DEFAULT_TIMEOUT = 15

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class SearchResult:
    """单条搜索结果"""

    title: str
    url: str
    snippet: str
    engine: str
    rank: int  # 在该引擎中的原始排名，从 1 开始

    def is_valid(self) -> bool:
        return bool(self.title and self.url.startswith("http"))


class BaseEngine:
    name = "base"

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.last_error: str | None = None
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)

    # ---------- 对外接口 ----------

    def search(self, query: str, num: int = 10) -> list[SearchResult]:
        """搜索入口，永不抛异常"""
        self.last_error = None
        log = get_logger()
        t0 = time.time()
        try:
            results = self._search(query, num)
        except Exception as exc:  # noqa: BLE001 - 引擎层吞掉一切网络/解析异常
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.debug("engine %s failed in %.2fs: %s", self.name, time.time() - t0, exc)
            return []
        valid = [r for r in results if r.is_valid()]
        elapsed = time.time() - t0
        log.debug(
            "engine %s done in %.2fs | raw=%d valid=%d | query=%r",
            self.name, elapsed, len(results), len(valid), query,
        )
        return valid

    # ---------- 工具方法 ----------

    def _get(self, url: str, **kw) -> requests.Response:
        headers = kw.pop("headers", {})
        headers.setdefault("Referer", "https://www.baidu.com/")
        log = get_logger()
        t0 = time.time()
        resp = self.session.get(url, timeout=self.timeout, headers=headers, **kw)
        resp.raise_for_status()
        log.debug(
            "http GET %s | status=%d | bytes=%d | %.2fs",
            url[:120], resp.status_code, len(resp.content), time.time() - t0,
        )
        return resp

    @staticmethod
    def _clean(text: str | None) -> str:
        return re.sub(r"\s+", " ", text or "").strip()


class BingEngine(BaseEngine):
    """Bing 中国版，稳定性和反爬友好度最好，作为主力引擎"""

    name = "bing"

    def _search(self, query: str, num: int) -> list[SearchResult]:
        resp = self._get(
            "https://cn.bing.com/search",
            params={"q": query, "count": min(max(num * 2, 10), 30), "mkt": "zh-CN"},
            headers={"Referer": "https://cn.bing.com/"},
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[SearchResult] = []
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a[href]")
            if not a:
                continue
            snippet_el = li.select_one(".b_caption p") or li.select_one("p")
            results.append(
                SearchResult(
                    title=self._clean(a.get_text()),
                    url=a["href"],
                    snippet=self._clean(snippet_el.get_text() if snippet_el else ""),
                    engine=self.name,
                    rank=len(results) + 1,
                )
            )
            if len(results) >= num:
                break
        return results


class BaiduEngine(BaseEngine):
    """百度：结果链接是跳转链接，需要解析真实 URL"""

    name = "baidu"

    def _search(self, query: str, num: int) -> list[SearchResult]:
        resp = self._get(
            "https://www.baidu.com/s",
            params={"wd": query, "rn": min(max(num * 2, 10), 20)},
            headers={"Referer": "https://www.baidu.com/"},
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[SearchResult] = []
        for container in soup.select("div.c-container"):
            a = container.select_one("h3 a[href]")
            if not a:
                continue
            # baidu.php 是推广/商业链接（广告），跳过以保证结果质量
            if "baidu.php?" in a["href"]:
                continue
            snippet_el = (
                container.select_one("[class*='abstract']")
                or container.select_one("[class*='summary-text']")
                or container.select_one("span[class*='content-right']")
                or container.select_one(".c-abstract")
            )
            results.append(
                SearchResult(
                    title=self._clean(a.get_text()),
                    url=a["href"],
                    snippet=self._clean(snippet_el.get_text() if snippet_el else ""),
                    engine=self.name,
                    rank=len(results) + 1,
                )
            )
            if len(results) >= num:
                break
        return results

    def resolve_redirect(self, url: str) -> str:
        """把百度跳转链接解析成真实 URL"""
        if "baidu.com/link" not in url:
            return url
        log = get_logger()
        for method in ("HEAD", "GET"):
            try:
                if method == "HEAD":
                    resp = self.session.head(
                        url, timeout=self.timeout, allow_redirects=True,
                        headers={"Referer": "https://www.baidu.com/"},
                    )
                else:
                    resp = self.session.get(
                        url, timeout=self.timeout, allow_redirects=True, stream=True,
                        headers={"Referer": "https://www.baidu.com/"},
                    )
                    resp.close()
                if resp.url and not resp.url.startswith("https://www.baidu.com/link"):
                    log.debug("baidu redirect resolved via %s | %s -> %s",
                              method, url[:80], resp.url[:80])
                    return resp.url
            except Exception as exc:  # noqa: BLE001
                log.debug("baidu redirect %s failed: %s", method, exc)
        log.debug("baidu redirect UNRESOLVED | %s", url[:80])
        return url


class SogouEngine(BaseEngine):
    """搜狗：同样是跳转链接；高频请求会触发 antispider，需预热 cookie 并带重试"""

    name = "sogou"

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        super().__init__(timeout)
        self._warmed = False

    def _warm_up(self) -> None:
        """先访问首页拿基础 cookie（SUID 等），更像真实浏览器"""
        if self._warmed:
            return
        try:
            self.session.get(
                "https://www.sogou.com/", timeout=self.timeout,
                headers={"Referer": "https://www.sogou.com/"},
            )
        except Exception:  # noqa: BLE001 - 预热失败不致命
            pass
        self._warmed = True

    def _new_session(self) -> None:
        self.session.close()
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self._warmed = False

    def _request_serp(self, query: str):
        """请求 SERP；被 antispider 拦截（403/跳转拦截页）时换全新会话重试一次"""
        log = get_logger()
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                self._warm_up()
                resp = self._get(
                    "https://www.sogou.com/web",
                    params={"query": query},
                    headers={"Referer": "https://www.sogou.com/"},
                )
                if "antispider" not in resp.url:
                    return resp
                last_exc = RuntimeError(f"sogou antispider redirect: {resp.url[:80]}")
            except requests.HTTPError as exc:
                resp = exc.response
                if resp is None or "antispider" not in str(resp.url):
                    raise  # 普通错误直接抛给引擎层兜底
                last_exc = exc
            log.debug("sogou antispider (attempt %d), retry with fresh session", attempt + 1)
            self._new_session()
        raise last_exc or RuntimeError("sogou blocked")

    def _search(self, query: str, num: int) -> list[SearchResult]:
        # 注意：requests 会自动做 URL 编码，这里必须传原始 query；
        # 之前传 quote_plus(query) 造成双重编码，搜狗实际搜索的是 "%E5%A6%82..." 字面串
        # 不带 num 参数（浏览器也只取默认每页条数），降低机器人特征
        resp = self._request_serp(query)
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[SearchResult] = []
        for container in soup.select("div.rb, div.vrwrap"):
            a = container.select_one("h3 a[href]")
            if not a:
                continue
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            snippet_el = (
                container.select_one("div.str-text-info")
                or container.select_one("[class*='str_info']")
                or container.select_one("p.str_info")
                or container.select_one("p.space-txt")
                or container.select_one("p.content")
            )
            results.append(
                SearchResult(
                    title=self._clean(a.get_text()),
                    url=href,
                    snippet=self._clean(
                        snippet_el.get_text() if snippet_el else ""
                    ),
                    engine=self.name,
                    rank=len(results) + 1,
                )
            )
            if len(results) >= num:
                break
        return results

    def resolve_redirect(self, url: str) -> str:
        """把搜狗跳转链接解析成真实 URL（搜狗用 JS 跳转，需解析响应体）"""
        if "sogou.com/link" not in url:
            return url
        log = get_logger()
        try:
            resp = self.session.get(
                url, timeout=self.timeout, allow_redirects=True,
                headers={"Referer": "https://www.sogou.com/"},
            )
            # HTTP 跳转成功
            if resp.url and "sogou.com/link" not in resp.url:
                log.debug("sogou redirect HTTP | %s -> %s", url[:80], resp.url[:80])
                return resp.url
            # JS 跳转：window.location.replace("...")
            m = re.search(
                r'window\.location\.replace\(\s*["\']([^"\']+)["\']\s*\)', resp.text
            )
            if not m:
                # 兜底：<meta http-equiv="refresh" content="0;URL='...'">
                m = re.search(r"URL='([^']+)'", resp.text)
            if m:
                log.debug("sogou redirect JS-regex | %s -> %s", url[:80], m.group(1)[:80])
                return m.group(1)
            log.debug("sogou redirect UNRESOLVED (no JS regex match) | %s", url[:80])
        except Exception as exc:  # noqa: BLE001
            log.debug("sogou redirect failed: %s | %s", exc, url[:80])
        return url


class SoEngine(BaseEngine):
    """360 搜索：结果相关性好，跳转链接为 JS 跳转（同搜狗），data-mdurl 有时直接给真实 URL"""

    name = "so"

    def _search(self, query: str, num: int) -> list[SearchResult]:
        resp = self._get(
            "https://www.so.com/s",
            params={"q": query, "num": min(max(num * 2, 10), 20)},
            headers={"Referer": "https://www.so.com/"},
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[SearchResult] = []
        for li in soup.select("li.res-list"):
            a = li.select_one("h3 a")
            if not a:
                continue
            # data-mdurl/data-url 属性常直接携带真实 URL，可免一次跳转解析
            href = a.get("data-mdurl") or a.get("data-url") or a.get("href") or ""
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.so.com" + href
            snippet_el = li.select_one("p.res-desc") or li.select_one(
                "[class*='res-desc']"
            )
            results.append(
                SearchResult(
                    title=self._clean(a.get_text()),
                    url=href,
                    snippet=self._clean(snippet_el.get_text() if snippet_el else ""),
                    engine=self.name,
                    rank=len(results) + 1,
                )
            )
            if len(results) >= num:
                break
        return results

    def resolve_redirect(self, url: str) -> str:
        """把 360 跳转链接解析成真实 URL（JS 跳转，解析响应体）"""
        if "so.com/link" not in url:
            return url
        log = get_logger()
        try:
            resp = self.session.get(
                url, timeout=self.timeout,
                headers={"Referer": "https://www.so.com/"},
            )
            m = re.search(
                r'window\.location\.replace\(\s*["\']([^"\']+)["\']\s*\)', resp.text
            )
            if not m:
                # 兜底：<meta http-equiv="refresh" content="0;URL='...'">
                m = re.search(r"URL='([^']+)'", resp.text)
            if m:
                log.debug("so redirect resolved | %s -> %s", url[:80], m.group(1)[:80])
                return m.group(1)
            log.debug("so redirect UNRESOLVED | %s", url[:80])
        except Exception as exc:  # noqa: BLE001
            log.debug("so redirect failed: %s | %s", exc, url[:80])
        return url


# 引擎注册表：新增引擎在这里注册即可
ENGINES: dict[str, type[BaseEngine]] = {
    "bing": BingEngine,
    "baidu": BaiduEngine,
    "sogou": SogouEngine,
    "so": SoEngine,
}


def create_engines(names: list[str], timeout: int) -> list[BaseEngine]:
    engines = []
    for name in names:
        cls = ENGINES.get(name.lower())
        if cls is None:
            raise ValueError(f"未知引擎: {name}，可用: {', '.join(ENGINES)}")
        engines.append(cls(timeout=timeout))
    return engines
