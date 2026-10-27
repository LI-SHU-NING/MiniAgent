"""日志模块

默认日志位置: <项目根>/logs/websearch.log
- 每次搜索都写入一条 INFO 级别记录，包含：query, engines, num_results, elapsed, 每条结果的引擎/得分/提取方式
- 按大小轮转：单文件 5MB，最多保留 3 份
- 仅写文件，不向终端输出（避免污染 CLI 输出）

Debug 模式（环境变量 WEBSEARCH_DEBUG=1 启用）：
- 日志级别降到 DEBUG
- 同时写一份到 logs/debug.log（独立文件，便于排查）
- DEBUG 日志会记录：HTTP 请求耗时、跳转解析尝试次数、抓取失败原因、合并前/后数量等细节
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "websearch.log"
_DEBUG_LOG_FILE = _LOG_DIR / "debug.log"
_LOGGER_NAME = "websearch"

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.propagate = False  # 避免重复输出到 root

    debug_enabled = os.environ.get("WEBSEARCH_DEBUG", "").strip() in ("1", "true", "yes", "on")
    logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)

    if not logger.handlers:
        # 主日志：INFO 及以上，按大小轮转
        main_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        main_handler.setLevel(logging.INFO)
        main_fmt = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        main_handler.setFormatter(main_fmt)
        logger.addHandler(main_handler)

        # Debug 日志：仅 debug 模式启用，写到独立文件
        if debug_enabled:
            debug_handler = RotatingFileHandler(
                _DEBUG_LOG_FILE,
                maxBytes=10 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            debug_handler.setLevel(logging.DEBUG)
            debug_fmt = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            debug_handler.setFormatter(debug_fmt)
            logger.addHandler(debug_handler)
            logger.debug("debug mode enabled (WEBSEARCH_DEBUG=1)")

    _logger = logger
    return logger


def log_file_path() -> Path:
    """供 CLI / 文档展示日志文件位置"""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_FILE


def debug_log_path() -> Path:
    """debug 日志文件路径"""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _DEBUG_LOG_FILE


def is_debug() -> bool:
    """当前是否启用 debug 日志"""
    return os.environ.get("WEBSEARCH_DEBUG", "").strip() in ("1", "true", "yes", "on")
