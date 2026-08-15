"""The application workflow. Nodes stay thin and call focused adapters."""

from __future__ import annotations

from datetime import datetime

from langgraph.graph import END, START, StateGraph

from .browser import fill_application
from .career_ops import CareerOpsStore
from .config import Settings
from .models import GraphState, RunStatus


def build_graph(settings: Settings, store: CareerOpsStore):
	async def load_context(state: GraphState) -> GraphState:
		return {
			"career_context": store.context(),
			"form_memory": store.form_memory(),
			"secret_names": store.secret_names(),
		}

	async def fill_form(state: GraphState) -> GraphState:
		run = state["run"]
		run.status = RunStatus.FILLING
		run.error = None
		run.updated_at = datetime.now()
		store.save_run(run)
		secrets = {name: store.get_secret(name) or "" for name in state.get("secret_names", [])}
		result = await fill_application(
			settings,
			run.job,
			state.get("career_context", ""),
			state.get("form_memory", {}),
			secrets,
			run_id=run.id,
		)
		run.result = result
		run.status = RunStatus.REVIEW if result.ready_for_review else RunStatus.INCOMPLETE
		run.error = None
		run.updated_at = datetime.now()
		store.save_run(run)
		return {"run": run}

	graph = StateGraph(GraphState)
	graph.add_node("load_context", load_context)
	graph.add_node("fill_form", fill_form)
	graph.add_edge(START, "load_context")
	graph.add_edge("load_context", "fill_form")
	graph.add_edge("fill_form", END)
	return graph.compile()
