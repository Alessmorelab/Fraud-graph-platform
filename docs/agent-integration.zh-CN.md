# 反诈证据研判 Agent 接入说明

## 定位

Agent 是任务四的辅助分析组件，不是自动处置机器人。它只读取当前账户的风险评分、结构化证据、关联账户、可疑路径和资金模式，生成带引用的中文摘要，并将证据包 SHA-256、模型版本和数据版本一并返回。

默认使用确定性降级模式。只有在明确获准向外部模型发送脱敏数据时，才开启 DeepSeek 外部调用。比赛演示推荐使用风险工作台中的临时 Key 验证框，不需要把密钥写入项目文件。

## 在研判助手中临时启用

1. 打开风险工作台，选择一个账户并进入“研判助手”。
2. 输入 DeepSeek API Key，选择 `V4 Flash` 或 `V4 Pro`。
3. 确认已经取得脱敏证据外发授权，点击“验证并启用”。
4. 验证成功后直接提问；点击“停用”可立即销毁临时会话。

Key 通过最小 Chat Completions 请求验证，不包含比赛数据。验证成功后，Key 仅保存在服务端进程内存中 30 分钟；浏览器只持有 `HttpOnly`、`SameSite=Strict` 会话 Cookie。页面不会回显 Key，也不会写入 `localStorage`、报告、日志或证据包。服务重启、会话到期或手动停用后，Key 立即失效。

本地演示仅允许通过 `127.0.0.1` 使用。若部署到其他机器，必须启用 HTTPS，并置于受控网络和身份认证之后。

## 服务端固定配置

无人值守环境仍可使用 `.env`：

1. 复制 `.env.example` 为 `.env`。
2. 在 `.env` 中填写 `DEEPSEEK_API_KEY=你的密钥`。
3. 经数据外发审批后，将 `FRAUD_GRAPH_AGENT_EXTERNAL_ENABLED` 改为 `true`。
4. 可按需调整 `DEEPSEEK_MODEL`、超时、重试和思考模式。

`.env` 已被 `.gitignore` 忽略，不应提交、截图或写入报告。未配置密钥或开关为 `false` 时，Agent 仍可工作，但会返回 `generation_mode=deterministic_fallback`。临时网页会话优先于服务端固定配置，且不会修改 `.env`。

## API

### `GET /v1/agent/capabilities`

返回外部模型是否开启、是否已配置密钥、当前模型、只读工具清单和安全护栏。`external_model_ready=false` 不代表 Agent 不可用，只表示使用本地确定性摘要。

### `POST /v1/agent/query`

请求：

```json
{
  "account_id": "账户ID",
  "question": "为什么该账户值得优先复核？"
}
```

响应固定包含：`answer`、`assessment`、`citations`、`tool_trace`、`limitations`、`recommended_actions`、`evidence_packet_sha256`、`model_version`、`data_version`、`production_eligible=false` 和 `verifier_passed`。

### 临时凭据会话

- `GET /v1/agent/session`：查询当前浏览器是否已有有效会话。
- `POST /v1/agent/session`：验证 Key 并创建 30 分钟内存会话；必须提交外发授权确认。
- `DELETE /v1/agent/session`：立即销毁当前会话和服务端内存中的 Key。

## 只读工具

`get_risk_summary`、`get_structured_evidence`、`get_suspicious_paths`、`get_associated_accounts`、`get_flow_patterns`、`get_report_boundary` 均绑定请求中的账户 ID。工具不接受 SQL、文件路径、URL 或其他根账户参数。

## 审计与降级

DeepSeek 支持工具调用轮次，最多 6 轮；网络超时、429、5xx、协议错误、无效工具参数或证据引用失败都会降级。校验器会检查引用 ID 是否存在于当前证据包、正文是否实际使用引用、是否出现跨账户引用，以及是否提出自动冻结、止付或涉诈确定性结论。失败时只返回确定性摘要，不保留未经校验的模型文本。

最终回答先按固定字段进行解析和类型校验。若 DeepSeek 返回普通文本、不完整 JSON 或字段类型错误，服务会改用 JSON Object 模式自动整理，并最多修复重试一次；修复结果仍需通过证据引用和越权校验，两次均不合规时才进入确定性降级。原始不合规回答不会写入日志或报告。

DeepSeek 正文中出现的证据标记会与当前证据包重新对照。正文引用真实但 `citations` 清单漏报时，服务自动补齐并保留 AI 回答；不存在的证据、跨账户引用和越权处置仍会触发安全证据摘要。接口通过 `citation_repair_applied` 标记引用是否经过自动校正。回答正文分为简短研判结论和 1 至 6 条关键依据，页面另行展示 AI 参考提示、编号证据、局限性与辅助处置建议。

## 数据边界

比赛数据即使已经脱敏，也属于业务数据。没有明确外发授权时必须保持 `FRAUD_GRAPH_AGENT_EXTERNAL_ENABLED=false`。Agent 不得修改证据包、报告哈希、人工复核记录或任何业务处置状态。
