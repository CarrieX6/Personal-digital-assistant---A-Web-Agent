from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol, Sequence


class MemoryEmbeddingProvider(Protocol):
    """Local/provider-neutral contract for memory embeddings."""

    model_id: str
    dimension: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class HashingMemoryEmbeddingProvider:
    """Zero-download local embedding baseline with controlled semantic concepts.

    This is intentionally deterministic and inexpensive. It combines hashed
    lexical features with a small, auditable concept map and can later be
    replaced by a local Transformer provider through the same protocol.
    """

    dimension: int = 256
    model_id: str = "local-hash-semantic-v1"

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(str(text))

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(str(text)) for text in texts]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Backward-compatible alias for integrations using the v1 contract."""

        return self.embed_documents(texts)

    def close(self) -> None:
        return None

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, weight in _semantic_features(text):
            digest = hashlib.blake2b(
                feature.encode("utf-8"),
                digest_size=8,
                person=b"mem-embed-v1",
            ).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            return vector
        return [value / norm for value in vector]


_CONCEPT_GROUPS: dict[str, tuple[str, ...]] = {
    "preference": ("偏好", "喜欢", "倾向", "习惯", "希望", "优先", "默认"),
    "concise": ("简洁", "精炼", "简短", "短答", "少说", "不啰嗦"),
    "detailed": ("详细", "展开", "完整", "细致", "多解释"),
    "commute": ("通勤", "上班", "上下班", "出行", "交通方式"),
    "bicycle": ("自行车", "单车", "骑车", "脚踏车", "bike"),
    "transit": ("公交", "地铁", "公共交通", "巴士", "subway"),
    "location": ("住在", "居住", "地址", "位置", "所在地", "哪座城市"),
    "identity": ("名字", "姓名", "称呼", "我是谁", "个人资料"),
    "display": ("界面", "主题", "外观", "配色", "颜色"),
    "green": ("绿色", "翠绿", "墨绿", "green"),
    "workflow": ("流程", "步骤", "操作", "方法", "怎么处理"),
    "failure": ("失败", "错误", "报错", "出错", "异常", "故障", "问题"),
    "retry": ("重试", "再试", "再次执行", "重新执行", "复用", "回退"),
    "task": ("任务", "待办", "进度", "下一步", "未完成"),
}


def _semantic_features(value: str) -> list[tuple[str, float]]:
    lowered = re.sub(r"\s+", " ", value.casefold()).strip()
    if not lowered:
        return []
    features: list[tuple[str, float]] = []
    for token in re.findall(r"[a-z0-9][a-z0-9-]{1,}", lowered):
        features.append((f"word:{token}", 1.0))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) <= 8:
            features.append((f"chunk:{chunk}", 0.8))
        features.extend(
            (f"c2:{chunk[index:index + 2]}", 0.45)
            for index in range(len(chunk) - 1)
        )
        features.extend(
            (f"c3:{chunk[index:index + 3]}", 0.65)
            for index in range(len(chunk) - 2)
        )
    for concept, synonyms in _CONCEPT_GROUPS.items():
        if any(synonym in lowered for synonym in synonyms):
            features.append((f"concept:{concept}", 2.4))
    return features


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))
