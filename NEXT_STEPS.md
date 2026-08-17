# 比赛提交准备清单

## 当前状态

✅ **任务1：资金图谱样本构建** - 已完成
- 数据版本：d02568e53fa4e3e3
- 账户数：11,087
- 交易边数：904,395
- 时间切分：严格防泄露

✅ **任务2：涉诈账户静态回溯识别** - 阶段性完成
- 实验版本：89e72026b92b871f
- 锁定测试 ROC-AUC：0.9573
- 锁定测试 PR-AUC：0.1753
- 锁定测试 Top5% 召回率：50.0%
- 画像依赖明显，且 59 个涉诈账户仅 3 个有交易；非生产时序结论

✅ **任务3：可疑关联账户与资金链路挖掘** - 技术交付完成
- 任务三版本：50d309d703cbd6e8
- 案例包版本：a04894ae2c18991c
- 149条关联记录、326条路径、45个结构模式
- 自动预检：5/5通过；关系真值缺失，正式人工解释验收待真实复核员完成

✅ **任务4：辅助研判报告生成** - 已通过验收
- 验收通过率：100%
- 案例数：5份
- 证据完整性：100%

---

## 待办事项（按优先级）

### 🔴 优先级1：完成任务3正式人工验收（提交结论前必须）

**时间预估**：1-2小时

**步骤**：

1. **找两位独立复核员**（不能是同一人，不能互相讨论初始结论）

2. **复核员1操作**：
   ```bash
   cd "/path/to/fraud-graph-platform"
   
   # 阅读5份案例报告
   open var/artifacts/case_samples/a04894ae2c18991c/reports/case-01.md
   open var/artifacts/case_samples/a04894ae2c18991c/reports/case-02.md
   open var/artifacts/case_samples/a04894ae2c18991c/reports/case-03.md
   open var/artifacts/case_samples/a04894ae2c18991c/reports/case-04.md
   open var/artifacts/case_samples/a04894ae2c18991c/reports/case-05.md
   
   # 编辑复核模板（填写真实信息）
   # 替换 REPLACE_WITH_REVIEWER_1_ID 为真实姓名/工号
   # 替换 REPLACE_WITH_TIMEZONE_AWARE_ISO8601 为当前时间（如：2026-08-12T10:30:00+08:00）
   # 将 independence_attested 改为 true
   # 将三个null改为true/false：
   #   - evidence_consistent: 报告中的分数、账户、交易可在JSON中找到
   #   - path_explanation_pass: 链路连续、时间递增
   #   - readability_pass: 报告支持复核操作，无虚构结论
   ```

3. **复核员2操作**（独立进行，不看复核员1的结论）：
   ```bash
   # 同样流程，编辑 primary-reviewer-2.json
   ```

4. **运行验收**：
   ```bash
   fraud-graph evaluate-cases \
     --input var/artifacts/case_samples/a04894ae2c18991c/reviewer-1-submission.json \
     --input var/artifacts/case_samples/a04894ae2c18991c/reviewer-2-submission.json \
     --case-sample-dir var/artifacts/case_samples/a04894ae2c18991c \
     --output var/artifacts/case_samples/a04894ae2c18991c/task3_acceptance.json
   ```

5. **检查结果**：
   - 需要 `acceptance_passed: true`
   - 需要 `case_pass_rate >= 0.7`

---

### 🟡 优先级2：整理比赛提交材料（1-2天）

#### 2.1 准备技术文档

创建 `比赛提交/` 目录：

```bash
mkdir -p 比赛提交/{任务1,任务2,任务3,任务4,技术报告,代码}
```

**任务1材料**：
- [ ] 图构建代码：`src/fraud_graph/ingestion/` 和 `src/fraud_graph/features/`
- [ ] 字段字典：数据契约说明
- [ ] 时间切分说明：`docs/architecture.md` 中的时序控制部分
- [ ] 节点/边/标签样例：提取几条示例数据
- [ ] 图统计报告：数据版本 manifest.json

**任务2材料**：
- [x] 风险识别模型代码：`src/fraud_graph/modeling/retrospective.py`
- [x] 行为、图、快照变化和无监督异常特征说明
- [x] 规则、无监督、行为、画像和混合模型消融
- [x] 指标报告：`docs/task2-model-performance.md`
- [x] 机器产物：`var/artifacts/retrospective_experiments/89e72026b92b871f/`

**任务3材料**：
- [x] 关联账户清单：`var/artifacts/task3/50d309d703cbd6e8/top20_associations.parquet`
- [x] 可疑路径结果：`var/artifacts/task3/50d309d703cbd6e8/suspicious_paths.parquet`
- [x] 结构模式结果：`var/artifacts/task3/50d309d703cbd6e8/flow_patterns.parquet`
- [x] 任务三实现报告：`docs/task3-implementation-report.zh-CN.md`
- [x] 5个典型案例分析：`var/artifacts/case_samples/a04894ae2c18991c/reports/`
- [ ] 链路可视化提交截图：从风险工作台选取案例截图
- [ ] 人工复核结果：`task3_acceptance.json`

**任务4材料**：
- [ ] 研判报告生成代码：`src/fraud_graph/evidence/`
- [ ] 证据字段说明：报告中的证据类型
- [ ] 报告样例：5份案例报告
- [ ] 验收结果：`task4_acceptance.json`

#### 2.2 撰写技术报告

包含：
1. 整体方案设计（架构图）
2. 各任务技术路线
3. 关键技术创新点
4. 指标达成情况
5. 系统特色（防泄露、可追溯、版本化）

---

### 🟢 优先级3：优化和增强（可选）

#### 3.1 补充模型证据（数据允许时）

- [ ] 获得标签发生/可得时间后执行真正的时序回测
- [ ] 获得更多有交易涉诈账户后重新评估图模型
- [ ] 监控画像字段的分布偏移与子群表现
- [ ] 不以增加 GNN 复杂度替代标签和交易覆盖问题

#### 3.2 增强可视化

- [ ] 交互式图谱展示优化
- [ ] 风险传播路径动画
- [ ] 仪表盘增强

#### 3.3 补充文档

- [ ] 用户使用手册
- [ ] API文档完善
- [ ] 部署指南

---

## 快速命令参考

### 查看当前最佳模型指标
```bash
cat var/artifacts/retrospective_experiments/89e72026b92b871f/metadata.json
```

### 重新导出案例样本
```bash
fraud-graph export-case-samples \
  --score-version 33b390249f766d12 \
  --limit 5
```

### 启动Web工作台查看结果
```bash
fraud-graph serve \
  --score-version 33b390249f766d12 \
  --case-sample-version a04894ae2c18991c \
  --config config/default.yaml
```

### 查看所有案例报告
```bash
ls -lh var/artifacts/case_samples/a04894ae2c18991c/reports/
```

---

## 时间规划建议

- **今天**：完成任务3人工复核（2小时）
- **明天**：整理任务1-4提交材料（4小时）
- **后天**：撰写技术报告（4小时）
- **第4天**：检查、打包、提交（2小时）

**总计**：约12小时可完成所有必需工作

---

## 风险提示

1. ⚠️ 任务3人工复核是**必须项**，系统明确要求真实人工签名
2. ⚠️ 确保提交材料中不包含敏感信息（已脱敏数据应该没问题）
3. ⚠️ 检查比赛官方提交格式要求，按要求组织文件结构
4. ⚠️ 保留完整的可复现路径和命令

---

## 联系信息

比赛联系人：储新龙
电话：19825077126
邮箱：849383517@qq.com
