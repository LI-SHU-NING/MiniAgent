"""websearch —— 多引擎聚合搜索 + 网页正文提取 CLI 工具

Agent 调用入口:
    from websearch.api import search
    result = search("关键词", num=5)
"""
from .api import search
from .engines import ENGINES

__version__ = "0.1.0"
__all__ = ["search", "ENGINES", "__version__"]
