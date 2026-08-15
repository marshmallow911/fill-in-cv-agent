import unittest
from contextlib import redirect_stderr
from io import StringIO

from browseragent.cli import _choose_exact_role, _parser
from browseragent.models import Job


class CliTests(unittest.TestCase):
	def test_apply_and_resume_have_one_fill_mode(self):
		self.assertEqual(_parser().parse_args(["apply"]).command, "apply")
		self.assertEqual(_parser().parse_args(["resume", "run-1"]).command, "resume")
		with redirect_stderr(StringIO()):
			with self.assertRaises(SystemExit):
				_parser().parse_args(["apply", "--fast"])
			with self.assertRaises(SystemExit):
				_parser().parse_args(["resume", "run-1", "--no-fast"])

	def test_snapshot_supports_optional_dropdown_probing(self):
		plain = _parser().parse_args(["snapshot", "job-1"])
		probed = _parser().parse_args(["snapshot", "job-1", "--probe-dropdowns"])

		self.assertEqual(plain.command, "snapshot")
		self.assertFalse(plain.probe_dropdowns)
		self.assertTrue(probed.probe_dropdowns)

	def test_supported_sensitive_values_have_local_secret_commands(self):
		for name in ("national_id", "passport_number", "social_security_number"):
			with self.subTest(name=name):
				parsed = _parser().parse_args(["secrets", "set", name])
				self.assertEqual(parsed.name, name)

	def test_single_role_needs_no_extra_choice(self):
		job = Job(
			id="one",
			priority="S",
			company="公司",
			role="算法工程师",
			url="https://example.com",
			source_line=1,
		)
		self.assertIs(_choose_exact_role(job), job)

	def test_slashes_inside_one_role_are_not_split(self):
		job = Job(
			id="one",
			priority="A",
			company="公司",
			role="大模型算法研究员（Agent/RL/推理）",
			url="https://example.com",
			source_line=1,
		)
		self.assertIs(_choose_exact_role(job), job)


if __name__ == "__main__":
	unittest.main()
