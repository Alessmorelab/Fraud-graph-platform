# 任务一：滚动有向资金图快照

## 1. 目标

任务一将规范化交易明细转换为可复现的动态图样本，而不是只生成一张前端关系图。
默认每 7 天设置一个观察锚点，每个锚点向前观察 28 天：

```text
snapshot(T) = transactions[T - 28 days, T)
```

半开区间保证观察时点 `T` 及其之后的交易不会进入该快照。第一张快照覆盖源数据最早的
完整 28 天，最后额外保留一个以源数据最大交易时间后 1 微秒为截止点的终态快照，确保源交易
覆盖率为 100%。观察锚点对齐数据源时区的自然日零点，清单中同时保留带时区的标准时间。

## 2. 构建命令

```bash
fraud-graph build-graph-snapshots \
  --data-version <data-version> \
  --config config/default.yaml
```

窗口和步长可以通过 `--observation-days` 与 `--anchor-step-days` 覆盖。步长不得大于观察窗口，
否则相邻窗口间会出现未覆盖交易。

## 3. 产物结构

```text
var/artifacts/graph_snapshots/<snapshot_set_version>/
  manifest.json
  summary.md
  snapshot_stats.parquet
  accounts.parquet
  labels.parquet
  snapshots/<snapshot_id>/
    manifest.json
    nodes.parquet
    edges.parquet
    events.parquet
```

`accounts.parquet` 保存静态账户属性。`labels.parquet` 单独保存最终静态标签，不写入节点特征表。

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

## 5. 标签边界

当前赛事标签缺少风险事件发生时间和标签可得时间。因此动态图快照可以完整构建，但标签仍属于
观察期结束后的静态最终状态。产物会写入：

```text
label_time_status = missing
supervised_temporal_training_ready = false
eligibility_blockers = [LABEL_TIME_MISSING]
```

这不影响交易动态图、图统计、静态标签下的动态行为回溯和无监督异常检测；但不得据此声称模型
在风险发生前完成了严格的未来预测。

## 6. 与后续任务的关系

任务二应在快照节点和边之上计算 PageRank、K-core、社区、互转环、二跳暴露和跨快照变化趋势，
再与无图特征模型进行消融对比。任务三使用 `events.parquet` 或规范化交易明细恢复时间有序资金链路。
