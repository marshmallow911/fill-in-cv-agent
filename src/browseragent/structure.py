"""Code-first preparation of repeatable resume sections before field filling."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field


class ResumeInventory(BaseModel):
	education: int = Field(default=0, ge=0, le=20)
	experience: int = Field(default=0, ge=0, le=20)
	project: int = Field(default=0, ge=0, le=30)
	publication: int = Field(default=0, ge=0, le=30)
	patent: int = Field(default=0, ge=0, le=20)
	award: int = Field(default=0, ge=0, le=20)
	certification: int = Field(default=0, ge=0, le=20)
	competition: int = Field(default=0, ge=0, le=20)
	campus: int = Field(default=0, ge=0, le=20)


class SectionPreparation(BaseModel):
	section: str
	target_count: int
	initial_count: int
	final_count: int
	added: int = 0
	status: str
	message: str = ""
	add_candidates: list[str] = Field(default_factory=list)


PREPARE_REPEAT_SECTIONS_SCRIPT = r"""(targets) => (async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const clean = value => String(value || '').replace(/[\s　]+/g, '').trim();
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const definitions = {
    education: /教育|学历|education/i,
    experience: /工作|实习|任职|experience|employment/i,
    project: /项目|研究经历|project|research/i,
    publication: /论文|著作|publication|paper/i,
    patent: /专利|patent/i,
    award: /荣誉|奖励|award|honou?r/i,
    certification: /证书|认证|certification|certificate/i,
    competition: /竞赛|比赛|competition|contest/i,
    campus: /校园经历|学生工作|社团|campus/i,
  };
  const fieldDefinitions = {
    education: /学校|院校|学院|专业|学历|学位|入学|毕业|education|school|university|college|degree|major/i,
    experience: /公司|单位|部门|职位|职务|工作描述|任职|入职|离职|company|employer|department|position|job|work/i,
    project: /项目名称|项目角色|项目描述|项目链接|project|portfolio/i,
    publication: /论文|著作|期刊|会议|作者|发表|publication|paper|journal|conference|author/i,
    patent: /专利|专利号|发明|patent|invention/i,
    award: /荣誉|奖励|奖项|award|honou?r/i,
    certification: /证书|认证|certification|certificate/i,
    competition: /竞赛|比赛|赛事|competition|contest/i,
    campus: /校园经历|学生工作|社团|campus|student/i,
  };
  const standardSelector = 'button, [role="button"], a, [onclick], [tabindex="0"]';
  const semanticText = el => clean([
    el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
    el.getAttribute('data-title'), el.getAttribute('data-label'),
  ].filter(value => typeof value === 'string' && value).join(' '));
  const classText = el => typeof el.className === 'string' ? clean(el.className) : '';
  const controlText = el => clean(`${semanticText(el)} ${classText(el)}`);
  const addPattern = /添加|新增|增加|继续添加|再加一条|add|new|plus|^\+$/i;
  const addClassPattern = /(^|[-_])(add|plus)([-_]|$)|addmore|moreadd/i;
  const excludedAddPattern = /上传|附件|头像|照片|图片|upload|image|img|avatar|photo|志愿|volunteer/i;
  const deletePattern = /删除(这段|该条|本条)?(经历|信息|记录)?|remove|delete/i;
  const typeFor = text => Object.entries(definitions).find(([, pattern]) => pattern.test(text))?.[0] || null;
  const typesFor = text => Object.entries(definitions).filter(([, pattern]) => pattern.test(text)).map(([type]) => type);
  const fieldText = el => clean([
    el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.name,
    el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText : '',
    el.closest('[class*="form"], [class*="field"], [class*="item"], td, li')?.querySelector('label, [class*="label"]')?.innerText,
  ].filter(Boolean).join(' '));
  const fieldKey = el => {
    const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText : '';
    const nearby = el.closest('[class*="form"], [class*="field"], [class*="item"], td, li')?.querySelector('label, [class*="label"]')?.innerText;
    const key = [el.getAttribute('placeholder'), el.getAttribute('aria-label'), explicit, nearby, el.name]
      .find(value => value && clean(value));
    return clean(key).replace(/\[\d+\]|\d+/g, '#');
  };
  const controls = root => [...root.querySelectorAll('input, textarea, select')].filter(visible);
  const canonical = el => {
    const standard = el.closest(standardSelector);
    if (standard) return standard;
    for (let node = el; node && node !== document.body; node = node.parentElement) {
      if (getComputedStyle(node).cursor === 'pointer') return node;
      if (node !== el && semanticText(node).length > 100) break;
    }
    return el;
  };
  const rawAddCandidates = () => {
    const selectors = `${standardSelector}, [class*="add"], [class*="Add"], [class*="plus"], [class*="Plus"]`;
    const raw = [...document.querySelectorAll(selectors)].filter(visible).filter(el =>
      !el.disabled && el.getAttribute('aria-disabled') !== 'true' &&
      (addPattern.test(semanticText(el)) || addClassPattern.test(classText(el))) && !excludedAddPattern.test(controlText(el))
    ).map(canonical);
    const unique = [...new Set(raw)];
    // Nested icon/wrapper nodes often all carry an `add` class but represent
    // one physical control. Keep only the deepest candidate before scoring.
    return unique.filter(node => !unique.some(other => other !== node && node.contains(other)));
  };
  const headings = () => [...document.querySelectorAll(
    'h1, h2, h3, h4, h5, h6, legend, [class*="title"], [class*="Title"], [class*="header"], [class*="Header"]'
  )].filter(visible).map(el => ({el, text: clean(el.innerText || el.textContent)}))
    .filter(item => item.text && item.text.length <= 120 && typesFor(item.text).length === 1);
  const nearestHeading = (button, headingItems) => headingItems.filter(item =>
    item.el.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING
  ).pop() || null;
  const bestLocationFor = (button, type, headingItems) => {
    const directType = typeFor(controlText(button));
    const preceding = nearestHeading(button, headingItems);
    const precedingType = preceding ? typeFor(preceding.text) : null;
    // A nearby structural heading is stronger evidence than any broad ancestor.
    // Never let a button from one section inherit another type through the page wrapper.
    if (directType && directType !== type) return null;
    if (!directType && precedingType && precedingType !== type) return null;
    let best = null;
    for (let node = button.parentElement, depth = 1; node && node !== document.body && depth <= 12; node = node.parentElement, depth++) {
      const fieldCounts = Object.fromEntries(Object.entries(fieldDefinitions).map(([name, pattern]) => [
        name, controls(node).filter(el => pattern.test(fieldText(el))).length,
      ]));
      const directHeadings = headingItems.filter(item => node.contains(item.el) && item.el.parentElement === node);
      const headingTypes = [...new Set(directHeadings.map(item => typeFor(item.text)).filter(Boolean))];
      const targetFields = fieldCounts[type] || 0;
      const otherFields = Math.max(0, ...Object.entries(fieldCounts).filter(([name]) => name !== type).map(([, count]) => count));
      let score = 0;
      if (directType === type) score += 120;
      if (headingTypes.length === 1 && headingTypes[0] === type) score += 80;
      if (precedingType === type && node.contains(preceding.el)) score += 45;
      score += Math.min(targetFields, 6) * 12;
      score -= Math.min(otherFields, 6) * 4;
      score -= depth;
      if (!best || score > best.score) best = {button, type, scope: node, score, depth, targetFields, heading: preceding?.text || ''};
    }
    return best && best.score >= 35 ? best : null;
  };
  const locationsFor = type => {
    const headingItems = headings();
    return rawAddCandidates().map(button => bestLocationFor(button, type, headingItems)).filter(Boolean)
      .sort((a, b) => b.score - a.score || a.depth - b.depth);
  };
  const commonAncestor = elements => {
    if (!elements.length) return null;
    let node = elements[0].parentElement;
    while (node && node !== document.body && !elements.every(el => node.contains(el))) node = node.parentElement;
    return node && node !== document.body ? node : null;
  };
  const existingScopeFor = type => {
    const matches = controls(document).filter(el => fieldDefinitions[type].test(fieldText(el)));
    return commonAncestor(matches);
  };
  const globalCardCount = type => {
    const signatures = new Map();
    for (const el of controls(document)) {
      if (!fieldDefinitions[type].test(fieldText(el))) continue;
      const signature = fieldKey(el);
      if (signature) signatures.set(signature, (signatures.get(signature) || 0) + 1);
    }
    return Math.max(0, ...signatures.values());
  };
  const countCards = (scope, type) => {
    const signatures = new Map();
    for (const el of controls(scope)) {
      const signature = fieldKey(el);
      if (!signature || !fieldDefinitions[type].test(signature)) continue;
      signatures.set(signature, (signatures.get(signature) || 0) + 1);
    }
    const bySignature = Math.max(0, ...signatures.values());
    const deletes = [...scope.querySelectorAll(standardSelector)].filter(visible)
      .filter(el => deletePattern.test(controlText(el)));
    return Math.max(bySignature, deletes.length);
  };
  const describe = item => {
    const el = item.button;
    return [
      `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}`,
      `text=${semanticText(el).slice(0, 80) || '-'}`,
      `class=${classText(el).slice(0, 100) || '-'}`,
      `heading=${item.heading || '-'}`,
      `fields=${item.targetFields}`,
      `cards=${countCards(item.scope, item.type)}`,
      `score=${item.score}`,
    ].join(', ');
  };

  const results = [];
  for (const [type, rawTarget] of Object.entries(targets)) {
    const target = Number(rawTarget || 0);
    if (target <= 0) continue;
    const existingScope = existingScopeFor(type);
    const observedInitial = Math.max(globalCardCount(type), existingScope ? countCards(existingScope, type) : 0);
    if (observedInitial >= target) {
      results.push({section: type, target_count: target, initial_count: observedInitial, final_count: observedInitial,
        added: 0, status: 'already_sufficient', message: 'counted from repeated field signatures', add_candidates: []});
      continue;
    }
    const matching = locationsFor(type);
    const diagnostics = matching.slice(0, 8).map(describe);
    const winner = matching[0];
    const runnerUp = matching[1];
    if (!winner || runnerUp && winner.score - runnerUp.score < 15) {
      results.push({section: type, target_count: target, initial_count: observedInitial, final_count: observedInitial, added: 0,
        status: matching.length ? 'ambiguous_add_button' : 'no_add_button',
        message: winner ? `top candidates too close (${winner.score} vs ${runnerUp.score})` : 'no section-scoped add control found',
        add_candidates: diagnostics});
      continue;
    }
    let button = winner.button;
    let scope = winner.scope;
    const initial = Math.max(observedInitial, countCards(scope, type));
    let current = initial;
    let added = 0;
    let status = current >= target ? 'already_sufficient' : 'prepared';
    let message = '';
    while (current < target && added < 10) {
      const before = current;
      button.scrollIntoView({block: 'center'});
      button.click();
      const deadline = Date.now() + 3000;
      while (Date.now() < deadline) {
        await wait(100);
        const refreshed = locationsFor(type);
        if (refreshed.length && (!refreshed[1] || refreshed[0].score - refreshed[1].score >= 15)) {
          button = refreshed[0].button;
          scope = refreshed[0].scope;
        }
        current = countCards(scope, type);
        if (current > before) break;
      }
      if (current !== before + 1) {
        status = 'add_not_verified';
        message = `card count did not increase exactly once (${before} -> ${current})`;
        break;
      }
      added += 1;
    }
    if (current < target && status === 'prepared') {
      status = 'limit_reached';
      message = 'stopped at the per-section safety limit';
    }
    results.push({section: type, target_count: target, initial_count: initial, final_count: current, added, status, message,
      add_candidates: diagnostics});
  }
  return results;
})()"""


async def extract_resume_inventory(llm, context: str) -> ResumeInventory:
	"""Count explicit CV records once; this does not infer missing experiences."""
	from browser_use.llm.messages import SystemMessage, UserMessage

	response = await llm.ainvoke(
		[
			SystemMessage(
				content=(
					"Count explicit candidate records by resume section. Count each education, internship/work experience, "
					"project/research experience, publication, patent, award, certification, competition, and campus record. "
					"For education, count only degree/credential-granting records with an explicit education level or degree; "
					"a visiting-student, exchange-study, summer-school, or other non-degree affiliation must not create a new "
					"education card. Do not invent or merge records. An award won in a competition counts as competition, "
					"not both award and competition."
				)
			),
			UserMessage(content=f"CANDIDATE SOURCES:\n{context}"),
		],
		output_format=ResumeInventory,
	)
	return response.completion


async def prepare_page_sections(browser_session, inventory: ResumeInventory) -> list[SectionPreparation]:
	"""Apply an already validated inventory to the current page topology."""
	page = await browser_session.must_get_current_page()
	raw = await page.evaluate(PREPARE_REPEAT_SECTIONS_SCRIPT, inventory.model_dump())
	return [SectionPreparation.model_validate(item) for item in json.loads(raw or "[]")]


async def prepare_repeat_sections(browser_session, llm, context: str) -> tuple[ResumeInventory, list[SectionPreparation]]:
	"""Backward-compatible combined structure preparation helper."""
	inventory = await extract_resume_inventory(llm, context)
	return inventory, await prepare_page_sections(browser_session, inventory)
