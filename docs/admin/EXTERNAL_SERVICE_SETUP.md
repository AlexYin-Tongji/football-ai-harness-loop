# 外部服务配置清单

密钥只应通过本机环境变量、部署平台 Secret 或隐藏输入脚本配置。不要写入 Git、
数据库、截图、Issue、PR 或报告正文。

## 第一组：当前可运行所需

| 配置 | 从哪里获取 | 用途 | 必需性 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | [DeepSeek API Keys](https://platform.deepseek.com/api_keys) | 报告研究、编辑与预测委员会 | 必需 |
| `SPORTMONKS_API_TOKEN` | [Sportmonks Dashboard](https://my.sportmonks.com/) | 球员统计、转会关系、阵容、比赛事件和时间线 | 强烈建议 |
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com/apis/credentials) | 查找白名单官方频道的可嵌入集锦 | 需要视频时必需 |
| `YOUTUBE_OFFICIAL_CHANNEL_IDS` | 各官方 YouTube 频道的 Channel ID | 限制视频只来自人工批准的官方频道 | 需要视频时必需 |

申请 Sportmonks 套餐前应确认覆盖：2026 世界杯、球员赛季统计、fixtures、
events、timeline、lineups、scores、transfers/rumours，以及产品展示和缓存权利。

Google 项目中需要启用 **YouTube Data API v3**。本项目只做公开只读查询，不需要
OAuth；API Key 建议限制为该 API，并按部署环境限制来源。

建议先提供的官方频道 ID：FIFA、各参赛足协，以及确有需要的联赛/俱乐部频道。
频道 ID 不是密钥，但必须人工确认频道真实性。

本地 Beta 可把这些值放入 Git 忽略的 `.env`。后端和 `scripts/run_deepseek.ps1`
会自动读取 `.env`，但真实值不得提交。首批视频白名单可先使用 FIFA 官方频道：

```text
YOUTUBE_OFFICIAL_CHANNEL_IDS=UCpcTrCXblq78GZrTUTLWeBw
```

服务启动后可访问 `/v1/product/status` 查看哪些外部服务已配置；接口只返回布尔状态，
不返回密钥。管理员可访问 `/admin` 并输入 `ADMIN_TOKEN`，查看 Sportmonks 五大联赛
覆盖、NewsAPI、视频和视觉核验健康卡片。

## 第二组：扩大新闻覆盖

| 配置 | 用途 | 必需性 |
|---|---|---|
| `NEWS_API_KEY` | 多语言新闻发现；线索仍须经过 Source Registry 和可信度分层 | 可选 |
| `FOOTBALL_DATA_API_KEY` | 赛程与赛果备用核对 | 可选 |

接入前仍需确认套餐是否允许生产使用、缓存、摘要和再分发。拿到 Key 不等于自动取得
新闻正文、摄影图片或视频的版权。

### 推荐供应商取舍

| 方向 | 推荐提供方 | 变量建议 | 何时接入 | 产品判断 |
|---|---|---|---|---|
| 新闻发现-免费补广度 | [GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | 无需 Key | 已接入候选发现层 | 适合扩大语言与地域覆盖；只做线索，不单独当事实来源 |
| 新闻发现-简单商用 | [NewsAPI.org](https://newsapi.org/pricing) | `NEWS_API_KEY` | 需要稳定英文媒体检索时 | API 简单；生产需 Business/Advanced；不提供完整正文 |
| 新闻发现-生产增强 | [NewsAPI.ai / Event Registry](https://newsapi.ai/) | `EVENT_REGISTRY_API_KEY` | 需要事件聚类、实体、情绪、多语言深度检索时 | 比 NewsAPI.org 更适合“转会事件簇”和跨语言去重，但需单独评估价格和再分发条款 |
| 赛程赛果备用 | [football-data.org](https://www.football-data.org/pricing) | `FOOTBALL_DATA_API_KEY` | 预测基线需要第二结构化来源时 | 轻量、便宜，适合兜底核对；深度球员/事件不如 Sportmonks |
| 结构化足球备用 | [API-Football / API-SPORTS](https://www.api-football.com/documentation-v3) | `API_FOOTBALL_KEY` | Sportmonks 覆盖不足或需要第二付费足球源时 | 覆盖广，可作为竞争性备选；接入前必须先登记 Source Registry |
| 视觉相关性核验 | [OpenAI Vision](https://developers.openai.com/api/docs/guides/images-vision) | `OPENAI_API_KEY` | 需要判断 Commons 图是否真是目标球员/比赛时 | 适合“图像理解 + 中文解释 + 结构化输出”；成本需单独设预算 |
| 基础图像安全/标签 | [Google Cloud Vision](https://cloud.google.com/vision/pricing) | `GOOGLE_CLOUD_VISION_API_KEY` 或 `GOOGLE_APPLICATION_CREDENTIALS` | 需要低成本 OCR、标签、安全识别时 | 官方价目包含每月前 1000 units 免费；适合机器标签，不适合作为球员身份最终判断 |

## 第三组：自动视觉核验

当前 Commons 图片会校验许可证，并要求标题或元数据能匹配目标姓名；没有自动视觉模型
时仍标记为“待人工确认”。若需要自动确认图片中是否为目标球员或对应比赛，还需选择一
个支持图像输入的模型供应商并提供 API Key。

推荐先接入 Google Cloud Vision：它有免费额度，适合做标签、OCR、安全识别和 Web
实体辅助。产品上仍应把输出当成“相关性证据”，不是球员身份的最终事实；`uncertain`
继续交给人工确认。若以后需要更强的中文图像解释，再补 OpenAI Vision 或其他多模态
模型，并设置独立成本预算。

## Sportmonks 五大联赛覆盖

Sportmonks token 是否“已配置”不等于套餐覆盖了目标联赛。管理健康卡会分别探测：

- Premier League
- La Liga
- Serie A
- Bundesliga
- Ligue 1

若显示 `not_covered`，通常需要在 Sportmonks 后台选择/购买对应联赛或开启试用；代码
无法绕过套餐权限。免费层一般用于原型验证，不应假设包含五大联赛。

## 非外部申请项

- `ADMIN_TOKEN`：由部署者本地生成的高强度随机值，不需要向第三方申请。
- `DEEPSEEK_MAX_CONCURRENCY=2`：系统稳定性参数，默认已配置。
- `LLM_TIMEOUT_SECONDS=120`：深度报告建议值；不是密钥。
- `LICENSED_MEDIA_ENABLED=true`：媒体总开关。

## 安全交付格式

不要把真实值填入仓库文件。安全配置时只需要映射以下变量：

```text
DEEPSEEK_API_KEY=<secret>
SPORTMONKS_API_TOKEN=<secret>
YOUTUBE_API_KEY=<secret>
YOUTUBE_OFFICIAL_CHANNEL_IDS=<channel-id-1>,<channel-id-2>
NEWS_API_KEY=<optional-secret>
FOOTBALL_DATA_API_KEY=<optional-secret>
EVENT_REGISTRY_API_KEY=<optional-secret>
API_FOOTBALL_KEY=<optional-secret>
GOOGLE_CLOUD_VISION_API_KEY=<optional-vision-secret>
GOOGLE_APPLICATION_CREDENTIALS=<optional-service-account-json-path>
OPENAI_API_KEY=<optional-secret-for-vision>
```

已在聊天或其他明文渠道出现过的密钥，应先轮换再用于生产。
