import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

from browseragent.llm import build_gateway_chat_openai, first_json_value, parse_responses_sse


class GatewayJsonTests(unittest.TestCase):
	def test_ignores_text_after_complete_json(self):
		self.assertEqual(first_json_value('{"action": [{"wait": {"seconds": 1}}]} trailing'), {"action": [{"wait": {"seconds": 1}}]})

	def test_handles_markdown_prefix_and_braces_inside_strings(self):
		text = '```json\n{"thinking": "use {carefully}", "action": []}\n``` extra'
		self.assertEqual(first_json_value(text), {"thinking": "use {carefully}", "action": []})

	def test_rejects_incomplete_json(self):
		with self.assertRaises(ValueError):
			first_json_value('{"action": [')

	def test_parses_unconditionally_streamed_responses_api(self):
		stream = "\n".join(
			[
				'event: response.output_text.delta',
				'data: {"type":"response.output_text.delta","delta":"O"}',
				'data: {"type":"response.output_text.delta","delta":"K"}',
				'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}',
			]
		)
		output, status, usage = parse_responses_sse(stream)
		self.assertEqual((output, status), ("OK", "completed"))
		self.assertEqual(usage["total_tokens"], 3)

	def test_gateway_returns_validated_model_despite_trailing_text(self):
		from browser_use.llm.messages import SystemMessage
		from browser_use.llm.openai.chat import ChatOpenAI
		from browser_use.llm.views import ChatInvokeCompletion

		class Output(BaseModel):
			value: int

		async def raw_response(*args, **kwargs):
			return ChatInvokeCompletion(completion='{"value": 7} trailing', usage=None, stop_reason="stop")

		llm = build_gateway_chat_openai(model="gpt-5.4", api_key="test", base_url="https://example.com/v1")
		with patch.object(ChatOpenAI, "ainvoke", new=raw_response):
			result = asyncio.run(llm.ainvoke([SystemMessage(content="Return data")], output_format=Output))

		self.assertEqual(result.completion, Output(value=7))

	def test_responses_wire_api_uses_reasoning_verbosity_and_no_storage(self):
		from browser_use.llm.messages import SystemMessage

		calls = []

		class Responses:
			async def create(self, **kwargs):
				calls.append(kwargs)
				return SimpleNamespace(output_text="ok", usage=None, status="completed")

		llm = build_gateway_chat_openai(
			model="gpt-5.4",
			api_key="test",
			base_url="https://example.com/codex/v1",
			reasoning_effort="xhigh",
			wire_api="responses",
			model_verbosity="high",
			disable_response_storage=True,
		)
		with patch.object(llm, "get_client", return_value=SimpleNamespace(responses=Responses())):
			result = asyncio.run(llm.ainvoke([SystemMessage(content="Hello")]))

		self.assertEqual(result.completion, "ok")
		self.assertEqual(calls[0]["reasoning"], {"effort": "xhigh"})
		self.assertEqual(calls[0]["text"], {"verbosity": "high"})
		self.assertFalse(calls[0]["store"])


if __name__ == "__main__":
	unittest.main()
