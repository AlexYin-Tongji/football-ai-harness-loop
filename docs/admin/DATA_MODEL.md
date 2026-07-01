# 后台数据组织

后台按五个领域组织，不按“页面表格”堆放数据。

| 领域 | 核心对象 | 说明 |
|---|---|---|
| Source | source_registry、source_items、evidence | 来源权限、采集元数据和可引用证据 |
| Football | matches、teams、players、lineups | 结构化足球事实与时间快照 |
| Editorial | story_clusters、reports、report_versions、corrections | 去重事件、报告版本和更正链 |
| Agent | workflow_runs、workflow_steps、model_opinions、prediction_snapshots | 有界执行、模型意见与赛前冻结版本 |
| Governance | connector_health、model_configs、audit_logs | 连接器健康、配置审批和敏感操作审计 |

`source_items` 只保存标题、URL、发布时间、来源、语言和短摘录；完整新闻正文不入库。`evidence` 是事实层，每条记录包含 `source_item_id`、规范化 claim、事件时间、抓取时间、信任等级和校验状态。报告引用 evidence，而不是引用聊天记录。

本地 Beta 已使用 SQLite WAL 持久化 `research_jobs`、`prediction_outcomes` 与 `audit_logs`；数据库文件不进入 Git。服务重启会明确中断未完成模型调用。生产仍使用 PostgreSQL/Temporal：事件版本 append-only，报告和预测不可变，检索向量仅是索引。后台接口默认隐藏，只有启用 `ADMIN_ENABLED=true` 且提供正确 `X-Admin-Token` 才可访问；赛果写入还要求 `X-Admin-Role: result_writer`。

权限角色至少拆分为 `source_admin`、`evidence_reviewer`、`editor`、`result_writer`、`model_admin` 与 `audit_reader`，禁止使用一个万能后台账号。
