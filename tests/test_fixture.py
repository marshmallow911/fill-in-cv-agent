import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from browseragent.fixture import CAPTURE_FORM_SCRIPT, capture_form_fixture


class FixtureCaptureTests(unittest.TestCase):
	def test_capture_never_reads_browser_secrets_and_redacts_form_values(self):
		self.assertNotIn("document.cookie", CAPTURE_FORM_SCRIPT)
		self.assertNotIn("localStorage", CAPTURE_FORM_SCRIPT)
		self.assertNotIn("sessionStorage", CAPTURE_FORM_SCRIPT)
		self.assertIn("<redacted>", CAPTURE_FORM_SCRIPT)
		self.assertIn("script, noscript, iframe", CAPTURE_FORM_SCRIPT)

	def test_writes_offline_html_and_structured_json(self):
		payload = {
			"title": "申请表",
			"url": "https://jobs.example.com/form?id=1",
			"viewport": {"width": 1200, "height": 800},
			"controls": [{"id": "native-1", "kind": "text", "label": "姓名", "value": "<redacted>"}],
			"custom_ids": [],
			"html": "<!doctype html><html><body>fixture</body></html>",
		}

		class Page:
			async def evaluate(self, script, *args):
				return json.dumps(payload, ensure_ascii=False)

		class Session:
			async def must_get_current_page(self):
				return Page()

		with tempfile.TemporaryDirectory() as temporary:
			output = asyncio.run(capture_form_fixture(Session(), Path(temporary)))
			self.assertTrue((output / "page.html").is_file())
			data = json.loads((output / "form.json").read_text())
			self.assertEqual(data["controls"][0]["value"], "<redacted>")
			self.assertEqual(data["dropdown_probes"], {})


if __name__ == "__main__":
	unittest.main()
