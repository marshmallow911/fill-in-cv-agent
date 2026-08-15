import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from browseragent.fast_fill import (
	APPLY_DATE_SCRIPT,
	APPLY_DATE_PICKER_SCRIPT,
	APPLY_SCRIPT,
	PREPARE_DATE_INPUT_SCRIPT,
	READ_DATE_VALUE_SCRIPT,
	RESTORE_DATE_INPUT_SCRIPT,
	FastAssignment,
	FastAssignmentBatch,
	FastField,
	FastFillTracker,
	PING_PAGE_SCRIPT,
	_ensure_live_page,
	_field_block,
	_hydrate_card_contexts,
	_semantic_assignment_error,
	fast_fill_current_page,
	fast_fill_until_stable,
	plan_field_blocks,
	plan_review_blocks,
)
from browseragent.cascade import FAST_VERIFY_SELECTION_SCRIPT


class FakePage:
	def __init__(self, fields):
		self.fields = fields
		self.assignments = []
		self.apply_calls = 0

	async def evaluate(self, script, *args):
		if not args:
			return json.dumps(self.fields, ensure_ascii=False)
		if script == FAST_VERIFY_SELECTION_SCRIPT:
			return json.dumps({
				"persisted": True, "stable": True,
				"first": args[0]["wanted"], "second": args[0]["wanted"],
			})
		if isinstance(args[0], dict) and "wanted" in args[0]:
			return json.dumps({"opened": False, "visible_options": []})
		if isinstance(args[0], dict) and "path" in args[0]:
			self.apply_calls += 1
			self.assignments.append(args[0])
			return json.dumps(
				{
					"success": True,
					"mode": "custom",
					"selected": args[0]["path"],
					"committed": True,
					"detected_pattern": "dom-diff-overlay",
					"visible_options": args[0]["path"],
				}
			)
		if isinstance(args[0], dict) and "token" in args[0]:
			return json.dumps({"tagged": False})
		if isinstance(args[0], dict) and "field_id" in args[0] and "value" in args[0]:
			self.apply_calls += 1
			self.assignments.append(args[0])
			return json.dumps({"changed": True, "status": "verified"})
		if isinstance(args[0], str):
			return json.dumps(None)
		self.apply_calls += 1
		current = args[0]
		self.assignments.extend(current)
		return json.dumps({
			"changed": [item["field_id"] for item in current],
			"failed": [],
			"outcomes": [
				{
					"field_id": item["field_id"],
					"requested": item["value"],
					"before": "",
					"after": item["value"],
					"status": "verified",
				}
				for item in current
			],
		})


class FakeSession:
	def __init__(self, page):
		self.page = page

	async def must_get_current_page(self):
		return self.page


class FakeLLM:
	def __init__(self, assignments):
		self.assignments = assignments

	async def ainvoke(self, messages, output_format=None):
		return SimpleNamespace(completion=FastAssignmentBatch(assignments=self.assignments))


class FastFillTests(unittest.TestCase):
	def test_page_plan_groups_major_blocks_and_respects_limit(self):
		fields = [
			FastField(id="f1", label="姓名", kind="text", section="基本信息"),
			FastField(id="f2", label="手机号", kind="text", section="基本信息"),
			FastField(id="f3", label="学校", kind="text", section="教育经历"),
			FastField(id="f4", label="专业", kind="text", section="教育经历"),
			FastField(id="f5", label="公司", kind="text", section="实习经历"),
			FastField(id="f6", label="职位", kind="text", section="实习经历"),
			FastField(id="f7", label="项目名称", kind="text", section="项目经历"),
		]

		plan = plan_field_blocks(fields, parallelism=3)

		self.assertEqual(len(plan), 3)
		self.assertEqual({field.id for _, block in plan for field in block}, {field.id for field in fields})

	def test_explicit_project_card_type_wins_over_company_word_in_description(self):
		field = FastField(
			id="project-role", label="项目角色", kind="text", card_type="project",
			card_context="与私募公司合作，负责平台开发",
		)
		self.assertEqual(_field_block(field), "project")

	def test_review_plan_isolates_identified_cards_but_keeps_empty_cards_together(self):
		fields = [
			FastField(
				id="p1-role", label="项目职责", kind="text", card_type="project",
				card_index=1, card_count=3, card_context="量化研究平台，负责总体统筹",
			),
			FastField(
				id="p1-desc", label="项目描述", kind="textarea", card_type="project",
				card_index=1, card_count=3, card_context="量化研究平台，负责总体统筹",
			),
			FastField(
				id="p2-role", label="项目职责", kind="text", card_type="project",
				card_index=2, card_count=3, card_context="编码水印，负责算法开发",
			),
			FastField(
				id="p3-name", label="项目名称", kind="text", card_type="project",
				card_index=3, card_count=3, card_context="",
			),
			FastField(
				id="p4-name", label="项目名称", kind="text", card_type="project",
				card_index=4, card_count=4, card_context="",
			),
		]

		plan = plan_review_blocks(fields)

		self.assertEqual(
			[(name, [field.id for field in block]) for name, block in plan],
			[
				("project card 1", ["p1-role", "p1-desc"]),
				("project card 2", ["p2-role"]),
				("project", ["p3-name", "p4-name"]),
			],
		)

	def test_scan_inventory_hydrates_custom_control_context_from_sibling_values(self):
		fields = [
			FastField(
				id="company", label="公司名称", kind="custom_select", card_type="experience",
				card_index=3, card_count=3,
			),
			FastField(
				id="role", label="职位名称", kind="text", current_value="金融科技实习生",
				card_type="experience", card_index=3, card_count=3,
			),
			FastField(
				id="description", label="工作描述", kind="textarea", current_value="开发合规数据分类 Agent",
				card_type="experience", card_index=3, card_count=3,
			),
		]

		_hydrate_card_contexts(fields)

		self.assertIn("职位名称=金融科技实习生", fields[0].card_context)
		self.assertIn("工作描述=开发合规数据分类 Agent", fields[0].card_context)

	def test_stopped_cdp_client_is_reconnected_and_original_target_refocused(self):
		class DeadPage:
			_target_id = "form-target"

			async def evaluate(self, script, *args):
				raise RuntimeError("Client is not started. Call start() first")

		class LivePage:
			async def evaluate(self, script, *args):
				self.assert_ping = script == PING_PAGE_SCRIPT
				return "true"

		class Session:
			def __init__(self):
				self.connected = False
				self.focused = []

			async def must_get_current_page(self):
				return LivePage() if self.connected else DeadPage()

			async def connect(self):
				self.connected = True

			async def get_or_create_cdp_session(self, target_id, focus=False):
				self.focused.append((target_id, focus))

		session = Session()
		page, status = asyncio.run(_ensure_live_page(session, DeadPage(), field_id="f1"))

		self.assertIsInstance(page, LivePage)
		self.assertIn("reconnected:", status)
		self.assertEqual(session.focused, [("form-target", True)])

	def test_page_plan_never_overwrites_a_large_other_block(self):
		fields = [
			*[FastField(id=f"pub{i}", label="论文名称", kind="text", section="论文") for i in range(12)],
			*[FastField(id=f"edu{i}", label="学校", kind="text", section="教育经历") for i in range(8)],
			*[FastField(id=f"job{i}", label="公司", kind="text", section="工作经历") for i in range(6)],
			*[FastField(id=f"project{i}", label="项目名称", kind="text", section="项目经历") for i in range(5)],
		]

		plan = plan_field_blocks(fields, parallelism=3)

		self.assertEqual(len(plan), 3)
		self.assertEqual(
			[field.id for _, block in plan for field in block],
			[field.id for field in fields],
		)

	def test_small_page_uses_one_solver(self):
		fields = [FastField(id=f"f{i}", label=f"字段{i}", kind="text") for i in range(5)]
		self.assertEqual(plan_field_blocks(fields, parallelism=3), [("all", fields)])

	def test_filters_sensitive_unknown_and_invalid_select_assignments(self):
		page = FakePage(
			[
				{"id": "f1", "label": "姓名", "kind": "text", "options": []},
				{"id": "f2", "label": "期望薪资", "kind": "text", "options": []},
				{"id": "f3", "label": "最高学历", "kind": "select", "options": ["请选择", "本科", "硕士"]},
			]
		)
		llm = FakeLLM(
			[
				FastAssignment(field_id="f1", value="张三"),
				FastAssignment(field_id="f2", value="10000"),
				FastAssignment(field_id="f3", value="博士"),
				FastAssignment(field_id="missing", value="随意值"),
			]
		)
		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "姓名：张三", tracker))

		self.assertEqual(filled, ["姓名"])
		self.assertEqual(page.assignments, [{"field_id": "f1", "value": "张三"}])
		self.assertEqual(tracker.filled_labels, ["姓名"])
		# Sensitive/blocked fields never enter the Agent fallback queue. The safe
		# select remains deferred because the proposed value was not a real option.
		self.assertEqual(tracker.deferred_labels, ["最高学历"])
		self.assertEqual(tracker.code_passes[0]["filled_fields"], ["姓名"])
		self.assertEqual(tracker.code_passes[0]["deferred_fields"], ["最高学历"])
		self.assertIn("期望薪资", tracker.code_passes[0]["blocked_fields"])
		self.assertTrue(any(item["field"] == "最高学历" for item in tracker.code_passes[0]["deferred_details"]))

	def test_valid_exact_select_option_is_applied(self):
		page = FakePage([{"id": "f1", "label": "最高学历", "kind": "select", "options": ["本科", "硕士"]}])
		llm = FakeLLM([FastAssignment(field_id="f1", value="硕士")])

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "最高学历：硕士", FastFillTracker()))

		self.assertEqual(filled, ["最高学历"])
		self.assertEqual(page.assignments, [{"field_id": "f1", "value": "硕士"}])

	def test_trace_checkpoints_include_solver_assignments_and_write_outcomes(self):
		page = FakePage([{"id": "f1", "label": "姓名", "kind": "text"}])
		tracker = FastFillTracker()
		checkpoints = []

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page),
			FakeLLM([FastAssignment(field_id="f1", value="张三")]),
			"姓名：张三",
			tracker,
			checkpoint=lambda: checkpoints.append(tracker.current_stage),
		))

		code_pass = tracker.code_passes[0]
		self.assertEqual(filled, ["姓名"])
		self.assertEqual(code_pass["status"], "completed")
		self.assertTrue(code_pass["completed_at"])
		self.assertEqual(code_pass["solver_runs"][0]["assignments"][0]["value"], "张三")
		self.assertEqual(code_pass["native_write_attempts"][0]["status"], "verified")
		self.assertIn("page_1_sequential_writes_verified", checkpoints)
		self.assertEqual(checkpoints[-1], "page_1_completed")

	def test_explicit_gender_native_option_is_not_hard_blocked(self):
		page = FakePage([{"id": "f1", "label": "性别", "kind": "select", "options": ["男", "女"]}])
		llm = FakeLLM([FastAssignment(field_id="f1", value="男")])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "性别：男", tracker))

		self.assertEqual(filled, ["性别"])
		self.assertEqual(page.assignments, [{"field_id": "f1", "value": "男"}])
		self.assertEqual(tracker.code_passes[0]["blocked_fields"], [])

	def test_custom_dropdowns_are_mapped_once_then_executed_sequentially(self):
		page = FakePage(
			[
				{"id": "f1", "label": "性别", "kind": "custom_select"},
				{"id": "f2", "label": "户籍地", "kind": "custom_cascader"},
			]
		)
		llm = FakeLLM(
			[
				FastAssignment(field_id="f1", targets=["男"]),
				FastAssignment(field_id="f2", targets=["北京市", "西城区"]),
			]
		)
		tracker = FastFillTracker()

		filled = asyncio.run(
			fast_fill_current_page(FakeSession(page), llm, "性别：男；户籍地：北京市西城区", tracker)
		)

		self.assertEqual(filled, ["性别", "户籍地"])
		self.assertEqual(page.apply_calls, 2)
		self.assertEqual([item["field_id"] for item in page.assignments], ["f1", "f2"])
		self.assertEqual([item["targets"] for item in tracker.code_passes[0]["dropdown_attempts"]], [["男"], ["北京市", "西城区"]])
		self.assertTrue(all(item["success"] for item in tracker.code_passes[0]["dropdown_attempts"]))
		self.assertEqual(
			[item["targets"] for item in tracker.code_passes[0]["applied_values"]],
			[["男"], ["北京市", "西城区"]],
		)

	def test_reverted_custom_dropdown_is_deferred_with_agent_recovery_evidence(self):
		class RevertingPage(FakePage):
			async def evaluate(self, script, *args):
				if script == FAST_VERIFY_SELECTION_SCRIPT:
					return json.dumps({
						"persisted": False, "stable": True, "first": "", "second": "",
					})
				return await super().evaluate(script, *args)

		page = RevertingPage([{
			"id": "f1", "label": "公司名称*", "kind": "custom_select",
			"card_context": "职位名称=金融科技实习生",
		}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page),
			FakeLLM([FastAssignment(field_id="f1", targets=["示例证券公司"])]),
			"公司：示例证券公司", tracker,
		))

		self.assertEqual(filled, [])
		attempt = tracker.code_passes[0]["dropdown_attempts"][0]
		self.assertEqual(attempt["verification"], "reverted_after_blur")
		detail = tracker.code_passes[0]["deferred_details"][0]
		self.assertEqual(detail["requested_value"], "示例证券公司")
		self.assertEqual(detail["card_context"], "职位名称=金融科技实习生")
		self.assertEqual(detail["dropdown_evidence"]["actual_state"], "")

	def test_one_level_cascader_path_is_allowed_when_source_has_one_level(self):
		page = FakePage([{"id": "f1", "label": "工作城市", "kind": "custom_cascader", "dom_order": 0}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([FastAssignment(field_id="f1", targets=["北京"])]), "工作城市：北京", tracker
		))

		self.assertEqual(filled, ["工作城市"])
		self.assertEqual(page.assignments[0]["path"], ["北京"])

	def test_mixed_controls_execute_in_visual_order_not_control_family_order(self):
		page = FakePage([
			{"id": "custom", "label": "性别", "kind": "custom_select", "dom_order": 0, "top": 100, "left": 100},
			{"id": "native", "label": "姓名", "kind": "text", "dom_order": 1, "top": 100, "left": 500},
		])
		llm = FakeLLM([
			FastAssignment(field_id="custom", targets=["男"]),
			FastAssignment(field_id="native", value="张三"),
		])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "姓名：张三；性别：男", tracker))

		self.assertEqual(filled, ["性别", "姓名"])
		self.assertEqual([item["field_id"] for item in page.assignments], ["custom", "native"])
		self.assertEqual(
			[item["field_id"] for item in tracker.code_passes[0]["execution_order"]],
			["custom", "native"],
		)

	def test_ongoing_checkbox_dependency_runs_before_dates(self):
		page = FakePage([
			{"id": "date", "label": "结束时间", "kind": "custom_date", "dom_order": 0, "top": 100},
			{"id": "ongoing", "label": "至今", "kind": "ongoing_checkbox", "dom_order": 1, "top": 100, "current_value": "true"},
		])
		llm = FakeLLM([
			FastAssignment(field_id="date", value="2026/08"),
			FastAssignment(field_id="ongoing", value="false"),
		])

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "实习：2026/06–2026/08", FastFillTracker()))

		self.assertEqual(filled, ["结束时间", "至今"])
		self.assertEqual([item["field_id"] for item in page.assignments], ["ongoing", "date"])

	def test_failed_direct_text_write_uses_cdp_fill_before_finishing_field(self):
		class Element:
			def __init__(self, owner):
				self.owner = owner

			async def fill(self, value):
				self.owner.value = value

		class Page:
			def __init__(self):
				self.value = ""

			async def evaluate(self, script, *args):
				if not args:
					return json.dumps([{"id": "f1", "label": "姓名", "kind": "text", "dom_order": 0}])
				if isinstance(args[0], list):
					return json.dumps({"changed": [], "failed": ["f1"], "outcomes": [
						{"field_id": "f1", "requested": "张三", "status": "write_not_persisted"}
					]})
				return json.dumps({"found": True, "value": self.value})

			async def get_elements_by_css_selector(self, selector):
				return [Element(self)]

		page = Page()
		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([FastAssignment(field_id="f1", value="张三")]), "姓名：张三", tracker
		))

		self.assertEqual(filled, ["姓名"])
		self.assertEqual(tracker.code_passes[0]["execution_order"][0]["method"], "cdp-fill")

	def test_one_native_runtime_error_does_not_abort_later_fields(self):
		class Page:
			async def evaluate(self, script, *args):
				if script == PING_PAGE_SCRIPT:
					return "true"
				if not args:
					return json.dumps([
						{"id": "f1", "label": "字段一", "kind": "text", "dom_order": 0},
						{"id": "f2", "label": "字段二", "kind": "text", "dom_order": 1},
					])
				if script == APPLY_SCRIPT and args[0][0]["field_id"] == "f1":
					raise RuntimeError("transient field failure")
				if script == APPLY_SCRIPT:
					item = args[0][0]
					return json.dumps({"changed": [item["field_id"]], "outcomes": [{
						"field_id": item["field_id"], "requested": item["value"], "status": "verified",
					}]})
				return json.dumps({"found": False, "value": ""})

			async def get_elements_by_css_selector(self, selector):
				return []

		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(
			FakeSession(Page()), FakeLLM([
				FastAssignment(field_id="f1", value="甲"), FastAssignment(field_id="f2", value="乙"),
			]), "字段一：甲；字段二：乙", tracker,
		))

		self.assertEqual(filled, ["字段二"])
		self.assertEqual(len(tracker.code_passes[0]["execution_order"]), 2)
		self.assertTrue(tracker.code_passes[0]["execution_order"][1]["success"])

	def test_user_confirmed_gender_default_bypasses_empty_llm_assignment(self):
		page = FakePage([{"id": "f1", "label": "性别", "kind": "custom_select", "dom_order": 0}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([]), "候选人资料", tracker,
			confirmed_defaults={"个人信息-性别": "男"},
		))

		self.assertEqual(filled, ["性别"])
		self.assertEqual(page.assignments[0]["path"], ["男"])
		self.assertEqual(
			tracker.code_passes[0]["confirmed_default_assignments"][0]["targets"], ["男"]
		)

	def test_national_id_secret_name_maps_document_type_without_exposing_value(self):
		page = FakePage([{"id": "f1", "label": "证件类型", "kind": "custom_select", "dom_order": 0}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([]), "候选人资料", tracker,
			available_sensitive_types={"national_id"},
		))

		self.assertEqual(filled, ["证件类型"])
		self.assertEqual(page.assignments[0]["path"], ["居民身份证"])
		self.assertEqual(tracker.code_passes[0]["sensitive_type_assignments"][0]["targets"], ["居民身份证"])

	def test_secret_number_follows_left_hand_document_type_without_reaching_llm(self):
		secret = "TEST-NATIONAL-ID-0001"
		page = FakePage([
			{
				"id": "type", "label": "证件类型", "kind": "custom_select",
				"dom_order": 0, "top": 100, "left": 100,
			},
			{
				"id": "number", "label": "请输入证件号码", "kind": "text",
				"dom_order": 1, "top": 106, "left": 500, "disabled": True, "current_value": secret,
			},
		])

		class InspectingLLM:
			def __init__(self):
				self.prompts = []

			async def ainvoke(self, messages, output_format=None):
				self.prompts.append("\n".join(message.text for message in messages))
				return SimpleNamespace(completion=FastAssignmentBatch(assignments=[]))

		llm = InspectingLLM()
		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), llm, "候选人资料", tracker,
			available_sensitive_types={"national_id"},
			sensitive_values={"national_id": secret},
		))

		self.assertEqual(filled, ["证件类型", "请输入证件号码"])
		self.assertEqual([item["field_id"] for item in page.assignments], ["type", "number"])
		self.assertEqual(page.assignments[1]["value"], secret)
		self.assertTrue(all(secret not in prompt for prompt in llm.prompts))
		code_pass = tracker.code_passes[0]
		self.assertEqual(code_pass["sensitive_value_assignments"], [
			{"field_id": "number", "field": "请输入证件号码", "secret_name": "national_id"}
		])
		secret_applied = next(item for item in code_pass["applied_values"] if item["field_id"] == "number")
		self.assertEqual(secret_applied["value"], "<secret:national_id>")
		self.assertEqual(code_pass["native_write_attempts"][0]["requested"], "<secret:national_id>")
		self.assertEqual(code_pass["scanned_fields"][1]["current_value"], "<secret:national_id>")
		self.assertNotIn(secret, json.dumps(code_pass, ensure_ascii=False))

	def test_secret_value_never_enters_type_or_ambiguous_sensitive_field(self):
		page = FakePage([
			{"id": "type", "label": "证件类型", "kind": "text", "dom_order": 0},
			{"id": "vague", "label": "证件信息", "kind": "text", "dom_order": 1},
		])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([]), "候选人资料", tracker,
			sensitive_values={"national_id": "TEST-NATIONAL-ID-0001"},
		))

		self.assertEqual(filled, [])
		self.assertEqual(page.assignments, [])
		self.assertEqual(tracker.code_passes[0]["sensitive_value_assignments"], [])

	def test_custom_solver_failure_does_not_discard_text_writes(self):
		page = FakePage(
			[
				{"id": "f1", "label": "姓名", "kind": "text"},
				{"id": "f2", "label": "性别", "kind": "custom_select"},
			]
		)

		class DropdownFailingLLM:
			async def ainvoke(self, messages, output_format=None):
				payload = messages[-1].text
				if '"kind": "custom_select"' in payload:
					raise RuntimeError("dropdown mapping unavailable")
				return SimpleNamespace(
					completion=FastAssignmentBatch(assignments=[FastAssignment(field_id="f1", value="张三")])
				)

		tracker = FastFillTracker()
		filled = asyncio.run(
			fast_fill_current_page(FakeSession(page), DropdownFailingLLM(), "姓名：张三；性别：男", tracker)
		)

		self.assertEqual(filled, ["姓名"])
		self.assertEqual(page.assignments, [{"field_id": "f1", "value": "张三"}])
		self.assertEqual([item["phase"] for item in tracker.code_passes[0]["solver_runs"]], ["native", "custom"])
		self.assertEqual(tracker.code_passes[0]["solver_runs"][1]["status"], "failed")
		self.assertTrue(any(item["field"] == "性别" and item["reason"] == "solver_failed" for item in tracker.code_passes[0]["deferred_details"]))

	def test_cancelled_custom_solver_block_is_isolated_like_other_gateway_failures(self):
		page = FakePage([
			{"id": "f1", "label": "姓名", "kind": "text"},
			{"id": "f2", "label": "作者顺序", "kind": "custom_select"},
		])

		class CancellingLLM:
			async def ainvoke(self, messages, output_format=None):
				if '"kind": "custom_select"' in messages[-1].text:
					raise asyncio.CancelledError("gateway child request cancelled")
				return SimpleNamespace(
					completion=FastAssignmentBatch(assignments=[FastAssignment(field_id="f1", value="张三")])
				)

		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(FakeSession(page), CancellingLLM(), "姓名：张三", tracker))

		self.assertEqual(filled, ["姓名"])
		self.assertEqual(tracker.code_passes[0]["status"], "completed")
		self.assertEqual(tracker.code_passes[0]["solver_runs"][1]["status"], "failed")

	def test_date_picker_uses_source_supported_year_month(self):
		page = FakePage([{"id": "f1", "label": "在校时间*", "kind": "custom_date"}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([FastAssignment(field_id="f1", value="2021/09")]), "2021/09", tracker
		))

		self.assertEqual(filled, ["在校时间*"])
		self.assertEqual(page.assignments, [{"field_id": "f1", "value": "2021/09", "label": "在校时间*"}])
		self.assertEqual(tracker.code_passes[0]["execution_order"][0]["method"], "date-dom-direct")

	def test_date_picker_uses_cdp_typing_after_dom_value_is_rejected(self):
		class DateInput:
			def __init__(self, page):
				self.page = page

			async def fill(self, value):
				self.page.value = value

		class Page:
			def __init__(self):
				self.value = ""

			async def evaluate(self, script, *args):
				if not args:
					return json.dumps([{"id": "f1", "label": "开始时间", "kind": "custom_date"}])
				if script == APPLY_DATE_SCRIPT:
					return json.dumps({"changed": False, "status": "write_not_persisted"})
				if script == PREPARE_DATE_INPUT_SCRIPT:
					return json.dumps({"found": True, "was_readonly": True})
				if script == RESTORE_DATE_INPUT_SCRIPT:
					return json.dumps({"found": True, "value": self.value})
				if script == READ_DATE_VALUE_SCRIPT:
					return json.dumps({"found": True, "value": self.value})
				raise AssertionError("unexpected script")

			async def get_elements_by_css_selector(self, selector):
				return [DateInput(self)]

		page = Page()
		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([FastAssignment(field_id="f1", value="2021/09")]), "2021/09", tracker
		))

		self.assertEqual(filled, ["开始时间"])
		self.assertEqual(page.value, "2021/09")
		self.assertEqual(tracker.code_passes[0]["execution_order"][0]["method"], "date-cdp-fill")

	def test_controlled_date_uses_visible_picker_before_cdp_typing(self):
		class Page:
			def __init__(self):
				self.picker_calls = 0

			async def evaluate(self, script, *args):
				if not args:
					return json.dumps([{"id": "f1", "label": "实习开始时间", "kind": "custom_date"}])
				if script == APPLY_DATE_SCRIPT:
					return json.dumps({"changed": False, "status": "write_not_persisted"})
				if script == APPLY_DATE_PICKER_SCRIPT:
					self.picker_calls += 1
					return json.dumps({
						"changed": True, "status": "verified_picker_selection",
						"before": "", "after": "2025/06",
					})
				raise AssertionError("CDP date typing must not run after picker success")

		page = Page()
		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([FastAssignment(field_id="f1", value="2025/06")]),
			"实习时间：2025/06", tracker,
		))

		self.assertEqual(filled, ["实习开始时间"])
		self.assertEqual(page.picker_calls, 1)
		self.assertEqual(tracker.code_passes[0]["execution_order"][0]["method"], "date-picker-direct")
		self.assertEqual(tracker.code_passes[0]["date_picker_attempts"][0]["status"], "verified_picker_selection")

	def test_review_metrics_do_not_report_existing_values_as_missing(self):
		page = FakePage([{
			"id": "f1", "label": "公司名称", "kind": "text", "current_value": "示例公司",
		}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([]), "公司名称：示例公司", tracker, review_mode=True,
		))

		code_pass = tracker.code_passes[0]
		self.assertEqual(filled, [])
		self.assertEqual(code_pass["observed_existing_fields"], ["公司名称"])
		self.assertEqual(code_pass["field_counts"]["already_populated_before_pass"], 1)
		self.assertEqual(code_pass["field_counts"]["remaining_unresolved"], 0)
		self.assertEqual(code_pass["deferred_fields"], [])

	def test_semantic_guard_rejects_cross_type_mappings(self):
		self.assertEqual(
			_semantic_assignment_error(FastField(id="school", label="学校", kind="text"), "2021/09"),
			"year_month_assigned_to_non_date_field",
		)
		self.assertEqual(
			_semantic_assignment_error(FastField(id="degree", label="学历", kind="custom_select"), "访问学习"),
			"non_degree_value_assigned_to_degree_field",
		)
		self.assertEqual(
			_semantic_assignment_error(FastField(id="language", label="语言水平", kind="custom_select"), "96"),
			"numeric_score_assigned_to_language_level",
		)

	def test_semantic_guard_prevents_wrong_value_from_reaching_page(self):
		page = FakePage([{"id": "f1", "label": "学校", "kind": "text"}])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(
			FakeSession(page), FakeLLM([FastAssignment(field_id="f1", value="2021/09")]), "候选人资料", tracker
		))

		self.assertEqual(filled, [])
		self.assertEqual(page.assignments, [])
		self.assertEqual(tracker.code_passes[0]["proposals"][0]["status"], "semantic_type_mismatch")
		self.assertIn("semantic_type_mismatch", tracker.code_passes[0]["deferred_details"][0]["reason"])

	def test_dependency_change_schedules_one_rescan_for_newly_revealed_fields(self):
		tracker = FastFillTracker()

		async def one_pass(*args, **kwargs):
			if not tracker.code_passes:
				tracker.code_passes.append({
					"execution_order": [{"kind": "custom_select", "success": True}],
					"scanned_fields": [],
				})
				return ["左侧选择"]
			tracker.code_passes.append({"execution_order": [], "scanned_fields": []})
			return ["右侧新字段"]

		with patch("browseragent.fast_fill.fast_fill_current_page", side_effect=one_pass) as mocked:
			filled = asyncio.run(fast_fill_until_stable(None, None, "资料", tracker))

		self.assertEqual(filled, ["左侧选择", "右侧新字段"])
		self.assertEqual(mocked.call_count, 2)
		self.assertTrue(tracker.code_passes[0]["dependency_rescan_scheduled"])
		self.assertFalse(tracker.code_passes[1]["dependency_rescan_scheduled"])

	def test_second_pass_reviews_even_without_dependency_control_change(self):
		tracker = FastFillTracker()

		async def one_pass(*args, **kwargs):
			tracker.code_passes.append({
				"execution_order": [], "scanned_fields": [],
				"write_failures": [], "deferred_details": [{"field": "公司名称", "reason": "no_source_supported_value"}],
				"applied_values": [],
			})
			return ["姓名"] if len(tracker.code_passes) == 1 else []

		with patch("browseragent.fast_fill.fast_fill_current_page", side_effect=one_pass) as mocked:
			asyncio.run(fast_fill_until_stable(None, None, "资料", tracker))

		self.assertEqual(mocked.call_count, 2)
		self.assertTrue(mocked.call_args_list[1].kwargs["review_mode"])
		self.assertIn("公司名称", mocked.call_args_list[1].kwargs["review_notes"])

	def test_repeated_labels_without_unique_card_context_are_deferred(self):
		page = FakePage(
			[
				{"id": "f1", "label": "部门名称", "kind": "text", "section": "实习经历", "card_context": ""},
				{"id": "f2", "label": "部门名称", "kind": "text", "section": "实习经历", "card_context": ""},
			]
		)
		llm = FakeLLM(
			[
				FastAssignment(field_id="f1", value="信息技术部"),
				FastAssignment(field_id="f2", value="数字化部"),
			]
		)
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "候选人资料", tracker))

		self.assertEqual(filled, [])
		self.assertEqual(page.apply_calls, 0)
		self.assertEqual(tracker.deferred_labels, ["部门名称"])
		self.assertEqual(
			{item["field_id"] for item in tracker.code_passes[0]["ambiguous_repeated_fields"]},
			{"f1", "f2"},
		)

	def test_repeated_labels_with_unique_card_context_are_applied_and_audited(self):
		page = FakePage(
			[
				{
					"id": "f1",
					"label": "部门名称",
					"kind": "text",
					"section": "实习经历",
					"card_context": "公司名称=甲公司；职位=开发",
				},
				{
					"id": "f2",
					"label": "部门名称",
					"kind": "text",
					"section": "实习经历",
					"card_context": "公司名称=乙公司；职位=研究员",
				},
			]
		)
		llm = FakeLLM(
			[
				FastAssignment(field_id="f1", value="信息技术部"),
				FastAssignment(field_id="f2", value="数字化部"),
			]
		)
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "候选人资料", tracker))

		self.assertEqual(filled, ["部门名称", "部门名称"])
		self.assertEqual(page.apply_calls, 2)
		self.assertEqual(
			[item["value"] for item in tracker.code_passes[0]["applied_values"]],
			["信息技术部", "数字化部"],
		)
		self.assertEqual(
			[item["card_context"] for item in tracker.code_passes[0]["applied_values"]],
			["公司名称=甲公司；职位=开发", "公司名称=乙公司；职位=研究员"],
		)

	def test_empty_repeat_cards_with_unique_positions_are_applied(self):
		fields = [
			{
				"id": f"f{index}",
				"label": "论文名称",
				"kind": "text",
				"section": "publication",
				"card_context": "",
				"card_type": "publication",
				"card_index": index,
				"card_count": 5,
				"card_signature": "请输入论文名称|请输入影响因子|请选择",
			}
			for index in range(1, 6)
		]
		page = FakePage(fields)
		llm = FakeLLM([FastAssignment(field_id=f"f{index}", value=f"论文{index}") for index in range(1, 6)])
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "论文1\n论文2\n论文3\n论文4\n论文5", tracker))

		self.assertEqual(filled, ["论文名称"] * 5)
		self.assertEqual(page.apply_calls, 5)
		self.assertEqual([item["card_index"] for item in tracker.code_passes[0]["applied_values"]], [1, 2, 3, 4, 5])
		self.assertEqual(tracker.code_passes[0]["ambiguous_repeated_fields"], [])

	def test_empty_repeat_cards_with_duplicate_positions_are_deferred(self):
		fields = [
			{
				"id": "f1",
				"label": "论文名称",
				"kind": "text",
				"card_type": "publication",
				"card_index": 1,
				"card_count": 2,
				"card_signature": "论文名称|影响因子",
			},
			{
				"id": "f2",
				"label": "论文名称",
				"kind": "text",
				"card_type": "publication",
				"card_index": 1,
				"card_count": 2,
				"card_signature": "论文名称|影响因子",
			},
		]
		page = FakePage(fields)
		tracker = FastFillTracker()

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), FakeLLM([]), "论文资料", tracker))

		self.assertEqual(filled, [])
		self.assertEqual(page.apply_calls, 0)
		self.assertEqual({item["field_id"] for item in tracker.code_passes[0]["ambiguous_repeated_fields"]}, {"f1", "f2"})

	def test_cascading_region_fields_are_left_for_dedicated_tool(self):
		page = FakePage(
			[
				{"id": "f1", "label": "户籍地省份", "kind": "select", "options": ["北京"]},
				{"id": "f2", "label": "户籍地区县", "kind": "select", "options": ["西城区"]},
			]
		)
		llm = FakeLLM(
			[
				FastAssignment(field_id="f1", value="北京"),
				FastAssignment(field_id="f2", value="西城区"),
			]
		)

		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "户籍地：北京西城区", FastFillTracker()))

		self.assertEqual(filled, [])
		self.assertEqual(page.assignments, [])

	def test_parallel_solvers_execute_one_verified_write_per_field(self):
		fields = [
			{"id": "f1", "label": "姓名", "kind": "text", "section": "基本信息", "options": []},
			{"id": "f2", "label": "手机号", "kind": "text", "section": "基本信息", "options": []},
			{"id": "f3", "label": "学校", "kind": "text", "section": "教育经历", "options": []},
			{"id": "f4", "label": "专业", "kind": "text", "section": "教育经历", "options": []},
			{"id": "f5", "label": "公司", "kind": "text", "section": "实习经历", "options": []},
			{"id": "f6", "label": "职位", "kind": "text", "section": "实习经历", "options": []},
		]
		page = FakePage(fields)

		class ParallelLLM:
			def __init__(self):
				self.calls = 0
				self.active = 0
				self.max_active = 0

			async def ainvoke(self, messages, output_format=None):
				self.calls += 1
				self.active += 1
				self.max_active = max(self.max_active, self.active)
				await asyncio.sleep(0.01)
				payload = messages[-1].text
				assignments = [FastAssignment(field_id=field["id"], value=f"值-{field['id']}") for field in fields if field["id"] in payload]
				self.active -= 1
				return SimpleNamespace(completion=FastAssignmentBatch(assignments=assignments))

		llm = ParallelLLM()
		tracker = FastFillTracker()
		filled = asyncio.run(fast_fill_current_page(FakeSession(page), llm, "候选人资料", tracker, parallelism=3))

		self.assertEqual(llm.calls, 3)
		self.assertGreaterEqual(llm.max_active, 2)
		self.assertEqual(page.apply_calls, 6)
		self.assertEqual(len(filled), 6)
		self.assertEqual(len(tracker.last_plan), 3)

	def test_one_failed_block_does_not_discard_other_block_answers(self):
		fields = [
			{"id": "f1", "label": "姓名", "kind": "text", "section": "基本信息", "options": []},
			{"id": "f2", "label": "手机号", "kind": "text", "section": "基本信息", "options": []},
			{"id": "f3", "label": "学校", "kind": "text", "section": "教育经历", "options": []},
			{"id": "f4", "label": "专业", "kind": "text", "section": "教育经历", "options": []},
			{"id": "f5", "label": "公司", "kind": "text", "section": "实习经历", "options": []},
			{"id": "f6", "label": "职位", "kind": "text", "section": "实习经历", "options": []},
		]
		page = FakePage(fields)

		class PartiallyFailingLLM:
			async def ainvoke(self, messages, output_format=None):
				payload = messages[-1].text
				if "教育经历" in payload.split("CANDIDATE SOURCES", 1)[0]:
					raise RuntimeError("rate limited")
				assignments = [FastAssignment(field_id=field["id"], value=f"值-{field['id']}") for field in fields if field["id"] in payload]
				return SimpleNamespace(completion=FastAssignmentBatch(assignments=assignments))

		tracker = FastFillTracker()
		filled = asyncio.run(
			fast_fill_current_page(FakeSession(page), PartiallyFailingLLM(), "候选人资料", tracker, parallelism=3)
		)

		self.assertEqual(len(filled), 4)
		self.assertEqual(page.apply_calls, 4)
		self.assertTrue(any("教育经历" in warning and "普通 Agent" in warning for warning in tracker.warnings))


if __name__ == "__main__":
	unittest.main()
