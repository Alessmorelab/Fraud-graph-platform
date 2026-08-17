# 任务二：涉诈账户静态回溯识别

## 结论

任务二已形成可复现的账户级固定训练、验证和测试实验。验证集选出的静态比赛模型为
`hybrid_profile_logistic`，锁定测试集结果如下：

| 指标 | 锁定测试值 | 95% 分层 bootstrap 区间 |
|---|---:|---:|
| ROC-AUC | 0.9573 | 0.9386 - 0.9752 |
| PR-AUC | 0.1753 | 0.0547 - 0.4123 |
| Recall@Top5% | 0.5000 | 0.1667 - 0.7500 |
| Precision@Top5% | 0.0541 | - |
| Lift@Top5% | 9.9865 | - |

实验版本：`89e72026b92b871f`
数据版本：`d02568e53fa4e3e3`
图快照版本：`7d210bb931418adf`

这些结果满足比赛提出的 ROC-AUC 和 Top-5% 召回门槛，但不能解释为生产实时预警能力。
测试集只有 12 个正样本，置信区间较宽；标签又缺少发生时间和可得时间，因此产物持续标记为
`production_eligible=false`。

## 评估设计

- 账户级固定划分：训练 6,653、验证 2,217、测试 2,217 个账户。
- 正样本分布：训练 35、验证 12、测试 12。
- 按目标与是否有交易进行确定性分层，同一账户不会跨分区。
- 所有候选仅在训练集拟合；验证集按 PR-AUC、Recall@Top5%、ROC-AUC 的顺序选型。
- 测试集只用于最终模型及预先定义基线的单次评估。
- 无监督模型仅在训练侧拟合，再对验证或测试账户评分。
- 测试指标使用 500 次分层 bootstrap 给出 95% 区间。
- 图特征在不读取标签的前提下由全量比赛资金图计算，属于 transductive 评估；它不代表对图外
  新账户的 inductive 泛化能力。

旧模型 `27e53f783dde8968` 的 ROC-AUC 0.9341 来自全量账户上的分层随机 OOF，只保留为历史
诊断结果，不再作为任务二最终测试指标。

## 数据约束

| 项目 | 数量 |
|---|---:|
| 全部账户 | 11,087 |
| 涉诈账户 | 59 |
| 有交易账户 | 7,795 |
| 有交易的涉诈账户 | 3 |
| 无交易的涉诈账户 | 56 |

涉诈账户的交易覆盖率仅为 5.08%。因此当前标签主要不能由资金行为或图结构解释，任何高分模型都
必须检查是否依赖账户画像。该限制不是更换 GNN 或增加模型复杂度可以解决的。

## 特征与模型

静态比赛模型使用 73 个输入：

- 24 个交易行为特征；
- 21 个全量资金图特征，包括 PageRank、K-core、社区规模、互转比例和金额集中度；
- 20 个非隔离快照变化特征，包括活跃率、波动、趋势和状态变化；
- 5 个训练侧无监督异常分数；
- 3 个账户画像字段。

无监督分数组合 Isolation Forest、novelty LOF、社区内稳健偏离和跨快照变化偏离。它们不读取
标签；最终作为监督模型输入，同时保留纯无监督基线。

## 锁定测试对比

| 模型 | ROC-AUC | PR-AUC | Recall@Top5% | Lift@Top5% |
|---|---:|---:|---:|---:|
| 混合+画像 Logistic，验证集选中 | 0.9573 | 0.1753 | 0.5000 | 9.9865 |
| 无画像证据模型 HistGradientBoosting | 0.8230 | 0.0162 | 0.1667 | 3.3288 |
| 行为 Logistic 基线 | 0.8050 | 0.0159 | 0.1667 | 3.3288 |
| 画像 Logistic 基线 | 0.9492 | 0.2129 | 0.4167 | 8.3221 |
| 行为+画像 Logistic 基线 | 0.9565 | 0.1704 | 0.5000 | 9.9865 |
| 规则基线 | 0.2055 | 0.0052 | 0.0000 | 0.0000 |
| 纯无监督组合 | 0.3407 | 0.0060 | 0.0000 | 0.0000 |

混合模型相对行为+画像基线只有小幅增益：ROC-AUC 增加 0.0008、PR-AUC 增加 0.0049，Top-5%
召回不变。画像-only 在测试集上的 PR-AUC 更高，但模型选择已经在验证集完成，不能查看测试结果
后改选模型。

线性模型系数审计同样显示画像依赖：按绝对系数排序的前列几乎全部为 `region_code` 哑变量，
`customer_type_对公` 也位于前列；资金证据中较靠前的是 `graph_neighbor_degree_mean`、
`graph_community_size`、`night_txn_count` 和 `anomaly_temporal_change`。系数受标准化和类别编码
影响，不能直接解释为因果关系，但足以证明高分并非主要来自资金图信号。

因此结论必须分成两层：

1. 静态比赛排序可采用 `hybrid_profile_logistic`。
2. 资金图、时序变化和无监督异常提供了可解释证据及小幅增量，但不是当前高分的主要来源。

## 产物

`var/artifacts/retrospective_experiments/89e72026b92b871f/` 包含：

- `metadata.json`：完整配置、指标、置信区间和阻断项；
- `split_assignments.parquet`：固定账户划分；
- `enhanced_features.parquet`：不含标签的增强特征；
- `validation_predictions.parquet`：候选消融预测；
- `test_predictions.parquet`：锁定测试预测；
- `scores.parquet`：全量重训后的案例排序分数；
- `evaluation_model.joblib` 与 `scoring_model.joblib`；
- `feature_importance.csv`：选中线性模型的编码后系数与绝对系数排序；
- `report.md`：自动生成的机器结果摘要。

复现命令：

```bash
.venv/bin/fraud-graph run-retrospective-experiment \
  --feature-version f86409807550d65e \
  --snapshot-set-version 7d210bb931418adf \
  --allow-static-labels \
  --config config/default.yaml
```

## 方法依据

- Chandola et al., *Anomaly Detection: A Survey*, ACM Computing Surveys, 2009,
  DOI `10.1145/1541880.1541882`。
- Akoglu et al., *Graph based anomaly detection and description: a survey*, DMKD, 2015,
  DOI `10.1007/s10618-014-0365-y`。
- Van Vlasselaer et al., *APATE*, Decision Support Systems, 2015,
  DOI `10.1016/j.dss.2015.04.013`。
- Carcillo et al., *Combining unsupervised and supervised learning in credit card fraud
  detection*, Information Sciences, 2021, DOI `10.1016/j.ins.2019.05.042`。
- Dal Pozzolo et al., *Credit Card Fraud Detection: A Realistic Modeling and a Novel Learning
  Strategy*, IEEE TNNLS, 2018, DOI `10.1109/TNNLS.2017.2736643`。

## 使用边界

当前阻断项为 `LABEL_TIME_MISSING`、`RETROSPECTIVE_STATIC_TARGET`、
`TRANSDUCTIVE_GRAPH_EVALUATION`、`ACTIVE_POSITIVES_TOO_FEW`、
`PROFILE_SHORTCUT_DEPENDENCY` 和 `MODEL_APPROVAL_REQUIRED`。
获得标签时间后必须重新执行时序回测；在此之前不得将当前指标描述为线上发现率、实时预测能力或
生产级模型性能。
