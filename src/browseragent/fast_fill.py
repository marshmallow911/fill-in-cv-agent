"""Code-first filling for text, native select, and recognized custom controls.

The page is scanned once, bounded read-only solvers map independent form blocks,
then validated writes run against live controls and are verified. Ambiguous or
unsuccessful controls are intentionally left to the browser agent.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field


BLOCKED_LABELS = re.compile(
	r"身份证|证件号|护照|社会安全|政治面貌|党员|民族|种族|宗教|婚姻|残疾|退伍|"
	r"薪资|工资|期望薪酬|当前薪酬|授权|签证|工作许可|竞业|犯罪|背景调查|"
	r"national\s*id|identity|passport|social\s*security|race|ethnicity|religion|"
	r"marital|disability|veteran|salary|compensation|work\s*authori[sz]ation|visa|criminal",
	re.IGNORECASE,
)

SENSITIVE_VALUE_PATTERNS = {
	# Match number/value controls only. Type selectors such as “证件类型” are
	# handled separately and must never receive the secret itself.
	"national_id": re.compile(
		r"(?:居民)?身份证(?:号码|号|编号)|证件(?:号码|号|编号)|"
		r"national\s*(?:id|identity)(?:\s*(?:number|no\.?|#))?|"
		r"identity\s*(?:card\s*)?(?:number|no\.?|#)",
		re.IGNORECASE,
	),
	"passport_number": re.compile(r"护照(?:号码|号|编号)|passport\s*(?:number|no\.?|#)", re.IGNORECASE),
	"social_security_number": re.compile(
		r"社会安全(?:号码|号)|社保(?:号码|号)|social\s*security\s*(?:number|no\.?|#)|\bssn\b",
		re.IGNORECASE,
	),
}

CASCADE_LABELS = re.compile(
	r"户籍|籍贯|生源地|所在地|省份|城市|市区|区县|行政区|地区|province|city|district|region",
	re.IGNORECASE,
)

BLOCK_PATTERNS = {
	"education": re.compile(r"教育|学校|院校|学历|学位|专业|毕业|education|school|university|degree|major", re.I),
	"experience": re.compile(r"实习|工作|任职|公司|单位|职位|职务|在职|experience|employment|employer|company|position", re.I),
	"project": re.compile(r"项目|作品|课题|研究|成果|职责|描述|project|research|portfolio", re.I),
	"basic": re.compile(r"基本|个人|联系|姓名|手机|电话|邮箱|微信|生日|出生|姓名|name|phone|mobile|email|contact|birthday", re.I),
}

BLOCK_NAMES = {
	"basic": "基本信息",
	"education": "教育经历",
	"experience": "实习/工作经历",
	"project": "项目/研究经历",
	"other": "其他标准字段",
	"merged": "其他标准字段",
	"all": "当前页标准字段",
}

YEAR_MONTH_VALUE = re.compile(r"^\d{4}[/-](?:0?[1-9]|1[0-2])$")
DATE_FIELD_LABEL = re.compile(r"日期|时间|年月|入学|毕业|开始|结束|出生|date|time|month|year", re.I)
DEGREE_FIELD_LABEL = re.compile(r"学历|学位|degree|education\s*level", re.I)
DEGREE_VALUE = re.compile(r"博士|硕士|本科|学士|专科|大专|高中|中专|初中|小学|doctor|ph\.?d|master|bachelor|associate", re.I)
LANGUAGE_LEVEL_LABEL = re.compile(r"语言(?:水平|等级|能力)|外语(?:水平|等级|能力)|language\s*(?:level|proficiency)", re.I)


def _semantic_assignment_error(field: "FastField", value: str) -> str:
	"""Reject type-confused LLM mappings before they can touch the page."""
	clean = value.strip()
	if YEAR_MONTH_VALUE.fullmatch(clean) and field.kind != "custom_date" and not DATE_FIELD_LABEL.search(field.label):
		return "year_month_assigned_to_non_date_field"
	if field.kind == "custom_date" and not YEAR_MONTH_VALUE.fullmatch(clean):
		return "date_field_requires_year_month"
	if DEGREE_FIELD_LABEL.search(field.label) and not DEGREE_VALUE.search(clean):
		return "non_degree_value_assigned_to_degree_field"
	if field.kind in {"select", "custom_select"} and LANGUAGE_LEVEL_LABEL.search(field.label) and re.fullmatch(r"\d+(?:\.\d+)?", clean):
		return "numeric_score_assigned_to_language_level"
	if field.kind == "ongoing_checkbox" and clean.casefold() not in {"true", "false"}:
		return "ongoing_checkbox_requires_boolean"
	return ""


class FastField(BaseModel):
	id: str
	label: str
	kind: str
	dom_order: int = 0
	top: float = 0
	left: float = 0
	disabled: bool = False
	options: list[str] = Field(default_factory=list)
	section: str = ""
	card_context: str = ""
	card_type: str = ""
	card_index: int = 0
	card_count: int = 0
	card_signature: str = ""
	current_value: str = ""


class FastAssignment(BaseModel):
	field_id: str
	value: str = ""
	targets: list[str] = Field(default_factory=list, max_length=4)


class FastAssignmentBatch(BaseModel):
	assignments: list[FastAssignment] = Field(default_factory=list)


def _hydrate_card_contexts(fields: list[FastField]) -> None:
	"""Rebuild repeated-card context from the complete scan inventory.

	DOM-local extraction can miss sibling values when a framework nests a custom
	control in an extra form-item wrapper. Grouping the already scanned controls by
	their structural card identity is framework-independent and gives every empty
	control the same record evidence.
	"""
	groups: dict[tuple[str, int, int], list[FastField]] = {}
	for field in fields:
		if field.card_type and field.card_index > 0:
			groups.setdefault((field.card_type, field.card_index, field.card_count), []).append(field)
	for values in groups.values():
		for field in values:
			facts: list[str] = []
			for sibling in values:
				if sibling.id == field.id or not sibling.current_value or BLOCKED_LABELS.search(sibling.label):
					continue
				facts.append(f"{sibling.label[:80]}={sibling.current_value[:160]}")
				if len(facts) >= 6:
					break
			structural_context = "；".join(facts)
			if structural_context:
				combined = "；".join(part for part in (field.card_context, structural_context) if part)
				field.card_context = combined[:800]


@dataclass
class FastFillTracker:
	filled_labels: list[str] = field(default_factory=list)
	deferred_labels: list[str] = field(default_factory=list)
	pages_scanned: int = 0
	warnings: list[str] = field(default_factory=list)
	last_plan: list[str] = field(default_factory=list)
	solver_calls: int = 0
	code_passes: list[dict] = field(default_factory=list)
	resume_inventory: dict = field(default_factory=dict)
	section_preparation: list[dict] = field(default_factory=list)
	structure_error: str = ""
	current_stage: str = "initialized"


SCAN_SCRIPT = r"""() => {
	const reviewFilled = window.__browseragentReviewFilled === true;
	delete window.__browseragentReviewFilled;
  // IDs are valid for one scan only. Dependency selections can reveal controls
  // and renumber every later field; leaving an old ID behind creates duplicate
  // selectors and can route a second-pass write into a completely different field.
  document.querySelectorAll('[data-browseragent-fast-id]').forEach(el =>
    el.removeAttribute('data-browseragent-fast-id'));
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => {
    const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const wrapping = el.closest('label');
	const formItem = el.closest('[class*="form-item"], [class*="formItem"], [class*="field-item"], [class*="fieldItem"], td, li');
	const structuredLabel = formItem?.querySelector(
	  ':scope > label, :scope > [class*="label"], label, [class*="form-item-label"], [class*="field-label"]'
	);
	let ancestorLabel = null;
	for (let node = el.parentElement, depth = 1; node && node !== document.body && depth <= 7; node = node.parentElement, depth++) {
	  const controls = node.querySelectorAll('input, textarea, select');
	  if (controls.length > 4) break;
	  const candidates = [...node.querySelectorAll(':scope > label, :scope > [class*="label"], :scope > [class*="title"]')];
	  const candidate = candidates.find(item => item !== el && !item.contains(el) && item.innerText?.trim() &&
	    !/(mtd-select-filter-label|mtd-select-filter-hint)/i.test(String(item.className || '')) &&
	    !/^(请选择|请输入|选择|select|choose)$/i.test(item.innerText.trim()));
	  if (candidate) { ancestorLabel = candidate; break; }
	}
	const candidates = [explicit?.innerText, structuredLabel?.innerText, ancestorLabel?.innerText,
	  el.getAttribute('aria-label'), wrapping?.innerText, el.getAttribute('placeholder'), el.name]
	  .map(text => String(text || '').trim()).filter(Boolean);
	const informative = candidates.find(text => !/^(请选择|请输入|选择|select|choose)$/i.test(text));
	return (informative || candidates[0] || '').slice(0, 240);
  };
	const controlsIn = node => [...node.querySelectorAll('input, textarea, select')].filter(visible)
	  .filter(item => !['hidden', 'password', 'file'].includes((item.type || '').toLowerCase()));
	const deletePattern = /删除(这段|该条|本条)?(经历|信息|记录)?|remove|delete/i;
	const clickable = node => [...node.querySelectorAll('button, [role="button"], a')].filter(visible);
	const stableFieldKey = item => (item.getAttribute('placeholder') || item.getAttribute('aria-label') || item.name || '')
	  .trim().replace(/\[\d+\]|\d+/g, '#');
	const signatureFor = node => [...new Set(controlsIn(node).map(item =>
	  stableFieldKey(item)
	).filter(Boolean))].sort().join('|');
	const isRepeatedSibling = node => {
	  if (!node.parentElement) return false;
	  const signature = signatureFor(node);
	  if (!signature || controlsIn(node).length < 2) return false;
	  return [...node.parentElement.children].filter(sibling =>
	    sibling !== node && controlsIn(sibling).length >= 2 && signatureFor(sibling) === signature
	  ).length > 0;
	};
	const cardFor = el => {
	  for (let node = el.parentElement, depth = 1; node && node !== document.body && depth <= 9;
	       node = node.parentElement, depth++) {
	    const controls = controlsIn(node);
	    if (controls.length < 2 || controls.length > 40) continue;
	    const deletes = clickable(node).filter(item => deletePattern.test((item.innerText || item.textContent || '').trim()));
	    if (deletes.length === 1 || isRepeatedSibling(node)) return node;
	  }
	  return null;
	};
	const cardTypeFor = signature => {
	  const definitions = {
	    education: /学校|院校|学院|专业|学历|学位|入学|毕业|education|school|university|college|degree|major/i,
	    experience: /公司|单位|部门|职位|职务|工作描述|任职|入职|离职|company|employer|department|position|job|work/i,
	    project: /项目名称|项目角色|项目描述|项目链接|project|portfolio/i,
	    publication: /论文|著作|期刊|会议|作者|发表|publication|paper|journal|conference|author/i,
	    patent: /专利|专利号|发明|patent|invention/i,
	    competition: /竞赛|比赛|赛事|competition|contest/i,
	  };
	  return Object.entries(definitions).find(([, pattern]) => pattern.test(signature))?.[0] || '';
	};
	const cardMetaFor = el => {
	  const card = cardFor(el);
	  if (!card) return {context: '', type: '', index: 0, count: 0, signature: ''};
	  const signature = signatureFor(card).slice(0, 500);
	  const siblings = [...card.parentElement.children].filter(node =>
	    controlsIn(node).length >= 2 && signatureFor(node) === signature
	  );
	  const index = siblings.indexOf(card);
	  const sensitive = /身份证|证件号|护照|手机|电话|邮箱|性别|薪资|工资|national\s*id|identity|passport|phone|mobile|email|gender|salary/i;
	  const facts = [];
	  for (const item of controlsIn(card)) {
	    if (item === el) continue;
	    const label = labelFor(item);
	    if (!label || sensitive.test(label)) continue;
	    const inputType = String(item.type || '').toLowerCase();
	    const value = ['checkbox', 'radio'].includes(inputType)
	      ? (item.checked ? 'true' : '')
	      : item.tagName === 'SELECT'
	        ? (item.selectedOptions?.[0]?.textContent || '').trim()
	        : String(item.value || '').trim();
	    if (!value || /^(请选择|请输入|select|choose)$/i.test(value)) continue;
	    facts.push(`${label.slice(0, 80)}=${value.slice(0, 120)}`);
	    if (facts.length >= 5) break;
	  }
	  return {
	    context: facts.join('；').slice(0, 600),
	    type: cardTypeFor(signature),
	    index: index >= 0 ? index + 1 : 0,
	    count: siblings.length > 1 ? siblings.length : 0,
	    signature,
	  };
	};
	const sectionFor = (el) => {
	  const fieldset = el.closest('fieldset');
	  if (fieldset?.querySelector(':scope > legend')) return fieldset.querySelector(':scope > legend').innerText.trim().slice(0, 120);
	  const section = el.closest('section, [role="group"], [class*="section"], [class*="module"], [class*="block"]');
	  const heading = section?.querySelector('h1, h2, h3, h4, h5, h6, [class*="title"], [class*="header"]');
	  if (heading?.innerText) return heading.innerText.trim().slice(0, 120);
	  const preceding = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6')]
	    .filter(item => item.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING).pop();
	  return preceding?.innerText?.trim().slice(0, 120) || '';
	};
	const compositeSelector = '.mtd-select-filter, .mtd-select, .mtd-cascader, [role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"], [class*="cascader"], [class*="picker"]';
	const customControls = () => {
	  const raw = [...document.querySelectorAll(compositeSelector)].filter(visible);
	  return raw.filter(el => !raw.some(parent => parent !== el && parent.contains(el) &&
	    parent.matches('.mtd-select-filter, .mtd-select, .mtd-cascader')));
	};
	const customCurrent = el => {
	  const inputValue = String(el.matches?.('input') ? el.value : el.querySelector?.('input')?.value || '').trim();
	  if (inputValue) return inputValue;
	  // MTD uses descriptive hints such as “请选择证件类型”. They are
	  // placeholders, not selected values. Checking the hint class first avoids
	  // silently dropping these controls from the empty-field inventory.
	  const hint = el.querySelector('[class*="hint"], [data-placeholder]');
	  if (hint && visible(hint) && String(hint.innerText || hint.textContent || hint.getAttribute('data-placeholder') || '').trim()) return '';
	  const selected = el.querySelector('[aria-selected="true"], [class*="selected"], [class*="value"], [class*="label"]:not([class*="hint"])');
	  const text = String(selected?.innerText || selected?.textContent || '').trim();
	  return /^(请选择|请输入|选择|select|choose)(?:.{0,30})?$/i.test(text) ? '' : text;
	};
  let index = 0;
	const positionFor = el => {
	  const rect = el.getBoundingClientRect();
	  return {top: rect.top + window.scrollY, left: rect.left + window.scrollX};
	};
	const nativeFields = [...document.querySelectorAll('input, textarea, select')].flatMap((el) => {
    const type = (el.type || el.tagName).toLowerCase();
    if (!visible(el) || el.readOnly ||
        ['hidden', 'file', 'submit', 'button', 'reset', 'image', 'checkbox', 'radio', 'password'].includes(type)) return [];
	// Inputs inside a composite picker are implementation details, not ordinary
	// text fields. Writing their DOM value bypasses the widget state and is not
	// reliable, so defer the complete control to the adaptive fallback executor.
	const composite = el.closest('[role="combobox"], [aria-haspopup], [aria-controls], [aria-owns], [class*="cascader"], [class*="picker"], [class*="dropdown"], [class*="select-filter"]');
	if (el.tagName !== 'SELECT' && composite && composite !== el) return [];
    const current = el.tagName === 'SELECT' ? el.value : el.value?.trim();
	if (current && !reviewFilled) return [];
    const label = labelFor(el);
    if (!label) return [];
    const id = `f${++index}`;
    el.dataset.browseragentFastId = id;
	const card = cardMetaFor(el);
	const position = positionFor(el);
	return [{id, label, section: sectionFor(el) || card.type, card_context: card.context,
	  card_type: card.type, card_index: card.index, card_count: card.count, card_signature: card.signature,
	  top: position.top, left: position.left, disabled: Boolean(el.disabled), current_value: String(current || '').trim(),
	  kind: el.tagName === 'SELECT' ? 'select' : type,
      options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()).filter(Boolean).slice(0, 80) : []}];
  });
	const ongoingCheckboxFields = [...document.querySelectorAll('input[type="checkbox"]')].flatMap(el => {
	  if (!visible(el) || el.disabled) return [];
	  const rawLabel = labelFor(el);
	  const ongoingMatch = rawLabel.match(/至今|目前|仍在职|仍在读|当前|present|current|ongoing/i);
	  if (!ongoingMatch) return [];
	  const label = ongoingMatch[0];
	  const id = `f${++index}`;
	  el.dataset.browseragentFastId = id;
	  const card = cardMetaFor(el);
	  const position = positionFor(el);
	  return [{id, label, section: sectionFor(el) || card.type, card_context: card.context,
	    card_type: card.type, card_index: card.index, card_count: card.count, card_signature: card.signature,
	    current_value: el.checked ? 'true' : 'false', top: position.top, left: position.left,
	    disabled: Boolean(el.disabled), kind: 'ongoing_checkbox', options: ['true', 'false']}];
	});
	const customFields = customControls().flatMap(el => {
	  const currentValue = customCurrent(el);
	  const innerInput = el.querySelector('input');
	  const hintText = String(
	    innerInput?.getAttribute('placeholder') ||
	    el.querySelector('[class*="hint"], [data-placeholder]')?.innerText ||
	    el.querySelector('[class*="hint"], [data-placeholder]')?.getAttribute('data-placeholder') || ''
	  ).trim();
	  const hintIdentity = hintText.replace(/^(?:请选择|请输入|选择|select|choose)\s*/i, '').trim();
	  // A compound form item can contain both “证件类型” and “证件号码”. The
	  // control-specific hint is more precise than the shared outer label.
	  const label = hintIdentity || labelFor(innerInput || el);
	  // A placeholder is not a field identity. Mapping dozens of anonymous
	  // "请选择" controls together can make the structured solver fail and must
	  // never prevent ordinary text fields from being filled.
	  if (!label || /^(请选择|请输入|选择|select|choose)$/i.test(label)) return [];
	  const id = `f${++index}`;
	  el.dataset.browseragentFastId = id;
	  const card = cardMetaFor(el);
	  const position = positionFor(el);
	  const classes = String(el.className || '');
	  const dateLike = /date|time|calendar|month|year|日期|时间|在校时间|毕业时间|在职时间|结束时间|项目时间/i.test(`${classes} ${label}`);
	  // Filled ordinary selectors are out of scope. Dates stay in the inventory
	  // so a review pass can compare imported/parser values with the CV.
	  if (currentValue && !dateLike && !reviewFilled) return [];
	  const cascader = /cascader|户籍|籍贯|生源地|省份|城市|市区|区县|行政区|地区|province|city|district|region/i.test(`${classes} ${label}`);
	  return [{id, label, section: sectionFor(el) || card.type, card_context: card.context,
	    card_type: card.type, card_index: card.index, card_count: card.count, card_signature: card.signature,
	    current_value: currentValue,
	    top: position.top, left: position.left,
	    kind: dateLike ? 'custom_date' : cascader ? 'custom_cascader' : 'custom_select', options: []}];
	});
	// Build stable visual rows first. Controls in one form row can differ by a few
	// pixels because labels, validation text, and custom widgets have different
	// internal boxes. Within each row, left-to-right ordering is authoritative so
	// a selector can enable the text control beside it before that control runs.
	const byTop = [...nativeFields, ...ongoingCheckboxFields, ...customFields].sort((a, b) => a.top - b.top || a.left - b.left);
	const rows = [];
	for (const item of byTop) {
	  const row = rows.find(candidate => Math.abs(candidate.anchor - item.top) <= 24);
	  if (row) row.items.push(item);
	  else rows.push({anchor: item.top, items: [item]});
	}
	const ordered = rows.flatMap(row => row.items.sort((a, b) => a.left - b.left || a.top - b.top));
	ordered.forEach((item, order) => { item.dom_order = order; });
	return ordered;
}"""

ENABLE_REVIEW_SCAN_SCRIPT = r"""() => {
  window.__browseragentReviewFilled = true;
  return true;
}"""


APPLY_SCRIPT = r"""(assignments) => (async () => {
  const attempted = [];
  const outcomes = [];
  for (const item of assignments) {
    const el = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(item.field_id)}"]`);
    if (!el) {
      outcomes.push({field_id: item.field_id, requested: String(item.value ?? ''), status: 'missing_control', before: '', after: ''});
      continue;
    }
    const before = String(el.value ?? '').trim();
    if (el.disabled || el.readOnly) {
      outcomes.push({field_id: item.field_id, requested: String(item.value ?? ''), status: 'not_editable', before, after: before});
      continue;
    }
    let value = String(item.value ?? '').trim();
    if (!value) {
      outcomes.push({field_id: item.field_id, requested: '', status: 'empty_assignment', before, after: before});
      continue;
    }
    const requested = value;
	if (before === requested) {
	  attempted.push({field_id: item.field_id, requested, expected: requested, before, verified_existing: true});
	  continue;
	}
    if (el.tagName === 'SELECT') {
      const option = [...el.options].find(o => o.text.trim() === value || o.value === value);
      if (!option || !option.value) {
        outcomes.push({field_id: item.field_id, requested, status: 'option_not_found', before, after: before});
        continue;
      }
      value = option.value;
    }
    const setter = Object.getOwnPropertyDescriptor(
      el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype :
      el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype,
      'value'
    )?.set;
    setter ? setter.call(el, value) : (el.value = value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
	attempted.push({field_id: item.field_id, requested, expected: value, before});
  }
  await new Promise(resolve => setTimeout(resolve, 150));
  const changed = [];
  const failed = [];
  for (const item of attempted) {
    const el = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(item.field_id)}"]`);
    const actual = String(el?.value ?? '').trim();
	const status = actual === item.expected ? (item.verified_existing ? 'verified_existing' : 'verified') : 'write_not_persisted';
	(status === 'verified' || status === 'verified_existing' ? changed : failed).push(item.field_id);
    outcomes.push({...item, actual, after: actual, status});
  }
  return {changed, failed, outcomes};
})()"""

APPLY_DATE_SCRIPT = r"""(params) => (async () => {
  const root = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(params.field_id)}"]`);
  const inputs = root ? (root.matches?.('input') ? [root] : [...root.querySelectorAll('input')]) : [];
  if (inputs.length !== 1) return {changed: false, status: inputs.length ? 'ambiguous_date_inputs' : 'missing_date_input'};
  const input = inputs[0];
  const identity = `${params.label || ''} ${root.className || ''} ${input.className || ''} ${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`;
  if (!/(date|time|calendar|month|year|日期|时间|年月|入学|毕业|开始|结束|出生)/i.test(identity))
    return {changed: false, status: 'date_target_identity_mismatch'};
  const before = String(input.value || '').trim();
  const value = String(params.value || '').trim();
  if (!/^\d{4}[\/-](?:0?[1-9]|1[0-2])$/.test(value)) return {changed: false, status: 'invalid_year_month'};
  const normalized = value.replace('-', '/');
	const beforeNormalized = before.replace('-', '/');
	if (beforeNormalized === normalized) return {changed: true, status: 'verified_existing', before, after: before};
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  // React keeps a private value tracker on controlled inputs. Calling the
  // native setter without rewinding that tracker can make React discard the
  // following input event as a no-op and restore the previous value.
  const tracker = input._valueTracker;
  setter ? setter.call(input, normalized) : (input.value = normalized);
  tracker?.setValue?.(before);
  input.dispatchEvent(new InputEvent('input', {
    bubbles: true, inputType: 'insertText', data: normalized,
  }));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
  input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
  input.dispatchEvent(new Event('blur', {bubbles: true}));
  await new Promise(resolve => setTimeout(resolve, 200));
  const after = String(input.value || '').trim().replace('-', '/');
  return {changed: after === normalized, status: after === normalized ? 'verified' : 'write_not_persisted'};
})()"""


# Select a year and month through the widget's own visible panel when a
# controlled input rejects direct value assignment. The selectors describe
# common date-picker component families (MTD, Ant, Element and ARIA grids), not
# any application page. Candidate clicks are confined to the active date popup;
# this script can never reach a form submit button.
APPLY_DATE_PICKER_SCRIPT = r"""(params) => (async () => {
  const root = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(params.field_id)}"]`);
  const inputs = root ? (root.matches?.('input') ? [root] : [...root.querySelectorAll('input')]) : [];
  if (inputs.length !== 1) return {changed: false, status: inputs.length ? 'ambiguous_date_inputs' : 'missing_date_input'};
  const input = inputs[0];
  const match = String(params.value || '').trim().match(/^(\d{4})[\/-](0?[1-9]|1[0-2])$/);
  if (!match) return {changed: false, status: 'invalid_year_month'};
  const targetYear = Number(match[1]);
  const targetMonth = Number(match[2]);
  const canonical = value => {
    const parsed = String(value || '').match(/(\d{4})\D+(\d{1,2})/);
    return parsed ? `${parsed[1]}/${String(Number(parsed[2])).padStart(2, '0')}` : '';
  };
  const expected = `${targetYear}/${String(targetMonth).padStart(2, '0')}`;
  const before = String(input.value || '').trim();
  if (canonical(before) === expected) return {changed: true, status: 'verified_existing', before, after: before};
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const click = el => {
    if (!visible(el) || el.matches?.('[disabled], [aria-disabled="true"]')) return false;
    el.scrollIntoView?.({block: 'nearest', inline: 'nearest'});
    el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'mouse'}));
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
    el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}));
    el.click();
    return true;
  };
  const popupSelector = [
    '.mtd-datepicker-pop-wrapper', '.mtd-datepicker-pop',
    '.ant-picker-dropdown', '.el-picker-panel', '.el-date-picker',
    '[role="dialog"]', '[class*="datepicker-pop"]', '[class*="date-picker-pop"]',
    '[class*="picker-dropdown"]', '[class*="calendar-pop"]'
  ].join(',');
  const datePopup = () => [...document.querySelectorAll(popupSelector)].filter(visible).find(el =>
    el.querySelector('.mtd-month-panel-list-data, .mtd-year-panel-list-data, [class*="month-panel"], [class*="year-panel"], [role="grid"]')
  );
  input.focus?.();
  click(input);
  await wait(180);
  let popup = datePopup();
  if (!popup) return {changed: false, status: 'date_popup_not_opened', before, after: String(input.value || '').trim()};

  const yearCellSelector = [
    '.mtd-year-panel-list-data', '.ant-picker-year-panel .ant-picker-cell-inner',
    '.el-year-table td .cell', '[class*="year-panel"] [role="gridcell"]',
    '[class*="year-panel"] button'
  ].join(',');
  const monthCellSelector = [
    '.mtd-month-panel-list-data', '.ant-picker-month-panel .ant-picker-cell-inner',
    '.el-month-table td .cell', '[class*="month-panel"] [role="gridcell"]',
    '[class*="month-panel"] button'
  ].join(',');
  const exactCell = (selector, accepted) => [...popup.querySelectorAll(selector)].filter(visible).find(el => {
    const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    return accepted.has(text);
  });
  const yearTexts = new Set([String(targetYear), `${targetYear}年`]);
  const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const monthTexts = new Set([
    String(targetMonth), String(targetMonth).padStart(2, '0'), `${targetMonth}月`,
    `${String(targetMonth).padStart(2, '0')}月`, monthNames[targetMonth - 1],
  ]);

  let monthCell = exactCell(monthCellSelector, monthTexts);
  const visibleYearLabel = [...popup.querySelectorAll(
    '.mtd-month-calendar-year-btn, .mtd-date-calendar-year-btn, .ant-picker-year-btn, '
    + '.el-date-picker__header-label, [aria-label*="year" i], [title*="year" i]'
  )].filter(visible).map(el => String(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
    .map(text => Number(text.match(/\d{4}/)?.[0])).find(Number.isFinite);
  let yearResolved = visibleYearLabel === targetYear;
  if (!monthCell || !yearResolved) {
    // Switch from the month view to its year view. Header controls are scoped to
    // the popup, and only controls whose identity explicitly says "year" qualify.
    const yearMode = [...popup.querySelectorAll(
      '.mtd-month-calendar-year-btn, .mtd-date-calendar-year-btn, .ant-picker-year-btn, '
      + '.el-date-picker__header-label, button, [role="button"]'
    )].filter(visible).find(el => {
      const identity = `${el.className || ''} ${el.getAttribute('aria-label') || ''} ${el.title || ''}`;
      const text = String(el.innerText || el.textContent || '').trim();
      return /year|年份|年选择/i.test(identity) || /^\d{4}年?$/.test(text);
    });
    if (yearMode) {
      click(yearMode);
      await wait(120);
      popup = datePopup() || popup;
    }
  }

  let yearCell = exactCell(yearCellSelector, yearTexts);
  for (let attempt = 0; !yearCell && attempt < 16; attempt++) {
    const shownYears = [...popup.querySelectorAll(yearCellSelector)].filter(visible)
      .map(el => Number(String(el.innerText || el.textContent || '').match(/\d{4}/)?.[0]))
      .filter(Number.isFinite);
    if (!shownYears.length) break;
    const goLeft = targetYear < Math.min(...shownYears);
    const goRight = targetYear > Math.max(...shownYears);
    if (!goLeft && !goRight) break;
    const navigation = [...popup.querySelectorAll('button, [role="button"], i, span')].filter(visible).find(el => {
      const identity = `${el.className || ''} ${el.getAttribute('aria-label') || ''} ${el.title || ''}`;
      return goLeft
        ? /left-switcher|prev(?:ious)?(?:-year)?|上一(?:年|页)|向左/i.test(identity)
        : /right-switcher|next(?:-year)?|下一(?:年|页)|向右/i.test(identity);
    });
    if (!navigation || !click(navigation)) break;
    await wait(80);
    popup = datePopup() || popup;
    yearCell = exactCell(yearCellSelector, yearTexts);
  }
  if (yearCell) {
    click(yearCell);
    await wait(120);
    popup = datePopup() || popup;
    yearResolved = true;
  }

  monthCell = exactCell(monthCellSelector, monthTexts);
  if (!yearResolved) return {
    changed: false, status: 'year_option_not_found',
    before, after: String(input.value || '').trim(),
  };
  if (!monthCell) return {
    changed: false, status: 'month_option_not_found',
    before, after: String(input.value || '').trim(),
  };
  click(monthCell);
  await wait(300);
  const after = String(input.value || '').trim();
  return {
    changed: canonical(after) === expected,
    status: canonical(after) === expected ? 'verified_picker_selection' : 'picker_selection_not_persisted',
    before, after,
  };
})()"""

APPLY_CHECKBOX_SCRIPT = r"""(params) => (async () => {
  const input = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(params.field_id)}"]`);
  if (!input || input.type !== 'checkbox') return {changed: false, status: 'missing_checkbox'};
  const expected = String(params.value).toLowerCase() === 'true';
  const before = Boolean(input.checked);
  if (before === expected) return {changed: true, status: 'verified_existing', before, after: before};
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
  setter ? setter.call(input, expected) : (input.checked = expected);
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  await new Promise(resolve => setTimeout(resolve, 250));
  const after = Boolean(input.checked);
  return {changed: after === expected, status: after === expected ? 'verified' : 'write_not_persisted', before, after};
})()"""


READ_FAST_VALUE_SCRIPT = r"""(field_id) => {
  const el = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(field_id)}"]`);
  return JSON.stringify({found: Boolean(el), value: String(el?.value ?? '').trim()});
}"""

PREPARE_DATE_INPUT_SCRIPT = r"""(params) => {
  const root = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(params.field_id)}"]`);
  const inputs = root ? (root.matches?.('input') ? [root] : [...root.querySelectorAll('input')]) : [];
  if (inputs.length !== 1) return JSON.stringify({found: false, reason: inputs.length ? 'ambiguous_date_inputs' : 'missing_date_input'});
  const input = inputs[0];
  const identity = `${params.label || ''} ${root.className || ''} ${input.className || ''} ${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`;
  if (!/(date|time|calendar|month|year|日期|时间|年月|入学|毕业|开始|结束|出生)/i.test(identity))
    return JSON.stringify({found: false, reason: 'date_target_identity_mismatch', identity: identity.slice(0, 300)});
  input.setAttribute('data-browseragent-date-input', params.token);
  const wasReadonly = input.hasAttribute('readonly');
  if (wasReadonly) input.removeAttribute('readonly');
  return JSON.stringify({found: true, was_readonly: wasReadonly, identity: identity.slice(0, 300)});
}"""

RESTORE_DATE_INPUT_SCRIPT = r"""(params) => {
  const input = document.querySelector(`[data-browseragent-date-input="${CSS.escape(params.token)}"]`);
  if (!input) return JSON.stringify({found: false, value: ''});
  input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
  input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  input.dispatchEvent(new Event('blur', {bubbles: true}));
  if (params.was_readonly) input.setAttribute('readonly', '');
  input.removeAttribute('data-browseragent-date-input');
  return JSON.stringify({found: true, value: String(input.value || '').trim()});
}"""

READ_DATE_VALUE_SCRIPT = r"""(field_id) => {
  const root = document.querySelector(`[data-browseragent-fast-id="${CSS.escape(field_id)}"]`);
  const input = root?.matches?.('input') ? root : root?.querySelector?.('input');
  return JSON.stringify({found: Boolean(input), value: String(input?.value || '').trim()});
}"""

PING_PAGE_SCRIPT = r"""() => true"""


async def _cdp_fill_text_fallback(page, field_id: str, value: str) -> tuple[bool, str]:
	"""Use trusted typing only after a direct DOM write failed verification."""
	try:
		elements = await page.get_elements_by_css_selector(
			f'[data-browseragent-fast-id="{field_id}"]'
		)
		if len(elements) != 1:
			return False, f"expected one element, found {len(elements)}"
		await elements[0].fill(value)
		await asyncio.sleep(0.15)
		state = json.loads(await page.evaluate(READ_FAST_VALUE_SCRIPT, field_id))
		return state.get("value") == value, str(state.get("value", ""))
	except Exception as exc:
		return False, f"{type(exc).__name__}: {exc}"


async def _cdp_fill_date_fallback(page, field_id: str, label: str, value: str) -> tuple[bool, str]:
	"""Type into a controlled readonly date input, restore it, then verify persistence."""
	state = {"found": False, "was_readonly": False}
	token = secrets.token_hex(12)
	try:
		state = json.loads(await page.evaluate(PREPARE_DATE_INPUT_SCRIPT, {
			"field_id": field_id, "label": label, "token": token,
		}))
		if not state.get("found"):
			return False, str(state.get("reason") or "date input unavailable")
		elements = await page.get_elements_by_css_selector(
			f'[data-browseragent-date-input="{token}"]'
		)
		if len(elements) != 1:
			return False, f"expected one date input, found {len(elements)}"
		normalized = _canonical_year_month(value)
		await elements[0].fill(normalized)
		await page.evaluate(
			RESTORE_DATE_INPUT_SCRIPT,
			{"token": token, "was_readonly": bool(state.get("was_readonly"))},
		)
		await asyncio.sleep(0.5)
		current = json.loads(await page.evaluate(READ_DATE_VALUE_SCRIPT, field_id))
		actual = str(current.get("value", "")).strip()
		return _canonical_year_month(actual) == normalized, actual
	except Exception as exc:
		return False, f"{type(exc).__name__}: {exc}"
	finally:
		try:
			await page.evaluate(
				RESTORE_DATE_INPUT_SCRIPT,
				{"token": token, "was_readonly": bool(state.get("was_readonly"))},
			)
		except Exception:
			pass


def _canonical_year_month(value: str) -> str:
	"""Normalize the display formats commonly used by month pickers."""
	match = re.search(r"(\d{4})\D+(\d{1,2})", str(value or "").strip())
	if not match:
		return str(value or "").strip().replace("-", "/")
	return f"{match.group(1)}/{int(match.group(2)):02d}"


async def _ensure_live_page(browser_session, page, *, field_id: str = ""):
	"""Reattach once when a long solver phase outlives the root CDP client."""
	try:
		await page.evaluate(PING_PAGE_SCRIPT)
		return page, "already_live"
	except asyncio.CancelledError:
		raise
	except Exception as first_error:
		message = f"{type(first_error).__name__}: {first_error}"
		# A detached target can be fixed by acquiring a fresh Page actor. A stopped
		# root client needs a real reconnect to the same dedicated Chrome profile.
		try:
			fresh = await browser_session.must_get_current_page()
			await fresh.evaluate(PING_PAGE_SCRIPT)
			return fresh, f"fresh_page:{message}"
		except asyncio.CancelledError:
			raise
		except Exception as fresh_error:
			if "Client is not started" not in f"{message} {fresh_error}":
				raise first_error
		old_target_id = getattr(page, "_target_id", None) or getattr(
			browser_session, "agent_focus_target_id", None,
		)
		connect = getattr(browser_session, "connect", None)
		if not callable(connect):
			raise first_error
		await connect()
		if old_target_id:
			focus = getattr(browser_session, "get_or_create_cdp_session", None)
			if callable(focus):
				try:
					await focus(old_target_id, focus=True)
				except (ValueError, RuntimeError):
					pass
		fresh = await browser_session.must_get_current_page()
		await fresh.evaluate(PING_PAGE_SCRIPT)
		return fresh, f"reconnected:{message}:field={field_id or '(scan)'}"


def _now() -> str:
	return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _field_block(field: FastField) -> str:
	# Explicit DOM/card classification is authoritative. Free-form card content
	# can mention another entity (for example a project description mentioning a
	# partner company) and must not move that field into the experience solver.
	if field.card_type in {"education", "experience", "project"}:
		return field.card_type
	section_text = str(field.section or "")
	for name in ("education", "experience", "project", "basic"):
		if section_text == name or BLOCK_PATTERNS[name].search(section_text):
			return name
	text = field.label
	for name in ("education", "experience", "project", "basic"):
		if BLOCK_PATTERNS[name].search(text):
			return name
	return "other"


def plan_field_blocks(fields: list[FastField], parallelism: int) -> list[tuple[str, list[FastField]]]:
	"""Create a bounded, deterministic page plan without another model call."""
	if parallelism <= 1 or len(fields) < 6:
		return [("all", fields)]
	groups: dict[str, list[FastField]] = {}
	for field in fields:
		groups.setdefault(_field_block(field), []).append(field)
	if len(groups) < 2:
		return [("all", fields)]
	if len(groups) > parallelism:
		# Keep the largest blocks independent and merge small fragments so API
		# concurrency stays bounded. Field order is restored below.
		keep = {name for name, _ in sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)[: parallelism - 1]}
		merged: dict[str, list[FastField]] = {name: values for name, values in groups.items() if name in keep}
		# ``other`` is a real field block and is often the largest one on ATS
		# pages (publications, languages, and site-specific fields land there).
		# Reusing that key for the remainder used to overwrite the entire kept
		# block, silently dropping fields before the solver ever saw them.
		remainder_name = "merged" if "other" in keep else "other"
		merged[remainder_name] = [field for name, values in groups.items() if name not in keep for field in values]
		groups = merged
	position = {field.id: index for index, field in enumerate(fields)}
	return sorted(groups.items(), key=lambda item: min(position[field.id] for field in item[1]))


def plan_review_blocks(fields: list[FastField]) -> list[tuple[str, list[FastField]]]:
	"""Give each identifiable repeated card a focused repair prompt."""
	groups: dict[tuple[str, str, int], list[FastField]] = {}
	for field in fields:
		block = _field_block(field)
		# Split only cards with live context. Completely empty cards must remain in
		# one block so independent solvers cannot choose the same unused CV record.
		card_index = field.card_index if field.card_index > 0 and field.card_context else 0
		key = (block, field.card_type if card_index else "", card_index)
		groups.setdefault(key, []).append(field)
	position = {field.id: index for index, field in enumerate(fields)}
	planned = []
	for (block, card_type, card_index), values in groups.items():
		name = f"{block} card {card_index}" if card_index else block
		planned.append((name, values))
	return sorted(planned, key=lambda item: min(position[field.id] for field in item[1]))


def _confirmed_default_assignments(
	fields: list[FastField], defaults: dict[str, str] | None
) -> list[FastAssignment]:
	"""Map unique user-confirmed defaults without asking an LLM to rediscover them."""
	if not defaults:
		return []
	def normalize(value: str) -> str:
		text = re.sub(r"^(?:请输入|请选择|选择)", "", str(value).strip(), flags=re.I)
		return re.sub(r"[\s*_：:（）()\[\]【】\-]+", "", text).casefold()

	field_names = {field.id: normalize(field.label) for field in fields}
	name_counts: dict[str, int] = {}
	for name in field_names.values():
		name_counts[name] = name_counts.get(name, 0) + 1
	assignments: list[FastAssignment] = []
	for field in fields:
		name = field_names[field.id]
		if len(name) < 2 or name_counts[name] != 1 or field.kind in {"custom_date", "custom_cascader"}:
			continue
		matches = [
			str(value).strip() for key, value in defaults.items()
			if str(value).strip() and (
				normalize(key) == name or normalize(key).endswith(name) or name.endswith(normalize(key))
			)
		]
		if len(matches) != 1:
			continue
		value = matches[0]
		if normalize(field.current_value) == normalize(value):
			continue
		if field.kind == "select" and value not in field.options:
			continue
		assignments.append(FastAssignment(
			field_id=field.id,
			targets=[value] if field.kind == "custom_select" else [],
			value="" if field.kind == "custom_select" else value,
		))
	return assignments


def _sensitive_type_assignments(
	fields: list[FastField], available_types: set[str] | None
) -> list[FastAssignment]:
	"""Derive only a document type from an available secret name, never its value."""
	if not available_types:
		return []
	assignments: list[FastAssignment] = []
	for field in fields:
		if field.kind != "custom_select":
			continue
		if re.search(r"证件类型|证件种类|document\s*type|identity\s*type", field.label, re.I):
			if "national_id" in available_types and "居民身份证" not in field.current_value:
				assignments.append(FastAssignment(field_id=field.id, targets=["居民身份证"]))
	return assignments


def _sensitive_value_assignments(
	fields: list[FastField], sensitive_values: dict[str, str] | None
) -> tuple[list[FastAssignment], dict[str, str]]:
	"""Map explicit secret values locally without exposing them to a solver."""
	if not sensitive_values:
		return [], {}
	assignments: list[FastAssignment] = []
	secret_names_by_field: dict[str, str] = {}
	for field in fields:
		if field.kind not in {"text", "textarea", "tel"}:
			continue
		if re.search(r"类型|种类|类别|type|kind|category", field.label, re.I):
			continue
		matches = [
			name for name, pattern in SENSITIVE_VALUE_PATTERNS.items()
			if sensitive_values.get(name) and pattern.search(field.label)
		]
		if len(matches) != 1:
			continue
		name = matches[0]
		assignments.append(FastAssignment(field_id=field.id, value=sensitive_values[name]))
		secret_names_by_field[field.id] = name
	return assignments, secret_names_by_field


async def _solve_block(
	llm, context: str, name: str, fields: list[FastField], review_notes: str = ""
) -> FastAssignmentBatch:
	from browser_use.llm.messages import SystemMessage, UserMessage

	messages = [
		SystemMessage(
			content=(
				"Map candidate facts to empty job-form fields. Use explicit facts and only deterministic derivations from "
				"CANDIDATE SOURCES (for example, an author's ordinal position in an explicitly ordered author list). "
				"Safe deterministic conversions include PhD/doctoral candidate to 博士, an explicit bachelor's degree to 本科, "
				"and an experience explicitly described as an internship to work type 实习. Do not derive subjective traits. "
				"For a project-role field, derive a concise functional role only from explicit responsibility verbs in that "
				"same project record: 负责总体统筹/中期结题报告 supports 项目整体统筹, while explicit 开发/实现/搭建 "
				"work supports 主要开发人员. Never infer seniority, leadership, ownership, or a role when the record only "
				"names a project without describing the candidate's actions. "
				"CURRENT_VALUE is the live value already present on the page. If it is non-empty and agrees with the source, "
				"omit that field. Propose a replacement only when the source clearly and specifically contradicts it. "
				"Never infer, embellish, or answer legal, salary, identity-number, work-authorization, "
				"declaration, consent, or self-identification questions. Omit uncertain fields. For native select fields, "
				"A document TYPE may be mapped only when an AVAILABLE SENSITIVE FIELD TYPE or user-confirmed default explicitly "
				"supports it; never infer document type from name, nationality, location, or language. "
				"A simple gender field may be mapped only when its exact value is explicitly stated in the sources; "
				"never infer it from name, photo, title, pronouns, or context. "
				"For a native select, VALUE must exactly equal one supplied option. For custom_select, OPTIONS may be empty "
				"because its portal opens only during execution; still return one source-supported semantic target in TARGETS "
				"using the candidate source's exact wording. The executor will search the live menu and verify the selected option. "
				"For custom_date return an explicit source-supported year/month in VALUE using YYYY/MM; omit unknown dates. "
				"For ongoing_checkbox return VALUE false when the matching record has an explicit end date, and true only "
				"when the source explicitly says present/current/ongoing. Resolve this dependency even if other fields are omitted. "
				"A year/month may only go to a date/time field in the same record; never put it in school, college, major, "
				"company, role, project, publication, or other text/select fields. School, college and major must come from "
				"the matching education record. A degree/education-level field accepts only an actual degree level such as "
				"博士、硕士、本科; visiting/exchange study is not a degree. A numeric language test score is not a language "
				"level dropdown value. For author order, derive it from an explicit ordered author list and use 一作、二作、三作. "
				"For custom_cascader return the shortest complete path explicitly supported by the source, with one to four levels. "
				"Do not prepend a country or province merely because it appears elsewhere in the profile; never invent missing levels. "
				"CARD_CONTEXT identifies an already populated record. "
				"For a partially populated card, identify its matching candidate record from CARD_CONTEXT and fill every remaining "
				"source-supported field in that same card; do not discard the record merely because part of it is already represented. "
				"Exclude that record only when choosing data for a different empty card. For completely empty cards, map only the remaining "
				"unrepresented records, in candidate-source order when there is more than one. CARD_INDEX identifies the target DOM "
				"card only; it is NOT the ordinal of the candidate record because sites append new blank cards after existing records. "
				"If the unmatched record is not unique, omit the assignment. Return each field_id at most once."
			)
		),
		UserMessage(
			content=(
				f"FORM BLOCK: {BLOCK_NAMES.get(name, name)}\n\n"
				f"CANDIDATE SOURCES:\n{context}\n\n"
				f"REVIEW NOTES FROM THE PREVIOUS PASS:\n{review_notes or '(first pass)'}\n\n"
				f"FIELDS TO FILL OR REVIEW:\n{json.dumps([item.model_dump() for item in fields], ensure_ascii=False)}"
			)
		),
	]
	response = await llm.ainvoke(messages, output_format=FastAssignmentBatch)
	return response.completion


async def fast_fill_current_page(
	browser_session,
	llm,
	context: str,
	tracker: FastFillTracker,
	*,
	parallelism: int = 3,
	confirmed_defaults: dict[str, str] | None = None,
	available_sensitive_types: set[str] | None = None,
	sensitive_values: dict[str, str] | None = None,
	review_notes: str = "",
	review_mode: bool = False,
	checkpoint: Callable[[], None] | None = None,
) -> list[str]:
	"""Fill safe empty controls on the focused page and return verified labels."""
	tracker.pages_scanned += 1
	page = await browser_session.must_get_current_page()
	page, initial_recovery = await _ensure_live_page(browser_session, page)
	if review_mode:
		await page.evaluate(ENABLE_REVIEW_SCAN_SCRIPT)
	raw_fields = await page.evaluate(SCAN_SCRIPT)
	try:
		fields = [FastField.model_validate(item) for item in json.loads(raw_fields or "[]")]
	except (json.JSONDecodeError, TypeError, ValueError) as exc:
		tracker.warnings.append(f"代码优先流程无法解析页面字段：{exc}")
		return []
	_hydrate_card_contexts(fields)

	sensitive_value_assignments, secret_names_by_field = _sensitive_value_assignments(fields, sensitive_values)
	secret_field_ids = set(secret_names_by_field)
	code_pass = {
		"status": "scanned",
		"started_at": _now(),
		"completed_at": "",
		"page_number": tracker.pages_scanned,
		"scanned_fields": [
			{
				**item.model_dump(),
				"current_value": (
					f"<secret:{secret_names_by_field[item.id]}>"
					if item.id in secret_field_ids and item.current_value else item.current_value
				),
			}
			for item in fields
		],
		"blocked_fields": [
			item.label for item in fields if BLOCKED_LABELS.search(item.label) and item.id not in secret_field_ids
		],
		"cascade_fields": [item.label for item in fields if CASCADE_LABELS.search(item.label)],
		"ambiguous_repeated_fields": [],
		"proposals": [],
		"filled_fields": [],
		"newly_filled_fields": [],
		"corrected_fields": [],
		"verified_existing_fields": [],
		"observed_existing_fields": [item.label for item in fields if item.current_value],
		"field_counts": {
			"scanned": len(fields),
			"already_populated_before_pass": sum(bool(item.current_value) for item in fields),
			"empty_before_pass": sum(not item.current_value for item in fields),
		},
		"applied_values": [],
		"write_failures": [],
		"native_write_attempts": [],
		"dropdown_attempts": [],
		"date_picker_attempts": [],
		"browser_recoveries": [],
		"execution_order": [],
		"solver_runs": [],
		"confirmed_default_assignments": [],
		"sensitive_type_assignments": [],
		"sensitive_value_assignments": [
			{"field_id": item.field_id, "field": next(field.label for field in fields if field.id == item.field_id),
			 "secret_name": secret_names_by_field[item.field_id]}
			for item in sensitive_value_assignments
		],
		"deferred_details": [],
		"deferred_fields": [],
	}
	tracker.code_passes.append(code_pass)
	if initial_recovery != "already_live":
		code_pass["browser_recoveries"].append({
			"field_id": "", "field": "(scan)", "status": initial_recovery,
		})
	tracker.current_stage = f"page_{tracker.pages_scanned}_scanned"
	if checkpoint:
		checkpoint()
	# Coupled province/city/district controls must be selected as one ordered path.
	# Filling them independently can leave a valid-looking but inconsistent value.
	safe_fields = [
		item for item in fields
		if (not BLOCKED_LABELS.search(item.label) or item.id in secret_field_ids)
		and (not CASCADE_LABELS.search(item.label) or item.kind == "custom_cascader")
	]
	if review_mode:
		# The repair pass is coverage-first: retry controls that are still empty
		# after dependencies and first-pass writes have settled. Re-solving every
		# populated control delayed genuinely missing fields and repeated successful
		# dropdown interactions without adding coverage.
		safe_fields = [item for item in safe_fields if not item.current_value]
	groups: dict[str, list[FastField]] = {}
	for item in safe_fields:
		# Section markup varies wildly and some sites include a card number in the
		# heading. Treat the normalized label itself as the duplicate key so a
		# cosmetic section difference can never bypass the card-context guard.
		key = " ".join(item.label.lower().split())
		groups.setdefault(key, []).append(item)
	ambiguous_ids: set[str] = set()
	for repeated in groups.values():
		if len(repeated) < 2:
			continue
		identities = []
		for item in repeated:
			semantic = " ".join(item.card_context.lower().split())
			if semantic:
				identities.append(f"context:{semantic}")
			elif item.card_type and item.card_index > 0 and item.card_count > 1 and item.card_signature:
				identities.append(f"position:{item.card_type}:{item.card_index}/{item.card_count}:{item.card_signature}")
			else:
				identities.append("")
		if any(not value for value in identities) or len(set(identities)) != len(identities):
			ambiguous_ids.update(item.id for item in repeated)
			code_pass["ambiguous_repeated_fields"].extend(
				{
					"field_id": item.id,
					"field": item.label,
					"section": item.section,
					"card_context": item.card_context,
					"card_type": item.card_type,
					"card_index": item.card_index,
					"card_count": item.card_count,
				}
				for item in repeated
			)
	if ambiguous_ids:
		for item in safe_fields:
			if item.id in ambiguous_ids and item.label not in tracker.deferred_labels:
				tracker.deferred_labels.append(item.label)
		code_pass["deferred_fields"].extend(item.label for item in safe_fields if item.id in ambiguous_ids)
		code_pass["deferred_details"].extend(
			{"field_id": item.id, "field": item.label, "reason": "ambiguous_card_identity"}
			for item in safe_fields if item.id in ambiguous_ids
		)
		safe_fields = [item for item in safe_fields if item.id not in ambiguous_ids]
	if not safe_fields:
		code_pass["field_counts"].update({
			"newly_filled": 0,
			"corrected": 0,
			"verified_existing": 0,
			"observed_existing_not_rewritten": len(code_pass["observed_existing_fields"]),
			"remaining_unresolved": 0,
		})
		code_pass["status"] = "completed"
		code_pass["completed_at"] = _now()
		tracker.current_stage = f"page_{tracker.pages_scanned}_completed"
		if checkpoint:
			checkpoint()
		return []

	known = {item.id: item for item in safe_fields}
	solver_failed_ids: set[str] = set()
	tracker.last_plan = []

	async def solve_phase(phase: str, phase_fields: list[FastField]) -> list[FastAssignment]:
		"""Solve one isolated control family so custom widgets cannot block text."""
		if not phase_fields:
			return []
		blocks = plan_review_blocks(phase_fields) if review_mode else plan_field_blocks(phase_fields, parallelism)
		tracker.last_plan.extend(
			f"{'文本' if phase == 'native' else '下拉'}：{BLOCK_NAMES.get(name, name)}"
			for name, _ in blocks
		)
		tracker.solver_calls += len(blocks)
		code_pass["status"] = f"{phase}_solving"
		tracker.current_stage = f"page_{tracker.pages_scanned}_{phase}_solving"
		if checkpoint:
			checkpoint()
		semaphore = asyncio.Semaphore(max(1, parallelism))

		async def solve_bounded(name: str, block_fields: list[FastField]):
			async with semaphore:
				return await _solve_block(llm, context, name, block_fields, review_notes)

		results = await asyncio.gather(
			*(solve_bounded(name, block_fields) for name, block_fields in blocks),
			return_exceptions=True,
		)
		phase_assignments: list[FastAssignment] = []
		for (name, block_fields), result in zip(blocks, results, strict=True):
			run = {
				"phase": phase,
				"block": BLOCK_NAMES.get(name, name),
				"field_count": len(block_fields),
				"field_ids": [item.id for item in block_fields],
			}
			if isinstance(result, BaseException):
				error = f"{type(result).__name__}: {result}".strip()
				run.update({"status": "failed", "assignment_count": 0, "error": error})
				solver_failed_ids.update(item.id for item in block_fields)
				tracker.warnings.append(
					f"fast {'文本' if phase == 'native' else '下拉'}区块“{BLOCK_NAMES.get(name, name)}”"
					f"求解失败，已交给普通 Agent：{error}"
				)
			else:
				run.update({
					"status": "completed",
					"assignment_count": len(result.assignments),
					"assignments": [item.model_dump() for item in result.assignments],
					"error": "",
				})
				phase_assignments.extend(result.assignments)
			code_pass["solver_runs"].append(run)
		code_pass["status"] = f"{phase}_solved"
		tracker.current_stage = f"page_{tracker.pages_scanned}_{phase}_solved"
		if checkpoint:
			checkpoint()
		return phase_assignments

	validated: list[dict[str, str]] = []
	dropdown_plans: list[dict[str, object]] = []
	seen: set[str] = set()
	semantic_rejections: dict[str, str] = {}
	native_fields = [
		item for item in safe_fields
		if item.kind not in {"custom_select", "custom_cascader"} and item.id not in secret_field_ids
	]
	dropdown_fields = [item for item in safe_fields if item.kind in {"custom_select", "custom_cascader"}]
	confirmed_assignments = _confirmed_default_assignments(safe_fields, confirmed_defaults)
	type_assignments = _sensitive_type_assignments(safe_fields, available_sensitive_types)
	default_assignments = sensitive_value_assignments + type_assignments + confirmed_assignments
	code_pass["confirmed_default_assignments"] = [item.model_dump() for item in confirmed_assignments]
	code_pass["sensitive_type_assignments"] = [item.model_dump() for item in type_assignments]

	# Solvers may map independent blocks concurrently, but they never touch the
	# page. All mutations are serialized in visual DOM order below.
	native_assignments = [
		item for item in default_assignments
		if item.field_id in known and known[item.field_id].kind not in {"custom_select", "custom_cascader"}
	] + await solve_phase("native", native_fields)
	for assignment in native_assignments:
		field = known.get(assignment.field_id)
		value = assignment.value.strip()
		if not field or field.id in seen or (BLOCKED_LABELS.search(field.label) and field.id not in secret_field_ids):
			continue
		if field.kind in {"custom_select", "custom_cascader"} or not value:
			continue
		if field.kind == "select" and value not in field.options:
			code_pass["proposals"].append({"field": field.label, "value": value, "status": "invalid_option"})
			continue
		semantic_error = _semantic_assignment_error(field, value)
		if semantic_error:
			semantic_rejections[field.id] = semantic_error
			code_pass["proposals"].append({
				"field_id": field.id, "field": field.label, "value": value,
				"status": "semantic_type_mismatch", "reason": semantic_error,
			})
			continue
		seen.add(field.id)
		validated.append({"field_id": field.id, "value": value})
		code_pass["proposals"].append({
			"field_id": field.id,
			"field": field.label,
			"section": field.section,
			"card_context": field.card_context,
			"card_type": field.card_type,
			"card_index": field.card_index,
			"card_count": field.card_count,
			"value": f"<secret:{secret_names_by_field[field.id]}>" if field.id in secret_field_ids else value,
			"status": "validated",
		})

	dropdown_assignments = [
		item for item in default_assignments if known[item.field_id].kind in {"custom_select", "custom_cascader"}
	] + await solve_phase("custom", dropdown_fields)
	for assignment in dropdown_assignments:
		field = known.get(assignment.field_id)
		if not field or field.id in seen or field.kind not in {"custom_select", "custom_cascader"}:
			continue
		targets = [target.strip() for target in assignment.targets if target.strip()]
		value = assignment.value.strip()
		if not targets and value:
			targets = [value]
		valid_count = len(targets) == 1 if field.kind == "custom_select" else 1 <= len(targets) <= 4
		if not valid_count:
			code_pass["proposals"].append({
				"field_id": field.id, "field": field.label, "targets": targets,
				"status": "invalid_target_path",
			})
			continue
		semantic_error = _semantic_assignment_error(field, targets[-1])
		if semantic_error:
			semantic_rejections[field.id] = semantic_error
			code_pass["proposals"].append({
				"field_id": field.id, "field": field.label, "targets": targets,
				"status": "semantic_type_mismatch", "reason": semantic_error,
			})
			continue
		seen.add(field.id)
		dropdown_plans.append({"field_id": field.id, "targets": targets})
		code_pass["proposals"].append({
			"field_id": field.id,
			"field": field.label,
			"section": field.section,
			"card_context": field.card_context,
			"card_type": field.card_type,
			"card_index": field.card_index,
			"card_count": field.card_count,
			"targets": targets,
			"status": "validated_dropdown",
		})

	# Execute one visual queue: top-to-bottom and, within a row, left-to-right.
	# Each field gets a code-first attempt and is verified before the next field.
	# Trusted CDP typing/clicking is used only for that same field after failure.
	from .cascade import select_fast_cascade_path

	changed: set[str] = set()
	failed: set[str] = set()
	native_by_id = {str(item["field_id"]): item for item in validated}
	dropdown_by_id = {str(item["field_id"]): item for item in dropdown_plans}
	operation_ids = sorted(
		[field.id for field in safe_fields if field.id in native_by_id or field.id in dropdown_by_id],
		# Ongoing-state toggles enable/disable date inputs and therefore must settle
		# before the normal visual top-to-bottom queue starts.
		key=lambda field_id: (
			0 if known[field_id].kind == "ongoing_checkbox" else 1,
			known[field_id].dom_order, known[field_id].top, known[field_id].left,
		),
	)
	code_pass["execution_order"] = []
	for field_id in operation_ids:
		field = known[field_id]
		order_entry = {
			"field_id": field_id, "field": field.label, "dom_order": field.dom_order,
			"top": field.top, "left": field.left, "kind": field.kind,
		}
		try:
			page, recovery = await _ensure_live_page(browser_session, page, field_id=field_id)
			if recovery != "already_live":
				code_pass["browser_recoveries"].append({
					"field_id": field_id, "field": field.label, "status": recovery,
				})
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			failed.add(field_id)
			order_entry.update({
				"method": "browser-recovery", "success": False,
				"error": f"{type(exc).__name__}: {exc}",
			})
			code_pass["browser_recoveries"].append({
				"field_id": field_id, "field": field.label, "status": "failed",
				"error": f"{type(exc).__name__}: {exc}",
			})
			code_pass["execution_order"].append(order_entry)
			continue
		if field_id in native_by_id:
			assignment = native_by_id[field_id]
			direct_changed = False
			try:
				if field.kind == "custom_date":
					raw = await page.evaluate(APPLY_DATE_SCRIPT, {**assignment, "label": field.label})
					date_result = json.loads(raw or "{}")
					apply_result = {
						"changed": [field_id] if date_result.get("changed") else [],
						"outcomes": [{"field_id": field_id, "requested": assignment["value"], **date_result}],
					}
				elif field.kind == "ongoing_checkbox":
					raw = await page.evaluate(APPLY_CHECKBOX_SCRIPT, assignment)
					checkbox_result = json.loads(raw or "{}")
					apply_result = {
						"changed": [field_id] if checkbox_result.get("changed") else [],
						"outcomes": [{"field_id": field_id, "requested": assignment["value"], **checkbox_result}],
					}
				else:
					raw = await page.evaluate(APPLY_SCRIPT, [assignment])
					apply_result = json.loads(raw or "{}")
				outcomes = apply_result.get("outcomes", []) if isinstance(apply_result, dict) else []
				if field_id in secret_field_ids:
					marker = f"<secret:{secret_names_by_field[field_id]}>"
					outcomes = [
						{
							**outcome,
							**{
								key: marker
								for key in ("requested", "expected", "before", "actual", "after")
								if key in outcome
							},
						}
						for outcome in outcomes
					]
				code_pass["native_write_attempts"].extend(outcomes)
				direct_changed = field_id in set(apply_result.get("changed", []))
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				code_pass["native_write_attempts"].append({
					"field_id": field_id,
					"requested": (
						f"<secret:{secret_names_by_field[field_id]}>"
						if field_id in secret_field_ids else assignment["value"]
					),
					"status": "direct_write_error", "error": f"{type(exc).__name__}: {exc}",
				})
			if direct_changed:
				changed.add(field_id)
				order_entry.update({
					"method": (
						"date-dom-direct" if field.kind == "custom_date" else
						"checkbox-dom-direct" if field.kind == "ongoing_checkbox" else "dom-direct"
					),
					"success": True,
				})
			elif field.kind == "ongoing_checkbox":
				failed.add(field_id)
				order_entry.update({"method": "checkbox-dom-direct", "success": False, "error": "checkbox write not persisted"})
			elif field.kind == "select":
				result = await select_fast_cascade_path(page, field_id, [str(assignment["value"])])
				success = result.success and result.committed
				(changed if success else failed).add(field_id)
				order_entry.update({"method": result.open_method, "success": success, "error": result.error or ""})
				code_pass["dropdown_attempts"].append({
					"field_id": field_id, "field": field.label, "targets": [assignment["value"]],
					"success": result.success, "selected": result.selected, "committed": result.committed,
					"detected_pattern": result.detected_pattern, "open_method": result.open_method,
					"visible_options": result.visible_options, "verification": result.verification,
					"actual_state": result.actual_state, "error": result.error or "", "fallback_for": "native_select",
				})
			elif field.kind == "custom_date":
				picker_success = False
				picker_result: dict[str, object] = {}
				try:
					raw = await page.evaluate(
						APPLY_DATE_PICKER_SCRIPT,
						{**assignment, "label": field.label},
					)
					picker_result = json.loads(raw or "{}")
					picker_success = bool(picker_result.get("changed"))
				except asyncio.CancelledError:
					raise
				except Exception as exc:
					picker_result = {
						"changed": False, "status": "picker_script_error",
						"error": f"{type(exc).__name__}: {exc}",
					}
				code_pass["date_picker_attempts"].append({
					"field_id": field_id, "field": field.label,
					"requested": assignment["value"], **picker_result,
				})
				if picker_success:
					changed.add(field_id)
					order_entry.update({
						"method": "date-picker-direct", "success": True,
						"actual": str(picker_result.get("after", "")),
					})
				else:
					success, actual = await _cdp_fill_date_fallback(
						page, field_id, field.label, str(assignment["value"]),
					)
					(changed if success else failed).add(field_id)
					order_entry.update({
						"method": "date-cdp-fill", "success": success, "actual": actual,
						"picker_status": str(picker_result.get("status", "")),
						"error": "" if success else actual or "date write not persisted",
					})
			else:
				success, actual = await _cdp_fill_text_fallback(page, field_id, str(assignment["value"]))
				(changed if success else failed).add(field_id)
				order_entry.update({
					"method": "cdp-fill", "success": success,
					"actual": (
						f"<secret:{secret_names_by_field[field_id]}>"
						if field_id in secret_field_ids else actual
					),
				})
		else:
			plan = dropdown_by_id[field_id]
			result = await select_fast_cascade_path(page, field_id, list(plan["targets"]))
			success = result.success and result.committed
			(changed if success else failed).add(field_id)
			order_entry.update({"method": result.open_method, "success": success, "error": result.error or ""})
			code_pass["dropdown_attempts"].append({
				"field_id": field_id, "field": field.label, "targets": plan["targets"],
				"success": result.success, "selected": result.selected, "committed": result.committed,
				"detected_pattern": result.detected_pattern, "open_method": result.open_method,
				"visible_options": result.visible_options, "verification": result.verification,
				"actual_state": result.actual_state, "error": result.error or "",
			})
		code_pass["execution_order"].append(order_entry)
		code_pass["status"] = "sequential_writes_in_progress"
		tracker.current_stage = f"page_{tracker.pages_scanned}_field_{field.dom_order}_verified"
		if checkpoint:
			checkpoint()
	code_pass["status"] = "sequential_writes_verified"
	tracker.current_stage = f"page_{tracker.pages_scanned}_sequential_writes_verified"
	if checkpoint:
		checkpoint()

	filled = [known[field_id].label for field_id in known if field_id in changed]
	newly_filled_ids = {field_id for field_id in changed if not known[field_id].current_value}
	requested_by_id = {
		str(item["field_id"]): str(item.get("value", "")) for item in validated
	}
	requested_by_id.update({
		str(item["field_id"]): str(item.get("targets", [""])[-1])
		for item in dropdown_plans if item.get("targets")
	})
	corrected_ids = {
		field_id for field_id in changed
		if known[field_id].current_value and field_id in requested_by_id
		and _canonical_year_month(requested_by_id[field_id])
			!= _canonical_year_month(known[field_id].current_value)
	}
	verified_existing_ids = set(changed) - newly_filled_ids - corrected_ids
	observed_existing_ids = {
		field.id for field in safe_fields
		if field.current_value and field.id not in changed and field.id not in failed
		and field.id not in semantic_rejections and field.id not in solver_failed_ids
	}
	tracker.filled_labels.extend(label for label in filled if label not in tracker.filled_labels)
	filled_ids = set(changed)
	code_pass["write_failures"] = [
		{
			"field_id": field_id,
			"field": known[field_id].label,
			"value": (
				f"<secret:{secret_names_by_field[field_id]}>" if field_id in secret_field_ids else
				next((item["value"] for item in validated if item["field_id"] == field_id), "")
			),
			"targets": next((item["targets"] for item in dropdown_plans if item["field_id"] == field_id), []),
		}
		for field_id in failed if field_id in known
	]
	code_pass["applied_values"] = [
		{
			"field_id": assignment["field_id"],
			"field": known[assignment["field_id"]].label,
			"section": known[assignment["field_id"]].section,
			"card_context": known[assignment["field_id"]].card_context,
			"card_type": known[assignment["field_id"]].card_type,
			"card_index": known[assignment["field_id"]].card_index,
			"card_count": known[assignment["field_id"]].card_count,
			"value": (
				f"<secret:{secret_names_by_field[assignment['field_id']]}>"
				if assignment["field_id"] in secret_field_ids else assignment["value"]
			),
		}
		for assignment in validated
		if assignment["field_id"] in filled_ids
	]
	code_pass["applied_values"].extend(
		{
			"field_id": plan["field_id"],
			"field": known[str(plan["field_id"])].label,
			"section": known[str(plan["field_id"])].section,
			"card_context": known[str(plan["field_id"])].card_context,
			"card_type": known[str(plan["field_id"])].card_type,
			"card_index": known[str(plan["field_id"])].card_index,
			"card_count": known[str(plan["field_id"])].card_count,
			"targets": plan["targets"],
		}
		for plan in dropdown_plans
		if str(plan["field_id"]) in filled_ids
	)
	for field in safe_fields:
		if (
			field.id not in filled_ids and field.id not in observed_existing_ids
			and field.label not in tracker.deferred_labels
		):
			tracker.deferred_labels.append(field.label)
	code_pass["filled_fields"] = filled
	code_pass["newly_filled_fields"] = [known[field_id].label for field_id in known if field_id in newly_filled_ids]
	code_pass["corrected_fields"] = [known[field_id].label for field_id in known if field_id in corrected_ids]
	code_pass["verified_existing_fields"] = [known[field_id].label for field_id in known if field_id in verified_existing_ids]
	code_pass["observed_existing_fields"] = [
		field.label for field in fields
		if field.current_value and field.id not in failed and field.id not in corrected_ids
	]
	code_pass["field_counts"].update({
		"newly_filled": len(newly_filled_ids),
		"corrected": len(corrected_ids),
		"verified_existing": len(verified_existing_ids),
		"observed_existing_not_rewritten": len(code_pass["observed_existing_fields"]),
		"remaining_unresolved": sum(
			field.id not in filled_ids and field.id not in observed_existing_ids for field in safe_fields
		),
	})
	code_pass["deferred_fields"].extend(
		field.label for field in safe_fields
		if field.id not in filled_ids and field.id not in observed_existing_ids
	)
	code_pass["deferred_details"].extend(
		{
			"field_id": field.id,
			"field": field.label,
			"reason": (
				"write_not_persisted" if field.id in failed else
				"solver_failed" if field.id in solver_failed_ids else
				f"semantic_type_mismatch:{semantic_rejections[field.id]}" if field.id in semantic_rejections else
				"no_source_supported_value"
			),
			"card_context": field.card_context,
			"requested_value": (
				f"<secret:{secret_names_by_field[field.id]}>"
				if field.id in secret_field_ids else requested_by_id.get(field.id, "")
			),
			"dropdown_evidence": next(({
				"targets": attempt.get("targets", []),
				"selected": attempt.get("selected", []),
				"visible_options": attempt.get("visible_options", [])[:20],
				"verification": attempt.get("verification", ""),
				"actual_state": attempt.get("actual_state", ""),
				"error": attempt.get("error", ""),
			} for attempt in code_pass["dropdown_attempts"] if attempt.get("field_id") == field.id), {}),
		}
		for field in safe_fields if field.id not in filled_ids and field.id not in observed_existing_ids
	)
	code_pass["status"] = "completed"
	code_pass["completed_at"] = _now()
	tracker.current_stage = f"page_{tracker.pages_scanned}_completed"
	if checkpoint:
		checkpoint()
	return filled


async def fast_fill_until_stable(
	browser_session,
	llm,
	context: str,
	tracker: FastFillTracker,
	*,
	parallelism: int = 3,
	confirmed_defaults: dict[str, str] | None = None,
	available_sensitive_types: set[str] | None = None,
	sensitive_values: dict[str, str] | None = None,
	checkpoint: Callable[[], None] | None = None,
	max_passes: int = 2,
) -> list[str]:
	"""Run a fill pass followed by one evidence-backed review/repair pass."""
	all_filled: list[str] = []
	review_notes = ""
	for pass_index in range(max(1, min(max_passes, 2))):
		filled = await fast_fill_current_page(
			browser_session,
			llm,
			context,
			tracker,
			parallelism=parallelism,
			confirmed_defaults=confirmed_defaults,
			available_sensitive_types=available_sensitive_types,
			sensitive_values=sensitive_values,
			review_notes=review_notes,
			review_mode=pass_index > 0,
			checkpoint=checkpoint,
		)
		all_filled.extend(label for label in filled if label not in all_filled)
		code_pass = tracker.code_passes[-1] if tracker.code_passes else {}
		dependency_changed = any(
			item.get("success") and item.get("kind") in {"custom_select", "custom_cascader", "select"}
			for item in code_pass.get("execution_order", [])
		) or any(item.get("disabled") for item in code_pass.get("scanned_fields", []))
		actionable_review = bool(
			filled or code_pass.get("write_failures") or code_pass.get("deferred_details")
		)
		rescan = pass_index == 0 and actionable_review
		code_pass["dependency_rescan_scheduled"] = rescan
		code_pass["review_rescan_scheduled"] = rescan
		code_pass["dependency_changed"] = dependency_changed
		if rescan:
			review_payload = {
				"instruction": (
					"This is the second and final review. Re-evaluate omitted fields using deterministic CV inference, "
					"complete partially populated cards from their matched source record, and retry failed writes. "
					"Do not invent unsupported values. CURRENT_VALUE is the live page value; assign a "
					"replacement only when the CV clearly supports a different value."
				),
				"first_pass_write_failures": code_pass.get("write_failures", []),
				"first_pass_deferred": code_pass.get("deferred_details", []),
				"first_pass_applied": code_pass.get("applied_values", []),
			}
			review_notes = json.dumps(review_payload, ensure_ascii=False)[:30000]
		if checkpoint:
			checkpoint()
		if not rescan:
			break
	return all_filled
