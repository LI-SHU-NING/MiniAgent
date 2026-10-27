"""记忆模块：
1. ConversationManager - 会话内短期记忆，超预算自动用 LLM 压缩旧消息
2. VectorMemory        - 跨会话长期记忆：优先走统一配置的 OpenAI 兼容
   /embeddings API，未配置或调用失败时回落内置本地向量器；JSON 本地存储。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import numpy as np

from .llm import LLMClient, split_think

# ---------------------------------------------------------------- 短期记忆


class ConversationManager:
    """维护一段消息历史；字符数超预算时把最老的一批压缩为摘要。"""

    KEEP_RECENT = 6  # 压缩时保留最近 N 条不参与摘要

    def __init__(self, llm: LLMClient, budget_chars: int = 24000):
        self.llm = llm
        self.budget = budget_chars
        self.messages: list[dict] = []
        self.summary = ""  # 累积摘要

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})

    def total_chars(self) -> int:
        return sum(len(str(m.get("content", ""))) for m in self.messages)

    def maybe_compress(self):
        if self.total_chars() <= self.budget:
            return
        cut = max(0, len(self.messages) - self.KEEP_RECENT)
        old = self.messages[:cut]
        if not old:
            return
        text = "\n".join(f"[{m['role']}] {str(m.get('content', ''))[:800]}" for m in old)
        resp = self.llm.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "请把以下对话历史压缩成一段要点摘要（保留任务目标、关键结论、"
                        "重要数据与未尽事项），500 字以内，直接输出摘要:\n\n" + text
                    ),
                }
            ],
            temperature=0.2,
        )
        new_summary, _ = split_think(resp.content or "")
        self.summary = (self.summary + "\n" + new_summary).strip()
        self.messages = self.messages[cut:]

    def get_context(self) -> list[dict]:
        """返回可注入 system 的记忆上下文。"""
        if self.summary:
            return [{"role": "system", "content": f"【此前对话摘要】\n{self.summary}"}]
        return []

    def clear(self):
        self.messages = []
        self.summary = ""


# ---------------------------------------------------------------- 长期记忆

_DOC_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "

# 内置本地向量器维度（无状态哈希，确定性，适配增量写入）
_LOCAL_DIM = 1024
_log = logging.getLogger(__name__)


def _tokenize(text: str) -> list[str]:
    """中英文混合分词（英文/数字按词，中文用 jieba），供内置向量器使用。"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    han = re.findall(r"[\u4e00-\u9fff]", text)
    if han:
        try:
            import jieba

            tokens.extend(t for t in jieba.cut(text) if t.strip())
        except Exception:  # noqa: BLE001
            tokens.extend(han)
            tokens.extend(han[i] + han[i + 1] for i in range(len(han) - 1))
    return tokens


class VectorMemory:
    """长期向量记忆，JSON 文件存储。

    嵌入优先走统一配置的 OpenAI 兼容接口 POST {base_url}/embeddings
    （环境变量 EMBED_API_BASE / EMBED_API_KEY / EMBED_MODEL 优先，其次构造参数）；
    未配置 embed_model 或调用失败时回落内置本地向量器（纯离线，不访问网络）。
    """

    def __init__(
        self,
        base_url: str,
        embed_model: str,
        path: str,
        top_k: int = 3,
        api_key: str = "",
        embed_base_url: str = "",
        embed_api_key: str = "",
        allow_local_fallback: bool = True,
    ):
        # 统一配置 + 环境变量覆盖（环境变量优先）
        self.base_url = (
            os.getenv("EMBED_API_BASE") or embed_base_url or base_url or ""
        ).rstrip("/")
        self.api_key = os.getenv("EMBED_API_KEY") or embed_api_key or api_key or ""
        self.embed_model = os.getenv("EMBED_MODEL") or embed_model or ""
        self.allow_local_fallback = allow_local_fallback
        self.path = Path(path)
        self.top_k = top_k
        self.entries: list[dict] = []  # {text, vector(list), ts, kind}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.entries = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False), encoding="utf-8"
        )

    def _embed(self, text: str, is_query: bool = False) -> list[float]:
        """返回文本向量：优先调用 API，未配置/失败时回落内置本地向量器。"""
        prefix = _QUERY_PREFIX if is_query else _DOC_PREFIX
        raw = prefix + text
        if self.embed_model and self.base_url:
            try:
                return self._embed_api(raw)
            except Exception as exc:  # noqa: BLE001
                if not self.allow_local_fallback:
                    raise
                _log.warning("记忆嵌入 API 调用失败，回落内置本地向量器: %s", exc)
        return self._embed_local(raw)

    def _embed_api(self, text: str) -> list[float]:
        """OpenAI 兼容嵌入接口：POST {base_url}/embeddings（Bearer 鉴权）。"""
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = httpx.post(
            f"{self.base_url}/embeddings",
            json={"model": self.embed_model, "input": text},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"][0]["embedding"]
        embs = data.get("embeddings") or [data.get("embedding")]
        return embs[0]

    def _embed_local(self, text: str) -> list[float]:
        """内置本地向量器：jieba 分词 + MD5 哈希特征，纯离线、确定性、无状态。"""
        vec = np.zeros(_LOCAL_DIM, dtype=np.float32)
        for tok in _tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % _LOCAL_DIM] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec.tolist()

    def add(self, text: str, kind: str = "fact") -> str:
        text = text.strip()
        if not text:
            return "空内容，未保存。"
        # 去重：完全相同文本不再存
        if any(e["text"] == text for e in self.entries):
            return "该记忆已存在。"
        vec = self._embed(text)
        self.entries.append({"text": text, "vector": vec, "ts": time.time(), "kind": kind})
        self._save()
        return f"已保存长期记忆: {text[:100]}"

    def search(self, query: str, top_k: int | None = None) -> list[str]:
        if not self.entries:
            return []
        k = top_k or self.top_k
        qv = np.array(self._embed(query, is_query=True), dtype=np.float32)
        best: list[tuple[float, str]] = []
        for e in self.entries:
            ev = np.array(e["vector"], dtype=np.float32)
            if ev.shape != qv.shape:  # 跨后端维度不一致的旧条目跳过
                continue
            denom = np.linalg.norm(qv) * np.linalg.norm(ev)
            score = float(np.dot(qv, ev) / denom) if denom else 0.0
            best.append((score, e["text"]))
        best.sort(reverse=True, key=lambda x: x[0])
        return [t for s, t in best[:k] if s > 0.25]  # 相似度阈值过滤噪声

    def recall_context(self, query: str) -> str:
        """为 Agent 启动召回相关记忆，拼为上下文文本。"""
        hits = self.search(query)
        if not hits:
            return ""
        return "相关长期记忆:\n" + "\n".join(f"- {h}" for h in hits)
