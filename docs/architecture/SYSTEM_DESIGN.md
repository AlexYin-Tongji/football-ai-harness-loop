# 系统架构设计

## 1. 架构目标

支持世界杯期间快速上线，同时确保内容和预测可追溯、可重放、可停止、可降级。第一阶段采用模块化单体，工作流与模型任务独立进程部署；达到明确容量或团队边界后再拆服务。

## 2. 系统上下文

```mermaid
flowchart LR
    U["中文足球用户"] --> WEB["Web / PWA"]
    E["编辑与运营"] --> CMS["运营后台"]
    WEB --> API["产品 API"]
    CMS --> API
    API --> CORE["内容与预测领域核心"]
    CORE --> HR["Harness 工作流运行时"]
    HR --> SRC["新闻 / 赛事 / 统计数据源"]
    HR --> LLM["可替换的模型提供方"]
    CORE --> DB["PostgreSQL + pgvector"]
    CORE --> OBJ["对象存储"]
    CORE --> CACHE["Redis"]
    CORE --> OBS["日志 / Trace / 指标 / 告警"]
```

## 3. 推荐技术基线

| 层 | MVP 选择 | 理由 |
|---|---|---|
| Web/PWA | Next.js + TypeScript | SEO、内容页与运营后台可共用组件 |
| 产品 API | FastAPI + Python | 与采集、NLP、模型共享语言和类型 |
| 工作流 | Temporal | 持久化执行、定时、重试、人工审批、可重放 |
| 关系数据 | PostgreSQL | 主张、来源、文章、比赛和版本均适合关系模型 |
| 语义检索 | pgvector | MVP 避免单独维护向量库 |
| 缓存/限流 | Redis | 热点比赛、任务锁、配额与短期缓存 |
| 原文/制品 | S3 兼容对象存储 | 保存许可范围内的抓取快照、特征和模型制品 |
| 模型训练 | Python + scikit-learn/LightGBM | 先建立可解释且可回测的强基线 |
| 可观测性 | OpenTelemetry + Sentry + Prometheus/Grafana | 统一追踪工作流、API 与模型运行 |

技术版本在实现时锁定，不把具体 LLM 或数据商写死在领域层。

## 4. 模块边界

```text
identity          用户、角色、订阅、审计主体
source-registry   数据源、许可、配额、可信等级、采集适配器
ingestion         获取、解析、原始快照、幂等键
knowledge         实体解析、主张、证据、事件簇、冲突关系
editorial         选题、稿件、引用、审批、发布、更正
tournament        比赛、赛程、结果、球队、阵容、事件
prediction        特征快照、模型注册、预测、评估、模型卡
workflow          Harness 状态机、工具权限、重试、暂停、恢复
delivery          Web API、搜索、通知、外部渠道
observability     Trace、质量指标、成本、反馈与告警
```

模块只能通过显式接口和领域事件交互。新闻正文不能直接写入比赛事实表；预测服务只能读取已通过数据质量门的特征快照。

## 5. Harness：模型外的可靠性运行时

Harness 不等于一个提示词，也不等于自主智能体集合。它包含：

1. **任务控制器**：显式状态机/DAG、截止时间、预算和终止条件。
2. **上下文构建器**：只装载当前步骤所需的主张、证据与模板。
3. **工具注册表**：按角色授权采集、检索、写草稿、评估；发布工具仅供审批步骤调用。
4. **记忆与状态**：工作流状态存在数据库；LLM 对话历史不是事实源。
5. **验证器**：结构校验、引用覆盖、数值一致性、事实冲突和敏感内容检查。
6. **人在回路**：低置信度、冲突消息、重要更正和模型上线均暂停等待批准。
7. **可观测性**：记录输入版本、提示词版本、模型、工具调用、输出、成本与判定。
8. **评估器**：离线样本、线上反馈和失败聚类反向改进规则、提示词和模型。

### 5.1 运行约束

- 每次运行有 `run_id`、幂等键、最大步数、最大重试与成本预算。
- 模型只能提出工具调用；Harness 校验参数、权限、配额后执行。
- 工具输出视为不可信输入，必须经过 schema 与安全检查。
- LLM 不能直接发布、改比赛结果、覆盖原始记录或切换生产模型。
- 超限、来源冲突或校验失败进入人工队列/死信队列，不无限自修复。

## 6. 两条 Loop

### 6.1 内容闭环

```mermaid
flowchart LR
    A["发现 Discover"] --> B["获取 Fetch"]
    B --> C["解析 Normalize"]
    C --> D["聚类 Cluster"]
    D --> E["核验 Verify"]
    E --> F["成稿 Draft"]
    F --> G["批评 / 校验 Critique"]
    G --> H{"质量门"}
    H -->|通过| I["人工批准"]
    H -->|修订且未超预算| F
    H -->|冲突/超限| J["人工队列"]
    I --> K["发布 Publish"]
    K --> L["反馈 / 更正 Observe"]
    L --> A
```

`Critique` 只返回结构化问题清单，不直接覆写原稿。最多两轮自动修订，避免循环漂移。

### 6.2 预测闭环

```mermaid
flowchart LR
    A["数据摄取"] --> B["质量检查"]
    B --> C["时间点特征快照"]
    C --> D["训练 / 回测"]
    D --> E{"离线门槛"}
    E -->|失败| F["实验记录"]
    E -->|通过| G["人工批准 / 注册"]
    G --> H["影子预测"]
    H --> I{"线上校准门"}
    I -->|通过| J["生产服务"]
    I -->|失败| K["回滚"]
    J --> L["赛果回填 / 漂移监控"]
    L --> C
```

训练与文章生成不共用 LLM。LLM 可以把结构化预测翻译成解释，但不能改变概率。

## 7. 核心数据模型

| 实体 | 关键字段 |
|---|---|
| `Source` | id, domain, type, trust_tier, license_policy, robots_policy |
| `RawDocument` | id, source_id, canonical_url, published_at, fetched_at, content_hash, object_uri |
| `Claim` | id, subject, predicate, object, qualifiers, asserted_at, status |
| `Evidence` | claim_id, document_id, excerpt_locator, stance, extraction_version |
| `StoryCluster` | id, topic, entities, stage, confidence, first_seen, last_seen |
| `Article` | id, type, locale, status, current_version_id, scheduled_at |
| `ArticleVersion` | id, article_id, body, citations, prompt_version, approved_by |
| `Match` | id, provider_ids, teams, kickoff_utc, venue, stage, status, result |
| `FeatureSnapshot` | id, match_id, cutoff_at, schema_version, data_hash, uri |
| `ModelVersion` | id, code_sha, data_window, metrics, calibration, artifact_uri, stage |
| `Prediction` | match_id, model_version, snapshot_id, probabilities, created_at |
| `WorkflowRun` | id, type, state, budget, attempts, trace_id, started_at, ended_at |

所有时间在存储层使用 UTC，展示层转为 `Asia/Shanghai`。比赛与球队使用内部稳定 ID，供应商 ID 仅作为映射。

## 8. 对外 API 草案

```text
GET  /v1/digests/world-cup?date=2026-06-28
GET  /v1/digests/transfers?date=2026-06-28
GET  /v1/stories/{story_id}
GET  /v1/stories/{story_id}/sources
GET  /v1/matches?from=&to=&timezone=Asia/Shanghai
GET  /v1/matches/{match_id}
GET  /v1/matches/{match_id}/predictions/latest
GET  /v1/models/current/card
POST /v1/admin/articles/{id}/approve
POST /v1/admin/articles/{id}/request-changes
POST /v1/admin/workflows/{id}/retry
POST /v1/admin/models/{id}/promote
```

写操作使用角色权限、审计日志和幂等键。预测响应包含 `model_version`、`data_cutoff` 与 `generated_at`。

## 9. 可靠性与降级

- 目标：内容页月可用性 99.9%；赛前 2 小时预测读取 99.95%。
- RPO：15 分钟；RTO：60 分钟。原始数据和已发布版本不可变备份。
- 外部数据源使用断路器、速率限制、指数退避和供应商健康分。
- 单一新闻源故障：继续处理其他源并标记覆盖下降。
- LLM 故障：保留结构化赛程、赛果、来源列表和上一版已批准预测。
- 主数据源故障：冻结赛果/预测更新，页面显示数据截止时间，不用未经批准的抓取源自动顶替。

## 10. 安全与合规

- 密钥放入托管 Secret Store，禁止进入提示词、日志和仓库。
- 外部文档按提示注入不可信内容处理；网页文字不能改变系统规则或调用权限。
- 抓取适配器必须在 Source Registry 中有负责人、许可依据与停用开关。
- 最小化保存用户数据；关注列表与通知订阅分离，支持删除。
- 所有后台审批、发布、更正和模型晋级写入不可篡改审计日志。

## 11. 演进触发条件

只有满足以下任一条件才拆服务：

- 采集吞吐或故障显著影响用户 API。
- 预测团队需要独立发布与计算资源。
- 单模块部署频率/权限边界阻塞其他团队。
- PostgreSQL/Temporal 的可观测数据证明存在瓶颈。

在此之前，清晰模块边界比网络边界更重要。

