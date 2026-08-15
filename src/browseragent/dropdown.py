"""Inspect dropdown DOM before choosing an interaction strategy."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class InspectDropdownParams(BaseModel):
	index: int = Field(description="Index of the closed or open dropdown control")


class DropdownInspection(BaseModel):
	kind: Literal["native_select", "custom_single", "cascader", "popup_picker", "unknown"]
	label: str = ""
	framework: str = "unknown"
	tag: str = ""
	role: str = ""
	expanded: bool = False
	option_count: int = 0
	option_samples: list[str] = Field(default_factory=list)
	requires_confirm: bool = False
	confirm_labels: list[str] = Field(default_factory=list)
	attributes: dict[str, str] = Field(default_factory=dict)
	html_excerpt: str = ""
	recommended_action: str
	warning: str = ""


class SmartSelectParams(BaseModel):
	index: int = Field(description="Index of the closed dropdown-like control")
	targets: list[str] = Field(
		min_length=1,
		max_length=4,
		description='Exact desired value or ordered path, e.g. ["硕士"] or ["北京", "西城区"]',
	)

	@field_validator("targets")
	@classmethod
	def clean_targets(cls, values: list[str]) -> list[str]:
		cleaned = [value.strip() for value in values]
		if any(not value for value in cleaned):
			raise ValueError("smart select targets cannot contain empty values")
		return cleaned


class SmartSelectResult(BaseModel):
	success: bool
	kind: str = "unknown"
	selected: list[str] = Field(default_factory=list)
	committed: bool = False
	detected_pattern: str = "unknown"
	open_method: str = "unknown"
	visible_options: list[str] = Field(default_factory=list)
	verification: str = ""
	actual_state: str = ""
	message: str = ""
	inspection: DropdownInspection | None = None


INSPECT_DROPDOWN_SCRIPT = r"""async () => {
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const clean = (value, max = 160) => String(value || '').replace(/[\s　]+/g, ' ').trim().slice(0, max);
  const optionSelector = [
    '[role="option"]', '[role="menuitem"]', '[role="treeitem"]',
    '.ant-cascader-menu-item', '.ant-select-item-option', '.el-cascader-node',
    '.el-select-dropdown__item', '.mtd-select-option', '.mtd-select-dropdown-item',
	'.mtd-select-item', '.mtd-select-item-content', '.mtd-dropdown-menu-item', '.mtd-cascader-menu-item', '.mtd-radio', '.mtd-checkbox',
    '[class*="select-option"]', '[class*="option-item"]', '[class*="cascader"] li', '[class*="radio"]', 'label'
  ].join(',');
  const textOf = (el) => clean(el?.innerText || el?.textContent);
	const triggerSelector = 'select, .mtd-select-filter, .mtd-select, .mtd-cascader, [role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"], [aria-controls], [aria-owns], .ant-select, .ant-cascader-picker, .el-select, .el-cascader, [class*="cascader"], [class*="picker"]';
	// MTD exposes the inner text input as an indexed element. Always promote it
	// to the complete filter control before opening or reading attributes.
	const mtdTrigger = this.closest?.('.mtd-select-filter, .mtd-select, .mtd-cascader');
	const trigger = mtdTrigger || (this.matches?.(triggerSelector) ? this : this.closest?.(triggerSelector) ||
	  this.parentElement?.querySelector?.(triggerSelector) || this);
	const attrs = Object.fromEntries([...trigger.attributes].filter(attr =>
	  ['id', 'name', 'class', 'role', 'type', 'aria-label', 'aria-haspopup', 'aria-expanded', 'aria-controls', 'aria-owns', 'data-testid'].includes(attr.name)
	).map(attr => [attr.name, clean(attr.value, 300)]));
	const explicit = trigger.id ? document.querySelector(`label[for="${CSS.escape(trigger.id)}"]`) : null;
	let outerLabel = null;
	for (let node = trigger.parentElement, depth = 1; node && node !== document.body && depth <= 7; node = node.parentElement, depth++) {
	  const candidates = [...node.querySelectorAll(':scope > label, :scope > [class*="label"], :scope > [class*="title"]')];
	  outerLabel = candidates.find(item => !item.contains(trigger) && item.innerText?.trim() &&
	    !/(mtd-select-filter-label|mtd-select-filter-hint)/i.test(String(item.className || '')) &&
	    !/^(请选择|请输入|选择|select|choose)$/i.test(item.innerText.trim()));
	  if (outerLabel) break;
	}
	const container = trigger.closest('[class*="form"], [class*="field"], [class*="item"], td, li, div');
	const labelCandidates = [explicit?.innerText, outerLabel?.innerText, trigger.getAttribute('aria-label'),
	  container?.querySelector('label, [class*="label"]')?.innerText, trigger.getAttribute('placeholder'), trigger.name]
	  .map(value => clean(value)).filter(Boolean);
	const label = labelCandidates.find(value => !/^(请选择|请输入|选择|select|choose)$/i.test(value)) || labelCandidates[0] || '';

	if (trigger.tagName === 'SELECT') {
	  const options = [...trigger.options].filter(item => item.value).map(textOf).filter(Boolean);
    return {kind: 'native_select', label, framework: 'native', tag: 'select', role: '', expanded: true,
      option_count: options.length, option_samples: options.slice(0, 30), requires_confirm: false,
	  confirm_labels: [], attributes: attrs, html_excerpt: clean(trigger.outerHTML, 1200),
	  recommended_action: 'Select the exact native option internally.'};
  }

	// Inspection is deliberately side-effect free. Opening here and then opening
	// again in the executor can toggle a menu closed. Only inspect an explicitly
	// ARIA-linked popup that is already visible; the adaptive executor owns the
	// complete open -> observe -> select -> verify transaction.
	const controlledId = trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns');
  const controlled = controlledId ? document.getElementById(controlledId) : null;
  let root = visible(controlled) ? controlled : null;
  let options = root ? [...root.querySelectorAll(optionSelector)].filter(visible) : [];
  if (root) options = [...root.querySelectorAll(optionSelector)].filter(visible);
  const optionTexts = [...new Set(options.map(textOf).filter(text => text && text.length <= 80))];
  const clickable = root ? [...root.querySelectorAll('button, [role="button"], a, .ant-btn, .el-button')] : [];
  const confirms = [...new Set(clickable.filter(visible).map(textOf).filter(text => ['确定', '确认'].includes(text)))];
	const classes = clean([trigger.className, container?.className, root?.className].join(' '), 500).toLowerCase();
  const framework = classes.includes('mtd-') ? 'meituan-mtd' : classes.includes('ant-') ? 'ant-design' : classes.includes('el-') ? 'element' :
    classes.includes('ivu-') ? 'view-ui' : classes.includes('van-') ? 'vant' : 'custom';
  const columns = root ? root.querySelectorAll('[class*="menu"], [class*="column"], [class*="panel"]').length : 0;
  const cascadeHint = /(cascader|province|city|district|region|area|省市|地区)/i.test(classes + ' ' + label) || columns > 1;
  const pickerHint = Boolean(root && confirms.length && options.length);
	const customHint = trigger !== this || trigger.getAttribute('role') === 'combobox' || trigger.getAttribute('role') === 'button' ||
	  trigger.hasAttribute('aria-haspopup') || /(select|dropdown|picker|choice)/i.test(classes);
  const kind = cascadeHint ? 'cascader' : pickerHint ? 'popup_picker' : options.length || customHint ? 'custom_single' : 'unknown';
  const recommended = kind === 'cascader' || kind === 'popup_picker'
    ? 'Call cascade_select on the original control index with the complete source-supported path; it will commit a scoped 确定/确认 button.'
    : kind === 'custom_single'
      ? 'Use an exact visible option from option_samples. Refresh page state, click that option once, and verify the control value.'
      : 'Do not guess. Leave this control for manual handling and report the DOM inspection as unknown.';
	return {kind, label, framework, tag: trigger.tagName.toLowerCase(), role: trigger.getAttribute('role') || '',
	expanded: trigger.getAttribute('aria-expanded') === 'true' || Boolean(root), option_count: optionTexts.length,
    option_samples: optionTexts.slice(0, 30), requires_confirm: confirms.length > 0,
	confirm_labels: confirms, attributes: attrs, html_excerpt: clean(trigger.outerHTML, 1200), recommended_action: recommended,
	    warning: options.length ? '' : 'Closed custom control; options will be discovered transactionally by the adaptive executor.'};
}"""


async def inspect_dropdown(browser_session, params: InspectDropdownParams) -> DropdownInspection:
	node = await browser_session.get_element_by_index(params.index)
	if node is None:
		return DropdownInspection(kind="unknown", recommended_action="Refresh page state; the requested element index is stale.", warning="Element is unavailable")
	from browser_use.actor.element import Element

	element = Element(browser_session, node.backend_node_id, node.session_id)
	try:
		raw = await element.evaluate(INSPECT_DROPDOWN_SCRIPT)
		return DropdownInspection.model_validate(json.loads(raw))
	except Exception as exc:
		return DropdownInspection(kind="unknown", recommended_action="Leave this control for manual handling.", warning=str(exc))


NATIVE_SELECT_SCRIPT = r"""(wanted) => {
  const normalize = (text) => String(text || '').replace(/[\s　]+/g, '').trim();
	const control = this.tagName === 'SELECT' ? this : this.closest?.('select') || this.parentElement?.querySelector?.('select');
	if (!control) return {success: false, error: 'resolved HTML does not contain a native select'};
	const candidates = [...control.options].filter(option => option.value &&
    (normalize(option.textContent) === normalize(wanted) || normalize(option.value) === normalize(wanted)));
  if (candidates.length !== 1) return {success: false,
    error: candidates.length ? `ambiguous native option: ${wanted}` : `native option not found: ${wanted}`};
  const option = candidates[0];
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
	setter ? setter.call(control, option.value) : (control.value = option.value);
	control.dispatchEvent(new Event('input', {bubbles: true}));
	control.dispatchEvent(new Event('change', {bubbles: true}));
	control.dispatchEvent(new Event('blur', {bubbles: true}));
	const selected = control.options[control.selectedIndex];
  const verified = selected && selected.value === option.value;
  return {success: verified, selected: verified ? selected.textContent.trim() : '',
    error: verified ? '' : 'native select value did not persist'};
}"""


async def smart_select(browser_session, params: SmartSelectParams) -> SmartSelectResult:
	"""Inspect, choose a strategy, select, commit, and verify in one tool call."""
	inspection = await inspect_dropdown(browser_session, InspectDropdownParams(index=params.index))
	if inspection.kind == "unknown":
		# Unknown framework is not a terminal diagnosis. The adaptive selector uses
		# DOM changes caused by opening the control and can safely discover a unique
		# option without knowing the component library in advance.
		from .cascade import select_cascade_path

		selection = await select_cascade_path(browser_session, params.index, params.targets)
		return SmartSelectResult(
			success=selection.success,
			kind=f"adaptive:{selection.detected_pattern}",
			selected=selection.selected,
			committed=selection.committed,
			detected_pattern=selection.detected_pattern,
			open_method=selection.open_method,
			visible_options=selection.visible_options,
			verification=selection.verification,
			actual_state=selection.actual_state,
			message=("Adaptive selection committed and verified" if selection.success else selection.error or inspection.warning),
			inspection=inspection,
		)
	if inspection.kind == "native_select":
		if len(params.targets) > 1:
			from .cascade import select_cascade_path

			selection = await select_cascade_path(browser_session, params.index, params.targets)
			return SmartSelectResult(
				success=selection.success,
				kind="native_cascade",
				selected=selection.selected,
				committed=selection.committed,
				open_method=selection.open_method,
				verification=selection.verification,
				actual_state=selection.actual_state,
				message=("Native cascade selected and verified" if selection.success else selection.error or "Selection failed"),
				inspection=inspection,
			)
		node = await browser_session.get_element_by_index(params.index)
		if node is None:
			return SmartSelectResult(success=False, kind=inspection.kind, message="Element index became stale", inspection=inspection)
		from browser_use.actor.element import Element

		element = Element(browser_session, node.backend_node_id, node.session_id)
		try:
			result = json.loads(await element.evaluate(NATIVE_SELECT_SCRIPT, params.targets[0]))
		except Exception as exc:
			return SmartSelectResult(success=False, kind=inspection.kind, message=str(exc), inspection=inspection)
		return SmartSelectResult(
			success=bool(result.get("success")),
			kind=inspection.kind,
			selected=[result["selected"]] if result.get("selected") else [],
			committed=bool(result.get("success")),
			message=result.get("error") or "Native option selected and verified",
			inspection=inspection,
		)

	if inspection.kind == "cascader" and len(params.targets) < 2:
		return SmartSelectResult(
			success=False,
			kind=inspection.kind,
			message="Cascader requires the complete multi-level path; one target is insufficient.",
			inspection=inspection,
		)
	from .cascade import select_cascade_path

	selection = await select_cascade_path(browser_session, params.index, params.targets)
	return SmartSelectResult(
		success=selection.success,
		kind=inspection.kind,
		selected=selection.selected,
		committed=selection.committed,
		detected_pattern=selection.detected_pattern,
		open_method=selection.open_method,
		visible_options=selection.visible_options,
		verification=selection.verification,
		actual_state=selection.actual_state,
		message=("Selection committed and verified" if selection.success else selection.error or "Selection failed"),
		inspection=inspection,
	)
