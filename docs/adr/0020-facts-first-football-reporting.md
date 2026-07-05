# ADR 0020：Facts-first 足球日报重构

- 状态：Accepted
- 日期：2026-07-05

## 背景

实测显示，模型和多 Agent 不是“当天赛事不全”的根因，而是它们过早参与了事实筛选：

1. football-data.org 在当前授权下能稳定返回世界杯赛程、开球时间、状态和比分，但不返回进球者、红黄牌、换人或分钟事件。
2. Sportmonks token 虽已配置，但当前探针显示五大联赛和 World Cup 均未返回可用数据；fixture story 请求返回空 data，无法作为进球时间线来源。
3. Guardian、BBC、GDELT、NewsAPI 属于新闻发现/叙事补充，不能保证完整列出一天所有比赛、进球者和分钟。
4. 旧流水线中，URL 收集、精简、Leader 分栏和栏目小组会在结构化事实清单固定前裁剪候选，导致比赛、人物背景和教练资料都可能被挤掉。

## 决策

1. 日报改为 Facts-first：先生成 `DailyFootballFactPack`，再进入新闻发现和写作。
2. `DailyFootballFactPack` 只保存规范化事实：北京时间窗口、比赛清单、比分、状态、事件时间线、覆盖缺口和来源尝试。
3. football-data.org 是当前默认 fixture/result provider；其输出回答“当天有哪些比赛、几点、比分是多少”。
4. events/timeline 是独立能力，不得从比分或新闻标题推断。若 Sportmonks/API-Football 等批准结构化源未覆盖，则事实包必须写入 `match_events_unavailable`，模型不得编写进球者和分钟。
5. player/coach 背景同样应进入事实包或人物事实层，不能依赖日报正文临时发挥；在结构化源无覆盖时只能写新闻证据中出现的有限背景。
6. 后续 Leader、栏目小组和最终合稿只消费事实包转出的 evidence 与 coverage warnings；它们可以扩展叙事，但不能改变当天比赛清单或把缺失事件补成事实。

## 后果

- “赛事完整性”从模型问题变成可审计的数据源覆盖问题。
- 产品可以明确告诉用户：当前能保证赛程/赛果，不能保证进球分钟，除非接入覆盖 events 的授权源。
- 新闻源继续有价值，但角色变为解释和补充，不再承担结构化事实主账本。
- 后续重构应把球员、教练、球队背景也做成事实层，而不是散落在 prompt 和分桌草稿中。

## 验证

- 单元测试覆盖北京时间 7 月 3 日事实包保留三场比赛。
- 单元测试覆盖 football-data 结构化 evidence 明确写出“不得编写未入证据的时间线”。
- 单元测试覆盖缺少 Sportmonks events 时输出 coverage warning。
- 真实验证应检查 report_request checkpoint 中 fixtures 数量、events 数量和 coverage issues，而不是只看最终报告文字。
