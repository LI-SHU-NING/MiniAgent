"""配置加载：config.yaml -> 强类型 Config 对象"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    """统一云端 API 模型（OpenAI 兼容）。

    规划 / 执行 / 视觉 / 记忆向量嵌入四条链路全部复用这里的
    base_url + api_key，是模型调用的唯一配置来源（不再有本地端口配置）。
    """
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    planner_model: str = "deepseek-flash"
    executor_model: str = "deepseek-flash"
    # 视觉模型：浏览器截图等图片交给它分析（留空则复用 executor_model）
    vision_model: str = "deepseek-v4-pro"
    # 记忆向量嵌入模型：调用 {embed_base_url or base_url}/embeddings（OpenAI 兼容）。
    # 留空 = 使用内置本地向量器（纯离线、无需任何外部服务）。
    embed_model: str = ""
    # 可选：独立的嵌入服务地址 / 密钥（留空则复用上面的 base_url / api_key）
    embed_base_url: str = ""
    embed_api_key: str = ""


class AgentConfig(BaseModel):
    max_steps_per_task: int = 15
    max_replans: int = 2
    tool_result_max_chars: int = 6000
    history_budget_chars: int = 24000
    tool_timeout: int = 120
    # 远程/慢速模型保护：单次 LLM 请求超时（秒）
    request_timeout: int = 300
    # qwen3 系列关闭思考模式（/no_think soft switch），显著降低响应时长
    no_think: bool = True
    # 单次回复 token 上限（防思考/输出失控）
    executor_max_tokens: int = 2048
    planner_max_tokens: int = 1024


class BrowserConfig(BaseModel):
    headless: bool = True
    timeout: int = 30
    viewport: list = Field(default_factory=lambda: [1280, 800])
    user_agent: Optional[str] = None


class SecurityConfig(BaseModel):
    workspace: str = "."
    confirm_shell: bool = True
    dangerous_patterns: list = Field(default_factory=list)


class MemoryConfig(BaseModel):
    enabled: bool = True
    path: str = "data/memory.json"
    top_k: int = 3
    # 嵌入 API 调用失败时是否回落内置本地向量器（纯离线，无需外部服务）
    allow_local_fallback: bool = True


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class LoggingConfig(BaseModel):
    file: str = ""


class Config(BaseModel):
    # 模型调用统一走下面的 api 段
    api: ApiConfig = Field(default_factory=ApiConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path | None = None) -> tuple[Config, Path]:
    """加载配置，返回 (Config, 项目根目录)。路径均为相对项目根。"""
    if path is None:
        # 默认从本文件向上找 config.yaml
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.yaml"
    else:
        cfg_path = Path(path).resolve()
        root = cfg_path.parent

    data: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    cfg = Config(**data)
    # workspace 解析为绝对路径
    ws = Path(cfg.security.workspace)
    if not ws.is_absolute():
        ws = root / ws
    cfg.security.workspace = str(ws.resolve())
    # 日志文件解析为绝对路径
    if cfg.logging.file:
        lf = Path(cfg.logging.file)
        if not lf.is_absolute():
            lf = root / lf
        cfg.logging.file = str(lf)
    return cfg, root
