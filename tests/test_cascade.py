import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from browseragent.cascade import (
	CASCADE_SCRIPT,
	TRY_SYNTHETIC_OPEN_SCRIPT,
	VERIFY_SELECTION_SCRIPT,
	CascadingSelectParams,
	select_cascade,
)


class CascadeTests(unittest.TestCase):
	def test_path_requires_at_least_two_nonempty_levels(self):
		with self.assertRaises(ValueError):
			CascadingSelectParams(index=1, path=["北京"])
		with self.assertRaises(ValueError):
			CascadingSelectParams(index=1, path=["北京", " "])

	def test_selection_uses_one_element_evaluation_with_complete_path(self):
		node = SimpleNamespace(backend_node_id=17, session_id="session")

		class Session:
			async def get_element_by_index(self, index):
				return node if index == 3 else None

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				self.backend_node_id = backend_node_id

			async def evaluate(self, script, path):
				self.path = path
				if script == TRY_SYNTHETIC_OPEN_SCRIPT:
					return json.dumps({"opened": True, "visible_options": ["北京"]})
				if script == VERIFY_SELECTION_SCRIPT:
					return json.dumps({"persisted": True, "stable": True, "first": path, "second": path})
				self.assert_script = script == CASCADE_SCRIPT
				return json.dumps({"success": True, "mode": "custom", "selected": path, "committed": True})

		params = CascadingSelectParams(index=3, path=["北京", "西城区"])
		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(select_cascade(Session(), params))

		self.assertTrue(result.success)
		self.assertTrue(result.committed)
		self.assertEqual(result.selected, ["北京", "西城区"])

	def test_unconfirmed_custom_selection_is_not_success(self):
		node = SimpleNamespace(backend_node_id=17, session_id="session")

		class Session:
			async def get_element_by_index(self, index):
				return node

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				pass

			async def evaluate(self, script, path):
				return json.dumps(
					{"success": False, "mode": "custom", "selected": path, "committed": False, "error": "popup remained open"}
				)

		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(select_cascade(Session(), CascadingSelectParams(index=3, path=["北京", "西城区"])))

		self.assertFalse(result.success)
		self.assertFalse(result.committed)
		self.assertIn("open", result.error)

	def test_missing_starting_control_stops_safely(self):
		class Session:
			async def get_element_by_index(self, index):
				return None

		result = asyncio.run(select_cascade(Session(), CascadingSelectParams(index=9, path=["河北省", "石家庄市"])))
		self.assertFalse(result.success)
		self.assertIn("unavailable", result.error)


if __name__ == "__main__":
	unittest.main()
