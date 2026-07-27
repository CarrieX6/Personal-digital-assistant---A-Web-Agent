from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .models import ToolInfo


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ToolError(ValueError):
    """Raised when a tool receives invalid input."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc
        return tool.handler(arguments)

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(name=tool.name, description=tool.description)
            for tool in self._tools.values()
        ]

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]


def _require_text(arguments: dict[str, Any]) -> str:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("The tool requires a non-empty 'text' argument")
    return text.strip()


def text_stats(arguments: dict[str, Any]) -> dict[str, Any]:
    text = _require_text(arguments)
    latin_words = re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", text)
    chinese_characters = re.findall(r"[\u4e00-\u9fff]", text)
    return {
        "characters": len(text),
        "characters_without_spaces": len(re.sub(r"\s", "", text)),
        "chinese_characters": len(chinese_characters),
        "latin_words": len(latin_words),
        "lines": len(text.splitlines()) or 1,
    }


def extract_keywords(arguments: dict[str, Any]) -> dict[str, Any]:
    text = _require_text(arguments)
    requested_limit = arguments.get("limit", 5)
    limit = min(max(int(requested_limit), 1), 10)

    latin_tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
        if token.lower() not in {"the", "and", "with", "that", "this"}
    ]
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    tokens = latin_tokens + chinese_chunks
    ranked = [token for token, _ in Counter(tokens).most_common(limit)]

    return {"keywords": ranked or ["未提取到关键词"], "method": "frequency-demo"}


def current_time(_: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return {
        "iso": now.isoformat(timespec="seconds"),
        "display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "Asia/Shanghai",
    }


def list_capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "examples": [
            "分析这段文字：……",
            "提取关键词：……",
            "统计字数：……",
            "现在几点？",
            "查看我的个人资产",
        ]
    }


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "text_stats",
            "统计文本字符、中文字符、英文或数字词以及行数",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "需要统计的原始文本"}
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            text_stats,
        )
    )
    registry.register(
        ToolSpec(
            "extract_keywords",
            "用轻量规则从文本中提取关键词",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "需要分析的原始文本"},
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少个关键词",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            extract_keywords,
        )
    )
    registry.register(
        ToolSpec(
            "current_time",
            "获取 Asia/Shanghai 当前时间",
            {"type": "object", "properties": {}, "additionalProperties": False},
            current_time,
        )
    )
    registry.register(
        ToolSpec(
            "list_capabilities",
            "列出当前 Agent 能完成的示例任务",
            {"type": "object", "properties": {}, "additionalProperties": False},
            list_capabilities,
        )
    )
    return registry
