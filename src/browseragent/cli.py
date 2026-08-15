"""Readable command-line entry point for the application assistant."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid
from datetime import datetime

from .career_ops import CareerOpsStore, mask_secret
from .config import Settings
from .graph import build_graph
from .models import ApplicationRun, RunStatus

SECRET_LABELS = {
	"national_id": "身份证号码",
	"passport_number": "护照号码",
	"social_security_number": "社会安全号码",
}


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="browseragent", description="本地、人工提交的求职填表 Agent")
	sub = parser.add_subparsers(dest="command", required=True)
	sub.add_parser("jobs", help="列出未投岗位")
	apply = sub.add_parser("apply", help="选择并填写一个岗位")
	apply.add_argument("job_id", nargs="?")
	snapshot = sub.add_parser("snapshot", help="导出当前填报页的脱敏离线测试夹具")
	snapshot.add_argument("job_id", nargs="?")
	snapshot.add_argument(
		"--probe-dropdowns",
		action="store_true",
		help="逐个探测自定义下拉选项；优先纯 DOM，必要时使用一次真实打开操作",
	)
	resume = sub.add_parser("resume", help="重新运行未完成的填表任务")
	resume.add_argument("run_id")
	sub.add_parser("runs", help="列出申请运行记录")
	submitted = sub.add_parser("submitted", help="确认已由用户手动提交")
	submitted.add_argument("run_id")
	cancel = sub.add_parser("cancel", help="取消运行")
	cancel.add_argument("run_id")
	secrets = sub.add_parser("secrets", help="管理本地敏感字段")
	secrets.add_argument("action", choices=("set", "show", "delete"))
	secrets.add_argument("name", choices=tuple(SECRET_LABELS))
	return parser


def _print_jobs(store: CareerOpsStore) -> None:
	jobs = store.jobs()
	if not jobs:
		print("没有未投递岗位。")
		return
	for index, job in enumerate(jobs, 1):
		print(f"{index:>2}. [{job.id}] {job.priority} | {job.company} | {job.role} | {job.location}")
		if job.reason:
			print(f"    {job.reason}")


def _choose_job(store: CareerOpsStore, job_id: str | None):
	jobs = store.jobs()
	if job_id:
		matches = [job for job in jobs if job.id == job_id]
		if not matches:
			raise ValueError(f"未找到待投岗位: {job_id}")
		return matches[0]
	_print_jobs(store)
	choice = input("\n请选择岗位序号: ").strip()
	if not choice.isdigit() or not 1 <= int(choice) <= len(jobs):
		raise ValueError("岗位序号无效")
	return jobs[int(choice) - 1]


def _choose_exact_role(job):
	# The recommendation file uses spaced slashes between roles. Unspaced slashes
	# such as Agent/RL/推理 are part of one role name and must stay intact.
	roles = [part.strip() for part in job.role.split(" / ") if part.strip()]
	if len(roles) <= 1:
		return job
	print("\n该推荐项包含多个岗位，请先选择本次具体填报岗位：")
	for index, role in enumerate(roles, 1):
		print(f"  {index}. {role}")
	choice = input("请选择岗位序号: ").strip()
	if not choice.isdigit() or not 1 <= int(choice) <= len(roles):
		raise ValueError("具体岗位序号无效")
	return job.model_copy(update={"role": roles[int(choice) - 1]})


def _confirm_job(job) -> bool:
	print("\n请确认本次填报岗位：")
	print(f"  公司：{job.company}\n  岗位：{job.role}\n  地点：{job.location}\n  链接：{job.url}")
	return input("输入 APPLY 确认启动浏览器，其他输入取消: ").strip() == "APPLY"


def _confirm_snapshot(job) -> bool:
	print("\n将打开浏览器并导出脱敏离线夹具，不会填写或提交：")
	print(f"  公司：{job.company}\n  岗位：{job.role}\n  链接：{job.url}")
	return input("输入 CAPTURE 确认启动，其他输入取消: ").strip() == "CAPTURE"


def _capture_missing_fields(store: CareerOpsStore, run: ApplicationRun) -> None:
	if not run.result or not run.result.missing_fields:
		return
	print("\n可以现在补充缺失信息；直接回车表示跳过。保存的值会供后续表单复用。")
	saved = 0
	for field in run.result.missing_fields:
		lower_field = field.casefold()
		is_national_id = any(token in lower_field for token in ("身份证", "证件号", "national id", "identity card"))
		if is_national_id:
			value = getpass.getpass(f"{field}: ").strip()
			if not value:
				continue
			if input("保存身份证号码到本地秘密文件？输入 SAVE: ").strip() == "SAVE":
				store.set_secret("national_id", value)
				saved += 1
			continue
		value = input(f"{field}: ").strip()
		if not value:
			continue
		if input("保存为后续申请默认值？输入 SAVE: ").strip() == "SAVE":
			store.save_memory_value(field, value)
			saved += 1
	if saved:
		print(f"已保存 {saved} 项。当前页面可人工补齐；后续运行会自动复用。")


def _print_result(store: CareerOpsStore, run: ApplicationRun) -> None:
	result = run.result
	if run.status == RunStatus.REVIEW:
		print(f"\n运行 {run.id} 已停在人工复核阶段。Agent 没有、也不能点击最终提交。")
	else:
		print(f"\n运行 {run.id} 尚未覆盖完整表单，当前进度已保存。")
	if not result:
		return
	for title, values in (
		("发现的表单区域", result.discovered_sections),
		("已复核区域", result.reviewed_sections),
		("剩余区域", result.remaining_sections),
		("已填写", result.filled_fields),
		("缺失信息", result.missing_fields),
		("需人工处理", result.manual_fields),
		("警告", result.warnings),
	):
		if not values:
			continue
		print(f"\n{title}：")
		for value in values:
			print(f"  - {value}")
	_capture_missing_fields(store, run)
	if run.status == RunStatus.REVIEW:
		print(f"\n人工提交成功后运行：browseragent submitted {run.id}")
	else:
		print(f"\n继续未完成区域：browseragent resume {run.id}")


async def _run_application(settings: Settings, store: CareerOpsStore, run: ApplicationRun) -> None:
	settings.validate(require_llm=True)
	store.save_run(run)
	graph = build_graph(settings, store)
	try:
		state = await graph.ainvoke({"run": run})
		_print_result(store, state["run"])
	except asyncio.CancelledError:
		run.status = RunStatus.FAILED
		run.error = "运行被用户中断，可使用 resume 继续"
		run.updated_at = datetime.now()
		store.save_run(run)
		raise
	except Exception as exc:
		run.status = RunStatus.FAILED
		run.error = str(exc)
		run.updated_at = datetime.now()
		store.save_run(run)
		raise


def _list_runs(store: CareerOpsStore) -> None:
	if not store.runs_path.exists():
		print("暂无运行记录。")
		return
	for path in sorted(store.runs_path.glob("*.json"), reverse=True):
		try:
			run = store.load_run(path.stem)
			print(f"{run.id} | {run.status.value:<9} | {run.job.company} | {run.job.role}")
		except Exception:
			print(f"{path.stem} | 无法读取")


def _manage_secret(store: CareerOpsStore, action: str, name: str) -> None:
	if action == "set":
		label = SECRET_LABELS[name]
		value = getpass.getpass(f"请输入{label}（输入不会回显）: ").strip()
		if not value:
			raise ValueError(f"{label}不能为空")
		confirm = getpass.getpass("请再次输入确认: ").strip()
		if value != confirm:
			raise ValueError("两次输入不一致")
		store.set_secret(name, value)
		print(f"已保存：{mask_secret(value)}")
	elif action == "show":
		value = store.get_secret(name)
		print(mask_secret(value) if value else "未保存")
	else:
		print("已删除" if store.delete_secret(name) else "原本未保存")


async def _async_main(args: argparse.Namespace, settings: Settings, store: CareerOpsStore) -> None:
	if args.command == "snapshot":
		job = _choose_exact_role(_choose_job(store, args.job_id))
		if not _confirm_snapshot(job):
			print("已取消，浏览器未启动。")
			return
		from browser_use import BrowserSession
		from .browser import build_browser_profile, wait_for_snapshot_handoff
		from .fixture import capture_form_fixture

		settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
		session = BrowserSession(browser_profile=build_browser_profile(settings, keep_alive=False))
		try:
			if not await wait_for_snapshot_handoff(session, job):
				print("已取消快照采集。")
				return
			output = await capture_form_fixture(
				session,
				settings.state_path.parent / "fixtures",
				probe_dropdowns=args.probe_dropdowns,
			)
			print(f"脱敏夹具已保存：{output}")
			print(f"  页面：{output / 'page.html'}")
			print(f"  结构：{output / 'form.json'}")
		finally:
			await session.stop()
	elif args.command == "apply":
		job = _choose_exact_role(_choose_job(store, args.job_id))
		if not _confirm_job(job):
			print("已取消，浏览器未启动。")
			return
		run = ApplicationRun(id=uuid.uuid4().hex[:12], job=job)
		print("填表流程：页面顺序代码填写（DOM → CDP）+ Agent fallback")
		await _run_application(settings, store, run)
	elif args.command == "resume":
		run = store.load_run(args.run_id)
		if run.status in {RunStatus.SUBMITTED, RunStatus.CANCELLED}:
			raise ValueError(f"运行状态为 {run.status.value}，不能恢复")
		if not _confirm_job(run.job):
			print("已取消恢复。")
			return
		print("填表流程：页面顺序代码填写（DOM → CDP）+ Agent fallback")
		await _run_application(settings, store, run)


def main() -> None:
	args = _parser().parse_args()
	settings = Settings.from_env()
	store = CareerOpsStore(settings.career_ops_path)
	try:
		settings.validate()
		if args.command == "jobs":
			_print_jobs(store)
		elif args.command == "runs":
			_list_runs(store)
		elif args.command == "secrets":
			_manage_secret(store, args.action, args.name)
		elif args.command == "submitted":
			run = store.load_run(args.run_id)
			if run.status != RunStatus.REVIEW:
				raise ValueError("只有 review 状态的运行可以确认提交")
			if input("确认你已在浏览器中手动提交成功？输入 SUBMITTED: ").strip() != "SUBMITTED":
				print("未确认，不更新任何记录。")
				return
			store.mark_submitted(run)
			run.status = RunStatus.SUBMITTED
			run.updated_at = datetime.now()
			store.save_run(run)
			print("已记录投递成功并更新推荐清单。")
		elif args.command == "cancel":
			run = store.load_run(args.run_id)
			run.status = RunStatus.CANCELLED
			run.updated_at = datetime.now()
			store.save_run(run)
			print("运行已取消，审计记录已保留。")
		else:
			asyncio.run(_async_main(args, settings, store))
	except (ValueError, FileNotFoundError) as exc:
		print(f"错误：{exc}", file=sys.stderr)
		raise SystemExit(2) from exc


if __name__ == "__main__":
	main()
