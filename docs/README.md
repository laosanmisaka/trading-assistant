# 文档索引

当前文档更新于 2026-10-03。规范约束见 [AGENTS.md](../AGENTS.md)。代码事实以当前提交为准；历史报告以各自写明的提交和时段为准。

## 当前使用与维护

- [使用手册](USER_GUIDE.md)：安装、GUI 操作、持仓与提醒。
- [运行手册](OPERATIONS.md)：数据缓存、盘后任务、监控、故障恢复。
- [技术总览](TECHNICAL_REFERENCE.md)：边界、模块与数据库。
- [当前策略合同](TRIPLE_BUY_WALKTHROUGH.md)：可观察窗口与 5min 事件、统计、证据。
- [czsc 桥接约束](CZSC_INTEGRATION.md)：实测版本与调用注意事项。
- [业务口径与保留事项](BUSINESS_RULES_CONFIRMATION.md)：现有实现及待维护者决策项。
- [已知限制](KNOWN_ISSUES.md)、[验证说明](VALIDATION.md)、[第三方来源](../THIRD_PARTY_NOTICES.md)。

## 评审与修复

- [2026-10-03 评审](reviews/REVIEW_REPORT_2026-10-03.md)：固定基线 `2edb3fb` 的问题与证据。
- [2026-10-03 整改](reviews/REMEDIATION_2026-10-03.md)：逐项处置与实际验收结果。
- [原始证据](reviews/evidence_2026_10_03.json)：缺陷复现，不是修复后的测试通过报告。

## 历史材料

以下保留研究和决策轨迹，不能作为当前操作指令、功能清单或收益承诺：

- [接手评估](PROJECT_ASSESSMENT.md)、[任务清单复核](TASK_LIST_REVIEW.md)。
- [几何买卖点设计历程](PLAN_BUYSELL_GEOMETRY.md)。
- [日线与 30min 多周期实验](STRATEGY_CHAN_MULTIFREQ.md)。

`outputs/` 和 `.workbuddy/` 是忽略的本机产物，不构成公共证据库。缺失的 `pool_random501.txt` 也不是可重跑输入。历史材料中依赖这些产物的数字目前未认证，现有随机池还存在当前存活样本选择偏差。
