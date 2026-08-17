# 任务 3/4 验收操作说明

## 当前结论

任务 4 已通过技术验收。验收对象是案例包 `a04894ae2c18991c`，共 5 份报告；结构一致率、至少两类证据
覆盖率、报告完整率和最终 Markdown 精确渲染匹配率均为 100%。验收结果保存在
`var/artifacts/case_samples/a04894ae2c18991c/task4_acceptance.json`。

任务 3 已完成两名独立人工复核员对 5 个案例的主审。正式验收共读取 10 条人工复核记录，
`evaluable=true`、`acceptance_passed=true`；5 个案例全部解决且通过，严格案例通过率、证据一致率、
路径解释通过率和可读性通过率均为 100%，没有需要第三人仲裁的分歧。正式结果保存在同一案例目录的
`task3_manual_acceptance.json`；`task3_acceptance_pending.json` 仅保留为人工复核前的自动预检记录。

任务 1、任务 2 均为 `static_retrospective_experiment`，所有相关模型保持
`production_eligible=false`。

## 已复核案例

| 案例 | 根账户 | 风险排序 | 等级 | 路径数 | 最大跳数 | 可核验的证据类型 |
|---|---:|---:|---:|---:|---:|---|
| case-01 | 11065 | 771 | A | 20 | 2 | 模型、行为、交易、图结构 |
| case-02 | 10688 | 1408 | B | 10 | 1 | 模型、行为、交易、图结构模式 |
| case-03 | 4379 | 17 | B | 4 | 1 | 模型、行为、交易、图结构模式 |
| case-04 | 7265 | 37 | B | 6 | 1 | 模型、行为、交易、图结构模式 |
| case-05 | 1570 | 71 | B | 6 | 1 | 模型、行为、交易、图结构模式 |

每份报告列出风险分数、训练模式、上线阻断项、行为特征、交易 ID、付款/收款账户、时间、金额、跳间隔、
金额比例、局限性和辅助处置建议。报告只支持人工研判，不授权自动止付或冻结。

## 人工复核规则

1. 两名反诈复核员分别阅读同一案例报告，禁止共享初始结论。
2. `evidence_consistent`：报告中的分数、账户、交易、时间顺序和路径均可在结构化案例 JSON 中逐项找到。
3. `path_explanation_pass`：链路连续、时间递增，且报告没有把直接关系描述成已证实团伙关系。
4. `readability_pass`：风险摘要、证据、链路、限制和建议能够直接支持复核操作，没有无依据结论。
5. `evidence_types_verified` 至少选择两类；仅看到模型分数不能通过。
6. 两名主审在任一布尔项上不一致时，增加一条 `review_type=adjudication` 的第三人记录。仲裁员不得是原主审。
7. 五个案例全部完成且无未仲裁冲突后才计算通过率。三个布尔项全部为真才算单案例通过，严格案例通过率
   不低于 70% 才达到任务 3 的人工解释验收线。

## 操作命令

两名主审必须分别领取 `review_templates/primary-reviewer-1.json` 和
`review_templates/primary-reviewer-2.json`，独立填写后分别提交，任何一方不得查看另一方结论。只有出现冲突时，
第三人才能填写 `review_templates/adjudicator.json` 中对应案例。不得用模型自动填写 `reviewer_id`、
`reviewed_at` 或人工结论。

```bash
fraud-graph evaluate-cases \
  --input var/artifacts/case_samples/a04894ae2c18991c/reviewer-1-submission.json \
  --input var/artifacts/case_samples/a04894ae2c18991c/reviewer-2-submission.json \
  --case-sample-dir var/artifacts/case_samples/a04894ae2c18991c \
  --output var/artifacts/case_samples/a04894ae2c18991c/task3_manual_acceptance.json
```

正式通过时，输出必须同时满足：`evaluable=true`、`acceptance_passed=true`、`resolved_cases>=5`、
`case_pass_rate>=0.7`，且 `adjudication_required_cases` 为空。

任务 4 可随时重放：

```bash
fraud-graph accept-task4 \
  --case-sample-dir var/artifacts/case_samples/a04894ae2c18991c \
  --output var/artifacts/case_samples/a04894ae2c18991c/task4_acceptance.json
```
