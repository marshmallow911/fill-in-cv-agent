import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from browseragent.cascade import (
	CASCADE_SCRIPT,
	CLEAR_TRIGGER_SCRIPT,
	FAST_CASCADE_SCRIPT,
	FAST_SYNTHETIC_OPEN_SCRIPT,
	FAST_VERIFY_SELECTION_SCRIPT,
	MARK_TRIGGER_SCRIPT,
	TRY_SYNTHETIC_OPEN_SCRIPT,
	VERIFY_SELECTION_SCRIPT,
	_trusted_click_selector,
	select_fast_cascade_path,
	select_cascade_path,
)
from browseragent.dropdown import INSPECT_DROPDOWN_SCRIPT, InspectDropdownParams, SmartSelectParams, inspect_dropdown, smart_select


class DropdownInspectionTests(unittest.TestCase):
	def test_custom_selector_opens_with_real_cdp_click_before_dom_selection(self):
		node = SimpleNamespace(backend_node_id=21, session_id="session")

		class Trigger:
			def __init__(self):
				self.clicks = 0

			async def click(self):
				self.clicks += 1

		trigger = Trigger()

		class Page:
			async def get_elements_by_css_selector(self, selector):
				return [trigger]

		class Session:
			async def get_element_by_index(self, index):
				return node

			async def must_get_current_page(self):
				return Page()

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				pass

			async def evaluate(self, script, *args):
				if script == TRY_SYNTHETIC_OPEN_SCRIPT:
					return json.dumps({"opened": False, "visible_options": []})
				if script == MARK_TRIGGER_SCRIPT:
					return json.dumps({"tagged": True, "tag": "div", "role": "button", "classes": ""})
				if script == CLEAR_TRIGGER_SCRIPT:
					return json.dumps(None)
				if script == VERIFY_SELECTION_SCRIPT:
					return json.dumps({"persisted": True, "stable": True, "first": args[0], "second": args[0]})
				return json.dumps({"success": True, "mode": "custom", "selected": args[0], "committed": True})

		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(select_cascade_path(Session(), 4, ["男"]))

		self.assertTrue(result.success)
		self.assertEqual(result.open_method, "cdp-real-click")
		self.assertEqual(trigger.clicks, 1)

	def test_custom_selector_skips_cdp_when_dom_open_succeeds(self):
		node = SimpleNamespace(backend_node_id=21, session_id="session")

		class Session:
			async def get_element_by_index(self, index):
				return node

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				pass

			async def evaluate(self, script, *args):
				if script == TRY_SYNTHETIC_OPEN_SCRIPT:
					return json.dumps({"opened": True, "visible_options": ["男", "女"]})
				if script == CASCADE_SCRIPT:
					return json.dumps({"success": True, "mode": "custom", "selected": ["男"], "committed": True})
				if script == VERIFY_SELECTION_SCRIPT:
					return json.dumps({"persisted": True, "stable": True, "first": args[0], "second": args[0]})
				raise AssertionError("CDP marker/cleanup should not run on the direct path")

		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(select_cascade_path(Session(), 4, ["男"]))

		self.assertTrue(result.success)
		self.assertEqual(result.open_method, "synthetic-direct")

	def test_custom_selector_rejects_value_that_reverts_after_blur(self):
		node = SimpleNamespace(backend_node_id=21, session_id="session")

		class Session:
			async def get_element_by_index(self, index):
				return node

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				pass

			async def evaluate(self, script, *args):
				if script == TRY_SYNTHETIC_OPEN_SCRIPT:
					return json.dumps({"opened": True, "visible_options": ["示例公司"]})
				if script == CASCADE_SCRIPT:
					return json.dumps({
						"success": True, "mode": "custom", "selected": ["示例公司"], "committed": True,
					})
				if script == VERIFY_SELECTION_SCRIPT:
					return json.dumps({"persisted": False, "stable": True, "first": "", "second": ""})
				raise AssertionError("unexpected script")

		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(select_cascade_path(Session(), 4, ["示例公司"]))

		self.assertFalse(result.success)
		self.assertFalse(result.committed)
		self.assertEqual(result.verification, "reverted_after_blur")
		self.assertIn("actual=(empty)", result.error)

	def test_fast_selector_retries_exact_option_with_trusted_click(self):
		class Page:
			async def evaluate(self, script, *args):
				if script == FAST_SYNTHETIC_OPEN_SCRIPT:
					return json.dumps({"opened": True, "visible_options": ["居民身份证"]})
				if script == FAST_CASCADE_SCRIPT:
					return json.dumps({
						"success": False, "mode": "custom", "selected": ["居民身份证"],
						"committed": False, "error": "selection could not be verified",
					})
				if script == FAST_VERIFY_SELECTION_SCRIPT:
					return json.dumps({
						"persisted": True, "stable": True,
						"first": "居民身份证", "second": "居民身份证",
					})
				if script == CLEAR_TRIGGER_SCRIPT:
					return json.dumps(None)
				raise AssertionError("unexpected script")

		async def trusted_option(*args, **kwargs):
			return True, ["居民身份证", "护照"], ""

		with patch("browseragent.cascade._trusted_option_click", new=trusted_option):
			result = asyncio.run(select_fast_cascade_path(Page(), "f1", ["居民身份证"]))

		self.assertTrue(result.success)
		self.assertTrue(result.committed)
		self.assertEqual(result.open_method, "cdp-option-click")
		self.assertEqual(result.verification, "stable_after_blur")

	def test_fast_selector_reopens_with_trusted_trigger_when_synthetic_menu_is_empty(self):
		class Trigger:
			def __init__(self):
				self.clicks = 0

			async def click(self):
				self.clicks += 1

		trigger = Trigger()

		class Page:
			async def evaluate(self, script, *args):
				if script == FAST_SYNTHETIC_OPEN_SCRIPT:
					return json.dumps({"opened": True, "visible_options": []})
				if script == FAST_CASCADE_SCRIPT:
					return json.dumps({
						"success": False, "mode": "custom", "selected": [],
						"committed": False, "error": "visible option not found",
					})
				if script == FAST_VERIFY_SELECTION_SCRIPT:
					return json.dumps({"persisted": True, "stable": True, "first": "一作", "second": "一作"})
				if script == CLEAR_TRIGGER_SCRIPT:
					return json.dumps(None)
				raise AssertionError("unexpected script")

			async def get_elements_by_css_selector(self, selector):
				return [trigger]

		option_attempts = 0

		async def trusted_option(*args, **kwargs):
			nonlocal option_attempts
			option_attempts += 1
			if option_attempts == 1:
				return False, [], "popup unavailable"
			return True, ["一作", "二作", "三作"], ""

		with patch("browseragent.cascade._trusted_option_click", new=trusted_option):
			result = asyncio.run(select_fast_cascade_path(Page(), "f1", ["一作"]))

		self.assertTrue(result.success)
		self.assertEqual(trigger.clicks, 1)
		self.assertEqual(option_attempts, 2)
		self.assertEqual(result.open_method, "cdp-option-click")

	def test_trusted_click_uses_live_page_mouse_coordinates(self):
		class Mouse:
			def __init__(self):
				self.actions = []

			async def move(self, x, y):
				self.actions.append(("move", x, y))

			async def click(self, x, y):
				self.actions.append(("click", x, y))

		class Page:
			mouse = None

			def __init__(self):
				self.pointer = Mouse()

			async def evaluate(self, script, selector):
				return json.dumps({"found": True, "x": 101.4, "y": 202.6})

			@property
			def mouse(self):
				async def value():
					return self.pointer
				return value()

		page = Page()
		clicked = asyncio.run(_trusted_click_selector(page, "[role=button]"))

		self.assertTrue(clicked)
		self.assertEqual(page.pointer.actions, [("move", 101, 203), ("click", 101, 203)])

	def test_smart_targets_reject_empty_values(self):
		with self.assertRaises(ValueError):
			SmartSelectParams(index=1, targets=[""])

	def test_returns_structured_native_strategy(self):
		node = SimpleNamespace(backend_node_id=21, session_id="session")

		class Session:
			async def get_element_by_index(self, index):
				return node

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				pass

			async def evaluate(self, script):
				return json.dumps(
					{
						"kind": "native_select",
						"label": "学历",
						"framework": "native",
						"tag": "select",
						"role": "",
						"expanded": True,
						"option_count": 2,
						"option_samples": ["本科", "硕士"],
						"requires_confirm": False,
						"confirm_labels": [],
						"recommended_action": "Call select_dropdown on this same index with exact option text.",
					}
				)

		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(inspect_dropdown(Session(), InspectDropdownParams(index=4)))

		self.assertEqual(result.kind, "native_select")
		self.assertEqual(result.option_samples, ["本科", "硕士"])
		self.assertFalse(result.requires_confirm)

	def test_stale_index_returns_unknown_without_guessing(self):
		class Session:
			async def get_element_by_index(self, index):
				return None

		result = asyncio.run(inspect_dropdown(Session(), InspectDropdownParams(index=99)))

		self.assertEqual(result.kind, "unknown")
		self.assertIn("stale", result.recommended_action)

	def test_smart_native_select_inspects_selects_and_verifies_in_one_call(self):
		node = SimpleNamespace(backend_node_id=21, session_id="session")

		class Session:
			async def get_element_by_index(self, index):
				return node

		class FakeElement:
			def __init__(self, browser_session, backend_node_id, session_id):
				pass

			async def evaluate(self, script, *args):
				if not args:
					return json.dumps(
						{
							"kind": "native_select", "label": "学历", "framework": "native", "tag": "select",
							"role": "", "expanded": True, "option_count": 2, "option_samples": ["本科", "硕士"],
							"requires_confirm": False, "confirm_labels": [], "recommended_action": "native",
						}
					)
				self.assert_target = args[0]
				return json.dumps({"success": True, "selected": args[0], "error": ""})

		with patch("browser_use.actor.element.Element", FakeElement):
			result = asyncio.run(smart_select(Session(), SmartSelectParams(index=4, targets=["硕士"])))

		self.assertTrue(result.success)
		self.assertTrue(result.committed)
		self.assertEqual(result.selected, ["硕士"])

	def test_smart_cascader_routes_complete_path_to_internal_selector(self):
		inspection = {
			"kind": "cascader", "label": "户籍地", "framework": "custom", "tag": "div", "role": "",
			"expanded": True, "option_count": 20, "option_samples": ["北京", "上海"], "requires_confirm": True,
			"confirm_labels": ["确定"], "recommended_action": "cascade",
		}

		async def fake_inspect(browser_session, params):
			from browseragent.dropdown import DropdownInspection

			return DropdownInspection.model_validate(inspection)

		async def fake_cascade(browser_session, index, path):
			from browseragent.cascade import CascadeResult

			return CascadeResult(success=True, mode="custom", selected=path, committed=True)

		with patch("browseragent.dropdown.inspect_dropdown", new=fake_inspect), patch(
			"browseragent.cascade.select_cascade_path", new=fake_cascade
		):
			result = asyncio.run(smart_select(object(), SmartSelectParams(index=7, targets=["北京", "西城区"])))

		self.assertTrue(result.success)
		self.assertEqual(result.kind, "cascader")
		self.assertEqual(result.selected, ["北京", "西城区"])

	def test_smart_native_dependent_selects_route_as_one_path(self):
		async def fake_inspect(browser_session, params):
			from browseragent.dropdown import DropdownInspection

			return DropdownInspection(
				kind="native_select", label="省份", framework="native", tag="select", expanded=True,
				option_count=2, option_samples=["河北省", "北京市"], requires_confirm=False,
				recommended_action="native",
			)

		async def fake_cascade(browser_session, index, path):
			from browseragent.cascade import CascadeResult

			return CascadeResult(success=True, mode="native", selected=path, committed=True)

		with patch("browseragent.dropdown.inspect_dropdown", new=fake_inspect), patch(
			"browseragent.cascade.select_cascade_path", new=fake_cascade
		):
			result = asyncio.run(smart_select(object(), SmartSelectParams(index=5, targets=["河北省", "石家庄市"])))

		self.assertTrue(result.success)
		self.assertEqual(result.kind, "native_cascade")
		self.assertEqual(result.selected, ["河北省", "石家庄市"])

	def test_smart_unknown_control_stops_without_selection(self):
		class Session:
			async def get_element_by_index(self, index):
				return None

		result = asyncio.run(smart_select(Session(), SmartSelectParams(index=99, targets=["硕士"])))

		self.assertFalse(result.success)
		self.assertEqual(result.kind, "adaptive:unknown")


if __name__ == "__main__":
	unittest.main()
