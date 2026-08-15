import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from browseragent.browser import (
	SafeClickParams,
	_task,
	_verified_control_descriptions,
	build_browser_profile,
	_existing_profile_cdp_url,
	build_safe_tools,
	choose_and_focus_web_tab,
	ensure_application_form,
	initial_navigation,
	is_submit_element,
	save_history_trace,
	wait_for_user_handoff,
	wait_for_snapshot_handoff,
)
from browseragent.models import Job


class BrowserSafetyTests(unittest.TestCase):
	def test_pre_agent_trace_can_be_saved_without_browser_history(self):
		with tempfile.TemporaryDirectory() as temporary:
			path = Path(temporary) / "pre-agent.trace.json"
			warning = save_history_trace(
				None,
				path,
				{},
				status="running",
				code_execution=[{"status": "native_solving"}],
			)

			self.assertIsNone(warning)
			trace = json.loads(path.read_text())
			self.assertEqual(trace["actions"], [])
			self.assertEqual(trace["code_execution"][0]["status"], "native_solving")

	def test_agent_task_receives_code_first_verified_and_deferred_queues(self):
		job = Job(id="job", priority="S", company="公司", role="岗位", url="https://example.com", source_line=1)
		task = _task(
			job,
			"候选人资料",
			{},
			[],
			code_filled=["姓名"],
			code_deferred=["研究方向"],
			code_deferred_details=[{
				"field": "公司名称*", "card_context": "职位名称=金融科技实习生",
				"requested_value": "示例证券", "dropdown_evidence": {"verification": "reverted_after_blur"},
			}],
			structure_preparation=[{"section": "education", "status": "prepared", "target_count": 3, "final_count": 3}],
		)

		self.assertIn("CODE-VERIFIED FIELDS", task)
		self.assertIn("- 姓名", task)
		self.assertIn("CODE-DEFERRED NATIVE FIELDS", task)
		self.assertIn("- 研究方向", task)
		self.assertIn("CODE-DEFERRED DETAILS", task)
		self.assertIn("金融科技实习生", task)
		self.assertIn("reverted_after_blur", task)
		self.assertIn("one distinct normal-browser fallback", task)
		self.assertIn("Never revisit or overwrite CODE-VERIFIED FIELDS", task)
		self.assertIn("STRUCTURE PREPARATION", task)
		self.assertIn('"status": "prepared"', task)

	def test_verified_controls_distinguish_repeated_cards(self):
		descriptions = _verified_control_descriptions([{"applied_values": [
			{
				"field": "项目角色 *", "card_type": "project", "card_index": 1, "card_count": 3,
				"card_context": "项目名称=项目甲；项目时间=2025/01",
			},
			{
				"field": "项目角色 *", "card_type": "project", "card_index": 3, "card_count": 3,
				"card_context": "项目名称=项目丙；项目时间=2025/03",
			},
		]}])

		self.assertEqual(len(descriptions), 2)
		self.assertIn("project card 1/3", descriptions[0])
		self.assertIn("项目甲", descriptions[0])
		self.assertIn("project card 3/3", descriptions[1])

	def test_trace_serializes_dom_elements_and_redacts_secrets(self):
		class FakeElement:
			def to_dict(self):
				return {"node_name": "BUTTON", "x_path": "//button", "attributes": {"value": "secret-value"}}

		history = SimpleNamespace(
			urls=lambda: ["https://apply.example.com/form"],
			action_history=lambda: [[{"click": {"index": 3}, "interacted_element": FakeElement()}]],
			errors=lambda: [None],
		)
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "run.trace.json"
			warning = save_history_trace(
				history,
				path,
				{"NATIONAL_ID": "secret-value"},
				status="running",
				code_execution=[{"filled_fields": ["姓名"]}],
			)
			trace = json.loads(path.read_text(encoding="utf-8"))

		self.assertIsNone(warning)
		self.assertEqual(trace["status"], "running")
		self.assertEqual(trace["code_execution"][0]["filled_fields"], ["姓名"])
		self.assertTrue(trace["saved_at"])
		self.assertEqual(trace["actions"][0][0]["interacted_element"]["node_name"], "BUTTON")
		self.assertEqual(trace["actions"][0][0]["interacted_element"]["attributes"]["value"], "<redacted>")

	def test_trace_write_failure_is_non_fatal(self):
		history = SimpleNamespace(urls=lambda: [], action_history=lambda: [], errors=lambda: [])
		with patch.object(Path, "write_text", side_effect=OSError("disk unavailable")):
			warning = save_history_trace(history, Path("unused.trace.json"), {})

		self.assertIn("不影响填报结果", warning)

	def test_browser_profile_excludes_chrome_url_extension_flag(self):
		from browseragent.config import Settings

		settings = Settings(
			career_ops_path=Path("."),
			state_path=Path(".browseragent/runs"),
			browser_profile_path=Path(".browseragent/test-profile"),
			llm_provider="openai",
			llm_model="test",
			llm_api_key="test",
			llm_base_url=None,
			reasoning_effort="low",
		)
		profile = build_browser_profile(settings)
		self.assertNotIn("--extensions-on-chrome-urls", profile.get_args())
		self.assertIn("--enable-automation", profile.ignore_default_args)
		self.assertTrue(profile.keep_alive)
		self.assertFalse(build_browser_profile(settings, keep_alive=False).keep_alive)

	def test_existing_dedicated_profile_reuses_its_live_cdp_endpoint(self):
		with tempfile.TemporaryDirectory() as directory:
			profile = Path(directory)
			(profile / "SingletonLock").symlink_to("machine-12345")
			process = SimpleNamespace(
				returncode=0,
				stdout=f"Google Chrome --user-data-dir={profile.resolve()} --remote-debugging-port=64746",
			)
			class Response:
				def read(self):
					return b'{"webSocketDebuggerUrl":"ws://127.0.0.1:64746/devtools/browser/test"}'

				def __enter__(self):
					return self

				def __exit__(self, *args):
					return None

			with patch("browseragent.browser.subprocess.run", return_value=process), patch(
				"browseragent.browser.urllib.request.urlopen", return_value=Response()
			):
				self.assertEqual(_existing_profile_cdp_url(profile), "http://127.0.0.1:64746")

	def test_final_submit_is_blocked(self):
		cases = [
			("button", {"type": "submit"}, "提交申请"),
			("input", {"type": "submit", "value": "Submit"}, ""),
			("button", {}, "Submit application"),
			("a", {"aria-label": "确认投递"}, ""),
			("button", {}, "Apply"),
			("button", {"_inside_form": "true"}, "完成"),
			("button", {"_inside_form": "true"}, "添加经历并提交申请"),
			("button", {"type": "button", "_page_url": "https://apply.example.com/form"}, "立即申请"),
		]
		for tag, attrs, text in cases:
			with self.subTest(text=text, attrs=attrs):
				self.assertTrue(is_submit_element(tag, attrs, text))

	def test_safe_navigation_is_allowed(self):
		cases = [
			("button", {"type": "button"}, "下一步"),
			("button", {"type": "submit"}, "Continue"),
			("button", {"_inside_form": "true"}, "添加教育经历"),
			("button", {"_inside_form": "true", "id": "apply-add-internship"}, "新增实习经历"),
			("button", {"_inside_form": "true"}, "Add another project"),
			("a", {}, "查看岗位详情"),
			("button", {"type": "button", "_page_url": "https://jobs.example.com/position/detail?id=1"}, "立即申请"),
			("a", {"_page_url": "https://jobs.example.com/job/view/1"}, "Apply now"),
		]
		for tag, attrs, text in cases:
			with self.subTest(text=text, attrs=attrs):
				self.assertFalse(is_submit_element(tag, attrs, text))

	def test_click_names_are_both_backed_by_guarded_index_params(self):
		tools = build_safe_tools()
		actions = tools.registry.registry.actions
		self.assertNotIn("send_keys", actions)
		self.assertNotIn("switch", actions)
		self.assertNotIn("close", actions)
		self.assertIn("click", actions)
		self.assertIn("safe_click", actions)
		self.assertIn("smart_select_control", actions)
		self.assertNotIn("inspect_dropdown_control", actions)
		self.assertNotIn("cascade_select", actions)
		self.assertNotIn("dropdown_options", actions)
		self.assertNotIn("select_dropdown", actions)
		self.assertIs(actions["click"].param_model, SafeClickParams)
		self.assertIs(actions["safe_click"].param_model, SafeClickParams)

	def test_fast_fill_tool_only_exists_when_enabled(self):
		self.assertNotIn("fast_fill_visible_fields", build_safe_tools().registry.registry.actions)

		async def fast_fill(_session):
			return []

		actions = build_safe_tools(fast_fill=fast_fill).registry.registry.actions
		self.assertIn("fast_fill_visible_fields", actions)

	def test_both_click_names_block_final_submission(self):
		tools = build_safe_tools()
		node = SimpleNamespace(
			tag_name="button",
			attributes={"type": "submit"},
			parent=None,
			get_all_children_text=lambda max_depth: "提交申请",
		)

		class FakeBrowserSession:
			async def get_element_by_index(self, index):
				return node

		async def exercise_aliases():
			for action_name in ("click", "safe_click"):
				result = await tools.registry.registry.actions[action_name].function(
					params=SafeClickParams(index=1), browser_session=FakeBrowserSession()
				)
				self.assertIn("BLOCKED", result.error)

		asyncio.run(exercise_aliases())

	def test_initial_navigation_uses_only_selected_job_url(self):
		job = Job(
			id="job",
			priority="S",
			company="公司",
			role="岗位",
			url="https://jobs.example.com/123",
			source_line=1,
		)
		self.assertEqual(
			initial_navigation(job),
			[{"navigate": {"url": "https://jobs.example.com/123", "new_tab": False}}],
		)

	def test_user_handoff_opens_job_then_accepts_current_form(self):
		job = Job(id="job", priority="S", company="公司", role="岗位", url="https://jobs.example.com/123", source_line=1)

		class FakeBrowserSession:
			def __init__(self):
				self.started = False
				self.navigated_to = None

			async def start(self):
				self.started = True

			async def navigate_to(self, url):
				self.navigated_to = url

			async def get_current_page_url(self):
				return "https://jobs.example.com/application/form"

			async def get_tabs(self):
				return [SimpleNamespace(target_id="form", url="https://jobs.example.com/application/form", title="申请表")]

			@property
			def agent_focus_target_id(self):
				return "form"

		session = FakeBrowserSession()
		with patch("builtins.input", return_value="READY"):
			accepted = asyncio.run(wait_for_user_handoff(session, job))

		self.assertTrue(accepted)
		self.assertTrue(session.started)
		self.assertEqual(session.navigated_to, job.url)

	def test_user_handoff_enters_unique_application_form_before_accepting(self):
		job = Job(
			id="job",
			priority="S",
			company="公司",
			role="岗位",
			url="https://jobs.example.com/position/detail?id=1",
			source_line=1,
		)

		class Entry:
			def __init__(self, session):
				self.session = session
				self.clicked = False

			async def click(self):
				self.clicked = True
				self.session.current_url = "https://jobs.example.com/application/form"

		class Page:
			def __init__(self, entry):
				self.entry = entry
				self.selector = None

			async def evaluate(self, script, token):
				self.token = token
				return {"tagged": True, "count": 1}

			async def get_elements_by_css_selector(self, selector):
				self.selector = selector
				return [self.entry]

		class Session:
			agent_focus_target_id = "job"

			def __init__(self):
				self.current_url = job.url
				self.entry = Entry(self)
				self.page = Page(self.entry)

			async def start(self):
				return None

			async def navigate_to(self, url):
				self.current_url = url

			async def get_current_page_url(self):
				return self.current_url

			async def must_get_current_page(self):
				return self.page

			async def get_tabs(self):
				title = "申请表" if "/application/" in self.current_url else "岗位"
				return [SimpleNamespace(target_id="job", url=self.current_url, title=title)]

		session = Session()
		with patch("builtins.input", return_value="READY"):
			accepted = asyncio.run(wait_for_user_handoff(session, job))

		self.assertTrue(accepted)
		self.assertTrue(session.entry.clicked)
		self.assertIn("data-browseragent-application-entry", session.page.selector)
		self.assertEqual(session.current_url, "https://jobs.example.com/application/form")

	def test_application_form_entry_refuses_ambiguous_navigation_controls(self):
		tab = SimpleNamespace(
			target_id="job", url="https://jobs.example.com/position/detail?id=1", title="岗位"
		)

		class Page:
			async def evaluate(self, script, token):
				return json.dumps({"tagged": False, "count": 2})

		class Session:
			async def must_get_current_page(self):
				return Page()

			async def get_tabs(self):
				return [tab]

		result, url = asyncio.run(ensure_application_form(Session(), tab, poll_attempts=1))

		self.assertIsNone(result)
		self.assertEqual(url, tab.url)

	def test_snapshot_handoff_preserves_an_existing_form_tab(self):
		job = Job(id="job", priority="S", company="公司", role="岗位", url="https://jobs.example.com/123", source_line=1)

		class Session:
			agent_focus_target_id = "form"

			def __init__(self):
				self.navigated_to = None

			async def start(self):
				return None

			async def get_tabs(self):
				return [SimpleNamespace(target_id="form", url="https://jobs.example.com/application/form", title="申请表")]

			async def navigate_to(self, url):
				self.navigated_to = url

		session = Session()
		with patch("builtins.input", return_value="READY"):
			accepted = asyncio.run(wait_for_snapshot_handoff(session, job))

		self.assertTrue(accepted)
		self.assertIsNone(session.navigated_to)

	def test_handoff_focuses_user_selected_non_blank_tab(self):
		class CompletedEvent:
			def __await__(self):
				async def done():
					return None

				return done().__await__()

			async def event_result(self, **kwargs):
				return "form"

		class EventBus:
			def __init__(self):
				self.target_id = None

			def dispatch(self, event):
				self.target_id = event.target_id
				return CompletedEvent()

		class FakeBrowserSession:
			agent_focus_target_id = "job"

			def __init__(self):
				self.event_bus = EventBus()

			async def get_tabs(self):
				return [
					SimpleNamespace(target_id="blank", url="about:blank", title=""),
					SimpleNamespace(target_id="job", url="https://jobs.example.com/123", title="岗位"),
					SimpleNamespace(target_id="form", url="https://apply.example.com/form", title="申请表"),
				]

		session = FakeBrowserSession()
		with patch("builtins.input", return_value="2"):
			tab = asyncio.run(choose_and_focus_web_tab(session))

		self.assertEqual(tab.target_id, "form")
		self.assertEqual(session.event_bus.target_id, "form")

	def test_user_can_cancel_before_agent_starts(self):
		job = Job(id="job", priority="S", company="公司", role="岗位", url="https://jobs.example.com/123", source_line=1)
		session = SimpleNamespace(
			start=lambda: None,
			navigate_to=lambda url: None,
		)

		async def cancelled():
			async def no_op():
				return None

			async def get_tabs():
				return [SimpleNamespace(url="about:blank")]

			async def navigate_to(url):
				return None

			session.start = no_op
			session.navigate_to = navigate_to
			session.get_tabs = get_tabs
			with patch("builtins.input", return_value="CANCEL"):
				return await wait_for_user_handoff(session, job)

		self.assertFalse(asyncio.run(cancelled()))


if __name__ == "__main__":
	unittest.main()
