"""命令行入口

用法:
        python -m websearch "关键词" [-n 5] [--json] [--no-content]
                               [--engine bing,baidu,sogou,so]
                               [--timeout 15] [--max-chars 3000]

Agent 子进程调用推荐:
    python -m websearch "关键词" --json
输出为稳定的 UTF-8 JSON，直接解析即可。
"""
from __future__ import annotations

import argparse
import json
import sys

from .api import search
from .engines import DEFAULT_TIMEOUT, ENGINES


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="websearch",
        description="聚合搜索 CLI 工具：多引擎搜索 + 网页正文提取（无大模型）",
    )
    p.add_argument("query", help="搜索关键词")
    p.add_argument("-n", "--num", type=int, default=5, help="返回结果条数（默认 5）")
    p.add_argument("--json", action="store_true", help="输出 JSON（Agent 调用推荐）")
    p.add_argument("--no-content", action="store_true", help="不抓取网页正文，只返回搜索结果")
    p.add_argument(
        "--engine",
        default="bing,baidu,sogou,so",
        help=f"引擎列表，逗号分隔（默认全部）。可用: {','.join(ENGINES)}",
    )
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单请求超时秒数")
    p.add_argument("--max-chars", type=int, default=3000, help="单条正文最大字符数")
    return p


def _print_human(result: dict, max_content_chars: int) -> None:
    print(f"关键词: {result['query']}")
    print(f"引擎:   {', '.join(result['engines_used'])}  耗时 {result['elapsed_seconds']}s")
    if result.get("engine_errors"):
        for eng, err in result["engine_errors"].items():
            print(f"警告: 引擎 {eng} 出错 -> {err}")
    print(f"结果: {result['num_results']} 条")
    print("=" * 70)
    for i, r in enumerate(result["results"], 1):
        print(f"[{i}] {r['title']}")
        print(f"    {r['url']}")
        print(f"    来源: {','.join(r['engines'])} | 得分: {r['score']}"
              f" | 覆盖: {r.get('coverage', 0):.2f}"
              f" | 提取: {r['content_method']} | 正文长: {len(r['content'])}")
        if r["content"]:
            print(f"    正文:")
            text = r["content"]
            for line in text.splitlines():
                print(f"      {line}")
        elif r["snippet"]:
            print(f"    摘要: {r['snippet']}")
        else:
            print("    (无正文/无摘要)")
        print("-" * 70)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Windows 控制台中文输出保护
    try:
        if getattr(sys.stdout, "encoding", "").lower().replace("-", "") != "utf8":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    result = search(
        query=args.query,
        num=args.num,
        engines=[e.strip() for e in args.engine.split(",") if e.strip()],
        fetch_content=not args.no_content,
        timeout=args.timeout,
        max_content_chars=args.max_chars,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result, args.max_chars)

    return 0 if result.get("results") else 1


if __name__ == "__main__":
    raise SystemExit(main())
