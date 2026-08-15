"""Deterministic handling for native and common custom cascading dropdowns."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CascadingSelectParams(BaseModel):
	index: int = Field(description="Index of the first/closed cascading dropdown control")
	path: list[str] = Field(min_length=2, max_length=4, description='Ordered exact path, e.g. ["北京", "西城区"]')

	@field_validator("path")
	@classmethod
	def clean_path(cls, values: list[str]) -> list[str]:
		cleaned = [value.strip() for value in values]
		if any(not value for value in cleaned):
			raise ValueError("cascade path cannot contain empty levels")
		return cleaned


class CascadeResult(BaseModel):
	success: bool
	mode: Literal["native", "custom", "unknown"] = "unknown"
	selected: list[str] = Field(default_factory=list)
	committed: bool = False
	detected_pattern: str = "unknown"
	visible_options: list[str] = Field(default_factory=list)
	open_method: str = "unknown"
	verification: str = ""
	actual_state: str = ""
	error: str | None = None


MARK_TRIGGER_SCRIPT = r"""(token) => {
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const triggerSelector = 'select, [role="combobox"], [role="button"], [aria-haspopup], [aria-controls], [aria-owns], [class*="select"], [class*="dropdown"], [class*="cascader"], [class*="picker"], [class*="choice"]';
  const mtdRoot = this.closest?.('.mtd-select-filter, .mtd-select, .mtd-cascader');
  const mtdButton = mtdRoot?.matches?.('[role="button"]') ? mtdRoot :
    mtdRoot?.querySelector?.(':scope > [role="button"], [role="button"]');
  const trigger = mtdButton || (this.matches?.(triggerSelector) ? this : this.closest?.(triggerSelector)) || this;
  if (!visible(trigger)) return {tagged: false, error: 'resolved dropdown trigger is not visible'};
  trigger.setAttribute('data-browseragent-real-trigger', token);
	const popupSelector = '[role="listbox"], [role="menu"], [role="tree"], .mtd-select-popup, .mtd-select-popup-wrapper, .mtd-select-dropdown, .mtd-select-filter-dropdown, .mtd-dropdown-menu, .ant-select-dropdown, .el-select-dropdown, [class*="select-dropdown"]';
	[...document.querySelectorAll(popupSelector)].filter(visible).forEach(popup =>
	  popup.setAttribute('data-browseragent-preexisting-popup', token));
  return {tagged: true, tag: trigger.tagName.toLowerCase(), role: trigger.getAttribute('role') || '',
    classes: String(trigger.className || '').slice(0, 300)};
}"""


CLEAR_TRIGGER_SCRIPT = r"""(token) => {
  document.querySelectorAll('[data-browseragent-real-trigger]').forEach(el => {
    if (el.getAttribute('data-browseragent-real-trigger') === token) el.removeAttribute('data-browseragent-real-trigger');
  });
	document.querySelectorAll('[data-browseragent-preexisting-popup]').forEach(el => {
	  if (el.getAttribute('data-browseragent-preexisting-popup') === token) el.removeAttribute('data-browseragent-preexisting-popup');
	});
	document.querySelectorAll('[data-browseragent-real-option]').forEach(el => {
	  if (el.getAttribute('data-browseragent-real-option') === token) el.removeAttribute('data-browseragent-real-option');
	});
}"""


# Verification is deliberately separate from the selection transaction. Some
# autocomplete widgets briefly mirror the search query in a label and then
# clear it after blur, which otherwise looks exactly like a committed choice.
VERIFY_SELECTION_SCRIPT = r"""async (wanted) => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const normalize = text => String(text || '').replace(/[\s　]+/g, '').replace(/[：:]+$/g, '');
  const administrative = text => normalize(text).replace(/(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$/u, '');
  const matches = (actual, target) => normalize(actual) === normalize(target)
    || administrative(actual) === administrative(target);
  const root = this.closest?.('.mtd-select-filter, .mtd-select, .mtd-cascader, [role="combobox"], [class*="select"], [class*="dropdown"], [class*="cascader"]') || this;
  const popupSelector = '[role="listbox"], [role="menu"], [role="tree"], .mtd-select-popup, .mtd-select-popup-wrapper, .mtd-select-dropdown, .ant-select-dropdown, .el-select-dropdown, [class*="select-dropdown"]';
  const closeTransientState = () => {
    [...root.querySelectorAll?.('input:not([disabled])') || []].forEach(input => input.blur());
    if (root.contains(document.activeElement)) document.activeElement?.blur?.();
  };
  const read = () => {
    if (root.tagName === 'SELECT') {
      return String(root.selectedOptions?.[0]?.textContent || root.value || '').trim();
    }
    const selected = [...root.querySelectorAll?.(
      '[aria-selected="true"], [class*="selected"], [class*="value"], [class*="label"]'
    ) || []].filter(visible).filter(el =>
      !/(hint|placeholder|option|dropdown|popup|search)/i.test(String(el.className || ''))
    ).map(el => String(el.value || el.innerText || el.textContent || '').trim()).filter(Boolean);
    const readonlyValues = [...root.querySelectorAll?.('input[readonly], input[type="hidden"]') || []]
      .map(input => String(input.value || '').trim()).filter(Boolean);
    // Editable inputs are evidence only after the popup is closed and blur has
    // settled. This supports comboboxes that store their committed value in the
    // visible input without accepting a still-open search query as success.
    const popupOpen = [...document.querySelectorAll(popupSelector)].some(popup => visible(popup) && !root.contains(popup));
    const editableValues = popupOpen ? [] : [...root.querySelectorAll?.('input:not([readonly]):not([type="hidden"])') || []]
      .map(input => String(input.value || '').trim()).filter(Boolean);
    return [...new Set([...selected, ...readonlyValues, ...editableValues])].join('|');
  };
  closeTransientState();
  await wait(700);
  const first = read();
  await wait(500);
  const second = read();
  const stable = normalize(first) === normalize(second);
  const persisted = stable && second.split('|').some(value =>
    matches(value, wanted) || normalize(value).includes(normalize(wanted))
  );
  return {persisted, stable, first, second};
}"""


PREPARE_TRUSTED_OPTION_SCRIPT = r"""(params) => (async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0
      && rect.width > 0 && rect.height > 0;
  };
  const normalize = text => String(text || '').replace(/[\s　]+/g, '').replace(/[：:]+$/g, '');
  const administrative = text => normalize(text).replace(/(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$/u, '');
  const matches = (actual, wanted) => normalize(actual) === normalize(wanted)
    || administrative(actual) === administrative(wanted);
  const rootNode = document.querySelector(params.root_selector);
  if (!rootNode) return {found: false, error: 'trusted-option root unavailable', visible_options: []};
  const root = rootNode.closest?.('.mtd-select-filter, .mtd-select, .mtd-cascader, [role="combobox"], [class*="select"], [class*="dropdown"]') || rootNode;
  const trigger = root.matches?.('[role="button"]') ? root : root.querySelector?.(':scope > [role="button"], [role="button"]') || root;
  const popupSelector = '[role="listbox"], [role="menu"], [role="tree"], .mtd-select-popup, .mtd-select-popup-wrapper, .mtd-select-dropdown, .mtd-select-filter-dropdown, .mtd-dropdown-menu, .ant-select-dropdown, .el-select-dropdown, [class*="select-dropdown"]';
  const optionSelector = '[role="option"], [role="menuitem"], [role="treeitem"], .mtd-select-item, .mtd-select-item-content, .mtd-select-option, .mtd-select-dropdown-item, .mtd-select-filter-option, .mtd-select-filter-item, .mtd-dropdown-menu-item, .ant-select-item-option, .el-select-dropdown__item, li, label';
  const roots = () => [...document.querySelectorAll(popupSelector)].filter(visible).filter(popup => !root.contains(popup));
  const candidates = () => {
    const raw = roots().flatMap(popup => [...popup.querySelectorAll(optionSelector)]).filter(visible)
      .filter(item => matches(item.innerText || item.textContent, params.wanted));
    const promoted = raw.map(item => item.closest(
      '[role="option"], [role="menuitem"], [role="treeitem"], .mtd-select-item, .mtd-select-option, .mtd-select-dropdown-item, .mtd-select-filter-option, .mtd-select-filter-item, .mtd-dropdown-menu-item, label'
    ) || item);
    const unique = [...new Set(promoted)];
    return unique.filter(item => !unique.some(other => other !== item && item.contains(other)));
  };
  if (!roots().length) {
    trigger.click();
    await wait(300);
  }
  let exact = candidates();
  if (!exact.length) {
    const inputs = [...new Set([
      ...[...root.querySelectorAll?.('input:not([readonly]):not([disabled])') || []],
      ...roots().flatMap(popup => [...popup.querySelectorAll('input:not([readonly]):not([disabled])')]),
    ])].filter(visible);
    if (inputs.length === 1) {
      const input = inputs[0];
      const before = String(input.value || '');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      setter ? setter.call(input, params.wanted) : (input.value = params.wanted);
      input._valueTracker?.setValue?.(before);
      input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: params.wanted}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      await wait(600);
      exact = candidates();
    }
  }
  const samples = [...new Set(roots().flatMap(popup => [...popup.querySelectorAll(optionSelector)])
    .filter(visible).map(item => String(item.innerText || item.textContent || '').trim()).filter(Boolean))].slice(0, 30);
  if (exact.length !== 1) return {
    found: false, error: exact.length ? 'ambiguous exact trusted option' : 'exact trusted option unavailable',
    visible_options: samples,
  };
  exact[0].setAttribute('data-browseragent-real-option', params.token);
  exact[0].scrollIntoView({block: 'nearest', inline: 'nearest'});
  return {found: true, visible_options: samples};
})()"""


TRY_SYNTHETIC_OPEN_SCRIPT = r"""async (wanted) => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0 &&
	  rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  };
  const normalize = text => String(text || '').replace(/[\s　]+/g, '').replace(/[：:]+$/g, '');
  const root = this.closest?.('.mtd-select-filter, .mtd-select, .mtd-cascader') || this;
  const trigger = root.matches?.('[role="button"]') ? root : root.querySelector?.(':scope > [role="button"], [role="button"]') || root;
	const popupSelector = '[role="listbox"], [role="menu"], [role="tree"], .mtd-select-popup, .mtd-select-popup-wrapper, .mtd-select-dropdown, .mtd-select-filter-dropdown, .mtd-dropdown-menu, .ant-select-dropdown, .el-select-dropdown, [class*="select-dropdown"]';
	const beforePopups = new Set([...document.querySelectorAll(popupSelector)].filter(visible));
	const beforeExpanded = trigger.getAttribute('aria-expanded');
  trigger.click();
  await wait(250);
  const popups = [...document.querySelectorAll(popupSelector)].filter(visible).filter(el => !root.contains(el));
	const newPopups = popups.filter(el => !beforePopups.has(el));
  const optionSelector = '[role="option"], [role="menuitem"], [role="treeitem"], .mtd-select-item, .mtd-select-item-content, .mtd-select-option, .mtd-select-dropdown-item, .mtd-select-filter-option, .mtd-select-filter-item, .mtd-dropdown-menu-item, li, label';
  const texts = [...new Set(popups.flatMap(popup => [...popup.querySelectorAll(optionSelector)])
    .filter(visible).map(el => normalize(el.innerText || el.textContent)).filter(Boolean))];
  const target = normalize(wanted);
  const targetVisible = texts.some(text => text === target || (target.length >= 2 && (text.includes(target) || target.includes(text))));
	const expandedNow = trigger.getAttribute('aria-expanded') === 'true' && beforeExpanded !== 'true';
	// An unrelated/stale portal must not suppress the trusted-click fallback.
	// The previous implementation treated any visible popup on the page as this
	// control being open, which is exactly how 性别 saw only “请选择”.
  return {opened: targetVisible || newPopups.length > 0 || expandedNow,
	  visible_options: texts.slice(0, 40), new_popup_count: newPopups.length};
}"""


CASCADE_SCRIPT = r"""async (path) => {
  const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0 &&
	  rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  };
  const normalize = (text) => String(text || '').replace(/[\s　]+/g, '').replace(/[：:]+$/g, '');
  const administrative = (text) => normalize(text).replace(/(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$/u, '');
  const semantic = (text) => normalize(text)
    .replace(/^(?:第一作者|第1作者|1作)$/u, '一作')
    .replace(/^(?:第二作者|第2作者|2作)$/u, '二作')
    .replace(/^(?:第三作者|第3作者|3作)$/u, '三作');
  const matches = (actual, wanted) => semantic(actual) === semantic(wanted) || administrative(actual) === administrative(wanted);
  const aliasMatches = (actual, wanted) => {
    const actualText = normalize(actual);
    const wantedText = normalize(wanted);
    return wantedText.length >= 2 && (actualText.includes(wantedText) || wantedText.includes(actualText));
  };
  const setNativeValue = (select, option) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
    setter ? setter.call(select, option.value) : (select.value = option.value);
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
    select.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const selectOption = (select, wanted) => {
    const candidates = [...select.options].filter(option => option.value && matches(option.textContent, wanted));
    if (candidates.length !== 1) throw new Error(candidates.length ? `ambiguous option: ${wanted}` : `option not found: ${wanted}`);
    setNativeValue(select, candidates[0]);
  };
	const triggerSelector = 'select, [role="combobox"], [role="button"], [aria-haspopup], [aria-controls], [aria-owns], [class*="select"], [class*="dropdown"], [class*="cascader"], [class*="picker"], [class*="choice"]';
	const triggerScore = (el, depth) => {
	  if (!el || !visible(el)) return -1;
	  let score = (el === this ? 1 : 0) - depth;
	  const role = el.getAttribute('role') || '';
	  const classes = String(el.className || '');
	  if (el.tagName === 'SELECT') score += 20;
	  if (role === 'combobox') score += 10;
	  if (el.hasAttribute('aria-haspopup')) score += 8;
	  if (el.hasAttribute('aria-expanded') || el.hasAttribute('aria-controls') || el.hasAttribute('aria-owns')) score += 5;
	  if (/(^|[-_])(select|dropdown|cascader|picker|choice)([-_]|$)/i.test(classes)) score += 4;
	  if (el.getAttribute('role') === 'button' || el.tabIndex >= 0) score += 2;
	  if (el.querySelector?.('input, [class*="arrow"], [class*="icon"]')) score += 1;
	  if (el !== this && /(input|search|textbox|editor)/i.test(classes) &&
	      !el.hasAttribute('aria-haspopup') && !el.hasAttribute('aria-controls') && role !== 'combobox') score -= 7;
	  if (el.tagName === 'INPUT' && !el.hasAttribute('aria-haspopup') && role !== 'combobox') score -= 5;
	  return score;
	};
	const ancestors = [];
	for (let el = this, depth = 0; el && el !== document.body && depth < 7; el = el.parentElement, depth++) {
	  if (el === this || el.matches?.(triggerSelector)) ancestors.push({el, depth});
	}
	// MTD indexes the internal text input, but the element that owns the open
	// interaction is its sibling/ancestor role=button. Clicking the input wrapper
	// leaves the portal closed and therefore exposes zero options.
	const mtdRoot = this.closest?.('.mtd-select-filter, .mtd-select, .mtd-cascader');
	const mtdButton = mtdRoot?.matches?.('[role="button"]') ? mtdRoot :
	  mtdRoot?.querySelector?.(':scope > [role="button"], [role="button"]');
	const trigger = mtdButton || ancestors.sort((a, b) => triggerScore(b.el, b.depth) - triggerScore(a.el, a.depth))[0]?.el ||
	  this.closest?.(triggerSelector) || this;
	const stateRoot = mtdRoot || trigger;
	const stateText = () => normalize([
	  trigger.getAttribute('aria-label'), trigger.getAttribute('title'),
	  ...[...stateRoot.querySelectorAll?.('[aria-selected="true"], [class*="selected"], [class*="value"], [class*="label"]') || []]
	    .map(el => el.value || el.innerText || el.textContent), stateRoot.innerText
	].filter(Boolean).join('|'));
	const initialState = stateText();
	const beforeVisible = new Set([...document.querySelectorAll('body *')].filter(visible));
	const rootFor = (el) => {
	  let fallback = null;
	  for (let node = el; node && node !== document.body; node = node.parentElement) {
	    const style = getComputedStyle(node);
	    if (['dialog', 'listbox', 'tree', 'menu'].includes(node.getAttribute('role'))) return node;
	    if (['fixed', 'absolute'].includes(style.position) || Number(style.zIndex) > 1) fallback = node;
	  }
	  return fallback;
	};
	// Inspection may have already opened the widget. Adopt a visible popup that
	// contains the exact first target instead of toggling the trigger closed.
	const targetPreopenedRoots =
	  [...document.querySelectorAll('body *')]
	    .filter(visible).filter(el => !trigger.contains(el))
	    .filter(el => matches(el.innerText || el.textContent, path[0]))
	    // Keep leaf-like exact text nodes. A parent whose child has the same text
	    // is only a wrapper and would make the candidate ambiguous.
	    .filter(el => ![...el.children].some(child => visible(child) && matches(child.innerText || child.textContent, path[0])))
	    .map(rootFor).filter(Boolean);
	// A real CDP click happens before this script starts, so an already-open
	// portal is part of the initial DOM snapshot. Adopt framework dropdown roots
	// independently of the requested text; otherwise a semantic target such as
	// “身份证” cannot discover the visible exact option “居民身份证” and a second
	// trigger click would merely toggle the menu closed.
	const frameworkPreopenedRoots = [...document.querySelectorAll([
	  '[role="listbox"]', '[role="menu"]', '[role="tree"]',
	  '.mtd-select-popup', '.mtd-select-popup-wrapper', '.mtd-select-dropdown', '.mtd-select-filter-dropdown', '.mtd-dropdown-menu',
	  '.ant-select-dropdown', '.el-select-dropdown', '[class*="select-dropdown"]'
	].join(','))].filter(visible).filter(el => !trigger.contains(el))
	  .filter(el => !el.hasAttribute('data-browseragent-preexisting-popup'));
	const preopenedRoots = [...new Set([...targetPreopenedRoots, ...frameworkPreopenedRoots])];
	const openTrigger = async () => {
	  trigger.scrollIntoView({block: 'center'});
	  trigger.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true, pointerType: 'mouse'}));
	  trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
	  trigger.click();
	  await wait(800);
	};
	let activeRoots = [];
	let detectedPattern = 'unknown';
	const refreshRoots = () => {
	  const controlledId = trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns');
	  const controlled = controlledId ? document.getElementById(controlledId) : null;
	  const semantic = [...document.querySelectorAll('[role="dialog"], [role="listbox"], [role="tree"], [role="menu"]')]
	    .filter(visible).filter(el => !beforeVisible.has(el));
	  const positioned = [...document.querySelectorAll('body *')].filter(el => {
	    if (!visible(el) || beforeVisible.has(el) || trigger.contains(el)) return false;
	    const style = getComputedStyle(el);
	    return ['fixed', 'absolute'].includes(style.position) || Number(style.zIndex) > 1;
	  });
	  const newestTopLevel = positioned.filter(el => !positioned.some(parent => parent !== el && parent.contains(el)));
	  const inline = [...trigger.parentElement?.querySelectorAll?.('[role="listbox"], [role="tree"], [role="menu"], ul, ol') || []]
	    .filter(visible).filter(el => !trigger.contains(el));
	  const adopted = preopenedRoots.filter(visible);
	  activeRoots = [...new Set([...(visible(controlled) ? [controlled] : []), ...adopted, ...semantic, ...newestTopLevel, ...inline])];
	  detectedPattern = visible(controlled) ? 'aria-controlled' : semantic.length ? 'semantic-overlay' :
	    adopted.length ? 'preopened-overlay' : newestTopLevel.length ? 'dom-diff-overlay' : 'inline-options';
	};
	const commitCustomPopup = async (lastOption, lastWanted) => {
	  const confirmTexts = new Set(['确定', '确认']);
	  refreshRoots();
	  const clickables = activeRoots.flatMap(root => [...root.querySelectorAll('button, [role="button"], a, input[type="button"], input[type="submit"], [tabindex]')]);
	  const confirms = [...new Set(clickables)].filter(visible)
	    .filter(item => !item.disabled && item.getAttribute('aria-disabled') !== 'true')
	    .filter(item => confirmTexts.has(normalize(item.value || item.innerText || item.textContent)));
	  if (confirms.length > 1) return {committed: false, error: 'ambiguous confirm buttons inside active popup'};
	  if (confirms.length === 1) {
	    confirms[0].scrollIntoView({block: 'nearest'});
	    confirms[0].click();
	  }
	  const deadline = Date.now() + 3000;
	  let blurredForVerification = false;
	  while (Date.now() < deadline) {
	    refreshRoots();
	    const persisted = matches(stateText(), lastWanted) || stateText().includes(normalize(lastWanted));
	    if (persisted) return {committed: true, method: confirms.length ? 'confirm' : 'verified-control-state'};
	    const closed = !visible(lastOption) || activeRoots.every(root => !visible(root));
	    const editableInputs = [...stateRoot.querySelectorAll?.('input') || []].filter(input => !input.disabled);
	    if (closed && !blurredForVerification) {
	      editableInputs.forEach(input => input.blur());
	      blurredForVerification = true;
	      await wait(200);
	      continue;
	    }
	    const stableInput = closed && blurredForVerification && editableInputs.length === 1 &&
	      matches(editableInputs[0].value, lastWanted);
	    if (stableInput) return {committed: true, method: 'verified-after-blur'};
	    await wait(100);
	  }
	  return {committed: false, error: `selection could not be verified; trigger before=${initialState || '(empty)'}; after=${stateText() || '(empty)'}`};
	};

	if (trigger.tagName === 'SELECT') {
    const all = [...document.querySelectorAll('select')].filter(visible);
	const start = all.indexOf(trigger);
    if (start < 0) return {success: false, mode: 'native', selected: [], error: 'starting select is unavailable'};
    const selected = [];
	let current = trigger;
    for (let level = 0; level < path.length; level++) {
      if (level > 0) {
        const deadline = Date.now() + 4000;
        while (Date.now() < deadline) {
          const refreshed = [...document.querySelectorAll('select')].filter(visible);
          const prior = refreshed.indexOf(current);
          const candidate = prior >= 0 ? refreshed.slice(prior + 1).find(item => !item.disabled) : null;
          if (candidate && [...candidate.options].some(option => option.value && matches(option.textContent, path[level]))) {
            current = candidate;
            break;
          }
          await wait(100);
        }
      }
      try {
        selectOption(current, path[level]);
      } catch (error) {
        return {success: false, mode: 'native', selected, error: error.message};
      }
      selected.push(path[level]);
      await wait(200);
    }
	return {success: true, mode: 'native', selected, committed: true};
  }

  const optionSelector = [
    '[role="option"]', '[role="menuitem"]', '[role="treeitem"]',
    '.ant-cascader-menu-item', '.ant-select-item-option', '.el-cascader-node',
	'.el-select-dropdown__item', '.mtd-select-option', '.mtd-select-dropdown-item',
	'.mtd-select-item', '.mtd-select-item-content', '.mtd-select-filter-option', '.mtd-select-filter-item', '.mtd-dropdown-menu-item',
	'.mtd-cascader-menu-item', '.mtd-radio', '.mtd-checkbox',
	'[class*="select-option"]', '[class*="option-item"]', 'li[class*="cascader"]', '[class*="radio"]', 'label'
  ].join(',');
	const visibleOptions = () => {
	  refreshRoots();
	  const candidates = activeRoots.flatMap(root => [...root.querySelectorAll(
	    `${optionSelector}, li, label, button, [role], [tabindex], input[type="radio"], input[type="checkbox"]`
	  )]);
	  return [...new Set(candidates)].filter(visible)
	    .filter(item => !trigger.contains(item) && !item.disabled && item.getAttribute('aria-disabled') !== 'true');
	};
	const candidateOptions = (wanted) => {
	  const available = visibleOptions();
	  const exact = available.filter(item => matches(item.innerText || item.textContent, wanted));
	  // Exact text always wins. A containment alias is allowed only when the
	  // complete visible menu yields one unique leaf candidate.
	  const raw = exact.length ? exact : available.filter(item => aliasMatches(item.innerText || item.textContent, wanted));
	  const promoted = raw.map(item => item.closest(
	    '[role="option"], [role="menuitem"], [role="treeitem"], .mtd-select-item, .mtd-select-option, .mtd-select-dropdown-item, .mtd-select-filter-option, .mtd-select-filter-item, .mtd-dropdown-menu-item, .mtd-cascader-menu-item, .mtd-radio, .mtd-checkbox, label'
	  ) || item);
	  const unique = [...new Set(promoted)];
	  return unique.filter(item => !unique.some(other => other !== item && item.contains(other)));
	};
	const usedFilterInputs = new Set();
	const activateOption = async option => {
	  option.scrollIntoView({block: 'nearest'});
	  for (const [type, EventType] of [
	    ['pointerdown', PointerEvent], ['mousedown', MouseEvent],
	    ['pointerup', PointerEvent], ['mouseup', MouseEvent]
	  ]) option.dispatchEvent(new EventType(type, {bubbles: true, cancelable: true, view: window, pointerType: 'mouse'}));
	  option.click();
	  await wait(100);
	};
	const setInputValue = (input, value) => {
	  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
	  setter ? setter.call(input, value) : (input.value = value);
	  input.dispatchEvent(new Event('input', {bubbles: true}));
	  input.dispatchEvent(new Event('change', {bubbles: true}));
	};
	const clearFilters = () => usedFilterInputs.forEach(input => {
	  if (input?.isConnected) setInputValue(input, '');
	});
	const filterFor = async (wanted) => {
	  refreshRoots();
	  const inputs = [...new Set([
	    ...[...stateRoot.querySelectorAll?.('input:not([readonly]):not([disabled])') || []],
	    ...activeRoots.flatMap(root => [...root.querySelectorAll('input:not([readonly]):not([disabled])')])
	  ])].filter(visible).filter(input => {
	    const identity = `${input.type || ''} ${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`;
	    return !input.value && /text|search|输入|搜索|查找|select|choose|name/i.test(identity);
	  });
	  if (inputs.length !== 1) return false;
	  const input = inputs[0];
	  usedFilterInputs.add(input);
	  setInputValue(input, wanted);
	  await wait(500);
	  return true;
	};
  const selected = [];
	let lastOption = null;
	refreshRoots();
	const firstAlreadyVisible = activeRoots.length > 0 && candidateOptions(path[0]).length > 0;
	if (!firstAlreadyVisible) {
	  await openTrigger();
	}
  for (const wanted of path) {
    let candidates = [];
	let filtered = false;
    const deadline = Date.now() + 4000;
    while (Date.now() < deadline) {
	  candidates = candidateOptions(wanted);
      if (candidates.length) break;
	  if (!filtered) filtered = await filterFor(wanted);
      await wait(100);
    }
	if (candidates.length !== 1) {
	  const samples = [...new Set(visibleOptions().map(item => normalize(item.innerText || item.textContent)).filter(Boolean))].slice(0, 30);
	  clearFilters();
	  return {success: false, mode: 'custom', selected, committed: false, detected_pattern: detectedPattern,
		visible_options: samples,
		error: candidates.length ? `ambiguous visible option: ${wanted}; visible options: ${samples.join(' | ')}` :
		  `visible option not found: ${wanted}; visible options: ${samples.join(' | ') || '(none)'}`};
    }
	await activateOption(candidates[0]);
	lastOption = candidates[0];
    selected.push(wanted);
    await wait(250);
  }
	const commit = await commitCustomPopup(lastOption, path.at(-1));
	if (!commit.committed) {
	  clearFilters();
	  return {success: false, mode: 'custom', selected, committed: false, error: commit.error};
	}
	return {success: true, mode: 'custom', selected, committed: true, detected_pattern: detectedPattern,
	  visible_options: [...new Set(visibleOptions().map(item => normalize(item.innerText || item.textContent)).filter(Boolean))].slice(0, 30)};
}"""


# Page-level variant used by code-first filling. BrowserUse's Element.evaluate
# binds `this` for us; Page.evaluate does not, so wrap the same tested executor
# in a normal function and bind it to the previously scanned DOM control.
_PAGE_CASCADE_FUNCTION = CASCADE_SCRIPT.replace("async (path) => {", "async function(path) {", 1)
FAST_CASCADE_SCRIPT = (
	"(params) => {\n"
	"  const target = document.querySelector(`[data-browseragent-fast-id=\"${CSS.escape(params.field_id)}\"]`);\n"
	"  if (!target) return {success: false, mode: 'unknown', selected: [], committed: false, "
	"error: `fast dropdown ${params.field_id} is unavailable`};\n"
	f"  const execute = {_PAGE_CASCADE_FUNCTION};\n"
	"  return execute.call(target, params.path);\n"
	"}"
)

_PAGE_SYNTHETIC_OPEN_FUNCTION = TRY_SYNTHETIC_OPEN_SCRIPT.replace("async (wanted) => {", "async function(wanted) {", 1)
FAST_SYNTHETIC_OPEN_SCRIPT = (
	"(params) => {\n"
	"  const target = document.querySelector(`[data-browseragent-fast-id=\"${CSS.escape(params.field_id)}\"]`);\n"
	"  if (!target) return {opened: false, visible_options: []};\n"
	f"  const execute = {_PAGE_SYNTHETIC_OPEN_FUNCTION};\n"
	"  return execute.call(target, params.wanted);\n"
	"}"
)

_PAGE_MARK_TRIGGER_FUNCTION = MARK_TRIGGER_SCRIPT.replace("(token) => {", "function(token) {", 1)
FAST_MARK_TRIGGER_SCRIPT = (
	"(params) => {\n"
	"  const target = document.querySelector(`[data-browseragent-fast-id=\"${CSS.escape(params.field_id)}\"]`);\n"
	"  if (!target) return {tagged: false, error: 'fast control unavailable'};\n"
	f"  const execute = {_PAGE_MARK_TRIGGER_FUNCTION};\n"
	"  return execute.call(target, params.token);\n"
	"}"
)

_PAGE_VERIFY_SELECTION_FUNCTION = VERIFY_SELECTION_SCRIPT.replace("async (wanted) => {", "async function(wanted) {", 1)
FAST_VERIFY_SELECTION_SCRIPT = (
	"(params) => {\n"
	"  const target = document.querySelector(`[data-browseragent-fast-id=\"${CSS.escape(params.field_id)}\"]`);\n"
	"  if (!target) return {persisted: false, stable: false, first: '', second: '', error: 'fast control unavailable'};\n"
	f"  const execute = {_PAGE_VERIFY_SELECTION_FUNCTION};\n"
	"  return execute.call(target, params.wanted);\n"
	"}"
)

TRIGGER_POINT_SCRIPT = r"""(selector) => (async () => {
  const trigger = document.querySelector(selector);
  if (!trigger) return JSON.stringify({found: false});
  trigger.scrollIntoView({block: 'center', inline: 'nearest'});
  await new Promise(resolve => setTimeout(resolve, 250));
  const rect = trigger.getBoundingClientRect();
  return JSON.stringify({found: rect.width > 0 && rect.height > 0,
    x: rect.left + rect.width / 2, y: rect.top + rect.height / 2});
})()"""


async def _trusted_click_selector(page, selector: str) -> bool:
	"""Click the post-scroll centre; BrowserUse Element.click caches pre-scroll coordinates."""
	if not hasattr(type(page), "mouse"):
		triggers = await page.get_elements_by_css_selector(selector)
		if len(triggers) != 1:
			return False
		await triggers[0].click()
		return True
	try:
		point = json.loads(await page.evaluate(TRIGGER_POINT_SCRIPT, selector))
		if not point.get("found"):
			return False
		mouse = await page.mouse
		await mouse.move(round(point["x"]), round(point["y"]))
		await asyncio.sleep(0.05)
		await mouse.click(round(point["x"]), round(point["y"]))
		return True
	except (AttributeError, NotImplementedError):
		# Compatibility for lightweight test doubles and older actor pages.
		triggers = await page.get_elements_by_css_selector(selector)
		if len(triggers) != 1:
			return False
		await triggers[0].click()
		return True


async def _trusted_option_click(page, root_selector: str, wanted: str, token: str) -> tuple[bool, list[str], str]:
	"""Prepare one exact visible option in DOM, then activate it with a trusted click."""
	try:
		raw = await page.evaluate(PREPARE_TRUSTED_OPTION_SCRIPT, {
			"root_selector": root_selector, "wanted": wanted, "token": token,
		})
		prepared = json.loads(raw or "{}")
		options = [str(item) for item in prepared.get("visible_options", [])]
		if not prepared.get("found"):
			return False, options, str(prepared.get("error") or "exact option unavailable")
		clicked = await _trusted_click_selector(page, f'[data-browseragent-real-option="{token}"]')
		if not clicked:
			return False, options, "trusted option click unavailable"
		await asyncio.sleep(0.35)
		return True, options, ""
	except asyncio.CancelledError:
		raise
	except Exception as exc:
		return False, [], f"{type(exc).__name__}: {exc}"


async def _verify_element_selection(element, result: CascadeResult, wanted: str) -> CascadeResult:
	"""Reject a selection that disappears after popup closure and framework settle."""
	if not (result.success and result.committed):
		return result
	try:
		state = json.loads(await element.evaluate(VERIFY_SELECTION_SCRIPT, wanted) or "{}")
	except asyncio.CancelledError:
		raise
	except Exception as exc:
		return result.model_copy(update={
			"success": False, "committed": False, "verification": "verification_error",
			"error": f"post-selection verification failed: {type(exc).__name__}: {exc}",
		})
	if state.get("persisted"):
		return result.model_copy(update={
			"verification": "stable_after_blur", "actual_state": str(state.get("second", "")),
		})
	actual = str(state.get("second", ""))
	return result.model_copy(update={
		"success": False, "committed": False, "verification": "reverted_after_blur",
		"actual_state": actual,
		"error": f"selection reverted after blur; expected={wanted}; actual={actual or '(empty)'}",
	})


async def _verify_fast_selection(page, field_id: str, result: CascadeResult, wanted: str) -> CascadeResult:
	"""Page-level equivalent used by the code-first scanned control executor."""
	if not (result.success and result.committed):
		return result
	try:
		raw = await page.evaluate(
			FAST_VERIFY_SELECTION_SCRIPT, {"field_id": field_id, "wanted": wanted},
		)
		state = json.loads(raw or "{}")
	except asyncio.CancelledError:
		raise
	except Exception as exc:
		return result.model_copy(update={
			"success": False, "committed": False, "verification": "verification_error",
			"error": f"post-selection verification failed: {type(exc).__name__}: {exc}",
		})
	if state.get("persisted"):
		return result.model_copy(update={
			"verification": "stable_after_blur", "actual_state": str(state.get("second", "")),
		})
	actual = str(state.get("second", ""))
	return result.model_copy(update={
		"success": False, "committed": False, "verification": "reverted_after_blur",
		"actual_state": actual,
		"error": f"selection reverted after blur; expected={wanted}; actual={actual or '(empty)'}",
	})


def _should_retry_trusted_option(result: CascadeResult, path: list[str]) -> bool:
	if len(path) != 1 or result.success:
		return False
	# A one-level semantic target is already source-validated by the caller. Any
	# synthetic failure may mean the framework ignored untrusted events, including
	# the common case where the script reports an opened trigger but sees no menu.
	return True


async def _retry_fast_trusted_option(page, field_id: str, path: list[str], result: CascadeResult, token: str) -> CascadeResult:
	if not _should_retry_trusted_option(result, path):
		return result
	root_selector = f'[data-browseragent-fast-id="{field_id}"]'
	# First reuse any portal that is genuinely open. Only when option preparation
	# still sees no menu do we issue a trusted trigger click and inspect again;
	# this avoids closing a valid but initially slow/searchable popup.
	clicked, options, error = await _trusted_option_click(page, root_selector, path[0], token)
	if not clicked and not options:
		try:
			await _trusted_click_selector(page, root_selector)
			await asyncio.sleep(0.25)
		except (AttributeError, NotImplementedError):
			pass
		clicked, retry_options, retry_error = await _trusted_option_click(page, root_selector, path[0], token)
		options = retry_options or options
		error = retry_error or error
	if not clicked:
		return result.model_copy(update={
			"visible_options": options or result.visible_options,
			"error": f"{result.error or 'synthetic selection failed'}; trusted fallback: {error}",
		})
	candidate = result.model_copy(update={
		"success": True, "committed": True, "selected": [path[0]],
		"visible_options": options or result.visible_options, "open_method": "cdp-option-click",
		"verification": "", "actual_state": "", "error": None,
	})
	return await _verify_fast_selection(page, field_id, candidate, path[0])


async def _retry_element_trusted_option(
	browser_session, element, path: list[str], result: CascadeResult, token: str,
) -> CascadeResult:
	if not _should_retry_trusted_option(result, path):
		return result
	try:
		marked = json.loads(await element.evaluate(MARK_TRIGGER_SCRIPT, token) or "{}")
		if not marked.get("tagged"):
			return result
		page = await browser_session.must_get_current_page()
		root_selector = f'[data-browseragent-real-trigger="{token}"]'
		clicked, options, error = await _trusted_option_click(page, root_selector, path[0], token)
		if not clicked and not options:
			try:
				await _trusted_click_selector(page, root_selector)
				await asyncio.sleep(0.25)
			except (AttributeError, NotImplementedError):
				pass
			clicked, retry_options, retry_error = await _trusted_option_click(page, root_selector, path[0], token)
			options = retry_options or options
			error = retry_error or error
		if not clicked:
			return result.model_copy(update={
				"visible_options": options or result.visible_options,
				"error": f"{result.error or 'synthetic selection failed'}; trusted fallback: {error}",
			})
		candidate = result.model_copy(update={
			"success": True, "committed": True, "selected": [path[0]],
			"visible_options": options or result.visible_options, "open_method": "cdp-option-click",
			"verification": "", "actual_state": "", "error": None,
		})
		return await _verify_element_selection(element, candidate, path[0])
	except asyncio.CancelledError:
		raise
	except Exception as exc:
		return result.model_copy(update={
			"error": f"{result.error or 'synthetic selection failed'}; trusted fallback error: {type(exc).__name__}: {exc}",
		})


async def select_cascade_path(browser_session, index: int, path: list[str]) -> CascadeResult:
	"""Select and commit one custom/native dropdown path."""
	node = await browser_session.get_element_by_index(index)
	if node is None:
		return CascadeResult(success=False, error=f"Element index {index} is unavailable")
	from browser_use.actor.element import Element

	element = Element(browser_session, node.backend_node_id, node.session_id)
	token = secrets.token_hex(12)
	open_method = "synthetic-direct"
	try:
		direct = json.loads(await element.evaluate(TRY_SYNTHETIC_OPEN_SCRIPT, path[0]))
		if direct.get("opened"):
			raw = await element.evaluate(CASCADE_SCRIPT, path)
			result = CascadeResult.model_validate(json.loads(raw))
			result = result.model_copy(update={"open_method": open_method})
			result = await _verify_element_selection(element, result, path[-1])
			return await _retry_element_trusted_option(browser_session, element, path, result, token)

		# Framework components such as Meituan MTD ignore untrusted JavaScript
		# click events. Mark the resolved owner trigger, then use BrowserUse's CDP
		# mouse implementation to produce a real pointer transaction.
		marked = json.loads(await element.evaluate(MARK_TRIGGER_SCRIPT, token))
		if marked.get("tagged") and marked.get("tag") != "select":
			page = await browser_session.must_get_current_page()
			if await _trusted_click_selector(page, f'[data-browseragent-real-trigger="{token}"]'):
				await asyncio.sleep(0.25)
				open_method = "cdp-real-click"
		raw = await element.evaluate(CASCADE_SCRIPT, path)
		result = CascadeResult.model_validate(json.loads(raw))
		result = result.model_copy(update={"open_method": open_method})
		result = await _verify_element_selection(element, result, path[-1])
		return await _retry_element_trusted_option(browser_session, element, path, result, token)
	except Exception as exc:
		return CascadeResult(success=False, open_method=open_method, error=str(exc))
	finally:
		try:
			await element.evaluate(CLEAR_TRIGGER_SCRIPT, token)
		except Exception:
			pass


async def select_fast_cascade_path(page, field_id: str, path: list[str]) -> CascadeResult:
	"""Open a scanned control with a real click, then select it deterministically."""
	open_method = "synthetic-direct"
	token = secrets.token_hex(12)
	try:
		selector = f'[data-browseragent-fast-id="{field_id}"]'
		direct_raw = await page.evaluate(
			FAST_SYNTHETIC_OPEN_SCRIPT,
			{"field_id": field_id, "wanted": path[0]},
		)
		direct = json.loads(direct_raw or "{}")
		if direct.get("opened"):
			raw = await page.evaluate(FAST_CASCADE_SCRIPT, {"field_id": field_id, "path": path})
			result = CascadeResult.model_validate(json.loads(raw or "{}"))
			result = result.model_copy(update={"open_method": open_method})
			result = await _verify_fast_selection(page, field_id, result, path[-1])
			result = await _retry_fast_trusted_option(page, field_id, path, result, token)
			try:
				await page.evaluate(CLEAR_TRIGGER_SCRIPT, token)
			except Exception:
				pass
			return result
		# Start an attributed open transaction. Existing page popups are marked as
		# baseline so the selector can only adopt the popup created for this field.
		marked_raw = await page.evaluate(FAST_MARK_TRIGGER_SCRIPT, {"field_id": field_id, "token": token})
		marked = json.loads(marked_raw or "{}")
		clicked = False
		if marked.get("tagged"):
			clicked = await _trusted_click_selector(page, f'[data-browseragent-real-trigger="{token}"]')
		if not clicked:
			# Compatibility fallback for an unknown component adapter.
			trigger_selector = f'{selector} > [role="button"], {selector} > .mtd-select-filter > [role="button"]'
			clicked = await _trusted_click_selector(page, trigger_selector)
			if not clicked:
				clicked = await _trusted_click_selector(page, selector)
		if clicked:
			await asyncio.sleep(0.25)
			open_method = "cdp-real-click"
	except (AttributeError, NotImplementedError):
		# Lightweight test doubles do not expose CDP element lookup. The executor
		# still retains its synthetic fallback for those environments.
		pass
	except Exception as exc:
		open_method = f"cdp-click-failed:{type(exc).__name__}"
	try:
		raw = await page.evaluate(FAST_CASCADE_SCRIPT, {"field_id": field_id, "path": path})
		result = CascadeResult.model_validate(json.loads(raw or "{}"))
		final = result.model_copy(update={"open_method": open_method})
		final = await _verify_fast_selection(page, field_id, final, path[-1])
		final = await _retry_fast_trusted_option(page, field_id, path, final, token)
	except Exception as exc:
		final = CascadeResult(success=False, open_method=open_method, error=str(exc))
	try:
		await page.evaluate(CLEAR_TRIGGER_SCRIPT, token)
	except Exception:
		pass
	return final


async def select_cascade(browser_session, params: CascadingSelectParams) -> CascadeResult:
	"""Backward-compatible wrapper for complete cascading paths."""
	return await select_cascade_path(browser_session, params.index, params.path)
