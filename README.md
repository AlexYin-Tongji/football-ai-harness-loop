# Football AI Harness Loop

面向中文足球用户的可信 AI 内容与比赛预测产品。项目以 **Harness（可靠性运行时）** 包住模型，以 **Loop（可观测、可纠偏的闭环）** 驱动新闻生产和预测迭代。

当前阶段：产品设计与 MVP 架构。

## 产品能力

- 每日转会消息：聚合、去重、分级、交叉核验并生成带来源的中文简报。
- 世界杯日报：自动整理赛果、焦点事件、球队动态和当日赛程。
- 比赛预测：输出胜/平/负与比分分布，并公开模型版本、更新时间和不确定性。

## 文档入口

- [产品需求文档](docs/product/PRD.md)
- [MVP 体验与页面规格](docs/product/UX_SPEC.md)
- [系统架构](docs/architecture/SYSTEM_DESIGN.md)
- [Harness / Loop 架构决策](docs/adr/0001-harness-loop-architecture.md)
- [预测模型设计](docs/ml/MATCH_PREDICTION.md)
- [数据源与内容合规](docs/data/SOURCE_POLICY.md)
- [世界杯冲刺路线图](docs/product/ROADMAP.md)
- [研发协作规范](CONTRIBUTING.md)

## 原则

1. 来源先于生成：没有可追溯证据，不进入事实稿。
2. 概率先于结论：预测展示概率、校准度与限制，不承诺赛果。
3. 人在回路：高风险内容、低置信度消息和模型上线均需人工批准。
4. 先闭环、后自治：每一步可重放、可审计、可停止、可降级。

## 仓库规划

```text
apps/          Web 与运营后台
services/      内容、工作流和预测服务
packages/      共享领域模型、提示词、评估与 UI
infra/         部署与可观测性配置
docs/          产品、架构、数据、模型与决策记录
```

> 本产品提供信息与概率分析，不构成投注或投资建议。
