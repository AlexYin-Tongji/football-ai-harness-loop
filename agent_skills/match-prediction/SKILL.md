---
name: match-prediction
description: 基于赛前截止时点证据生成中文比赛预测报告；用于胜平负、淘汰赛晋级概率、比分区间、正反证据与不确定性。
---

# 比赛预测委员会

## 输入契约

必须具备比赛双方、阶段、开球时间、data_cutoff 和至少一条带 URL、发布时间、source_id 的证据。开球后不得回写赛前版本。

## 工具顺序

1. `source-registry.list_approved_sources(match_data)` 确认数据源权限。
2. `football-data.list_competition_matches` 获取赛程、状态和赛果事实。
3. 必要时并行使用 Guardian、BBC 官方 RSS 或 `news-discovery.search_football_news` 发现阵容、伤停和外部预测；发现结果未经原站或官方来源复核不得升级为事实。

## 有界 Loop

1. Snapshot：锁定截止时间，去重并按 S0/S1/S2/S3 分层。
2. Statistical Baseline（代码）：每队至少三场结构化赛果时计算 Poisson；存在带来源 Elo 时做 Elo 调整。样本不足则明确不可用，不填默认强弱。
3. Form Analyst（Flash，一轮）：从实力、状态、休息、人员与战术匹配提出支持性概率。
4. Skeptic（Flash，一轮）：独立寻找反证、样本偏差与未知项，不读取第一席的推理。
5. Judge（Pro，一轮）：对照统计基线审阅结构化意见和证据，不做简单平均，给出球脉综合概率和理由。
6. External Comparison（代码 + Pro）：只展示输入证据明确提及的 Opta/Stats Perform/媒体预测；没有依据则为空。
7. Validator（确定性）：三项胜平负总和为 1±0.001；淘汰赛晋级概率总和为 1±0.001；引用 ID 必须存在；模型不得修改统计基线。
8. Revision（Pro，最多一轮）：仅在 schema 或证据门失败时修正；再次失败则转人工。

最大模型轮次 5、工具轮次 6、单次模型超时 90 秒、单次工具超时 15 秒。不得无限重试。

## 输出与边界

输出必须包含标题、摘要、分节、胜平负概率、最多三个比分、支持因素、反方因素、未知项和置信度。页面分开显示“球脉综合预测”“可复现统计基线”和“外部观点对照”。禁止补造首发、伤停、xG、排名或外部概率；禁止投注建议和确定性措辞；所有事实保留 evidence_id。
