import tempfile
import unittest
from pathlib import Path

from browseragent.career_ops import CareerOpsStore, mask_secret, parse_recommendations
from browseragent.models import ApplicationRun


SAMPLE = """# 推荐\n\n## S 级｜优先\n\n- [ ] **测试公司｜算法工程师**｜北京/上海｜[岗位详情](https://example.com/job/1)\n  - 匹配：图学习方向。\n  - 进展：\n\n- [x] **已投公司｜研究员**｜杭州｜[岗位详情](https://example.com/job/2)\n  - 匹配：研究方向。\n  - 进展：2026-08-01 已投递\n"""


class CareerOpsTests(unittest.TestCase):
	def setUp(self):
		self.temporary = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary.name)
		path = self.root / "data/2027-autumn-recommendations.md"
		path.parent.mkdir(parents=True)
		path.write_text(SAMPLE, encoding="utf-8")
		self.store = CareerOpsStore(self.root)

	def tearDown(self):
		self.temporary.cleanup()

	def test_parse_recommendations(self):
		jobs = parse_recommendations(SAMPLE)
		self.assertEqual(len(jobs), 2)
		self.assertEqual(jobs[0].company, "测试公司")
		self.assertEqual(jobs[0].location, "北京/上海")
		self.assertEqual(jobs[0].reason, "图学习方向。")
		self.assertTrue(jobs[1].checked)

	def test_pending_jobs_and_safe_submission_update(self):
		job = self.store.jobs()[0]
		run = ApplicationRun(id="run1", job=job)
		self.store.mark_submitted(run)
		text = self.store.recommendations_path.read_text(encoding="utf-8")
		self.assertIn("- [x] **测试公司", text)
		self.assertIn("已投递", text)

	def test_secret_permissions_and_mask(self):
		self.store.set_secret("national_id", "TEST-NATIONAL-ID-0001")
		self.assertEqual(self.store.get_secret("national_id"), "TEST-NATIONAL-ID-0001")
		self.assertEqual(self.store.secrets_path.stat().st_mode & 0o777, 0o600)
		self.assertEqual(mask_secret("TEST-NATIONAL-ID-0001"), "TES**************0001")


if __name__ == "__main__":
	unittest.main()
