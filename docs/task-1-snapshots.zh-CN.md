# 任务一：滚动有向资金图快照

## 1. 目标

任务一将规范化交易明细转换为可复现的动态图样本，而不是只生成一张前端关系图。
默认每 7 天设置一个观察锚点，每个锚点向前观察 28 天：

```text
snapshot(T) = transactions[T - 28 days, T)
```

半开区间保证观察时点 `T` 及其之后的交易不会进入该快照。第一张快照覆盖源数据最早的
完整 28 天，最后额外保留源数据最大交易日期次日零点的终态快照，确保源交易覆盖率为100%。
观察锚点对齐数据源时区的自然日零点，清单中同时保留带时区的标准时间。

项目同时生成两种边，禁止混用统计口径：

- 交易事件边：每笔规范化交易一条边，用于时序资金链和证据追溯；
- 聚合账户对边：按付款账户和收款账户聚合，用于度、密度、连通分量和图特征。

## 2. 构建命令

```bash
fraud-graph build-graph-snapshots \
  --data-version <data-version> \
  --config config/default.yaml
```

窗口和步长可以通过 `--observation-days` 与 `--anchor-step-days` 覆盖。步长不得大于观察窗口，
否则相邻窗口间会出现未覆盖交易。

时间分区参数可通过 `--train-fraction`、`--validation-fraction` 和 `--embargo-days` 覆盖。
隔离期不得短于观察窗口，默认均为28天。

## 3. 产物结构

```text
var/artifacts/graph_snapshots/<snapshot_set_version>/
  manifest.json
  summary.md
  audit.json
  global_graph_statistics.json
  snapshot_stats.parquet
  accounts.parquet
  labels.parquet
  global_nodes.parquet
  global_directed_edges.parquet
  samples/
    accounts.csv
    labels.csv
    transaction_events.csv
    global_nodes.csv
    global_directed_edges.csv
    snapshot_stats.csv
  snapshots/<snapshot_id>/
    manifest.json
    nodes.parquet
    edges.parquet
    events.parquet
```

`accounts.parquet` 保存静态账户属性。`labels.parquet` 单独保存最终静态标签，不写入节点特征表。

`global_nodes.parquet` 和 `global_directed_edges.parquet` 是完整交易期的聚合图。全局节点表包含
入/出度、交易笔数、金额和活动时间，但不含任何标签字段。全局边表按有向账户对聚合。

每个 `nodes.parquet` 包含全部账户，并标记该窗口是否活跃，同时提供入度、出度、流入/流出笔数、
流入/流出金额和净流量等基础状态。没有交易的账户作为孤立节点保留。

每个 `edges.parquet` 按付款账户到收款账户聚合，包含交易笔数、签名金额、绝对金额、最小/最大金额、
首次/末次交易时间、负金额笔数和自转账笔数。方向始终为付款账户指向收款账户。

每个 `events.parquet` 保留窗口内的规范化源交易和稳定交易 ID，用于聚合对账、链路解释和审计追溯。

## 4. 自动质量检查

构建任务会逐快照验证：

- 所有交易边的两端账户均存在于账户表；
- 规范化交易 ID 不重复；
- 节点流出笔数之和、节点流入笔数之和、聚合边交易笔数之和均等于事件数；
- 聚合边金额之和等于源事件金额之和；
- 事件时间均位于 `[window_start, anchor_at)`；
- 所有源交易至少进入一个快照；
- 标签与节点特征物理分离。

快照统计还包含活跃/孤立节点数、聚合有向边数、弱连通分量数、最大分量规模、最大入度/出度、
活跃嫌疑/受害账户数、自转账、负金额和未来交易泄露数。

`audit.json` 汇总任务一验收结果；任何核心对账失败都会在原子产物提交前直接中止构建。

## 5. 时间顺序分区与隔离期

所有快照按观察截止时间排序。数据量足够时，构建器按照目标比例选择
`train / validation / test`，并在相邻分区间保留至少一个完整观察窗口的隔离期：

```text
train -> embargo -> validation -> embargo -> test
```

隔离期快照保留在任务一产物中，以维持完整动态图和100%源交易覆盖；其 `split=null`、
`split_status=embargo`，后续任务二不得将其用于训练或评估。当前全量产物为11个训练快照、
4个验证快照、3个测试快照和6个隔离期快照，跨分区观察事件不重叠。

## 6. 标签边界

当前赛事标签缺少风险事件发生时间和标签可得时间。因此动态图快照可以完整构建，但标签仍属于
观察期结束后的静态最终状态。产物会写入：

```text
label_time_status = missing
supervised_temporal_training_ready = false
eligibility_blockers = [LABEL_TIME_MISSING]
```

这不影响交易动态图、图统计、静态标签下的动态行为回溯和无监督异常检测；但不得据此声称模型
在风险发生前完成了严格的未来预测。

## 7. 当前阶段产物

当前已生成：

```text
data_version         = d02568e53fa4e3e3
snapshot_set_version = 7d210bb931418adf
snapshot_count       = 24
source_coverage      = 100%
future_leakage       = 0
task1_acceptance     = passed
```

完整统计见 `results/graph-statistics.md`，机器可读验收结果见快照集目录中的 `audit.json`。

复现命令：

```bash
PYTHONPATH=src python -m fraud_graph build-graph-snapshots \
  --data-version d02568e53fa4e3e3 \
  --config config/default.yaml
```

## 8. 与后续任务的关系

任务二应在快照节点和边之上计算 PageRank、K-core、社区、互转环、二跳暴露和跨快照变化趋势，
再与无图特征模型进行消融对比。任务三使用 `events.parquet` 或规范化交易明细恢复时间有序资金链路。
