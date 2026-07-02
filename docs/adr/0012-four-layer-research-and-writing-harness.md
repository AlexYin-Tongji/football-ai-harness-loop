# ADR 0012：资料收集类采用四层 Harness 流水线

- 状态：Accepted
- 日期：2026-07-02

## 背景

旧版 `ResearchHarness` 已能用模型规划查询并调用批准来源，但它把“发现 URL、筛选证据、补充人物/媒体资料、交给撰写层”揉在一起。用户遇到资料不足或最终合稿 token 过大时，系统难以判断是哪一层失败，也无法让增强需求被明确治理。

## 决策

资料收集类请求统一拆为四层，每层都有自己的受限 Loop 和 Harness：

1. **URL 资料收集层**：模型只规划英文优先的查询词；Harness 只调用 Source Registry 已登记的 RSS、GDELT 与可选 NewsAPI，输出候选 URL/元数据/短摘录。连续两轮无新增或达到工具预算即停止。
2. **资料精简层**：模型把候选资料压缩成准确、简洁、可引用的 evidence 摘要；若模型不可用，使用确定性短摘要兜底。该层不新增事实。
3. **增强层**：模型根据精简资料提出 `player_profile`、`club_context`、`licensed_image`、`official_video`、`match_context` 或 `gif` 需求；Harness 只执行已登记的结构化资料和许可媒体工具。GIF/比赛动图当前没有批准来源，只能作为人工补充提示。
4. **撰写层**：接收精简证据包与增强素材，不重新研究。日报继续由赛事/转会分桌和总编辑合稿；预测继续由多个独立分析席与终审席汇总。

## API 与 MCP 边界

- 已存在的 RSS、GDELT、NewsAPI、football-data、Sportmonks、media-assets 本地 MCP server 继续作为只读能力边界。
- 当前主产品路径仍在模块化单体内直接调用这些只读适配器；能力清单会标注是否需要 key、是否已配置、Source Registry 状态是 approved/candidate/blocked。
- 不为四层流水线本身新增远程 MCP server。四层 Harness 是工作流控制层，不是外部资料源。
- Sportmonks、NewsAPI、Event Registry、API-Football 等候选或付费能力必须先完成 Source Registry/合同状态确认，才能扩大生产使用范围。

## 后果

- “近期资料不足”只代表四层流水线都没有取得可用资料；覆盖薄、单源失败或只有发现线索时会继续生成并写入 warning。
- 最终撰写层输入显著变小，不再携带候选 URL 池和冗余短摘录。
- 增强层可以提前找许可图片、官方视频或结构化球员资料，但不能抓取未经批准的 GIF、新闻配图、社媒截图或比赛片段。
- 旧 `ResearchHarness.collect()` 名称保留，内部改为四层流水线，避免破坏前端和任务 API。

## 验证

- 测试必须覆盖四层 `layer_runs` 顺序。
- 发现层线索可以返回资料包，但必须保留 `unverified_lead` 和传闻标签要求。
- 增强层预抓媒体必须由 Harness 注入，模型输出的 `media_assets` 仍被质量门拒绝。
- GIF 需求必须变成 warning，不允许自动抓取。
