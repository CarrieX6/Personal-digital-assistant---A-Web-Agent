from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import httpx

from .agent import DemoPlanner, PlanningResult, Planner, ToolObservation
from .models import ToolCall


SYSTEM_PROMPT = """你是 Agent Lab 的工具调用规划器。
你可以调用系统提供的工具来完成用户任务。
规则：
1. 只调用工具列表中真实存在的工具。
2. 工具参数必须严格符合 JSON Schema，不要编造缺失数据。
3. 一个任务可以调用多个互补工具。
4. 工具执行结果返回后，用简洁中文回答用户。
5. 如果现有工具无法完成任务，直接说明能力边界，不要伪造结果。
6. 如果系统提供本地图片附件 ID，且用户要求生成空间照片，调用
   create_spatial_scene 并原样传入 source_image_id。模型不可查看附件原图，
   不要推测图片内容。
"""


class LLMError(RuntimeError):
    """A safe, user-facing error raised by the configured LLM provider."""


def _content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(pieces).strip()
    return ""


class OpenAICompatiblePlanner:
    """Tool-calling planner for providers exposing Chat Completions semantics."""

    is_llm = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.chat_completions_url = (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else f"{self.base_url}/chat/completions"
        )
        self.model_name = model
        self.mode = f"llm:{model}"
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def plan(
        self, message: str, tool_schemas: list[dict[str, Any]]
    ) -> PlanningResult:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]
        assistant_message = self._chat(
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
        )
        tool_calls = self._parse_tool_calls(assistant_message.get("tool_calls", []))
        return PlanningResult(
            tool_calls=tool_calls,
            direct_answer=_content_as_text(assistant_message.get("content")),
            provider_context={
                "messages": messages,
                "assistant_message": assistant_message,
            },
        )

    def compose_answer(
        self,
        _: str,
        plan: PlanningResult,
        observations: list[ToolObservation],
    ) -> str:
        if not observations:
            return plan.direct_answer or "当前工具无法完成这个任务。"

        if not plan.provider_context:
            raise LLMError("模型调用上下文缺失，请重试。")

        messages = list(plan.provider_context["messages"])
        messages.append(plan.provider_context["assistant_message"])
        for observation in observations:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": observation.call.call_id,
                    "content": json.dumps(
                        observation.output,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )

        final_message = self._chat(messages=messages)
        answer = _content_as_text(final_message.get("content"))
        if not answer:
            raise LLMError("模型没有返回可读的最终回答。")
        return answer

    def _chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        try:
            response = self.client.post(
                self.chat_completions_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"模型服务返回错误状态 {exc.response.status_code}。"
            ) from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("无法解析模型服务响应，请检查接口地址和模型配置。") from exc

        if not isinstance(message, dict):
            raise LLMError("模型响应格式不正确。")
        return {
            key: message[key]
            for key in ("role", "content", "tool_calls")
            if key in message
        }

    @staticmethod
    def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
        if not isinstance(raw_calls, list):
            raise LLMError("模型返回的 tool_calls 不是列表。")

        parsed: list[ToolCall] = []
        for raw_call in raw_calls:
            try:
                function = raw_call["function"]
                raw_arguments = function.get("arguments", "{}")
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be an object")
                parsed.append(
                    ToolCall(
                        call_id=raw_call.get("id") or f"call_{uuid4().hex}",
                        name=function["name"],
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise LLMError("模型返回了无效的工具调用参数。") from exc
        return parsed


def build_planner_from_env(
    client: httpx.Client | None = None,
) -> Planner:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()

    if not api_key or not model:
        return DemoPlanner()

    if not base_url:
        base_url = "https://api.openai.com/v1"

    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    return OpenAICompatiblePlanner(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout,
        client=client,
    )
