# Browser Agent

一个读取本地 `career-ops` 资料并自动填写求职表单的 CLI Agent。它可以处理输入框、日期、下拉框和重复经历卡片，但**永远不会提交申请**。

## 安装与配置

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env
```

编辑 `.env`：

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=gpt-5-mini
MODEL_API_KEY=your-local-api-key
MODEL_BASE_URL=https://api.openai.com/v1
CAREER_OPS_PATH=~/career-ops
```

`.env`、浏览器登录状态、运行日志和本地快照均已被 Git 忽略。

## 使用

```bash
uv run browseragent jobs                 # 查看待申请岗位
uv run browseragent apply                # 开始填写
uv run browseragent runs                 # 查看运行记录
uv run browseragent resume RUN_ID         # 继续未完成的运行
uv run browseragent submitted RUN_ID      # 手动提交后更新状态
uv run browseragent snapshot JOB_ID       # 采集脱敏网页快照
```

运行 `apply` 后：

1. 选择岗位并输入 `APPLY`。
2. 在打开的浏览器中完成登录或验证码。
3. 停留在填报页面并输入 `READY`。
4. Agent 按页面顺序填写并检查遗漏，最后由用户人工确认。

Agent 会优先使用代码直接填写，失败或无法确定时再交给浏览器 Agent。登录、验证码、身份验证和最终提交始终由用户完成。

证件号码可以保存在本地：

```bash
uv run browseragent secrets set national_id
uv run browseragent secrets show national_id
uv run browseragent secrets delete national_id
```

秘密保存在 `career-ops/data/form-secrets.json`，权限为 `0600`。完整值不会进入模型提示词、快照或明文日志。

## 安全边界

- 开始前必须选择岗位并输入 `APPLY` 二次确认。
- Agent 无法使用最终提交动作。
- 最终提交按钮由代码拦截，只能由用户手动点击。
- `snapshot` 只生成脱敏测试文件，不读取 Cookie 或浏览器存储。

## 运行诊断

每次运行都会写入 `.browseragent/runs/<RUN_ID>.trace.json`。日志包含字段识别、填写结果、下拉选项、失败原因和 Agent 动作；敏感值会替换为 `<redacted>`。

排查问题时通常查看 `structure_execution.stage`、`code_execution` 和 `errors` 即可。

## 许可证

本项目采用 [MIT License](LICENSE)。
