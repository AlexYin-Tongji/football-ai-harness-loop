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

## 第二组：扩大新闻覆盖

| 配置 | 用途 | 必需性 |
|---|---|---|
| `NEWS_API_KEY` | 多语言新闻发现；线索仍须经过 Source Registry 和可信度分层 | 可选 |
| `FOOTBALL_DATA_API_KEY` | 赛程与赛果备用核对 | 可选 |

接入前仍需确认套餐是否允许生产使用、缓存、摘要和再分发。拿到 Key 不等于自动取得
新闻正文、摄影图片或视频的版权。

## 第三组：自动视觉核验

当前 Commons 图片会校验许可证，但画面相关性仍标记为“待人工确认”。若需要自动确认
图片中是否为目标球员或对应比赛，还需选择一个支持图像输入的模型供应商并提供 API
Key。供应商尚未锁定；确定后需要新增独立配置、成本上限和视觉结论审计字段。

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
```

已在聊天或其他明文渠道出现过的密钥，应先轮换再用于生产。
