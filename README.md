# Browser Agent

一个读取本地 career-ops 资料、自动填写求职表单、但永远不提交的 CLI Agent。

## 配置

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=gpt-5-mini
MODEL_API_KEY=your-local-api-key
MODEL_BASE_URL=https://api.openai.com/v1
OPENAI_REASONING_EFFORT=low
CAREER_OPS_PATH=~/career-ops
```

先运行 `cp .env.example .env`，再只在 `.env` 中填写自己的路径和 API key。程序会自动读取项目根目录的 `.env`，不需要手动 `export`。`MODEL_BASE_URL` 用于兼容 OpenAI API 协议的服务；系统环境变量优先于 `.env`。也可以使用 `.env.example` 中的 `BROWSERAGENT_*` 别名覆盖 `MODEL_*`。`.env` 保持 Git 忽略，其中的 API key 按本地明文读取，不会写入运行记录。浏览器登录状态和运行缓存保存在项目的 `.browseragent/`，不会提交到 Git。

## 使用

```bash
uv run browseragent jobs
uv run browseragent apply
uv run browseragent snapshot JOB_ID
uv run browseragent snapshot JOB_ID --probe-dropdowns
uv run browseragent runs
uv run browseragent resume RUN_ID
uv run browseragent submitted RUN_ID
```

如果推荐项包含多个岗位，CLI 会先要求选择一个具体岗位，再显示公司、岗位、地点和链接。只有输入大写 `APPLY` 后浏览器才会启动。表单结束时若有缺失字段，可以在 CLI 中补充并选择 `SAVE` 保存；当前页面由用户人工补齐，后续申请会自动复用。

所有运行都使用唯一的“确认真实表单 → 结构准备 → 通用字段规划 → 顺序代码填写 → 依赖重扫 → Agent fallback”流程：程序不会在职位详情页启动字段扫描；若能唯一定位表单外、非提交型的“立即申请/开始申请”导航入口，会先打开它并等待 URL 确实离开详情页，否则把控制权交还用户。进入表单后，先对照 CV 统计教育、实习/工作、项目、论文、专利、荣誉/竞赛等重复记录数量，只在唯一匹配的区块中补齐缺失卡片，并逐次验证数量确实增加；待页面结构稳定后，再扫描普通输入框、原生 select、自定义下拉、级联控件和年月控件，并按页面从上到下、同一行从左到右组成唯一执行队列。同一行保留暂时 disabled 的右侧输入框，先完成左侧选择并触发页面联动，再按顺序填写右侧；成功操作下拉或发现 disabled 控件后自动进行一次有界重扫，接住新出现或刚解锁的字段。搜索型下拉会输入来源支持的检索词、等待实时候选、精确选择并验证；失败时清空检索词，不能把搜索文字误报成已选值。年月控件也进入统一队列，先尝试 DOM 事件写入，受控组件拒绝时再使用一次 CDP 输入并恢复 readonly，最后验证值是否持久。每个字段成功或明确失败后才进入下一个字段。只有来源不明确、选项歧义、未知组件和两层验证均失败项进入 BrowserUse fallback。映射阶段可以并行，但不改变浏览器串行写入顺序；并行数默认 3，可通过 `BROWSERAGENT_MAPPING_PARALLELISM=1..4` 调整。用户确认的唯一默认值（例如个人信息-性别）由代码直接映射，不再依赖 LLM 重复判断；已保存的身份证、护照或社会安全号码只在字段标签唯一匹配时由本地代码直接填写，值不进入 LLM 或明文 trace。存在 `national_id` 时，证件类型会精确选择“居民身份证”，随后再填写右侧证件号码。没有对应本地值的敏感、法律、薪资和工作许可字段仍保持拦截，最终提交拦截保持不变。

浏览器启动后 Agent 不会立即运行。请先在打开的持久化浏览器中完成登录；可停留在岗位详情页或自行进入具体填报页，确认后在终端输入 `READY`。若仍在详情页，程序只会按上述安全规则进入表单，且在跳转完成前不会扫描或填写字段。如果存在多个有效标签页，程序会列出标题和 URL，由用户明确选择实际填报页；空白页不会显示。接管后 Agent 会锁定该标签，避免漂回岗位页。登录、验证码、OTP 或身份验证应始终由用户完成。若运行途中再次遇到这些步骤，Agent 会保存为未完成；运行 `resume RUN_ID`、人工处理并再次输入 `READY` 后继续。

`snapshot` 是独立的本地测试夹具采集器，不调用 LLM，也不填写或提交。输入 `CAPTURE` 并在目标填报页交接后，它会在 `.browseragent/fixtures/` 下生成脱敏的 `page.html` 和结构化 `form.json`。若当前只停留在职位详情页，采集器仅会点击唯一、位于表单外且文案精确匹配的“立即申请/开始申请”入口；无法唯一确认时要求人工进入，不会猜测。默认不打开任何下拉；`--probe-dropdowns` 会逐项记录自定义下拉候选，先尝试纯 DOM 交互，组件拒绝时才使用一次真实打开操作。快照不会读取 Cookie、localStorage、sessionStorage 或浏览器认证文件，离线 HTML 会移除脚本、iframe 和外部媒体。

身份证号码需要时可以单独保存：

```bash
uv run browseragent secrets set national_id
uv run browseragent secrets set passport_number
uv run browseragent secrets set social_security_number
uv run browseragent secrets show national_id
uv run browseragent secrets delete national_id
```

完整号码保存在 career-ops 的 `data/form-secrets.json`，权限为 `0600`。它不会进入模型提示词、运行快照或日志；`show` 只显示掩码。

## 安全边界

- 开始前必须选择岗位并输入 `APPLY` 二次确认。
- 默认点击和键盘提交动作不提供给 Agent。
- 最终提交按钮由代码层拦截，只能由用户在可见浏览器里手动点击。
- 只有用户随后运行 `submitted RUN_ID` 并输入 `SUBMITTED`，岗位才会标记为已投递。

## 运行诊断

每次运行会持续写入 `.browseragent/runs/<RUN_ID>.trace.json`，不必等 Agent 完成；在结构准备、字段扫描、文本/下拉求解、原生字段写入和每个下拉交互后都会生成检查点。敏感秘密会在落盘前替换为 `<redacted>`。

重点字段：

- `structure_execution.stage`：中断时最后执行到的阶段。
- `structure_execution.sections`：重复区块目标数、初始数、最终数、Add 候选与失败原因。
- `code_execution[].scanned_fields`：扫描到的字段、标签、控件类型、卡片上下文和稳定位置。
- `code_execution[].solver_runs`：每个求解块实际收到的字段 ID、模型返回 assignments 与错误。
- `code_execution[].proposals`：通过安全校验或被拒绝的映射方案。
- `code_execution[].execution_order`：实际逐字段执行顺序、页面坐标、控件类型、DOM/CDP 方法与结果。
- `code_execution[].native_write_attempts`：每次写入的 `before/requested/after/status`；常见状态包括 `verified`、`missing_control`、`already_filled`、`not_editable`、`option_not_found`、`write_not_persisted`。
- `code_execution[].dropdown_attempts`：下拉目标、可见选项、打开方式、提交验证与错误。
- `code_execution[].deferred_details`：未填写字段及明确原因。
- `actions` / `errors`：Agent fallback 的逐步动作和错误。

例如：

```bash
jq '{status, stage: .structure_execution.stage, structure: .structure_execution.sections,
  code: [.code_execution[] | {status, solver_runs, native_write_attempts,
  dropdown_attempts, deferred_details}], errors}' \
  .browseragent/runs/<RUN_ID>.trace.json
```

## 许可证

本项目采用 [MIT License](LICENSE)。
