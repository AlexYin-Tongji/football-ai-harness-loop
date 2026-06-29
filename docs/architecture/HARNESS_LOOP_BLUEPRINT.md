# Harness、Loop、Memory、Skills 与 MCP 蓝图

- 状态：Accepted for V2
- 日期：2026-06-29
- 目标：用最少的自治换取可靠、可审计、可持续迭代的足球报告生产

## 1. 核心决定

球脉不采用一个模型无限“思考—调用工具—再思考”的自由循环。三类报告都有显式任务图，模型只在节点内部做有限决策。

```text
用户请求
  → 路由到 Skill
  → 构建时间点上下文
  → 只读 MCP 工具获取资料
  → 抽取 / 聚类
  → 生成报告
  → 确定性质量门
  → 必要时一次修订
  → 保存检查点与摘要
  → 返回用户编辑 / 导出
```

选择这一结构的原因：

- Anthropic 区分预定义路径的 workflow 与模型自主控制的 agent，并建议从最简单、可组合的模式开始。[Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
- LangGraph 将线程状态保存为 checkpoint，并把短期线程状态与跨会话长期存储分开。[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- Structured Graph Harness 将控制流从隐式上下文提升为显式 DAG，以获得可终止、可验证和可恢复的执行。[SGH 论文](https://arxiv.org/abs/2604.11378)
- Temporal 的 durable execution 模式说明模型/API 调用应作为可重试活动，工作流状态独立持久化。[Temporal](https://temporal.io/)

## 2. Harness 的职责

Harness 是模型外的运行时，共有八个组件：

1. **Router**：从报告类型选择唯一 Skill，不让模型自由发明工作流。
2. **Plan Graph**：定义阶段、依赖、允许工具、预算和终止状态。
3. **Context Builder**：只加载当前节点需要的高信号证据。
4. **Tool Gateway**：连接 MCP，做权限、schema、超时、来源和大小检查。
5. **Memory Manager**：保存检查点、用户偏好、证据和评估摘要。
6. **Quality Gates**：验证引用、时点、概率、冲突、输出 schema 和隐私。
7. **Recovery Controller**：区分可重试、需修订、需用户输入和最终失败。
8. **Observability**：保存组件版本、步骤、耗时、token、成本与失败类型。

模型不能修改任务图、增加轮次、切换生产模型、写入比赛事实或发布社媒。

## 3. Loop 的层级

### 3.1 节点内 Tool Loop

仅“资料发现”节点允许模型选择只读工具：

```text
观察当前缺口 → 选择允许工具 → 执行 → 验证结果 → 更新缺口
```

- 转会报告最多 8 个工具轮次。
- 世界杯日报最多 6 个工具轮次。
- 比赛预测最多 5 个工具轮次。
- 同一查询最多重试一次；连续两次无新增证据立即停止。

### 3.2 Generator–Evaluator Loop

报告先生成，再使用独立校验上下文检查。确定性规则优先，LLM evaluator 只检查语义覆盖、矛盾和表达。

- 初稿 1 次。
- 质量检查 1 次。
- 只有明确错误清单时允许修订 1 次。
- 修订后仍失败则返回资料包和警告，不继续循环。

Anthropic 的 evaluator-optimizer 模式适用于有清晰标准且反馈能改善输出的任务；同时其前端 Harness 研究指出，让独立 evaluator 评判比让生成者自评更可靠。[Evaluator–optimizer](https://www.anthropic.com/engineering/building-effective-agents)、[Harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps)

### 3.3 产品改进 Loop

```text
运行 Trace → 用户修改/反馈 → 失败归类 → 黄金样本评估
→ 修改 Skill / Prompt / Tool / Gate → 离线回归 → 小流量上线
```

每次 Harness 变更都记录“预期改善什么指标”，上线后验证，避免凭感觉改提示词。该做法与 Agentic Harness Engineering 强调的组件、经验和决策可观测性一致。[AHE 论文](https://arxiv.org/abs/2604.25850)

## 4. 三个任务框架与轮次

这里的“模型轮次”指一次完整模型请求，不是用户必须进行多轮聊天。正常情况用户一次提交、一次获得报告；最多追加一次“按我的风格改写”。

| 任务 | 固定阶段 | Flash | Pro | Evaluator | 修订 | 最大模型调用 |
|---|---|---:|---:|---:|---:|---:|
| 每日转会 | 发现→抽取→实体/事件聚类→交叉核验→报告 | 2 | 1 | 1 | 1 | 5 |
| 世界杯日报 | 结构化赛果→新闻抽取→重要性排序→报告 | 1 | 1 | 1 | 1 | 4 |
| 比赛预测 | 上下文快照→正反证据→概率研判→校验→报告 | 1 | 1 | 1 | 1 | 4 |

默认不使用多智能体讨论。若未来评估证明“独立来源审计员”显著降低错误，才把 evaluator 独立为另一个 Agent。

### 4.1 每日转会报告

```text
source-discovery (MCP, ≤8 tool rounds)
→ claim-extraction (Flash)
→ entity-and-story-cluster (Flash)
→ evidence-gate (code)
→ report-compose (Pro)
→ citation-and-conflict-review (evaluator)
→ optional-repair (Pro, once)
```

硬停止条件：单一低等级来源不得写成事实；转载链不算独立佐证；连续两轮没有新主张停止搜索。

### 4.2 世界杯日报

```text
official-match-data (MCP)
→ news-discovery (MCP, ≤6 tool rounds)
→ extract-and-rank (Flash)
→ numeric/timezone-gate (code)
→ daily-report-compose (Pro)
→ citation-review
→ optional-repair
```

赛果、赛程、排名由结构化工具提供，大模型只负责解释与组织。

### 4.3 比赛预测报告

```text
cutoff-snapshot (MCP, ≤5 tool rounds)
→ evidence-balance (Flash)
→ prediction-judgment (Pro)
→ probability/source-gate (code)
→ skeptical-review
→ optional-repair
```

输出必须同时包含支持因素、反方证据和未知项。90 分钟概率与晋级概率分开；开赛后冻结。

## 5. 记忆设计

不使用“全部聊天记录 = 记忆”。上下文越长不等于效果越好；长上下文存在注意力稀释，因此应做压缩、结构化笔记和按需加载。[Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

| 层 | 内容 | 生命周期 | 是否直接进模型 |
|---|---|---|---|
| Run State | 当前阶段、检查点、预算、错误、工具结果引用 | 单次运行 | 仅当前节点所需字段 |
| Thread Memory | 用户本次改写要求、最近一步和未解决问题 | 当前报告会话 | 摘要 + 最近 2 轮 |
| User Memory | 语言、篇幅、球队偏好、常用报告结构 | 用户可删除 | 经用户同意且按需检索 |
| Evidence Memory | 主张、来源、时间、冲突、实体与事件簇 | 按来源策略 TTL | 检索后的最小证据包 |
| Episodic Memory | 失败类型、用户修改原因、评估分数 | 长期 | 聚合摘要，不加载原始 Trace |
| Procedural Memory | Skill、Prompt、schema、质量门版本 | Git 版本化 | 当前任务对应版本 |

规则：

- 每个阶段后写 checkpoint，崩溃后从最近成功阶段恢复。
- 原始工具结果保存为对象引用，不反复塞入上下文。
- 超过上下文预算时先清理旧工具结果，再生成结构化 handoff。
- 不保存或展示模型隐藏思维链；只保存最终结果、工具调用和可解释摘要。
- 用户偏好默认最小化，提供查看、修改和删除入口。

## 6. Skills 设计

Skill 负责“怎么完成一类任务”，MCP 负责“能访问什么外部能力”。两者不能混成一个巨大系统提示词。

采用 `SKILL.md` + `runtime.json`：

```text
agent_skills/
├── transfer-daily/
│   ├── SKILL.md          # 可移植的人类/模型指令
│   └── runtime.json      # 阶段、预算、MCP、记忆与质量门
├── world-cup-daily/
└── match-prediction/
```

Skill 使用渐进式披露：常驻上下文只有名称和触发描述，激活后加载 `SKILL.md`，再按需要加载参考资料。Anthropic 的 Agent Skills 也采用这一三层模式。[Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

每个 `runtime.json` 必须声明：

- `version` 和唯一 `report_type`。
- `phases` 与不可变顺序。
- `max_model_rounds`、`max_tool_rounds`、token 与时间预算。
- 允许的 `mcp_servers` 和具体工具前缀。
- 可读/可写 memory namespace。
- `quality_gates`、失败策略和输出 schema。

Skill 进入生产前需要黄金样本、注入测试、引用测试和成本基线；社区 Skill 不直接安装到生产。

## 7. MCP 设计

MCP 使用 host–client–server 边界；每个 server 聚焦一种能力。工具用于动作，resources 用于只读上下文，prompts 只提供可选模板。[MCP Architecture](https://modelcontextprotocol.io/docs/learn/architecture)

### 7.1 V2 服务器规划

| MCP Server | 类型 | 能力 | 权限 |
|---|---|---|---|
| `football-data` | remote HTTP | 赛程、赛果、阵容、统计 | 只读 |
| `news-evidence` | remote HTTP | 搜索、抓取许可页面、规范化证据 | 只读 |
| `source-registry` | internal | 来源等级、许可、配额、停用状态 | 只读 |
| `report-memory` | internal | 检查点、用户偏好、报告反馈 | 按 namespace 读写 |

不设计 `social-publisher` MCP。

### 7.2 工具原则

- 一个工具只做一件事，命名含 namespace，例如 `news.search_articles`。
- 参数使用明确 ID、UTC 时间和枚举，避免自由文本控制权限。
- 返回 `source_id`、`fetched_at`、`license`、`freshness` 和 schema 版本。
- 工具输出始终视为不可信数据，不能覆盖系统/Skill 指令。
- 每个 Skill 只看到允许工具的子集；不把数百个工具全塞给模型。
- 工具调用有超时、大小、域名、成本和并发限制。

Anthropic 的工具设计经验同样强调清晰边界、命名空间、示例、边缘条件和基于评估迭代。[Writing Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)

### 7.3 MCP 安全

- 远程 MCP 使用 OAuth 2.1、PKCE、短期 token 和最小 scope。
- token 必须绑定具体 server audience，禁止 token passthrough。
- 每个 server 独立连接与凭据，避免一个 server 读取另一个 server 的上下文。
- 本地 stdio 凭据只从环境/Secret Store 读取。
- 工具列表、annotations 和返回内容都不能自动信任。

这些要求来自 MCP 当前授权规范和架构边界。[MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

## 8. 隐私与密钥

- API Key 只存在后端进程环境或 Secret Manager；不进入浏览器、数据库、Prompt、Trace、异常或 Git。
- 前端只调用同源后端，不直接调用 DeepSeek。
- `.env*`、本地数据库和运行制品默认忽略。
- CI 扫描常见密钥形态；提交前扫描 staged diff。
- 日志只保留 provider、model、token、latency、request ID，不记录 Authorization 或完整 Prompt。
- 用户报告与偏好按用户 namespace 隔离；删除用户时同步删除长期记忆。
- 用于模型的来源正文做最小化和长度限制，不上传无关个人信息。

## 9. V2 与生产边界

V2 实现：Skill 注册、任务路由、内存检查点、同步 Harness Trace、mock/DeepSeek Provider、响应式网页与隐私检查。

生产前补齐：Temporal/PostgreSQL 持久化、真实 MCP client、供应商许可数据源、认证/多租户、异步任务队列、删除与数据导出、完整 eval 集。

