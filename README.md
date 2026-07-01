# Football AI Harness Loop

面向球迷与足球内容创作者的可信 AI 报告生成器。公共页面只呈现世界杯日报、转会情报和比赛预测；证据检索、模型协作与质量门由后台 Agent Core 完成。

V3 核心设计与已实现范围见 [Agent Core V3 报告](docs/architecture/AGENT_CORE_V3_REPORT.md)。

当前阶段：可使用的本地 Beta。真实 RSS → DeepSeek V4 → 引用/概率质量门 → 编辑/导出闭环已跑通；商业上线仍需取得数据许可并接入第二独立来源。

已实现可运行的响应式产品页与 [Report API](services/report_api/README.md)。默认 mock 模式用于开发测试；使用 `scripts/run_deepseek.ps1` 可安全输入密钥并启动真实研究模式，密钥不会写入项目文件。

```powershell
.\scripts\run_deepseek.ps1
# 浏览器打开 http://127.0.0.1:8000
```

## 产品能力

- 每日转会报告：聚合、去重、分级、交叉核验并返回带来源的中文报告。
- 世界杯日报：整理赛果、焦点事件、球队动态和当日赛程，供用户二次编辑。
- 比赛预测报告：用结构化数据与大模型研判生成概率、依据和风险提示。
- 人物与比赛故事：在证据允许时补球员画像、关联球队、数据卡和进球时间线。
- 许可媒体：展示带许可证/署名的 Commons 图片，并可接入官方频道可嵌入视频；不下载视频。
- 导出：复制 Markdown/纯文本或下载 JSON；产品不自动发布社媒。

## 文档入口

- [产品需求文档](docs/product/PRD.md)
- [MVP 体验与页面规格](docs/product/UX_SPEC.md)
- [系统架构](docs/architecture/SYSTEM_DESIGN.md)
- [Harness / Loop / Memory / Skills / MCP 蓝图](docs/architecture/HARNESS_LOOP_BLUEPRINT.md)
- [Harness / Loop 架构决策](docs/adr/0001-harness-loop-architecture.md)
- [API-first 报告决策](docs/adr/0002-api-first-report-workbench.md)
- [预测模型设计](docs/ml/MATCH_PREDICTION.md)
- [数据源与内容合规](docs/data/SOURCE_POLICY.md)
- [世界杯冲刺路线图](docs/product/ROADMAP.md)
- [V4 严格产品评审与竞品差距](docs/product/V4_PRODUCT_REVIEW.md)
- [研发协作规范](CONTRIBUTING.md)

## 原则

1. 来源先于生成：没有可追溯证据，不进入事实稿。
2. 概率先于结论：预测展示概率、校准度与限制，不承诺赛果。
3. 用户在回路：系统交付报告，不替用户完成最终编辑和社媒发布。
4. 先闭环、后自治：每一步可重放、可审计、可停止、可降级。

## 仓库规划

```text
apps/          Web 报告工作台
services/      内容、工作流和预测服务
packages/      共享领域模型、提示词、评估与 UI
infra/         部署与可观测性配置
docs/          产品、架构、数据、模型与决策记录
```

> 本产品提供信息与概率分析，不构成投注或投资建议。
