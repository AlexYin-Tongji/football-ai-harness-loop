# 数据源、核验与内容合规策略

## 1. 总原则

先使用正式 API、授权数据流和 RSS；网页抓取只用于明确允许的公开页面，并遵守站点条款、robots、频率限制和版权要求。产品保存“事实与有限证据定位”，不复制或再发布整篇原文。

此文档是产品与工程约束，不替代正式法律意见。

## 2. 数据源注册

任何连接器上线前必须登记：

- 所有者、域名、用途和数据类型。
- 获取方式：API / RSS / webhook / permitted web。
- 条款、许可、署名、缓存与再分发限制。
- robots 检查日期、请求频率和停用开关。
- 支持的语言、时区、预计延迟和质量等级。
- 个人信息、敏感内容和删除请求流程。

未登记来源只能进入研发沙箱，不能进入生产稿件。

## 3. 来源分层

| 等级 | 示例类型 | 默认用途 |
|---|---|---|
| S0 | FIFA、足协、俱乐部、赛事官方声明/数据 | 赛程、赛果、官宣等一手事实 |
| S1 | 通讯社、主流体育媒体、具名一线记者 | 重要进展，可交叉核验后入正文 |
| S2 | 可靠的地方媒体、专业数据/转会媒体 | 补充细节，通常需第二来源 |
| S3 | 聚合站、二次转述、未提供原始链接的账号 | 线索发现，不作为唯一证据 |
| S4 | 匿名爆料、内容农场、无法识别来源的截图 | 默认过滤或进入人工调查 |

来源等级是初始先验，不代表某条消息必然正确。系统持续统计更正率、独家命中率、引用原始来源比例和延迟，人工审核后调整。

## 4. 主张与证据

每个可核验句子拆为结构化主张：

```json
{
  "subject": "球员",
  "predicate": "transfer_stage",
  "object": "目标俱乐部",
  "qualifiers": {
    "stage": "negotiation",
    "fee": null,
    "currency": null
  },
  "asserted_at": "2026-06-28T00:00:00Z"
}
```

证据保存 URL、标题、作者、原始发布时间、抓取时间、语言、证据定位和立场（支持/反驳/提及）。转载链应尽量回溯原始报道，多个转载不算独立佐证。

## 5. 置信度

规则先行，模型仅辅助：

```text
confidence = source_prior
           + independent_corroboration
           + specificity_and_recency
           + directness
           - contradiction
           - repost_dependency
```

硬规则优先于分数：

- 官方辟谣后不得继续写成已确认。
- 单一 S3/S4 来源不得进入日报事实正文。
- 金额、合同年限、伤情等字段没有证据就保持未知。
- 两条关键来源冲突时必须展示分歧或转人工。
- 任何直接引语必须能定位到原文；AI 改写不能加引号。

## 6. 去重与事件演进

去重键综合 canonical URL、内容哈希、实体、动作、时间和语义相似度。聚类后保留：

- 首次出现时间与来源。
- 最近实质更新。
- 每个来源的依赖/转载关系。
- 从“关注”到“报价/协议/官宣”的状态变化。
- 被撤回、辟谣或更正的版本。

只改变措辞而没有新事实的文章不触发“新进展”提醒。

## 7. 内容生成约束

- 模型输入使用证据包，而不是无边界网页上下文。
- 生成输出必须包含逐句引用 ID；验证器检查覆盖率和数值一致性。
- 不生成来源没有提供的动机、诊断、报价或私下对话。
- 不长篇复述受版权保护的表达；摘要应具有转换性并链接原文。
- 图片、队徽、赛事标识和视频需要单独的授权登记。

### 媒体资产

- Wikimedia Commons 图片必须读取 `LicenseShortName`、作者、Credit 和文件页；只接受 CC BY、CC0 或 Public Domain 家族。许可合格不等于画面相关，未经视觉或人工确认时显示“相关性待人工确认”。
- YouTube 只保存官方频道白名单内可嵌入视频的 ID、标题、缩略图和外链；不下载或重新托管视频。
- Sportmonks `image_path` 和赛事媒体是否可展示取决于订阅合同，不因 API 返回 URL 就自动获得再分发权。
- 新闻正文中的摄影图片、社媒截图和精彩片段默认不可复用，除非 Source Registry 有明确许可记录。

## 8. 推荐接入顺序

### 世界杯事实数据

1. S0：FIFA 官方赛程/赛果作为展示核对源。
2. 生产：采购覆盖阵容、事件、伤停与统计的授权足球数据服务。
3. 原型：football-data.org 可验证赛程接入；上线前确认套餐覆盖、延迟与再分发权。

### 新闻发现

1. 媒体/俱乐部官方 RSS 与 API。当前开发源包括 Guardian Football RSS 与 BBC Sport Football RSS；只保存元数据和短摘录，并链接原文。
2. 商业新闻检索 API（生产套餐）。NewsAPI.org 适合快速补英文媒体广度；NewsAPI.ai / Event Registry 更适合事件聚类、实体识别和跨语言去重。
3. GDELT 用于多语言线索发现，不自动视为事实来源。
4. 允许抓取的站点适配器作为补充，并设置低频与缓存。

### 建模研究

StatsBomb Open Data 可用于历史研究，使用时遵守其署名和许可要求；实时世界杯数据需另行确认。

### 人物与比赛事件增强

生产建议使用 Sportmonks 等授权服务补球员赛季数据、转会关系、进球者、分钟、比分变化和阵容。新闻摘要只能作为叙事证据，不能替代结构化赛果事件。

## 9. 报告导出前检查清单

- [ ] 每个事实句有来源。
- [ ] 赛程、比分、时间和球队 ID 通过结构化校验。
- [ ] 转述链已尽量回溯到原始来源。
- [ ] 冲突、未知和截至时间均被明确显示。
- [ ] 没有超范围引用、未授权图片或伪造引语。
- [ ] 报告明确展示低置信度和高影响主张。
- [ ] 报告保留生成版本、用户改写和更正入口。
- [ ] 导出前提示用户复核；系统没有社媒发布凭据或自动发布动作。

## 10. 参考入口

- [FIFA World Cup 2026 官方赛程与赛果](https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/match-schedule-fixtures-results-teams-stadiums)
- [FIFA 男足世界排名](https://inside.fifa.com/fifa-world-ranking/men)
- [football-data.org v4 文档](https://docs.football-data.org/general/v4/index.html)
- [NewsAPI 文档](https://newsapi.org/docs)
- [NewsAPI.ai / Event Registry](https://newsapi.ai/)
- [GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [StatsBomb Open Data](https://github.com/statsbomb/open-data)
- [Sportmonks Football API v3](https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints)
- [API-Football 文档](https://www.api-football.com/documentation-v3)
- [YouTube Data API search.list](https://developers.google.com/youtube/v3/docs/search/list)
- [Wikimedia Commons API](https://commons.wikimedia.org/wiki/Commons:API)
