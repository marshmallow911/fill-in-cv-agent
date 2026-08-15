"""Configuration is read once here so business code stays deterministic."""

from __future__ import annotations

import os
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_local_model_config(project_dir: Path) -> tuple[dict, str | None]:
	"""Read the optional Codex-style local provider config and API key."""
	config_path = project_dir / ".codex-local" / "config.toml"
	auth_path = project_dir / ".codex-local" / "auth.json"
	config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
	api_key = None
	if auth_path.is_file():
		auth = json.loads(auth_path.read_text(encoding="utf-8"))
		api_key = auth.get("OPENAI_API_KEY") or None
	return config, api_key


def _env_int(name: str, default: int) -> int:
	value = os.getenv(name)
	if value is None:
		return default
	try:
		return int(value)
	except ValueError as exc:
		raise ValueError(f"{name} 必须是整数") from exc


@dataclass(frozen=True)
class Settings:
	career_ops_path: Path
	state_path: Path
	browser_profile_path: Path
	llm_provider: str
	llm_model: str
	llm_api_key: str | None
	llm_base_url: str | None
	reasoning_effort: str
	llm_wire_api: str = "chat_completions"
	model_verbosity: str = "medium"
	disable_response_storage: bool = True
	mapping_parallelism: int = 3

	@classmethod
	def from_env(cls) -> "Settings":
		project_dir = Path(__file__).resolve().parents[2]
		local_dir = project_dir / ".browseragent"
		load_dotenv(project_dir / ".env", override=False)
		local_model, local_api_key = _load_local_model_config(project_dir)
		provider_name = str(local_model.get("model_provider", "openai"))
		provider = local_model.get("model_providers", {}).get(provider_name, {})
		return cls(
			career_ops_path=Path(os.getenv("CAREER_OPS_PATH", "~/career-ops")).expanduser(),
			state_path=local_dir / "runs",
			browser_profile_path=local_dir / "browser-profile",
			llm_provider=os.getenv("BROWSERAGENT_LLM_PROVIDER") or os.getenv("MODEL_PROVIDER") or provider_name,
			llm_model=os.getenv("BROWSERAGENT_LLM_MODEL") or os.getenv("MODEL_NAME") or str(local_model.get("model", "gpt-5-mini")),
			llm_api_key=os.getenv("BROWSERAGENT_LLM_API_KEY") or os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY") or local_api_key,
			llm_base_url=os.getenv("BROWSERAGENT_LLM_BASE_URL") or os.getenv("MODEL_BASE_URL") or os.getenv("OPENAI_BASE_URL") or provider.get("base_url"),
			reasoning_effort=os.getenv("BROWSERAGENT_REASONING_EFFORT") or os.getenv("OPENAI_REASONING_EFFORT") or str(local_model.get("model_reasoning_effort", "low")),
			llm_wire_api=os.getenv("BROWSERAGENT_LLM_WIRE_API") or str(provider.get("wire_api", "chat_completions")),
			model_verbosity=os.getenv("BROWSERAGENT_MODEL_VERBOSITY") or str(local_model.get("model_verbosity", "medium")),
			disable_response_storage=bool(local_model.get("disable_response_storage", True)),
			mapping_parallelism=_env_int("BROWSERAGENT_MAPPING_PARALLELISM", 3),
		)

	def validate(self, *, require_llm: bool = False) -> None:
		if not self.career_ops_path.is_dir():
			raise ValueError(f"career-ops 路径不存在: {self.career_ops_path}")
		if not self.llm_base_url:
			raise ValueError(f"缺少 provider {self.llm_provider} 的 base_url")
		if self.llm_wire_api not in {"chat_completions", "responses"}:
			raise ValueError("wire_api 必须是 chat_completions 或 responses")
		if self.reasoning_effort not in {"low", "medium", "high", "xhigh"}:
			raise ValueError("reasoning effort 必须是 low、medium、high 或 xhigh")
		if self.model_verbosity not in {"low", "medium", "high"}:
			raise ValueError("model verbosity 必须是 low、medium 或 high")
		if not 1 <= self.mapping_parallelism <= 4:
			raise ValueError("BROWSERAGENT_MAPPING_PARALLELISM 必须在 1 到 4 之间")
		if require_llm and not self.llm_api_key:
			raise ValueError("缺少 MODEL_API_KEY（也兼容 BROWSERAGENT_LLM_API_KEY 或 OPENAI_API_KEY）")
