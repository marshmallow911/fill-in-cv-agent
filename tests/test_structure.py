import asyncio
import json
import unittest
from types import SimpleNamespace

from browseragent.structure import ResumeInventory, prepare_repeat_sections


class StructurePreparationTests(unittest.TestCase):
	def test_inventory_is_applied_before_field_fill(self):
		class LLM:
			async def ainvoke(self, messages, output_format=None):
				return SimpleNamespace(completion=ResumeInventory(education=3, experience=3, project=4, publication=5))

		class Page:
			def __init__(self):
				self.targets = None

			async def evaluate(self, script, targets):
				self.targets = targets
				return json.dumps(
					[
						{
							"section": "education",
							"target_count": 3,
							"initial_count": 2,
							"final_count": 3,
							"added": 1,
							"status": "prepared",
							"message": "",
						}
					]
				)

		class Session:
			def __init__(self):
				self.page = Page()

			async def must_get_current_page(self):
				return self.page

		session = Session()
		inventory, results = asyncio.run(prepare_repeat_sections(session, LLM(), "CV"))

		self.assertEqual(inventory.education, 3)
		self.assertEqual(session.page.targets["publication"], 5)
		self.assertEqual(results[0].added, 1)
		self.assertEqual(results[0].status, "prepared")

	def test_inventory_rejects_unbounded_counts(self):
		with self.assertRaises(ValueError):
			ResumeInventory(education=100)


if __name__ == "__main__":
	unittest.main()
