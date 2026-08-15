"""Capture a sanitized, offline form fixture from the focused browser tab."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


CAPTURE_FORM_SCRIPT = r"""() => {
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const clean = (value, limit = 500) => String(value || '').replace(/[\s　]+/g, ' ').trim().slice(0, limit);
  const generic = /^(请选择|请输入|选择|select|choose)(?:.{0,30})?$/i;
  const sensitive = /姓名|手机|电话|邮箱|证件号码|身份证号|护照号|出生|生日|地址|联系人|邮编|name|phone|mobile|email|identity\s*(?:number|no)|passport\s*(?:number|no)|birth|address/i;
  const labelFor = (el) => {
    const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const item = el.closest('[class*="form-item"], [class*="formItem"], [class*="field-item"], [class*="fieldItem"], td, li');
    const structured = item?.querySelector(':scope > label, :scope > [class*="label"], label, [class*="form-item-label"], [class*="field-label"]');
    const hint = el.closest('.mtd-select-filter, .mtd-select, .mtd-cascader')
      ?.querySelector('[class*="hint"], [data-placeholder]');
    const hintText = clean(hint?.innerText || hint?.textContent || hint?.getAttribute('data-placeholder'));
    const hintIdentity = hintText.replace(/^(?:请选择|请输入|选择|select|choose)\s*/i, '').trim();
    const values = [hintIdentity, explicit?.innerText, structured?.innerText, el.getAttribute('aria-label'),
      el.getAttribute('placeholder'), el.name].map(value => clean(value, 240)).filter(Boolean);
    return values.find(value => !generic.test(value)) || values[0] || '';
  };
  const sectionFor = (el) => {
    const fieldset = el.closest('fieldset');
    if (fieldset?.querySelector(':scope > legend')) return clean(fieldset.querySelector(':scope > legend').innerText, 160);
    const section = el.closest('section, [role="group"], [class*="section"], [class*="module"], [class*="block"]');
    const heading = section?.querySelector('h1, h2, h3, h4, h5, h6, [class*="title"], [class*="header"]');
    if (heading?.innerText) return clean(heading.innerText, 160);
    const preceding = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6')]
      .filter(item => item.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING).pop();
    return clean(preceding?.innerText, 160);
  };
  const customSelector = '.mtd-select-filter, .mtd-select, .mtd-cascader, [role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"], [class*="cascader"], [class*="picker"]';
  const rawCustom = [...document.querySelectorAll(customSelector)].filter(visible);
  const custom = rawCustom.filter(el => !rawCustom.some(parent => parent !== el && parent.contains(el) &&
    parent.matches('.mtd-select-filter, .mtd-select, .mtd-cascader')));
  const controls = [];
  let serial = 0;
  for (const el of custom) {
    const id = `custom-${++serial}`;
    el.setAttribute('data-browseragent-fixture-id', id);
    const input = el.querySelector('input');
    const label = labelFor(input || el);
    const selected = el.querySelector('[aria-selected="true"], [class*="selected"], [class*="value"], [class*="label"]:not([class*="hint"])');
    const value = clean(input?.value || selected?.innerText || selected?.textContent);
    controls.push({id, kind: 'custom', label, section: sectionFor(el), value: sensitive.test(label) ? '<redacted>' : value,
      placeholder: clean(input?.getAttribute('placeholder') || el.querySelector('[class*="hint"]')?.innerText),
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', classes: clean(el.className, 400),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true' || /(?:^|[-_])disabled(?:[-_]|$)/i.test(String(el.className))), options: []});
  }
  for (const el of [...document.querySelectorAll('input, textarea, select')]) {
    if (!visible(el) || el.closest('[data-browseragent-fixture-id^="custom-"]')) continue;
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'password', 'file', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    const id = `native-${++serial}`;
    el.setAttribute('data-browseragent-fixture-id', id);
    const label = labelFor(el);
    const shouldRedact = sensitive.test(`${label} ${el.placeholder || ''}`) || ['email', 'tel'].includes(type);
    controls.push({id, kind: el.tagName === 'SELECT' ? 'select' : type, label, section: sectionFor(el),
      value: shouldRedact ? '<redacted>' : clean(el.value, 4000), placeholder: clean(el.placeholder),
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', classes: clean(el.className, 400),
      disabled: Boolean(el.disabled), read_only: Boolean(el.readOnly),
      options: el.tagName === 'SELECT' ? [...el.options].map(option => clean(option.textContent)).filter(Boolean) : []});
  }

  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script, noscript, iframe, frame, object, embed, link[rel="preload"], link[rel="prefetch"], link[rel="modulepreload"], base')
    .forEach(el => el.remove());
  clone.querySelectorAll('*').forEach(el => {
    for (const attr of [...el.attributes]) {
      if (/^on/i.test(attr.name) || ['nonce', 'integrity', 'crossorigin', 'srcdoc'].includes(attr.name.toLowerCase())) el.removeAttribute(attr.name);
    }
    if (['IMG', 'SOURCE', 'VIDEO', 'AUDIO'].includes(el.tagName) && el.hasAttribute('src')) {
      el.setAttribute('data-original-src', el.getAttribute('src'));
      el.removeAttribute('src');
    }
  });
  for (const control of controls) {
    const cloned = clone.querySelector(`[data-browseragent-fixture-id="${CSS.escape(control.id)}"]`);
    if (!cloned || control.value !== '<redacted>') continue;
    if ('value' in cloned) cloned.setAttribute('value', '');
    cloned.querySelectorAll?.('input, textarea').forEach(item => item.setAttribute('value', ''));
  }
  clone.querySelectorAll('input[type="password"], input[type="hidden"]').forEach(el => el.setAttribute('value', ''));
  const banner = clone.ownerDocument.createElement('div');
  banner.setAttribute('style', 'position:sticky;top:0;z-index:2147483647;padding:10px;background:#fff3cd;color:#5f4500;font:14px sans-serif;border-bottom:1px solid #e5c96b');
  banner.textContent = 'BrowserAgent 脱敏离线表单夹具：脚本、登录状态、外部媒体和敏感字段值已移除；此页面不能提交。';
  clone.querySelector('body')?.prepend(banner);
  return JSON.stringify({title: document.title, url: location.href, viewport: {width: innerWidth, height: innerHeight},
    controls, custom_ids: controls.filter(item => item.kind === 'custom').map(item => item.id),
    html: '<!doctype html>\n' + clone.outerHTML});
}"""


PROBE_DROPDOWN_SCRIPT = r"""(params) => (async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0 &&
      rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  };
  const root = document.querySelector(`[data-browseragent-fixture-id="${CSS.escape(params.fixture_id)}"]`);
  if (!root) return JSON.stringify({opened: false, options: [], error: 'control missing'});
  const trigger = root.querySelector(':scope > [role="button"], :scope > .mtd-select-filter > [role="button"], [role="button"]') || root;
  if (params.open) {
    trigger.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true, pointerType: 'mouse'}));
    trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
    trigger.click();
    await wait(400);
  }
  const portalSelector = '[role="listbox"], [role="menu"], [role="tree"], .mtd-select-popup, .mtd-select-popup-wrapper, .mtd-select-dropdown, .mtd-select-filter-dropdown, .mtd-dropdown-menu, .ant-select-dropdown, .el-select-dropdown, [class*="select-dropdown"]';
  const portals = [...document.querySelectorAll(portalSelector)].filter(visible).filter(el => !root.contains(el));
  const optionSelector = '[role="option"], [role="menuitem"], [role="treeitem"], .mtd-select-item, .mtd-select-item-content, .mtd-select-option, .mtd-select-dropdown-item, .mtd-select-filter-option, .mtd-select-filter-item, .mtd-dropdown-menu-item, li, label';
  const globalOptionSelector = '[role="option"], [role="menuitem"], [role="treeitem"], .mtd-select-item, .mtd-select-item-content, .mtd-select-option, .mtd-select-dropdown-item, .mtd-select-filter-option, .mtd-select-filter-item, .mtd-dropdown-menu-item, .ant-select-item-option, .el-select-dropdown__item';
  const candidates = [...new Set([
    ...portals.flatMap(portal => [...portal.querySelectorAll(optionSelector)]),
    ...document.querySelectorAll(globalOptionSelector),
  ])].filter(visible).filter(el => !root.contains(el));
  const options = [...new Set(candidates
    .map(el => String(el.innerText || el.textContent || '').replace(/[\s　]+/g, ' ').trim())
    .filter(text => text && text.length <= 120))];
  return JSON.stringify({opened: portals.length > 0 || options.length > 0, options: options.slice(0, 200),
    trigger: {tag: trigger.tagName.toLowerCase(), role: trigger.getAttribute('role') || '',
      classes: String(trigger.className || '').slice(0, 240)},
    portals: portals.map(el => ({tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      classes: String(el.className || '').slice(0, 240)})).slice(0, 20), error: ''});
})()"""


TRIGGER_POINT_SCRIPT = r"""(fixture_id) => (async () => {
  const root = document.querySelector(`[data-browseragent-fixture-id="${CSS.escape(fixture_id)}"]`);
  if (!root) return JSON.stringify({found: false});
  const trigger = root.querySelector(':scope > [role="button"], :scope > .mtd-select-filter > [role="button"], [role="button"]') || root;
  trigger.scrollIntoView({block: 'center', inline: 'nearest'});
  await new Promise(resolve => setTimeout(resolve, 250));
  const rect = trigger.getBoundingClientRect();
  return JSON.stringify({found: rect.width > 0 && rect.height > 0,
    x: rect.left + rect.width / 2, y: rect.top + rect.height / 2});
})()"""


async def _trusted_click_fixture_trigger(page, fixture_id: str) -> bool:
	point = json.loads(await page.evaluate(TRIGGER_POINT_SCRIPT, fixture_id))
	if not point.get("found"):
		return False
	mouse = await page.mouse
	await mouse.move(round(point["x"]), round(point["y"]))
	await asyncio.sleep(0.05)
	await mouse.click(round(point["x"]), round(point["y"]))
	return True


async def capture_form_fixture(browser_session, output_root: Path, *, probe_dropdowns: bool = False) -> Path:
	"""Capture the focused page without reading browser storage or authentication data."""
	page = await browser_session.must_get_current_page()
	payload = {}
	best_payload = {}
	best_count = -1
	stable_polls = 0
	# SPA navigation can expose the final URL before React mounts the form. Wait
	# for the asynchronously mounted sections to settle, retaining the fullest
	# observed frame if a component briefly remounts during hydration.
	for attempt in range(50):
		candidate = json.loads(await page.evaluate(CAPTURE_FORM_SCRIPT))
		count = len(candidate.get("controls", []))
		if count > best_count:
			best_payload, best_count, stable_polls = candidate, count, 0
		elif count == best_count:
			best_payload, stable_polls = candidate, stable_polls + 1
		if best_count > 0 and attempt >= 15 and stable_polls >= 5:
			break
		await asyncio.sleep(0.2)
	payload = best_payload
	probe_results: dict[str, dict] = {}
	if probe_dropdowns:
		disabled_ids = {item["id"] for item in payload.get("controls", []) if item.get("disabled")}
		for fixture_id in payload.get("custom_ids", []):
			if fixture_id in disabled_ids:
				probe_results[fixture_id] = {"opened": False, "options": [], "error": "disabled", "open_method": "skipped-disabled"}
				continue
			result = json.loads(await page.evaluate(
				PROBE_DROPDOWN_SCRIPT, {"fixture_id": fixture_id, "open": True}
			))
			result["open_method"] = "synthetic-direct" if result.get("opened") else "not-opened"
			selector = f'[data-browseragent-fixture-id="{fixture_id}"]'
			triggers = await page.get_elements_by_css_selector(
				f'{selector} > [role="button"], {selector} > .mtd-select-filter > [role="button"]'
			)
			if not result.get("opened") and len(triggers) == 1:
				clicked = await _trusted_click_fixture_trigger(page, fixture_id)
				await asyncio.sleep(0.4)
				# The synthetic probe toggled nothing, so collect after one trusted open.
				result = json.loads(await page.evaluate(
					PROBE_DROPDOWN_SCRIPT, {"fixture_id": fixture_id, "open": False}
				))
				result["open_method"] = "cdp-real-click" if result.get("opened") else ("cdp-no-popup" if clicked else "not-opened")
			if result.get("opened") and len(triggers) == 1:
				if result.get("open_method") == "cdp-real-click":
					await _trusted_click_fixture_trigger(page, fixture_id)
				else:
					await page.evaluate(PROBE_DROPDOWN_SCRIPT, {"fixture_id": fixture_id, "open": True})
			elif result.get("opened"):
				# If the control has no separately addressable owner button, toggle the
				# same synthetic trigger once more so the next probe starts cleanly.
				await page.evaluate(
					PROBE_DROPDOWN_SCRIPT, {"fixture_id": fixture_id, "open": True}
				)
			result["trusted_trigger_count"] = len(triggers)
			probe_results[fixture_id] = result
		for control in payload.get("controls", []):
			if control["id"] in probe_results:
				control["options"] = probe_results[control["id"]].get("options", [])

	parsed = urlsplit(payload.get("url", ""))
	host = re.sub(r"[^a-zA-Z0-9.-]+", "-", parsed.hostname or "form").strip("-") or "form"
	timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
	output_dir = output_root / f"{timestamp}-{host}"
	output_dir.mkdir(parents=True, exist_ok=False)
	(output_dir / "page.html").write_text(payload.pop("html"), encoding="utf-8")
	payload["captured_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
	payload["dropdown_probes"] = probe_results
	(output_dir / "form.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	return output_dir
