# Report API 与 Web 工作台

V2 已能从网页提交报告请求，经 Skill 路由、上下文构建、模型生成、确定性质量门和检查点记忆后返回结果。

## 本地运行（无需密钥）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn services.report_api.main:app --reload
```

访问：

- 工作台：`http://127.0.0.1:8000/`
- API 文档：`http://127.0.0.1:8000/docs`
- 能力清单：`http://127.0.0.1:8000/v1/system/capabilities`

公共页面使用持久化任务接口：

- `POST /v1/research/jobs` 创建任务并立即返回任务 ID。
- `GET /v1/research/jobs/{id}` 返回真实阶段、进度和最终报告。
- `GET /v1/research/jobs/{id}/events` 返回产品级运行事件，用于查看每层输入、
  产出、降级和最终 Trace 摘要。
- 后端先经过五层资料流水线：URL 收集层生成受治理检索计划并调用已登记的
  RSS、GDELT、可选 NewsAPI；资料精简层压缩成可引用短证据；增强层按
  `research-enhancement` SKILL 补许可媒体、球员/比赛结构化信息或给出降级
  提示；Leader 先规划栏目和负责小组，再做交付审查；撰写层只接收
  Leader 批准的栏目合同、精简证据包和增强素材，不重新研究。
- 日报请求会先生成 `time_scope`：`report_date` 固定解释为北京时间自然日，
  资料收集、结构化赛程和最终合稿都使用同一个 UTC 半开窗口。football-data
  已配置时，世界杯日报/足球日报会把该窗口内的赛程和赛果作为结构化证据优先注入。
- GIF、新闻配图、社媒截图和比赛片段当前没有批准自动抓取来源；增强层只会
  写入人工补充提示，不会用未登记来源替代。
- 图片/视频媒体管线默认关闭。需要重新启用时设置
  `FOOTPULSE_MEDIA_PIPELINE_ENABLED=true`，并继续遵守 Source Registry 的许可和相关性校验。
- 同步兼容接口和可直接提交 evidence 的调试接口默认隐藏；只有设置
  `FOOTPULSE_INTERNAL_API_ENABLED=true` 时才启用。
- 本地任务数据库默认为 `data/footpulse.db`，不会进入 Git。

默认使用 `LLM_PROVIDER=mock`，页面、Harness 和测试均不需要真实密钥。

## 安全调用 DeepSeek

不要把密钥粘贴到聊天、代码、截图、Issue 或 PR。推荐使用只对当前进程有效的安全启动脚本：

```powershell
.\scripts\run_deepseek.ps1
```

脚本会先读取 Git 忽略的本地 `.env`。如果 `.env` 或进程环境中没有
`DEEPSEEK_API_KEY`，才使用隐藏输入读取密钥。密钥只写入当前服务进程环境，服务退出后
清理变量，不会进入浏览器或 Git。

若桌面环境或终端进程中残留了旧 `DEEPSEEK_API_KEY`，本地 `.env` 可显式加入
`FOOTPULSE_DOTENV_OVERRIDE=true`，让 gitignored `.env` 覆盖当前进程变量。默认值为
`false`，生产环境仍应优先使用部署平台 Secret。

可用状态检查：

- `/health`：服务是否启动，以及当前是 mock 还是 DeepSeek。
- `/v1/product/status`：DeepSeek、Sportmonks、football-data、NewsAPI、YouTube
  白名单和许可媒体开关是否已配置；同时返回 `model_status` 与最近一次模型错误的安全
  提示。接口只返回状态，不返回密钥。

## 当前边界

- 当前 MCP 能力表已定义；Sportmonks、football-data 与许可媒体工具按密钥安全降级。
- 页面使用批准来源和结构化 evidence 跑通闭环。
- 本地 Beta 使用 SQLite WAL 持久化任务、事件、故事记忆、结果和审计记录；
  服务重启时会把未完成任务重新放回恢复队列；如果已写入
  `report_request_ready` 检查点，会跳过资料收集并直接恢复撰写/质量门。
  生产版将把同一阶段和事件合同映射到 PostgreSQL/Temporal 检查点。
- 服务不读取社媒凭据，也不提供自动发布工具。
