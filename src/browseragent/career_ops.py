"""Read and update only career-ops user-layer files."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ApplicationRun, Job

RECOMMENDATIONS = "data/2027-autumn-recommendations.md"
JOB_LINE = re.compile(
	r"^- \[(?P<checked>[ xX])\] \*\*(?P<company>[^｜*]+)｜(?P<role>[^*]+)\*\*"
	r"(?:｜(?P<location>[^｜\[]+))?｜\[(?P<link_text>[^]]+)\]\((?P<url>[^)]+)\)"
)


def _job_id(company: str, role: str, url: str) -> str:
	raw = f"{company.strip()}|{role.strip()}|{url.strip()}".encode()
	return hashlib.sha256(raw).hexdigest()[:10]


def parse_recommendations(text: str) -> list[Job]:
	jobs: list[Job] = []
	priority = "未分组"
	lines = text.splitlines()
	for index, line in enumerate(lines):
		if line.startswith("## "):
			priority = line[3:].split("｜", 1)[0].strip()
		match = JOB_LINE.match(line)
		if not match:
			continue
		parts = match.groupdict()
		reason = ""
		if index + 1 < len(lines):
			next_line = lines[index + 1].strip()
			if next_line.startswith("- 匹配："):
				reason = next_line.removeprefix("- 匹配：").strip()
		company = parts["company"].strip()
		role = parts["role"].strip()
		url = parts["url"].strip()
		jobs.append(
			Job(
				id=_job_id(company, role, url),
				priority=priority,
				company=company,
				role=role,
				location=(parts["location"] or "").strip(),
				url=url,
				reason=reason,
				checked=parts["checked"].lower() == "x",
				source_line=index + 1,
			)
		)
	return jobs


class CareerOpsStore:
	def __init__(self, root: Path):
		self.root = root
		self.recommendations_path = root / RECOMMENDATIONS
		self.memory_path = root / "data/form-memory.json"
		self.secrets_path = root / "data/form-secrets.json"
		self.runs_path = root / "data/application-runs"

	def jobs(self, *, pending_only: bool = True) -> list[Job]:
		jobs = parse_recommendations(self.recommendations_path.read_text(encoding="utf-8"))
		return [job for job in jobs if not job.checked] if pending_only else jobs

	def context(self) -> str:
		parts: list[str] = []
		for relative in ("cv.md", "config/profile.yml", "modes/_profile.md", "modes/_custom.md"):
			path = self.root / relative
			if path.exists():
				parts.append(f"\n--- {relative} ---\n{path.read_text(encoding='utf-8')}")
		return "".join(parts)

	def form_memory(self) -> dict[str, Any]:
		return self._read_json(self.memory_path)

	def save_memory_value(self, key: str, value: str) -> None:
		data = self.form_memory()
		data[key] = {"value": value, "source": "user", "updated_at": datetime.now().isoformat(timespec="seconds")}
		self._write_json(self.memory_path, data)

	def secret_names(self) -> list[str]:
		return sorted(self._read_json(self.secrets_path))

	def get_secret(self, name: str) -> str | None:
		value = self._read_json(self.secrets_path).get(name)
		return str(value) if value else None

	def set_secret(self, name: str, value: str) -> None:
		data = self._read_json(self.secrets_path)
		data[name] = value
		self._write_json(self.secrets_path, data, private=True)

	def delete_secret(self, name: str) -> bool:
		data = self._read_json(self.secrets_path)
		removed = data.pop(name, None) is not None
		if removed:
			self._write_json(self.secrets_path, data, private=True)
		return removed

	def save_run(self, run: ApplicationRun) -> Path:
		path = self.runs_path / f"{run.id}.json"
		self._write_json(path, run.model_dump(mode="json"))
		return path

	def load_run(self, run_id: str) -> ApplicationRun:
		return ApplicationRun.model_validate(self._read_json(self.runs_path / f"{run_id}.json"))

	def mark_submitted(self, run: ApplicationRun) -> None:
		lines = self.recommendations_path.read_text(encoding="utf-8").splitlines()
		line_index = run.job.source_line - 1
		if line_index >= len(lines) or run.job.url not in lines[line_index]:
			raise ValueError("推荐清单已变化，无法安全定位原岗位；未做修改")
		lines[line_index] = lines[line_index].replace("- [ ]", "- [x]", 1)
		for index in range(line_index + 1, min(line_index + 5, len(lines))):
			if lines[index].strip().startswith("- 进展："):
				lines[index] = f"  - 进展：{datetime.now().date().isoformat()} 已投递"
				break
		self.recommendations_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

	@staticmethod
	def _read_json(path: Path) -> dict[str, Any]:
		if not path.exists():
			return {}
		return json.loads(path.read_text(encoding="utf-8"))

	@staticmethod
	def _write_json(path: Path, value: Any, *, private: bool = False) -> None:
		path.parent.mkdir(parents=True, exist_ok=True)
		temporary = path.with_suffix(path.suffix + ".tmp")
		temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
		if private:
			os.chmod(temporary, 0o600)
		temporary.replace(path)
		if private:
			os.chmod(path, 0o600)


def mask_secret(value: str) -> str:
	if len(value) <= 6:
		return "*" * len(value)
	return f"{value[:3]}{'*' * (len(value) - 7)}{value[-4:]}"
