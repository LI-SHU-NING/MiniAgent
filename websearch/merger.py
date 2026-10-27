"""结果合并：URL 规范化去重 + 标题相似度合并 + 跨引擎加权排序 + query 相关性重排"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlsplit

# 常见跟踪参数，去重时忽略
TRACK_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "ie", "fr", "ua", "ns", "rsp", "rsv_dl", "spm", "share_token",
}

# query 覆盖度加分上限：略低于一次跨引擎命中（10 分），
# 让"单引擎高相关"能与"跨引擎弱相关"竞争，但跨引擎一致仍优先
COVERAGE_WEIGHT = 8.0

# 词典消歧页（"如何（汉语词语）_百度百科"这类）惩罚分：
# 搜索长 query 时这类页面几乎必然跑题
DICT_PAGE_PENALTY = 6.0

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RUN = re.compile(r"[A-Za-z0-9]+")
# 标题形如 "词条名（消歧后缀）_XX百科" 视为词典页
_DICT_TITLE = re.compile(r"（[^（）]{1,12}）[_-][^\s]{0,6}百科")
# 常见"解释词义"型站点后缀，配合覆盖度判断
_DICT_SUFFIX = re.compile(r"_汉语国学|_汉语词典|_百度汉语$")


def normalize_url(url: str) -> str:
    """URL 规范化：去掉 scheme/www/末尾斜杠/fragment/跟踪参数"""
    try:
        parts = urlsplit(url.strip())
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parts.path.rstrip("/")
        query_pairs = [
            f"{k}={v}" for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACK_PARAMS
        ]
        query_pairs.sort()
        query = "&".join(query_pairs)
        return f"{netloc}{path}?{query}" if query else netloc + path
    except ValueError:
        return url


def normalize_title(title: str) -> str:
    """标题归一化：去空白和常见标点，便于模糊比较"""
    return re.sub(r"[\s\u3000\-_|·：:【】\[\]（）()「」『』,，。.！!？?…]+", "", title).lower()


def _titles_similar(t1: str, t2: str) -> bool:
    a, b = normalize_title(t1), normalize_title(t2)
    if not a or not b:
        return False
    if a == b:
        return True
    # 短标题（如文章标题）用较高的阈值，避免误合并
    return SequenceMatcher(None, a, b).ratio() >= 0.88


def query_units(query: str) -> list[str]:
    """把 query 拆成可匹配的语义单元：
    - 中文串 → 相邻二元组（"颈椎酸痛" → 颈椎/椎酸/酸痛），无需分词器即可近似词覆盖
    - 英文/数字串 → 整词（小写）
    """
    units: list[str] = []
    for m in _CJK_RUN.finditer(query):
        seg = m.group(0)
        if len(seg) == 1:
            units.append(seg)
        else:
            units.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    for m in _LATIN_RUN.finditer(query):
        units.append(m.group(0).lower())
    return units


def query_coverage(query: str, *texts: str) -> float:
    """query 语义单元在给定文本（标题+摘要）中的覆盖率，0~1"""
    units = query_units(query)
    if not units:
        return 0.0
    haystack = " ".join(t or "" for t in texts).lower()
    hit = sum(1 for u in units if u in haystack)
    return hit / len(units)


def _dict_page_penalty(title: str) -> float:
    """词典/消歧页惩罚："如何（汉语词语）_百度百科" 这类标题对长 query 几乎必然跑题"""
    if _DICT_TITLE.search(title) or _DICT_SUFFIX.search(title):
        return DICT_PAGE_PENALTY
    return 0.0


def merge_results(all_results: list, query: str = "") -> list[dict]:
    """合并多引擎结果。

    输入: SearchResult 列表（可来自多个引擎）+ 原 query（用于相关性重排）
    输出: 按 score 降序的 dict 列表
          {title, url, snippet, engines, ranks, score, coverage, url_key}
    """
    merged: list[dict] = []

    for r in sorted(all_results, key=lambda x: x.rank):
        url_key = normalize_url(r.url)
        target = None
        for m in merged:
            if m["url_key"] == url_key or _titles_similar(m["title"], r.title):
                target = m
                break

        if target is None:
            merged.append(
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "engines": [r.engine],
                    "ranks": {r.engine: r.rank},
                    "score": 0.0,
                    "url_key": url_key,
                }
            )
        else:
            # 保留信息更完整的字段
            if len(r.snippet) > len(target["snippet"]):
                target["snippet"] = r.snippet
            if r.engine not in target["engines"]:
                target["engines"].append(r.engine)
            target["ranks"][r.engine] = r.rank

    # 打分：跨引擎命中是最高权重（不依赖大模型的质量提升核心）
    # score = 命中引擎数 * 10
    #       + sum(各引擎: 1/rank，越靠前分越高)
    #       + query 覆盖度 * 8        —— 标题+摘要覆盖 query 语义单元的比例
    #       - 词典消歧页惩罚          —— "如何（汉语词语）_百度百科" 这类跑题页
    for m in merged:
        cross = len(m["engines"]) * 10.0
        rank_bonus = sum(1.0 / rank for rank in m["ranks"].values())
        coverage = query_coverage(query, m["title"], m["snippet"]) if query else 0.0
        penalty = _dict_page_penalty(m["title"])
        m["coverage"] = round(coverage, 3)
        m["score"] = round(cross + rank_bonus + coverage * COVERAGE_WEIGHT - penalty, 3)

    merged.sort(key=lambda m: m["score"], reverse=True)
    return merged
