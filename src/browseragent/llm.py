"""Small compatibility layer for gateways without strict structured outputs."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


def first_json_value(text: str) -> Any:
	"""Decode the first complete JSON object/array and ignore trailing chatter."""
	decoder = json.JSONDecoder()
	for index, char in enumerate(text):
		if char not in "{[":
			continue
		try:
			value, _ = decoder.raw_decode(text, index)
		except json.JSONDecodeError:
			continue
		return value
	raise ValueError("Model response contains no complete JSON value")


def parse_responses_sse(text: str) -> tuple[str, str, dict | None]:
	"""Parse gateways that return Responses API events as SSE unconditionally."""
	deltas: list[str] = []
	final_response = None
	for line in text.splitlines():
		if not line.startswith("data: "):
			continue
		try:
			event = json.loads(line[6:])
		except json.JSONDecodeError:
			continue
		if event.get("type") == "response.output_text.delta":
			deltas.append(event.get("delta", ""))
		if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
			final_response = event.get("response") or {}
	if final_response is None:
		raise ValueError("Responses SSE stream ended without a terminal event")
	status = str(final_response.get("status") or "")
	if status != "completed":
		raise ValueError(f"Responses API ended with status={status}: {final_response.get('error')}")
	return "".join(deltas), status, final_response.get("usage")


def build_gateway_chat_openai(*, wire_api="chat_completions", model_verbosity="medium", disable_response_storage=True, **kwargs):
	"""Create an OpenAI-compatible client with tolerant local structured parsing."""
	from browser_use.llm.exceptions import ModelProviderError
	from browser_use.llm.messages import ContentPartTextParam
	from browser_use.llm.openai.chat import ChatOpenAI
	from browser_use.llm.openai.responses_serializer import ResponsesAPIMessageSerializer
	from browser_use.llm.schema import SchemaOptimizer
	from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

	@dataclass
	class GatewayChatOpenAI(ChatOpenAI):
		wire_api: str = "chat_completions"
		model_verbosity: str = "medium"
		disable_response_storage: bool = True

		async def _invoke_raw(self, messages, **invoke_kwargs):
			if self.wire_api != "responses":
				return await super().ainvoke(messages, output_format=None, **invoke_kwargs)

			params = {
				"model": self.model,
				"input": ResponsesAPIMessageSerializer.serialize_messages(messages),
				"max_output_tokens": self.max_completion_tokens,
				"reasoning": {"effort": self.reasoning_effort},
				"text": {"verbosity": self.model_verbosity},
				"store": not self.disable_response_storage,
			}
			params = {key: value for key, value in params.items() if value is not None}
			try:
				response = await self.get_client().responses.create(**params)
			except Exception as exc:
				raise ModelProviderError(message=str(exc), model=self.name) from exc
			if isinstance(response, str):
				output_text, response_status, usage_data = parse_responses_sse(response)
				input_tokens = usage_data.get("input_tokens") if usage_data else None
				output_tokens = usage_data.get("output_tokens") if usage_data else None
				total_tokens = usage_data.get("total_tokens") if usage_data else None
				input_details = usage_data.get("input_tokens_details") if usage_data else None
			else:
				output_text = response.output_text or ""
				response_status = response.status
				input_tokens = response.usage.input_tokens if response.usage is not None else None
				output_tokens = response.usage.output_tokens if response.usage is not None else None
				total_tokens = response.usage.total_tokens if response.usage is not None else None
				input_details = response.usage.input_tokens_details if response.usage is not None else None
			usage = None
			if input_tokens is not None:
				usage = ChatInvokeUsage(
					prompt_tokens=input_tokens,
					prompt_cached_tokens=(
						input_details.get("cached_tokens") if isinstance(input_details, dict) else getattr(input_details, "cached_tokens", None)
					),
					prompt_cache_creation_tokens=None,
					prompt_image_tokens=None,
					completion_tokens=output_tokens,
					total_tokens=total_tokens,
				)
			return ChatInvokeCompletion(
				completion=output_text,
				usage=usage,
				stop_reason=response_status,
			)

		async def ainvoke(self, messages, output_format=None, **invoke_kwargs):
			if output_format is None:
				return await self._invoke_raw(messages, **invoke_kwargs)

			prepared = deepcopy(messages)
			schema = SchemaOptimizer.create_optimized_json_schema(
				output_format,
				remove_min_items=self.remove_min_items_from_schema,
				remove_defaults=self.remove_defaults_from_schema,
			)
			instruction = (
				"\nReturn exactly one JSON value matching this schema. Do not add markdown or text after it."
				f"\n<json_schema>\n{json.dumps(schema, ensure_ascii=False)}\n</json_schema>"
			)
			if prepared and prepared[0].role == "system":
				content = prepared[0].content
				if isinstance(content, str):
					prepared[0].content = content + instruction
				else:
					prepared[0].content = [*content, ContentPartTextParam(text=instruction)]

			raw = await self._invoke_raw(prepared, **invoke_kwargs)
			try:
				parsed = output_format.model_validate(first_json_value(raw.completion))
			except Exception as exc:
				raise ModelProviderError(message=f"Failed to parse gateway structured output: {exc}", model=self.name) from exc
			return ChatInvokeCompletion(
				completion=parsed,
				usage=raw.usage,
				stop_reason=raw.stop_reason,
				stop_details=raw.stop_details,
			)

	return GatewayChatOpenAI(
		**kwargs,
		wire_api=wire_api,
		model_verbosity=model_verbosity,
		disable_response_storage=disable_response_storage,
	)
