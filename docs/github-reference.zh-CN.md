# GitHub 原型与组件评估

核验日期：2026-08-07。以下项目用于借鉴组件设计，不存在一个可以直接满足银行生产要求的完整开源
反诈系统。研究代码进入企业代码库前必须完成许可证、安全、依赖、数据出境和模型风险审查。

| 项目 | 适合复用的思路 | 不应直接照搬的部分 | 建议定位 |
|---|---|---|---|
| [IBM AMLSim](https://github.com/IBM/AMLSim) | 合成交易网络、fan-in/fan-out/cycle 等洗钱模式 | 合成分布不能代替真实回测 | 测试数据和故障注入 |
| [CARE-GNN](https://github.com/YingtongDou/CARE-GNN) | 多关系邻居筛选、欺诈伪装问题 | 静态传导式研究实现，无生产时序治理 | 算法重实现参考 |
| [TGN](https://github.com/twitter-research/tgn) | 连续时间事件图、节点记忆、时间采样 | 粗时间桶下收益有限，状态跨时间切分易泄漏 | 精确事件时间场景参考 |
| [Fraud Detection Handbook](https://github.com/Fraud-Detection-Handbook/fraud-detection-handbook) | 时间验证、不平衡指标、基线实验规范 | 主要是表格交易欺诈，不含资金路径证据 | 评估规范参考 |
| [PyGOD](https://github.com/pygod-team/pygod) | 图异常分数和无监督基线 | 异常不能直接解释为涉诈 | 集成模型的独立信号 |
| [DIG](https://github.com/divelab/DIG) | GNN 解释器和候选子图 | 模型重要性不等于合规证据 | 候选边生成，不直接出报告 |
| [PyTorch Geometric Temporal](https://github.com/benedekrozemberczki/pytorch_geometric_temporal) | 图快照迭代和时序 GNN | 固定节点等假设需重新验证 | 粗时间桶原型 |
| [PyTorch Geometric](https://github.com/pyg-team/pytorch_geometric) | 图数据结构、邻居采样、异构图和解释框架 | 只是底层框架，不提供反诈业务语义 | 中小规模图学习底座 |
| [GraphStorm](https://github.com/awslabs/graphstorm) | 大规模分布式图训练 | 当前数据规模使用它过重 | 亿级以上边时再评估 |

`YingtongDou/PC-GNN` 在本次核验时 GitHub API 返回 404，不应作为可拉取、可构建的工程依赖；其类别
不平衡采样思路可以依据论文重新实现并保留引用。

建议组合是：AMLSim 负责合成场景，Handbook 负责评估规范，PyG 负责模型实现，PyGOD 提供独立异常
信号，自研 `as_of` 数据层和确定性路径引擎负责生产可信性。CARE-GNN/TGN/DIG 只提供算法思路，不能
替代标签治理、时间切分、证据校验、服务鉴权和模型审批。

许可证提示：本次通过 GitHub 官方 API 确认 AMLSim 与 CARE-GNN 为 Apache-2.0、TGN 当前仓库为
Apache-2.0、PyTorch Geometric 为 MIT、GraphStorm 为 Apache-2.0。其余仓库以及论文、数据、模型
权重可能采用不同许可，落地前仍需由法务针对固定 commit 复核，不能仅依据仓库首页结论。

