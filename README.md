# 反诈资金图谱平台

面向银行反诈运营的、可审计的批量风险评分与时序资金链路证据服务。

实现中明确区分以下职责：

- 不可变的源数据接入与数据质量发现；
- 截至观察时点的账户行为特征；
- 版本化的模型训练与批量评分；
- 确定性的交易路径证据；
- 只读的运营服务 API。

## 安全保证

- 账户工作簿中的风险标签仅用于跨文件一致性校验，绝不会写入账户特征表。
- 每一份数据、特征、模型和评分产物都具有由内容导出的不可变版本。
- 特征生成严格使用半开时间区间 `[as_of - lookback, as_of)`。
- 缺少 `label_available_at` 时，系统会阻止时序模型训练。
- `--allow-static-labels` 仅用于回放，生成的回溯模型元数据会明确写入
  `production_eligible=false`。
- 证据报告会引用源交易 ID；没有交易的账户不会被虚构资金路径。
- 模型输出仅用于辅助决策。服务不会自动执行止付、冻结或其他不可逆处置。

## 快速开始

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

fraud-graph ingest \
  --source-dir /path/to/source \
  --config config/default.yaml

# 构建 28 天观察窗口、7 天步长的滚动有向资金图快照。
fraud-graph build-graph-snapshots \
  --data-version <data-version> \
  --config config/default.yaml

fraud-graph build-features \
  --data-version <data-version> \
  --config config/default.yaml

# 标签工作簿必须提供 risk_event_at 和 label_available_at。
fraud-graph build-temporal-samples \
  --data-version <data-version> \
  --config config/default.yaml

fraud-graph train-temporal \
  --sample-version <sample-version> \
  --config config/default.yaml

# JSON/Parquet 验收检查；缺少案例真值或人工复核案例时会直接失败。
fraud-graph evaluate-reports --input reports.json
fraud-graph evaluate-associations --predictions associations.json --labels labels.parquet
fraud-graph export-case-samples --score-version <score-version> --limit 5
fraud-graph accept-task4 --case-sample-dir var/artifacts/case_samples/<version>
fraud-graph evaluate-cases \
  --input var/artifacts/case_samples/<version>/reviewer-1-submission.json \
  --input var/artifacts/case_samples/<version>/reviewer-2-submission.json \
  --case-sample-dir var/artifacts/case_samples/<version>

# 此覆盖选项只用于回放行方提供的静态标签数据集。
fraud-graph train \
  --feature-version <feature-version> \
  --feature-set behavior \
  --allow-static-labels \
  --config config/default.yaml

fraud-graph score \
  --model-version <model-version> \
  --config config/default.yaml

fraud-graph serve \
  --score-version <score-version> \
  --config config/default.yaml
```

## API 接口

- `GET /`：反诈运营研判工作台
- `GET /snapshots`：28 天滚动窗口的动态图快照工作台
- `GET /health/live`
- `GET /health/ready`
- `GET /v1/operations/summary`
- `GET /v1/accounts`
- `GET /v1/graph-snapshots`：列出与当前数据版本一致的观察时点
- `GET /v1/graph-snapshots/{snapshot_id}`：返回快照统计与受控规模的高活跃资金子图
- `GET /v1/accounts/{account_id}/workspace`
- `GET /v1/accounts/{account_id}/risk`
- `GET /v1/accounts/{account_id}/evidence`
- `GET /v1/accounts/{account_id}/associations?limit=20`
- `GET /v1/accounts/{account_id}/report`
- `GET /v1/capabilities`
- `GET /reviews`：案例包已配置时提供案例复核工作台
- `GET /v1/reviews/cases`
- `GET /v1/reviews/cases/{case_id}`
- `GET /v1/reviews/status`
- `GET /v1/reviews/cases/{case_id}/report`
- `GET /v1/reviews/templates/{template_name}`
- `POST /v1/reviews/submissions`：仅在可信 IAM 后显式启用时可写入

工作台由 FastAPI 直接提供包内静态资源，因此同一个 Python 进程和 Docker 镜像同时承载 UI 与 API。页面包括可分页的风险账户队列、账户检索、交互式账户研判图、动态图快照时间轴、Top-20 关联账户、资金链路证据、模型上线资格告警和报告入口。动态图快照页同时展示完整快照指标与高活跃、高金额子图，避免将浏览器抽样画布误认为全量资金网络。能力元数据为人工复核、案件反馈、IAM 和审计汇聚预留了集成边界，不会把尚未接通的能力伪装为可用功能。

视觉系统采用克制的冰白、海军蓝和蓝色操作态，参考公开 WIDTH 产品语言的层级感与信息密度，但为反诈运营场景独立实现，未复用任何 WIDTH 品牌资产。

如需启用只读案例复核页，在 `serve` 命令中增加 `--case-sample-version <version>`。默认关闭复核写入。生产部署只有同时启用 `--enable-review-writes` 和 `--trust-review-identity-headers`，并置于银行 IAM/API 网关之后时，才能开启写入；网关必须注入 `X-Verified-Reviewer-Id` 与 `X-Verified-Reviewer-Role`，服务不会从 JSON 请求体接受复核人身份。提交记录为追加式写入，与不可变证据包哈希绑定，并拒绝重复提交或不符合阶段的复核。

## 产物目录

```text
var/
  normalized/<data_version>/
    manifest.json
    accounts.parquet
    labels.parquet
    transactions/year=YYYY/month=MM/part-00000.parquet
  features/<feature_version>/
    manifest.json
    account_features.parquet
  artifacts/
    graph_snapshots/<snapshot_set_version>/
      manifest.json
      summary.md
      accounts.parquet
      labels.parquet
      snapshot_stats.parquet
      snapshots/<snapshot_id>/
        manifest.json
        nodes.parquet
        edges.parquet
        events.parquet
    models/<model_version>/
    temporal_samples/<sample_version>/
    temporal_models/<model_version>/
    scores/<score_version>/
```

动态图快照使用半开窗口 `[anchor_at - observation_days, anchor_at)`。节点表不包含
风险标签，静态标签单独保存在快照集根目录，避免被误作模型输入。默认保留窗口内源交易事件，
并对节点交易计数、聚合边计数、金额、账户覆盖和未来交易泄露逐快照对账。完整口径与产物说明见
`docs/task-1-snapshots.zh-CN.md`。

时序模型产物包含训练、验证和测试锚点的预测表，统一的规则/模型指标、子群指标，以及明确的泄露检查字段。在模型审批、校准、影子运行和回滚评审完成前，产物仍会标记为 `production_eligible=false`。关联账户与报告的验收工具位于 `fraud_graph.evidence.evaluation`。案例导出包含不可变证据包哈希，每个案例配置两个独立人工复核席位。

任务 3 的验收要求至少 5 个已解决案例、每个案例由 2 名独立人工复核人员确认，以及 70% 的严格案例通过率；自动预检查绝不能计入人工复核。复核意见不一致时必须由独立仲裁人处理。任务 4 的验收同时验证结构化证据和每份 Markdown 报告的精确确定性渲染结果。缺少案例真值或人工标注的复核案例时，系统会阻断验收，而不会伪造命中率。

基于当前提供的数据集，任务 1 和任务 2 仅能进行静态回溯实验，其产物必须持续标记为 `production_eligible=false`；在补齐真实的标签可得时间之前，不得宣称具备时序模型能力。

本仓库交付的是面向生产的端到端最小切片，并不声称当前数据集足以完成生产模型审批。所需上游字段与部署控制请参阅 `docs/architecture.md` 和 `docs/production-readiness.md`。中文企业方案与 GitHub 组件调研见 `docs/enterprise-solution.zh-CN.md` 和 `docs/github-reference.zh-CN.md`。
