import unittest

from browseragent.models import FillResult


class FillResultCoverageTests(unittest.TestCase):
	def test_unreviewed_discovered_section_prevents_review(self):
		result = FillResult(
			discovered_sections=["基本信息", "教育经历", "实习经历"],
			reviewed_sections=["基本信息", "教育经历"],
			ready_for_review=True,
		).enforce_section_coverage()

		self.assertFalse(result.ready_for_review)
		self.assertEqual(result.remaining_sections, ["实习经历"])

	def test_all_discovered_sections_allow_review(self):
		result = FillResult(
			discovered_sections=["基本信息", "教育经历", "实习经历", "项目经历"],
			reviewed_sections=["基本信息", "教育经历", "实习经历", "项目经历"],
			ready_for_review=True,
		).enforce_section_coverage()

		self.assertTrue(result.ready_for_review)
		self.assertEqual(result.remaining_sections, [])

	def test_missing_initial_survey_prevents_review(self):
		result = FillResult(ready_for_review=True).enforce_section_coverage()

		self.assertFalse(result.ready_for_review)
		self.assertEqual(result.remaining_sections, ["表单区域盘点"])


if __name__ == "__main__":
	unittest.main()
