# 比赛完成度检查清单

根据比赛文档《基于资金图谱的涉诈账户发现与可疑链路解释》

---

## 📊 总体完成度：🟡 86%

| 任务 | 技术实现 | 指标达成 | 文档完整度 | 总体评分 |
|------|---------|---------|-----------|---------|
| **任务1：图谱构建** | ✅ 100% | ✅ 100% | ⚠️ 60% | 🟡 87% |
| **任务2：风险识别** | ✅ 100% | ✅ 100% | ⚠️ 70% | 🟡 90% |
| **任务3：链路挖掘** | ✅ 100% | ⚠️ 待复核 | ⚠️ 65% | 🟡 75% |
| **任务4：报告生成** | ✅ 100% | ✅ 100% | ⚠️ 75% | 🟢 92% |

---

## 🔴 必须完成（阻断提交）

### 1. 任务3人工复核 ⚠️ **最高优先级**

**当前状态**: `pending_human_review`  
**必须做**: 2名独立复核员签名，≥70%通过率  
**时间**: 2-4小时

**位置**: 
- 复核模板：`var/artifacts/case_samples/cc6e83b59a6fe0b4/review_templates/`
- 案例报告：`var/artifacts/case_samples/cc6e83b59a6fe0b4/reports/`

**步骤**：
1. 找2位反诈复核员（不能是同一人，不能互相看对方的结论）
2. 复核员1独立阅读5份报告，填写 `primary-reviewer-1.json`
3. 复核员2独立阅读5份报告，填写 `primary-reviewer-2.json`
4. 运行验收：
```bash
fraud-graph evaluate-cases \
  --input var/artifacts/case_samples/cc6e83b59a6fe0b4/reviewer-1-submission.json \
  --input var/artifacts/case_samples/cc6e83b59a6fe0b4/reviewer-2-submission.json \
  --case-sample-dir var/artifacts/case_samples/cc6e83b59a6fe0b4
```

---

## 🟡 强烈建议完成（提升质量）

### 优先级1：核心文档（2-3小时）

#### 1. 字段字典 ❌
**文件**: `docs/data-dictionary.md`  
**内容**: 账户表、交易边表、标签表的字段说明

#### 2. 特征说明 ❌
**文件**: `docs/feature-explanation.md`  
**内容**: 27个特征的详细说明、计算公式、业务含义

#### 3. 基线对比报告 ⚠️
**文件**: `docs/baseline-comparison.md`  
**内容**: 
- Behavior基线 vs Combined最佳模型
- 包含PR-AUC对比（比赛要求）
- 提升幅度分析

---

### 优先级2：可视化（2-3小时）

#### 4. ROC/PR曲线 ❌
**命令**:
```python
# 生成ROC和PR曲线
import matplotlib.pyplot as plt
from sklearn.metrics import roc_curve, precision_recall_curve, auc
# ... 绘图代码
plt.savefig('results/figures/roc_pr_curves.png')
```

#### 5. 特征重要性 ❌
**命令**:
```bash
python -c "
import joblib, pandas as pd, matplotlib.pyplot as plt
model = joblib.load('var/artifacts/models/27e53f783dde8968/model.joblib')
classifier = model.named_steps['classifier']
features = model.named_steps['preprocess'].get_feature_names_out()
coefs = classifier.coef_[0]
df = pd.DataFrame({'feature': features, 'coef': coefs, 'abs': abs(coefs)})
df = df.sort_values('abs', ascending=False).head(10)
print(df)
df.to_csv('results/top10-features.csv', index=False)
"
```

#### 6. 链路可视化 ❌
**方法**: 启动Web工作台，截图保存
```bash
fraud-graph serve --score-version 57908dafafd18a42 --config config/default.yaml
# 访问 http://localhost:8000
# 查看账户图谱，截图保存到 results/figures/
```

---

### 优先级3：结构化输出（1-2小时）

#### 7. 数据样例 ❌
**命令**:
```bash
mkdir -p examples
# 账户样例
python -c "
import pyarrow.parquet as pq
df = pq.read_table('var/normalized/d02568e53fa4e3e3/accounts.parquet').to_pandas()
df.head(10).to_csv('examples/sample-accounts.csv', index=False)
"

# 交易样例
python -c "
import pyarrow.parquet as pq
df = pq.read_table('var/normalized/d02568e53fa4e3e3/transactions/year=2025/month=09/part-00000.parquet').to_pandas()
df.head(20).to_csv('examples/sample-transactions.csv', index=False)
"
```

#### 8. 关联账户清单 ⚠️
需要通过API导出Top-20关联账户

#### 9. 图统计报告 ⚠️
**文件**: `results/graph-statistics.md`  
**内容**:
- 节点数：11,088
- 边数：904,395
- 度分布统计
- 连通分量统计

---

### 优先级4：综合报告（3-4小时）

#### 10. 技术报告 ❌
**文件**: `docs/technical-report.md`  
**内容**:
- 整体方案设计（架构图）
- 各任务技术路线
- 实验结果分析
- 关键创新点
- 结论与展望

**大纲**:
```markdown
# 技术报告

## 1. 整体方案设计
- 系统架构
- 技术栈选择
- 数据流程

## 2. 任务1：资金图谱构建
- 图谱结构设计
- 时间窗口控制
- 防泄露机制

## 3. 任务2：风险识别
- 模型选择与训练
- 特征工程
- 性能优化
- 指标分析

## 4. 任务3：链路挖掘
- 路径搜索算法
- 关联账户发现
- 典型案例分析

## 5. 任务4：报告生成
- 证据整合
- 报告模板设计
- 验收结果

## 6. 关键创新点
- 时序防泄露机制
- 版本化管理
- 可审计架构

## 7. 实验结果
- 性能指标汇总
- 与比赛要求对比

## 8. 结论与展望
```

---

## 🟢 可选加分项

如果时间充裕（≥5天剩余）：

1. **图神经网络对比** (3-5天)
2. **命中率详细统计** (1天)
3. **时序模型验证** (1-2天)
4. **误差分析** (1天)

---

## ✅ 推荐工作流程

### 如果距离提交 ≤ 3天

**Day 1 上午 (2小时)**
- [ ] 完成任务3人工复核 ⚠️

**Day 1 下午 (4小时)**
- [ ] 编写字段字典
- [ ] 编写特征说明
- [ ] 提取特征重要性

**Day 2 全天 (8小时)**
- [ ] ROC/PR曲线可视化
- [ ] 基线对比报告
- [ ] 链路可视化截图
- [ ] 导出数据样例
- [ ] 图统计报告
- [ ] 撰写技术报告大纲

**Day 3 全天 (6小时)**
- [ ] 完成技术报告
- [ ] 整理所有文档
- [ ] 检查提交材料完整性
- [ ] 最终测试

---

## 📦 提交材料清单

### 必需材料

#### 代码部分
- [x] 完整源代码：`src/fraud_graph/`
- [x] 配置文件：`config/default.yaml`
- [x] 依赖说明：`pyproject.toml`
- [ ] 运行说明：`README.md`（需补充）

#### 任务1交付
- [x] 图构建代码 ✅
- [ ] 字段字典 ❌
- [ ] 时间切分说明 ⚠️
- [ ] 节点/边/标签样例 ❌
- [ ] 图统计报告 ⚠️

#### 任务2交付
- [x] 模型代码 ✅
- [ ] 特征说明 ❌
- [ ] 基线对比结果 ⚠️
- [x] 指标评估报告 ✅ (`docs/task2-model-performance.md`)

#### 任务3交付
- [x] 关联账户代码 ✅
- [ ] 关联账户清单 ⚠️
- [x] 可疑路径代码 ✅
- [ ] 可疑路径结果 ⚠️
- [ ] 链路可视化样例 ❌
- [x] 5个典型案例 ✅
- [ ] 人工复核结果 ❌ **最关键**

#### 任务4交付
- [x] 报告生成代码 ✅
- [ ] 证据字段说明 ⚠️
- [x] 报告样例 ✅
- [x] 评估结果 ✅

#### 综合材料
- [ ] 技术报告 ❌
- [x] 性能分析报告 ✅
- [ ] 可视化结果 ❌

---

## 📞 快速命令参考

### 启动Web工作台
```bash
fraud-graph serve \
  --score-version 57908dafafd18a42 \
  --case-sample-version cc6e83b59a6fe0b4 \
  --config config/default.yaml

# 访问 http://localhost:8000
```

### 查看模型指标
```bash
cat var/artifacts/models/27e53f783dde8968/metadata.json | jq '.metrics'
```

### 导出案例
```bash
ls var/artifacts/case_samples/cc6e83b59a6fe0b4/reports/
```

---

## 🎯 关键里程碑

| 里程碑 | 状态 | 优先级 |
|--------|------|--------|
| 任务3人工复核 | ⚠️ 待完成 | 🔴 最高 |
| 核心文档（3个） | ❌ 待完成 | 🟡 高 |
| 可视化（3个） | ❌ 待完成 | 🟡 高 |
| 技术报告 | ❌ 待完成 | 🟡 中 |
| 结构化输出 | ⚠️ 部分完成 | 🟢 中 |

---

**更新时间**: 2026-08-12  
**总体完成度**: 86%  
**预计剩余工作量**: 12-16小时
