from __future__ import annotations

import re
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from .models import CapabilityInfo, ToolInfo


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolExecutionContext:
    owner_id: str = "local"
    thread_id: str = "local:default"
    channel: str = "web"
    project_id: str | None = None
    idempotency_key: str | None = None


_CURRENT_TOOL_CONTEXT: ContextVar[ToolExecutionContext] = ContextVar(
    "tool_execution_context",
    default=ToolExecutionContext(),
)


def current_tool_context() -> ToolExecutionContext:
    return _CURRENT_TOOL_CONTEXT.get()


class ToolError(ValueError):
    """Raised when a tool receives invalid input."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk_level: Literal[
        "read", "local_write", "external_write", "destructive"
    ] = "read"
    requires_approval: bool = False
    idempotent: bool = True
    capability: CapabilityInfo | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc
        token = _CURRENT_TOOL_CONTEXT.set(context or ToolExecutionContext())
        try:
            return tool.handler(arguments)
        finally:
            _CURRENT_TOOL_CONTEXT.reset(token)

    def validate_call(self, name: str, arguments: dict[str, Any]) -> None:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc
        _validate_object(arguments, tool.parameters, path=name)

    def get_spec(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=tool.name,
                description=tool.description,
                risk_level=tool.risk_level,
                requires_approval=tool.requires_approval,
            )
            for tool in self._tools.values()
        ]

    def list_capabilities(self) -> list[CapabilityInfo]:
        return [
            tool.capability
            for tool in self._tools.values()
            if tool.capability is not None
        ]

    def openai_schemas(
        self,
        names: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        selected_names = set(names) if names is not None else None
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
            if selected_names is None or tool.name in selected_names
        ]


def _validate_object(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> None:
    if not isinstance(value, dict):
        raise ToolError(f"{path} arguments must be an object")

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for key in required:
        if key not in value:
            raise ToolError(f"{path} requires argument '{key}'")

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ToolError(
                f"{path} received unknown argument '{unknown[0]}'"
            )

    for key, item in value.items():
        item_schema = properties.get(key)
        if isinstance(item_schema, dict):
            _validate_schema_value(
                item,
                item_schema,
                path=f"{path}.{key}",
            )


def _validate_schema_value(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> None:
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }.get(expected, True)
    if not valid:
        raise ToolError(f"{path} must be {expected}")

    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{path} is not an allowed value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{path} exceeds the maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolError(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError(f"{path} is too long")
    if expected == "object":
        _validate_object(value, schema, path=path)
    if expected == "array" and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema_value(
                item,
                schema["items"],
                path=f"{path}[{index}]",
            )


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
    # China Standard Time has used UTC+08:00 year-round since 1991. A fixed
    # offset keeps this local current-time tool independent of the optional
    # system/IANA tzdata package, which is commonly absent on Windows.
    shanghai_timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.now(shanghai_timezone)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "Asia/Shanghai",
    }


def list_capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "examples": [
            "发送一张图片，生成可拖动视角的空间照片",
            "分析这段文字：……",
            "提取关键词：……",
            "统计字数：……",
            "现在几点？",
            "查看我的个人资产",
            "生成 Flux-GS 3D 场景：dataset_id=demo-scene（需要审批）",
            "记住：我偏好在本机处理私人图片",
            "查看我的记忆",
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
