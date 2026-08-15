"""Small data models shared by the CLI, graph, and browser adapter."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
	SELECTED = "selected"
	FILLING = "filling"
	INCOMPLETE = "incomplete"
	REVIEW = "review"
	SUBMITTED = "submitted"
	CANCELLED = "cancelled"
	FAILED = "failed"


class Job(BaseModel):
	id: str
	priority: str
	company: str
	role: str
	location: str = ""
	url: str
	reason: str = ""
	checked: bool = False
	source_line: int


class FillResult(BaseModel):
	"""Structured result returned by the browser agent before submission."""

	company: str = ""
	role: str = ""
	discovered_sections: list[str] = Field(
		default_factory=list,
		description="All form sections found during the initial top-to-bottom survey.",
	)
	reviewed_sections: list[str] = Field(
		default_factory=list,
		description="Sections processed and rechecked; unresolved fields must be listed as missing or manual.",
	)
	remaining_sections: list[str] = Field(
		default_factory=list,
		description="Discovered sections that have not yet been processed and rechecked.",
	)
	filled_fields: list[str] = Field(default_factory=list)
	missing_fields: list[str] = Field(default_factory=list)
	manual_fields: list[str] = Field(default_factory=list)
	warnings: list[str] = Field(default_factory=list)
	ready_for_review: bool = False

	def enforce_section_coverage(self) -> "FillResult":
		"""Derive completion from the section checklist instead of model confidence."""
		reviewed = {name.strip().casefold() for name in self.reviewed_sections if name.strip()}
		remaining = list(dict.fromkeys(name.strip() for name in self.remaining_sections if name.strip()))
		remaining_keys = {name.casefold() for name in remaining}
		for section in self.discovered_sections:
			name = section.strip()
			if name and name.casefold() not in reviewed and name.casefold() not in remaining_keys:
				remaining.append(name)
				remaining_keys.add(name.casefold())
		if not self.discovered_sections:
			remaining.append("表单区域盘点")

		coverage_complete = bool(self.discovered_sections) and not remaining
		warnings = list(self.warnings)
		if self.ready_for_review and not coverage_complete:
			warnings.append("区域覆盖检查未通过，运行未进入人工复核阶段。")
		return self.model_copy(
			update={
				"remaining_sections": remaining,
				"ready_for_review": self.ready_for_review and coverage_complete,
				"warnings": warnings,
			}
		)


class ApplicationRun(BaseModel):
	id: str
	job: Job
	status: RunStatus = RunStatus.SELECTED
	created_at: datetime = Field(default_factory=datetime.now)
	updated_at: datetime = Field(default_factory=datetime.now)
	result: FillResult | None = None
	error: str | None = None


class GraphState(TypedDict, total=False):
	run: ApplicationRun
	career_context: str
	form_memory: dict[str, Any]
	secret_names: list[str]
