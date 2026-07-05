---
name: source-discovery
description: 指导 URL 资料收集层如何在 Source Registry 边界内规划足球新闻、转会、比赛和官方资料检索。
---

# 足球来源发现层

## 目标

URL 收集层只负责扩大候选链接池，不写结论。它先覆盖常见高价值来源，再用已登记的发现 API 扩展到其他批准域名。所有结果都必须保留原链接、来源名、发布时间和来源状态，供后续层回溯。

## 常见种子来源

- 官方事实：FIFA、UEFA、各大联赛官网、俱乐部官网和足协官网。用于赛程、赛果、官宣、纪律、伤停公告和比赛中心。
- 英文主流：BBC Sport、The Guardian Football、Reuters、Sky Sports、ESPN Soccer、The Athletic。用于赛事脉络、转会进展和赛前消息；Guardian Open Platform 可作为已登记的关键词检索入口，只保留标题、链接、时间和短摘录。
- 西语来源：Marca、AS、Mundo Deportivo、Diario Sport。用于西甲、巴萨、皇马和西语转会线索。
- 意语来源：La Gazzetta dello Sport、Gianluca Di Marzio、Sky Sport Italia。用于意甲和意大利转会线索。
- 法德来源：L'Equipe、RMC Sport、Kicker、Sport1。用于法甲、德甲和国家队消息。
- 结构化/授权候选：football-data、Sportmonks、API-Football。用于赛程、赛果、阵容、球员和事件字段；是否可展示取决于配置和合同。

这些名字是检索规划参考，不等于自动抓取许可。只有 Source Registry 或 Publisher Registry 已登记、且当前访问方式允许的域名才能进入候选证据。

## 查询策略

1. 英文优先生成 2-4 个高召回查询，包含球队/球员英文名、赛事名、时间窗口和动作词；用户明确点名的球队或球员必须排在第一轮搜索。
2. 若主题明显属于西甲、意甲、法甲或德甲，增加一个原语言媒体查询。
3. 转会查询使用状态词：interest、talks、bid、offer、agreement、medical、official、denied。
4. 比赛查询使用：team news、lineup、injury、suspension、preview、highlights、match report。
5. 球队相关信息优先找俱乐部官网和本队所在联赛/足协；例如 Manchester United 先找 manutd.com、Premier League、BBC/Sky/Guardian，再扩展到批准媒体。
6. 球员相关信息优先找当前俱乐部、目标俱乐部、所在联赛和结构化资料接口；不要用模型记忆补年龄、身价、数据。
7. 发现 API 可以寻找种子之外的批准域名，但结果必须经过域名 allowlist 和后续精简层；未登记域名只能作为丢弃的噪声。

## 接受标准

- 每条候选必须带 URL、标题、来源、发布时间和短摘录。
- RSS 与 Guardian Open Platform 发布者短摘录可作为 publisher_report；GDELT/NewsAPI 元数据默认是 unverified_lead。
- 转载链只算一个来源；尽量回溯到原始报道、官方声明或具名发布者。
- 连续两轮没有新增独立来源时停止，不为了填满数量继续扩搜。
- 不访问社媒、论坛、截图站、未登记博客或未批准网页抓取。
